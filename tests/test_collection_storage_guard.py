"""Storage stops use tiny project-local fixtures and never connect to a real game."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

import battle_reports as cli
from capture.game_assets import battle_collector as collector_module
from parser.report_archive import ReportArchive


GIB = 1024 ** 3
IDENTITY = {"avatarId": "123", "serverId": "9", "scenarioId": "1"}


@pytest.fixture
def work(tmp_path):
    assert tmp_path.resolve().is_dir()
    return tmp_path


def payload(battle_id=1):
    data = {"battleID": battle_id, "eventDataList": [{"eventId": 0, "intList": [0], "tab": 0}]}
    components = {name: data for name in ("basic", "detail", "all", "statics")}
    components["snapshots"] = {"isComplete": True, "battleSnapData": []}
    return {"captureVersion": 2, "battleID": battle_id, "source": "all", "data": data,
            "eventCount": 1, "captureComplete": True, "components": components,
            "componentStatus": {name: {"available": True, "complete": True} for name in components},
            "runtimeIdentity": IDENTITY, "serializationIssues": []}


def args(work):
    return ["collect", "--db", str(work / "reports.db"), "--out-dir", str(work / "capture"),
            "--namespace", "storage-test", "--parallel", "2"]


def messages(text):
    decoder, result = json.JSONDecoder(), []
    while text.strip():
        item, length = decoder.raw_decode(text.lstrip())
        result.append(item)
        text = text.lstrip()[length:]
    return result


def test_actual_cli_refuses_before_creating_database_or_connecting(work):
    db = work / "not-created" / "reports.db"
    output = work / "not-created" / "capture"
    env = dict(os.environ, TEMP=str(work), TMP=str(work), PYTHONUTF8="1",
               PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    # This exceeds the entire physical drive, so concurrent cleanup cannot
    # accidentally let the real CLI attach to the game during this test.
    result = subprocess.run([sys.executable, "-B", str(cli.ROOT / "battle_reports.py"),
        "collect", "--db", str(db), "--out-dir", str(output), "--min-free-gib", "1000000"],
        cwd=cli.ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 2, result.stderr
    status = json.loads(result.stdout)
    assert status["phase"] == "storage_paused" and status["running"] is False
    assert status["retrying"] is False and status["error_type"] == "CollectionStorageLimit"
    assert status["storage"]["free_bytes"] < status["storage"]["minimum_free_bytes"]
    assert "手动启动" in status["error"] and not db.parent.exists()


def test_default_reserve_boundary_and_stop_are_latched(work, monkeypatch):
    assert cli.build_parser().parse_args(["collect"]).min_free_gib == 5
    free = {"bytes": 5 * GIB}
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _path: SimpleNamespace(free=free["bytes"]))
    guard = cli.CollectionStorageGuard((work / "not-created" / "database",))
    guard.check()
    free["bytes"] -= 1
    with pytest.raises(cli.CollectionStorageLimit) as first:
        guard.check()
    free["bytes"] = 20 * GIB
    with pytest.raises(cli.CollectionStorageLimit) as second:
        guard.check()
    assert first.value is second.value and not (work / "not-created").exists()


def install_collection_doubles(monkeypatch, free, drop_at):
    observed = {"entered": 0, "exited": 0, "captures": [], "pages": 0, "cycles": 0, "cleanup": 0}

    class Runner:
        def __init__(self, pid=None):
            self.pid = 12345

        def __enter__(self):
            observed["entered"] += 1
            return self

        def __exit__(self, *_args):
            observed["exited"] += 1

    def discover(self, scope, capabilities):
        observed["cycles"] += 1
        self.state["runtimeIdentity"] = IDENTITY
        self.state["reports"] = {str(bid): {"battleID": str(bid), "attempts": 0,
            "complete": False, "observations": {}, "lastSeenCycle": self.state["cycles"]} for bid in (1, 2, 3)}
        self.save()
        try:
            if drop_at in {"page", "capture"}:
                free["bytes"] = 4 * GIB
            if drop_at == "page":
                self.page("personal", 0, 0, 0)
            return self.collect_pending()
        finally:
            self.save()

    def capture(_runner, bid, folder, *_args, **kwargs):
        observed["captures"].append((bid, kwargs.get("request_missing", True)))
        raw = json.dumps(payload(bid), ensure_ascii=False).encode("utf-8")
        path = Path(folder) / ("battle_{}_decrypted.json".format(bid))
        path.write_bytes(raw)
        if drop_at == "import":
            free["bytes"] = 4 * GIB
        return {"battleID": bid, "path": str(path), "exported": True, "complete": True}

    def page(*_args, **_kwargs):
        observed["pages"] += 1
        raise AssertionError("A list request must not begin below the reserve")

    def runtime(_runner, code):
        if code == "storage-test-install":
            return json.dumps({"payload": {"runtimeIdentity": IDENTITY}})
        assert code == "storage-test-cleanup"
        observed["cleanup"] += 1
        return "cleaned"

    runner = ModuleType("run_lua_in_game")
    runner.GameLuaRunner = Runner
    collector = ModuleType("battle_collector")
    collector.BattleCollector = collector_module.BattleCollector
    monkeypatch.setitem(sys.modules, "run_lua_in_game", runner)
    monkeypatch.setitem(sys.modules, "battle_collector", collector)
    # Keep the real run_cycle/finally and prefetch-harvest paths under test.
    monkeypatch.setattr(collector_module.BattleCollector, "discover_scope", discover)
    monkeypatch.setattr(collector_module.BattleCollector, "page", page)
    monkeypatch.setattr(collector_module, "install_lua", lambda *_args: "storage-test-install")
    monkeypatch.setattr(collector_module, "cleanup_lua", lambda *_args: "storage-test-cleanup")
    monkeypatch.setattr(collector_module, "run_checked", runtime)
    monkeypatch.setattr(collector_module, "capture_report", capture)
    monkeypatch.setattr(collector_module, "prefetch_reports", lambda _runner, group, **_kwargs: {bid: {} for bid in group})
    monkeypatch.setattr(cli, "read_identity", lambda _runner: IDENTITY)
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _path: SimpleNamespace(free=free["bytes"]))
    from parser import semantic_catalog
    monkeypatch.setattr(semantic_catalog, "build_default_mapper", lambda: None)
    return observed


@pytest.mark.parametrize("drop_at", ["page", "capture"])
def test_threshold_crossing_prevents_next_list_or_report_request(work, monkeypatch, capsys, drop_at):
    free = {"bytes": 6 * GIB}
    observed = install_collection_doubles(monkeypatch, free, drop_at)
    assert cli.main(args(work)) == 2
    status = messages(capsys.readouterr().out)[-1]
    assert status["phase"] == "storage_paused" and status["retrying"] is False
    assert observed["cycles"] == observed["entered"] == observed["exited"] == observed["cleanup"] == 1
    assert observed["captures"] == [] and observed["pages"] == 0
    disk_status = json.loads((work / "capture/collector_status.json").read_text("utf-8"))
    assert disk_status["running"] is False and disk_status["current_stage"] == "storage_paused"


def test_low_space_preserves_current_and_prefetched_pending_bytes_without_importing(work, monkeypatch, capsys):
    free = {"bytes": 6 * GIB}
    observed = install_collection_doubles(monkeypatch, free, "import")
    assert cli.main(args(work)) == 2
    status = messages(capsys.readouterr().out)[-1]
    assert status["phase"] == "storage_paused" and status["running"] is False
    assert observed["entered"] == observed["exited"] == observed["cycles"] == observed["cleanup"] == 1
    assert observed["captures"] == [(1, True), (2, False)]
    account = next((work / "capture").glob("*/account.json")).parent
    state = json.loads((account / "collector_state.json").read_text("utf-8"))
    originals = {}
    for bid in ("1", "2"):
        pending = state["reports"][bid]["pendingImport"]
        path = Path(pending["path"])
        raw = path.read_bytes()
        assert path.stem == hashlib.sha256(raw).hexdigest()
        assert json.loads(raw)["battleID"] == int(bid)
        originals[path] = raw
    assert state["reports"]["3"]["attempts"] == 0
    with ReportArchive(work / "reports.db", read_only=True) as archive:
        assert archive.connection.execute("SELECT count(*) FROM report_files").fetchone()[0] == 0
    # The exact durable originals remain recoverable after space is released.
    free["bytes"] = 6 * GIB
    guard = cli.CollectionStorageGuard((work,))
    with ReportArchive(work / "reports.db") as archive:
        recovered = cli.recover_captures(archive, account, "storage-test", IDENTITY, before_import=guard.check)
        assert recovered["recovered"] == 2 and recovered["errors"] == []
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_recovery_checks_each_uncommitted_file_and_leaves_remaining_originals(work, monkeypatch):
    captures = work / "captures"
    captures.mkdir()
    originals = {}
    for bid in (1, 2):
        path = captures / ("battle_{}_decrypted.json".format(bid))
        originals[path] = json.dumps(payload(bid)).encode()
        path.write_bytes(originals[path])
    free = {"bytes": 6 * GIB}
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _path: SimpleNamespace(free=free["bytes"]))
    guard = cli.CollectionStorageGuard((work,))
    with ReportArchive(work / "reports.db") as archive:
        original_import = archive.import_bytes
        def import_then_fill(*args, **kwargs):
            result = original_import(*args, **kwargs)
            free["bytes"] = 4 * GIB
            return result
        monkeypatch.setattr(archive, "import_bytes", import_then_fill)
        with pytest.raises(cli.CollectionStorageLimit):
            cli.recover_captures(archive, captures, "storage-test", before_import=guard.check)
        assert archive.connection.execute("SELECT count(*) FROM report_files").fetchone()[0] == 1
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_readonly_status_and_original_export_ignore_collection_reserve(work, monkeypatch, capsys):
    raw = json.dumps(payload()).encode()
    db = work / "reports.db"
    with ReportArchive(db) as archive:
        snapshot = archive.import_bytes(raw)["snapshot_ids"][0]
    before = db.read_bytes()
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _path: (_ for _ in ()).throw(AssertionError("Read-only command checked collection reserve")))
    assert cli.main(["status", "--db", str(db)]) == 0
    capsys.readouterr()
    output = work / "export.json"
    assert cli.main(["export", str(snapshot), str(output), "--db", str(db)]) == 0
    capsys.readouterr()
    assert output.read_bytes() == raw and db.read_bytes() == before


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "1e308"])
def test_invalid_reserve_is_rejected_before_writable_database(work, capsys, value):
    assert cli.main(args(work) + ["--min-free-gib", value]) == 2
    assert "finite and positive" in messages(capsys.readouterr().out)[-1]["error"]
    assert not (work / "reports.db").exists()
