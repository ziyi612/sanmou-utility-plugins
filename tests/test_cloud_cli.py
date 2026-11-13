"""Cloud command status, failure accounting and real collector-lock isolation."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

import battle_reports as cli
from battle_ui.data import BattleData
from parser import quark_cloud as cloud
from parser.report_archive import ReportArchive


def configuration(tmp_path, *, enabled=True):
    path = tmp_path / "quark.local.json"
    path.write_text(json.dumps({"enabled": enabled, "cli_command": ["unused-test-cli"],
        "folder_id": "fixture-folder", "cache_dir": str(tmp_path / "cache"),
        "work_dir": str(tmp_path / "work"), "temp_dir": str(tmp_path / "temp"),
        "cache_max_bytes": 1024 * 1024}), encoding="utf-8")
    return path


def original(db):
    value = {"battleID": "1001", "source": "all", "padding": "kept" * 100,
             "data": {"battleID": "1001", "timeStamp": 1794500000,
                      "eventDataList": [{"eventId": 101, "tab": 0, "intList": [0, None, 2**90]}]}}
    raw = b"\xef\xbb\xbf" + json.dumps(value, ensure_ascii=False, indent=2).encode() + b"\r\n"
    with ReportArchive(db, storage_mode="lean") as archive:
        result = archive.import_bytes(raw)
    assert result["status"] == "imported", result
    return result["snapshot_ids"][0], result["file_id"], raw


def forbid_provider(*_args, **_kwargs):
    raise AssertionError("A local status/compact operation attempted a cloud or login request")


def test_cloud_status_missing_config_and_database_is_local_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cloud, "QuarkClient", forbid_provider)
    monkeypatch.setattr(cloud, "sync_archive", forbid_provider)
    db, config = tmp_path / "missing.db", tmp_path / "missing-config.json"
    assert cli.main(["cloud", "status", "--db", str(db), "--config", str(config)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["configured"] is False and status["enabled"] is False
    assert status["verified"] == status["released_original_bytes"] == 0
    assert not db.exists() and not config.exists()


def test_cloud_status_counts_pending_without_network_or_database_mutation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cloud, "QuarkClient", forbid_provider)
    db, config = tmp_path / "archive.db", configuration(tmp_path)
    _, _, raw = original(db)
    before = db.read_bytes()
    assert cli.main(["cloud", "status", "--db", str(db), "--config", str(config)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["configured"] and status["enabled"]
    assert status["pending"] == 1 and status["verified"] == 0
    assert db.read_bytes() == before
    with ReportArchive(db, read_only=True) as archive:
        assert archive.original_bytes(1) == raw
        assert archive.connection.execute("SELECT 1 FROM sqlite_master WHERE name='report_cloud_files'").fetchone() is None


def test_cli_failed_upload_returns_failure_without_success_count_or_raw_release(tmp_path, monkeypatch, capsys):
    class FailedProvider:
        def upload(self, _path):
            raise cloud.CloudError("fixture upload unavailable")

        def download(self, *_args):
            pytest.fail("Failed upload must not proceed to download verification")

    monkeypatch.setattr(cloud, "QuarkClient", lambda _config: FailedProvider())
    db, config = tmp_path / "upload-failure.db", configuration(tmp_path)
    sid, fid, raw = original(db)
    assert cli.main(["cloud", "sync", "--db", str(db), "--config", str(config)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["verified"] == result["released_bytes"] == 0
    assert result["failed"] == 1 and result["errors"][0]["file_id"] == fid
    with ReportArchive(db, read_only=True) as archive:
        assert archive.original_bytes(sid) == raw
        assert archive.connection.execute("SELECT status FROM report_cloud_files WHERE file_id=?", (fid,)).fetchone()[0] != "verified"


def test_cloud_compact_respects_the_real_collector_lock_then_reuses_stale_lock_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cloud, "QuarkClient", forbid_provider)
    db = tmp_path / "locked.db"
    sid, _, raw = original(db)
    lock = db.with_suffix(".collector.lock")
    before = db.read_bytes()
    with cli.collector_lock(lock):
        assert cli.main(["cloud", "compact", "--db", str(db)]) == 2
        result = json.loads(capsys.readouterr().out)
        assert "collector" in result["error"].lower() and result["complete"] is False
        assert db.read_bytes() == before
    assert lock.is_file()  # Presence alone does not block a subsequent command.
    assert cli.main(["cloud", "compact", "--db", str(db)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["after_bytes"] <= result["before_bytes"]
    with ReportArchive(db, read_only=True) as archive:
        assert archive.original_bytes(sid) == raw and archive.verify()["ok"]


def test_failed_login_has_no_authorization_url_or_enabled_archive_claim(tmp_path, monkeypatch, capsys):
    class UnavailableProvider:
        def run(self, *args, **_kwargs):
            assert args == ("login",)
            raise cloud.CloudError("授权服务连接超时，尚未获得授权链接")

    monkeypatch.setattr(cloud, "QuarkClient", lambda _config: UnavailableProvider())
    config = configuration(tmp_path, enabled=False)
    before = config.read_bytes()
    assert cli.main(["cloud", "login", "--db", str(tmp_path / "not-created.db"), "--config", str(config)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["complete"] is False and "尚未获得授权链接" in result["error"]
    assert "https://" not in json.dumps(result)
    assert config.read_bytes() == before


@pytest.mark.parametrize("fresh", [True, False])
def test_cli_login_opens_fresh_authorization_without_reusing_expired_link(tmp_path, monkeypatch, capsys, fresh):
    config = configuration(tmp_path, enabled=False)
    handoff = tmp_path / "authorization-url.json"
    url = "https://pan.quark.cn/open/v1/oauth/agent?fixture=consent"
    handoff.write_text(json.dumps({"url": url}), encoding="utf-8")
    old = time.time() - 60
    os.utime(handoff, (old, old))
    value = json.loads(config.read_text(encoding="utf-8"))
    value["cli_env"] = {"SANMOU_QUARK_AUTH_URL_FILE": str(handoff)}
    config.write_text(json.dumps(value), encoding="utf-8")
    opened, ready = [], threading.Event()

    def open_page(target, _config):
        opened.append(target)
        ready.set()

    class Provider:
        def run(self, *arguments, **_kwargs):
            assert arguments == ("login",)
            if fresh:
                handoff.write_text(json.dumps({"url": url}), encoding="utf-8")
                os.utime(handoff, (old + 1, old + 1))
                assert ready.wait(3), "CLI waited for consent without opening the fresh URL"
            else:
                assert not ready.wait(0.35), "Unchanged expired handoff must stay closed"
            return {"type": "result", "code": 0}

    monkeypatch.setattr(cloud, "open_authorization_page", open_page)
    monkeypatch.setattr(cloud, "QuarkClient", lambda _config: Provider())
    assert cli.main(["cloud", "login", "--config", str(config), "--db", str(tmp_path / "absent.db")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == 0
    assert opened == ([url] if fresh else [])
    assert not (tmp_path / "absent.db").exists()


def test_ui_original_preview_resolves_cloud_bytes_outside_read_transaction(tmp_path, monkeypatch):
    db = tmp_path / "preview.db"
    sid, fid, raw = original(db)
    with ReportArchive(db) as archive:
        with archive.connection:
            archive.connection.execute("CREATE TABLE report_cloud_files(file_id INTEGER PRIMARY KEY,status TEXT)")
            archive.connection.execute("INSERT INTO report_cloud_files VALUES(?,'verified')", (fid,))
            archive.connection.execute("UPDATE report_files SET raw_bytes=? WHERE id=?", (b"", fid))
    calls = []

    def resolver(connection, file_id, db_path, *, allow_network=True):
        calls.append((file_id, db_path, allow_network, connection.in_transaction))
        assert not connection.in_transaction
        return raw

    monkeypatch.setattr(cloud, "resolve_original", resolver)
    before = db.read_bytes()
    preview = BattleData(db).original_preview(sid, max_bytes=100)
    assert preview["text"] == raw[:100].decode("utf-8-sig", "replace")
    assert preview["bytes"] == len(raw) and preview["truncated"]
    assert preview["sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(calls) == 1 and calls[0][0] == fid
    assert db.read_bytes() == before


def test_ui_original_preview_rejects_corrupt_resolver_output(tmp_path, monkeypatch):
    db = tmp_path / "bad-preview.db"
    sid, fid, _ = original(db)
    with ReportArchive(db) as archive:
        with archive.connection:
            archive.connection.execute("CREATE TABLE report_cloud_files(file_id INTEGER PRIMARY KEY,status TEXT)")
            archive.connection.execute("INSERT INTO report_cloud_files VALUES(?,'verified')", (fid,))
            archive.connection.execute("UPDATE report_files SET raw_bytes=? WHERE id=?", (b"", fid))
    monkeypatch.setattr(cloud, "resolve_original", lambda *_args, **_kwargs: b"invalid original")
    with pytest.raises(ValueError, match="byte/hash validation"):
        BattleData(db).original_preview(sid)


def test_gui_cloud_page_is_accepted_without_starting_collection(tmp_path, monkeypatch):
    import battle_reports_gui as gui
    from battle_ui import app, backend, data

    events = []

    class View:
        def __init__(self, *_args, **_options):
            events.append("created")

        def after(self, _delay, callback):
            callback()

        def show_page(self, page):
            events.append(page)

        def mainloop(self):
            events.append("mainloop")

    monkeypatch.setenv("SANMOU_TEMP_DIR", str(tmp_path / "temp"))
    monkeypatch.setattr(backend, "Controller", lambda *_args, **_options: SimpleNamespace())
    monkeypatch.setattr(data, "BattleData", lambda _path: SimpleNamespace())
    monkeypatch.setattr(app, "BattleApp", View)
    assert gui.main(["--page", "cloud", "--db", str(tmp_path / "no-db.db"),
                     "--out-dir", str(tmp_path / "incoming"), "--parallel", "1"]) == 0
    assert events == ["created", "cloud", "mainloop"]
    assert not (tmp_path / "no-db.db").exists()
