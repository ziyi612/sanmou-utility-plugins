"""Second independent review: destructive path collisions and recovery isolation."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType

import pytest

import battle_reports as cli
from parser.report_archive import ReportArchive


@pytest.fixture
def work():
    base = Path('.runtime/test-temp').resolve()
    with tempfile.TemporaryDirectory(prefix='second-entry-review-', dir=base) as folder:
        directory = Path(folder).resolve()
        assert directory.parent == base
        yield directory


def raw_report(uid=123, **extra):
    return json.dumps({'battleID': uid, 'source': 'all', 'eventCount': 0,
                       'data': {'eventDataList': []}, **extra}).encode('utf-8')


def seed_database(path):
    with ReportArchive(path) as archive:
        result = archive.import_bytes(raw_report())
        return result['snapshot_ids'][0]


def integrity(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        return connection.execute('PRAGMA integrity_check').fetchone()[0]


@pytest.mark.parametrize('operation', ['export', 'verify'])
def test_database_cannot_be_used_as_an_output_file(work, operation, capsys):
    database = work / 'reports.db'
    snapshot_id = seed_database(database)
    if operation == 'export':
        argv = ['export', str(snapshot_id), str(database), '--db', str(database)]
    else:
        argv = ['verify', '--out', str(database), '--db', str(database)]
    assert cli.main(argv) == 2
    capsys.readouterr()
    assert database.read_bytes().startswith(b'SQLite format 3\0')
    assert integrity(database) == 'ok'


def test_verify_does_not_replace_existing_source_report(work, capsys):
    database, source = work / 'reports.db', work / 'battle_123_decrypted.json'
    original = raw_report()
    source.write_bytes(original)
    with ReportArchive(database) as archive:
        assert archive.import_file(source)['status'] == 'imported'
    assert cli.main(['verify', '--db', str(database), '--out', str(source)]) == 2
    capsys.readouterr()
    assert source.read_bytes() == original
    assert integrity(database) == 'ok'


def test_export_cannot_overwrite_database_through_hardlink(work, capsys):
    database, alias = work / 'reports.db', work / 'alias.db'
    snapshot_id = seed_database(database)
    try:
        os.link(database, alias)
    except OSError as exc:
        pytest.skip(f'Host filesystem does not support a hardlink in the project-local test directory: {exc}')
    assert cli.main(['export', str(snapshot_id), str(alias), '--db', str(database)]) == 2
    capsys.readouterr()
    assert database.read_bytes().startswith(b'SQLite format 3\0')
    assert integrity(database) == 'ok'


def mock_runtime(monkeypatch):
    calls = {'entered': 0, 'cycles': 0}
    class Runner:
        def __init__(self, pid=None):
            pass
        def __enter__(self):
            calls['entered'] += 1
            return self
        def __exit__(self, *args):
            pass
    class Collector:
        def __init__(self, runner, out_dir, **kwargs):
            pass
        def run_cycle(self):
            calls['cycles'] += 1
            return {'complete': True, 'errors': []}
    runners, collectors = ModuleType('run_lua_in_game'), ModuleType('battle_collector')
    runners.GameLuaRunner, collectors.BattleCollector = Runner, Collector
    monkeypatch.setitem(sys.modules, 'run_lua_in_game', runners)
    monkeypatch.setitem(sys.modules, 'battle_collector', collectors)
    monkeypatch.setattr(cli, 'read_identity', lambda runner: {'avatarId': '123', 'serverId': '9', 'scenarioId': '1'})
    return calls


def test_collection_status_tracks_connection_collection_wait_and_exit(work, monkeypatch, capsys):
    out = work / 'captures'
    out.mkdir()
    status_path = out / 'collector_status.json'
    status_path.write_text(json.dumps({'error': 'stale failure', 'retrying': False}), encoding='utf-8')
    observed = []
    attempts = 0

    class Runner:
        def __init__(self, pid=None):
            pass
        def __enter__(self):
            nonlocal attempts
            attempts += 1
            current = json.loads(status_path.read_text(encoding='utf-8'))
            observed.append(current)
            assert current['running'] is True and current['phase'] == 'connecting'
            assert current['cycle'] == attempts and current['complete'] is False
            assert 'error' not in current
            if attempts == 1:
                raise RuntimeError('temporary connection failure')
            return self
        def __exit__(self, *args):
            pass

    class Collector:
        def __init__(self, runner, out_dir, **kwargs):
            pass
        def run_cycle(self):
            current = json.loads(status_path.read_text(encoding='utf-8'))
            observed.append(current)
            assert current['running'] is True and current['phase'] == 'collecting'
            assert current['namespace'] == 'server:9/scenario:1/avatar:123'
            assert current['identity']['avatarId'] == '123'
            return {'complete': True, 'errors': []}

    def sleep(interval):
        current = json.loads(status_path.read_text(encoding='utf-8'))
        observed.append(current)
        assert current['running'] is True and current['phase'] == 'waiting'
        assert current['retrying'] is True
        assert current['error'] == 'temporary connection failure'

    runners, collectors = ModuleType('run_lua_in_game'), ModuleType('battle_collector')
    runners.GameLuaRunner, collectors.BattleCollector = Runner, Collector
    monkeypatch.setitem(sys.modules, 'run_lua_in_game', runners)
    monkeypatch.setitem(sys.modules, 'battle_collector', collectors)
    monkeypatch.setattr(cli, 'read_identity', lambda runner: {'avatarId': '123', 'serverId': '9', 'scenarioId': '1'})
    monkeypatch.setattr(cli.time, 'sleep', sleep)
    assert cli.main(['collect', '--max-cycles', '2', '--db', str(work / 'reports.db'), '--out-dir', str(out)]) == 0
    capsys.readouterr()
    current = json.loads(status_path.read_text(encoding='utf-8'))
    assert [item['phase'] for item in observed] == ['connecting', 'waiting', 'connecting', 'collecting']
    assert current['running'] is False and current['phase'] == 'finished'
    assert current['result'] == {'complete': True, 'errors': []}


def test_bad_account_binding_does_not_block_good_recovery_or_current_collection(work, monkeypatch, capsys):
    calls = mock_runtime(monkeypatch)
    out = work / 'captures'
    bad = out / 'broken-binding'
    bad.mkdir(parents=True)
    bad_bytes = b'{"namespace":'
    (bad / 'account.json').write_bytes(bad_bytes)
    namespace = 'other-account'
    good = out / hashlib.sha256(namespace.encode()).hexdigest()[:20]
    good.mkdir()
    (good / 'account.json').write_text(json.dumps({'namespace': namespace, 'identity':
        {'avatarId': '456', 'serverId': '9', 'scenarioId': '1'}}), 'utf-8')
    (good / 'battle_123_decrypted.json').write_bytes(raw_report(namespace=namespace))
    database = work / 'reports.db'
    code = cli.main(['collect', '--once', '--db', str(database), '--out-dir', str(out)])
    capsys.readouterr()
    assert code == 2  # A corrupt binding remains visible, while unrelated work continues.
    assert calls['cycles'] == 1
    assert (bad / 'account.json').read_bytes() == bad_bytes
    with ReportArchive(database) as archive:
        rows = archive.connection.execute('SELECT namespace,battle_uid FROM report_records').fetchall()
        assert [tuple(row) for row in rows] == [(namespace, '123')]


def test_recovery_rejects_namespace_conflict_without_losing_the_source(work):
    out = work / 'captures'
    out.mkdir()
    source = out / 'battle_123_decrypted.json'
    raw = raw_report(namespace='account-B')
    source.write_bytes(raw)
    with ReportArchive(work / 'reports.db') as archive:
        result = cli.recover_captures(archive, out, 'account-A')
        assert result['errors']
        assert archive.stats()['records'] == 0
    assert source.read_bytes() == raw


def test_recovery_rejects_mismatched_runtime_identity(work):
    out = work / 'captures'
    out.mkdir()
    expected = {'avatarId': '101', 'serverId': '9', 'scenarioId': '1'}
    source = out / 'battle_123_decrypted.json'
    raw = raw_report(namespace='account-A', runtimeIdentity={**expected, 'avatarId': '102'})
    source.write_bytes(raw)
    with ReportArchive(work / 'reports.db') as archive:
        result = cli.recover_captures(archive, out, 'account-A', expected_identity=expected)
        assert result['errors']
        assert archive.stats()['records'] == 0
    assert source.read_bytes() == raw


def test_recovery_still_accepts_historical_reports_without_identity_fields(work):
    out = work / 'captures'
    out.mkdir()
    source = out / 'battle_123_decrypted.json'
    raw = raw_report()
    source.write_bytes(raw)
    with ReportArchive(work / 'reports.db') as archive:
        result = cli.recover_captures(archive, out, 'account-A', expected_identity=
            {'avatarId': '101', 'serverId': '9', 'scenarioId': '1'})
        assert result['errors'] == []
        assert archive.stats()['records'] == 1
        snapshot = archive.list_reports()[0]['current_snapshot_id']
        assert archive.original_bytes(snapshot) == raw


@pytest.mark.parametrize('parameter', ['--interval', '--wait', '--request-delay'])
@pytest.mark.parametrize('value', ['nan', 'inf'])
def test_nonfinite_waits_rejected_before_database_or_runtime_activity(work, monkeypatch, capsys, parameter, value):
    calls = mock_runtime(monkeypatch)
    database, out = work / 'reports.db', work / 'captures'
    code = cli.main(['collect', '--once', '--db', str(database), '--out-dir', str(out), parameter, value])
    capsys.readouterr()
    assert code == 2
    assert calls['entered'] == calls['cycles'] == 0
    assert not database.exists()
    assert not out.exists()


@pytest.mark.parametrize('avatars,existing_binding,expected_scans', [
    (['123', '123'], False, 1),
    (['123', '123'], True, 1),
    (['123', '123', '456', '456'], False, 2),
])
def test_normal_cycles_recover_once_per_account(work, monkeypatch, capsys, avatars, existing_binding, expected_scans):
    calls = mock_runtime(monkeypatch)
    out, database = work / 'captures', work / 'reports.db'
    identities = [{'avatarId': avatar, 'serverId': '9', 'scenarioId': '1'} for avatar in avatars]
    identity_iterator = iter(identities)
    monkeypatch.setattr(cli, 'read_identity', lambda runner: next(identity_iterator))
    monkeypatch.setattr(cli.time, 'sleep', lambda seconds: None)
    if existing_binding:
        namespace = 'server:9/scenario:1/avatar:123'
        folder = out / hashlib.sha256(namespace.encode()).hexdigest()[:20]
        folder.mkdir(parents=True)
        (folder / 'account.json').write_text(json.dumps({'namespace': namespace, 'identity': identities[0]}), 'utf-8')
    scans = []
    recover = cli.recover_captures
    def observed_recover(archive, directory, namespace, **kwargs):
        scans.append(namespace)
        return recover(archive, directory, namespace, **kwargs)
    monkeypatch.setattr(cli, 'recover_captures', observed_recover)
    code = cli.main(['collect', '--max-cycles', str(len(avatars)), '--db', str(database), '--out-dir', str(out)])
    capsys.readouterr()
    assert code == 0
    assert calls['cycles'] == len(avatars)
    assert len(scans) == expected_scans
    assert len(set(scans)) == expected_scans


def test_failed_database_callback_triggers_recovery_before_next_cycle(work, monkeypatch, capsys):
    calls = mock_runtime(monkeypatch)
    monkeypatch.setattr(cli.time, 'sleep', lambda seconds: None)
    out, database = work / 'captures', work / 'reports.db'
    namespace = 'server:9/scenario:1/avatar:123'
    raw = raw_report(namespace=namespace, runtimeIdentity={'avatarId': '123', 'serverId': '9', 'scenarioId': '1'})
    scans = []
    recover = cli.recover_captures
    def observed_recover(archive, directory, selected_namespace, **kwargs):
        scans.append(selected_namespace)
        return recover(archive, directory, selected_namespace, **kwargs)
    monkeypatch.setattr(cli, 'recover_captures', observed_recover)
    attempts = []
    original_import = ReportArchive.import_file
    def fail_first_import(archive, path, namespace='default'):
        attempts.append(str(path))
        if len(attempts) == 1:
            return {'status': 'failed', 'error': 'simulated database commit failure'}
        return original_import(archive, path, namespace=namespace)
    monkeypatch.setattr(ReportArchive, 'import_file', fail_first_import)
    class Collector:
        def __init__(self, runner, directory, **kwargs):
            self.path = Path(directory) / 'battle_123_decrypted.json'
            self.callback = kwargs['on_export']
        def run_cycle(self):
            calls['cycles'] += 1
            if calls['cycles'] == 1:
                self.path.write_bytes(raw)
                try:
                    self.callback(self.path)
                except RuntimeError:
                    return {'complete': False, 'errors': ['database commit failed']}
                pytest.fail('The intentionally failed commit was not surfaced to the collector')
            # No second export callback: the root's recovery must ingest it.
            return {'complete': True, 'errors': []}
    monkeypatch.setattr(sys.modules['battle_collector'], 'BattleCollector', Collector)
    code = cli.main(['collect', '--max-cycles', '2', '--db', str(database), '--out-dir', str(out)])
    capsys.readouterr()
    assert code == 0
    assert calls['cycles'] == 2 and len(attempts) == 1
    assert scans == [namespace, namespace]
    with ReportArchive(database) as archive:
        assert archive.stats()['records'] == 1
        snapshot = archive.list_reports()[0]['current_snapshot_id']
        assert archive.original_bytes(snapshot) == raw


def test_output_directory_lock_blocks_another_database_before_runtime_entry(work, monkeypatch, capsys):
    calls = mock_runtime(monkeypatch)
    out, second_database = work / 'captures', work / 'second.db'
    # Simulate an existing collector that owns first.db and this shared output.
    with cli.collector_lock(work / 'first.collector.lock'), cli.collector_lock(out / '.collector.lock'):
        code = cli.main(['collect', '--once', '--db', str(second_database), '--out-dir', str(out)])
        output = capsys.readouterr().out
        assert code == 2
        assert calls['entered'] == calls['cycles'] == 0
        assert 'collector' in output.lower()
    # The rejected launch released its database lock; a later valid launch works.
    assert cli.main(['collect', '--once', '--db', str(second_database), '--out-dir', str(out)]) == 0
    capsys.readouterr()
    assert calls['entered'] == calls['cycles'] == 1
