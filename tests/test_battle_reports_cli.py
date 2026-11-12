"""CLI integration tests against real retained reports and isolated project-local DBs.

Collection uses explicit doubles: these tests never attach to or send RPCs to
the game, and therefore do not establish live full-account collection coverage.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace

import pytest

import battle_reports as cli


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "capture/output/processed/battle_reports"
SAMPLES = sorted(REPORTS.glob("battle_*_decrypted.json"))


@pytest.fixture
def work():
    base = Path(".runtime/test-temp").resolve()
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sanmou-cli-test-", dir=base) as folder:
        path = Path(folder).resolve()
        # Establish the exact cleanup boundary before TemporaryDirectory removes it.
        assert path.parent == base
        yield path


def json_messages(text):
    decoder, messages = json.JSONDecoder(), []
    rest = text.strip()
    while rest:
        obj, used = decoder.raw_decode(rest)
        messages.append(obj)
        rest = rest[used:].strip()
    return messages


def run(args, capsys):
    code = cli.main([str(arg) for arg in args])
    captured = capsys.readouterr()
    return code, json_messages(captured.out)


def test_real_seven_report_import_status_duplicate_verify_export_and_readonly_query(work, capsys):
    assert len(SAMPLES) == 7, "The retained integration fixture set changed; review the new scope."
    db = work / "reports.db"
    code, messages = run(["import", REPORTS, "--db", db, "--namespace", "test"], capsys)
    assert code == 0
    imported = messages[-1]
    assert imported["files"] == 7
    first = imported["database"]
    assert first["records"] == first["snapshots"] == first["files"] == 7
    assert first["current_events"] == first["events"] == 9658
    assert first["current_completeness"] == {"legacy": 7}
    assert first["collection_coverage"] == "unknown"

    code, messages = run(["status", "--db", db, "--limit", 20], capsys)
    assert code == 0 and len(messages[-1]["reports"]) == 7
    # The report with zero events is retained rather than filtered out.
    assert any(row["event_count"] == 0 for row in messages[-1]["reports"])

    code, messages = run(["import", REPORTS, "--db", db, "--namespace", "test"], capsys)
    assert code == 0
    duplicate = messages[-1]
    assert all(row["status"] == "duplicate" for row in duplicate["results"])
    for key in ("files", "records", "snapshots", "fields", "events", "sides", "heroes", "skills"):
        assert duplicate["database"][key] == first[key]

    verification = work / "verification.json"
    code, messages = run(["verify", "--db", db, "--out", verification], capsys)
    assert code == 0 and messages[-1]["ok"] is True
    assert json.loads(verification.read_text("utf-8")) == messages[-1]

    snapshot_id = imported["results"][0]["snapshot_ids"][0]
    original = Path(imported["results"][0]["path"])
    exported = work / "roundtrip.json"
    code, _ = run(["export", snapshot_id, exported, "--db", db], capsys)
    assert code == 0 and exported.read_bytes() == original.read_bytes()

    code, messages = run(["query", "SELECT count(*) AS n FROM report_records", "--db", db], capsys)
    assert code == 0 and messages[-1]["rows"] == [[7]]
    code, messages = run(["query", "DELETE FROM report_records", "--db", db], capsys)
    assert code == 2 and messages[-1].get("error")
    code, messages = run(["status", "--db", db], capsys)
    assert code == 0 and messages[-1]["database"]["records"] == 7


def test_corrupt_file_returns_failure_without_rolling_back_successful_file(work, capsys):
    db = work / "reports.db"
    good, bad = work / "battle_a_decrypted.json", work / "battle_z_decrypted.json"
    good.write_bytes(SAMPLES[0].read_bytes())
    bad_bytes = b'{"battleID":"broken", "data":'
    bad.write_bytes(bad_bytes)
    code, messages = run(["import", good, bad, "--db", db, "--namespace", "test"], capsys)
    assert code == 2
    assert messages[-1]["database"]["records"] == 1
    assert messages[-1]["database"]["failed_files"] == 1
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as connection:
        retained = connection.execute("SELECT raw_bytes FROM report_files WHERE parse_status='failed'").fetchone()[0]
    assert retained == bad_bytes
    code, messages = run(["verify", "--db", db], capsys)
    assert code == 0 and messages[-1]["ok"] is True


@pytest.mark.parametrize("target", ["database", "verification", "export"])
def test_c_drive_output_is_rejected_before_any_c_drive_write(work, capsys, monkeypatch, target):
    if os.name != "nt":
        pytest.skip("Windows drive policy is evaluated using Windows Path semantics")
    db = work / "reports.db"
    code, messages = run(["import", SAMPLES[0], "--db", db, "--namespace", "test"], capsys)
    assert code == 0
    snapshot_id = messages[-1]["results"][0]["snapshot_ids"][0]
    forbidden = "C:/codex-cli-storage-policy-test/never-created.json"
    original_connect = sqlite3.connect
    original_write_bytes, original_write_text = Path.write_bytes, Path.write_text

    def assert_allowed(value):
        if isinstance(value, (str, Path)):
            assert Path(value).resolve().drive.casefold() != "c:", "test blocked an attempted C-drive write"

    def guarded_connect(value, *args, **kwargs):
        assert_allowed(value)
        return original_connect(value, *args, **kwargs)

    def guarded_bytes(path, *args, **kwargs):
        assert_allowed(path)
        return original_write_bytes(path, *args, **kwargs)

    def guarded_text(path, *args, **kwargs):
        assert_allowed(path)
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    monkeypatch.setattr(Path, "write_bytes", guarded_bytes)
    monkeypatch.setattr(Path, "write_text", guarded_text)
    args = {
        "database": ["import", SAMPLES[0], "--db", forbidden],
        "verification": ["verify", "--db", db, "--out", forbidden],
        "export": ["export", snapshot_id, forbidden, "--db", db],
    }[target]
    code, messages = run(args, capsys)
    assert code == 2
    assert "C:" in messages[-1]["error"] and "project-local" in messages[-1]["error"]


def mock_collection(monkeypatch, results):
    # These integration doubles model a writable collector, independent of the
    # host drive's current free space; low-space behavior has separate tests.
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _path: SimpleNamespace(free=20 * 1024 ** 3))
    observed = {"cycles": 0, "entered": 0, "exited": 0, "directories": []}

    class Runner:
        def __init__(self, pid=None):
            self.pid = pid

        def __enter__(self):
            observed["entered"] += 1
            return self

        def __exit__(self, *args):
            observed["exited"] += 1

    class Collector:
        def __init__(self, runner, out_dir, **kwargs):
            self.directory = Path(out_dir)
            self.directory.mkdir(parents=True, exist_ok=True)
            self.callback = kwargs["on_export"]
            assert kwargs["namespace"] == "test"
            assert tuple(kwargs["scopes"]) == ("personal", "union", "star")
            observed["directories"].append(self.directory)

        def run_cycle(self):
            observed["cycles"] += 1
            action = results[observed["cycles"] - 1]
            # Model a persisted retry cursor and an exported file before stop/failure.
            (self.directory / "collector_state.json").write_text('{"cursor":17}', "utf-8")
            self.callback(SAMPLES[0])
            if isinstance(action, BaseException):
                raise action
            return action

    runner_module, collector_module = ModuleType("run_lua_in_game"), ModuleType("battle_collector")
    runner_module.GameLuaRunner = Runner
    collector_module.BattleCollector = Collector
    monkeypatch.setitem(sys.modules, "run_lua_in_game", runner_module)
    monkeypatch.setitem(sys.modules, "battle_collector", collector_module)
    monkeypatch.setattr(cli, "read_identity", lambda runner: {"avatarId": "123", "serverId": "9", "scenarioId": "1"})
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    return observed


def collect_args(work):
    return ["collect", "--namespace", "test", "--db", work / "reports.db", "--out-dir", work / "capture"]


def test_collect_once_import_callback_and_success_exit(work, capsys, monkeypatch):
    observed = mock_collection(monkeypatch, [{"complete": True, "errors": []}])
    code, messages = run(collect_args(work) + ["--once"], capsys)
    assert code == 0
    assert observed["cycles"] == observed["entered"] == observed["exited"] == 1
    assert messages[-1]["database"]["records"] == 1
    assert messages[-1]["namespace"] == "test"
    saved = json.loads((work / "capture/collector_status.json").read_text("utf-8"))
    assert saved["result"]["complete"] is True


def test_collect_max_cycles_retries_and_keeps_failure_visible(work, capsys, monkeypatch):
    observed = mock_collection(monkeypatch, [RuntimeError("temporary unavailable"), {"complete": False, "errors": ["missing page"]}])
    code, messages = run(collect_args(work) + ["--max-cycles", 2], capsys)
    assert code == 2
    assert observed["cycles"] == observed["entered"] == observed["exited"] == 2
    statuses = [message for message in messages if "cycle" in message]
    assert statuses[0]["retrying"] is True
    assert statuses[1]["result"]["complete"] is False
    assert statuses[1]["database"]["records"] == 1


def test_native_failure_preserves_operation_and_thread_in_status_and_diagnostic(work, capsys, monkeypatch):
    from capture.game_assets.runtime_errors import RuntimeBridgeUnavailable
    error = RuntimeBridgeUnavailable("native Lua hook failed")
    error.result = {"stage": "lua_hook", "nativeOperation": "lua_pcall", "rawL": "0x1000",
                    "threadId": 123, "error": "Error: access violation accessing 0x1f0"}
    mock_collection(monkeypatch, [error])
    code, messages = run(collect_args(work) + ["--once"], capsys)
    assert code == 2 and messages[-1]["runtime_error"] == error.result
    saved = json.loads((work / "capture/collector_status.json").read_text(encoding="utf-8"))
    assert saved["runtime_error"] == error.result and saved["reconnecting"] is True
    diagnostics = [json.loads(line) for line in (work / "capture/collector_diagnostics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert next(row for row in diagnostics if row["phase"] == "cycle_failed")["runtime_error"] == error.result


def test_collect_keyboard_interrupt_retains_database_and_retry_state(work, capsys, monkeypatch):
    observed = mock_collection(monkeypatch, [KeyboardInterrupt()])
    code, messages = run(collect_args(work), capsys)
    assert code == 0 and messages[-1]["stopped"] is True
    assert messages[-1]["database"]["records"] == 1
    assert observed["exited"] == 1
    assert json.loads((observed["directories"][0] / "collector_state.json").read_text("utf-8")) == {"cursor": 17}
    # A second invocation can acquire the database lock after the interruption.
    observed = mock_collection(monkeypatch, [{"complete": True, "errors": []}])
    code, _ = run(collect_args(work) + ["--once"], capsys)
    assert code == 0 and observed["cycles"] == 1


@pytest.mark.parametrize("interrupted", [False, True])
def test_collect_and_stop_never_scan_full_archive_statistics(work, capsys, monkeypatch, interrupted):
    from parser.report_archive import ReportArchive
    action = KeyboardInterrupt() if interrupted else {"complete": True, "errors": []}
    mock_collection(monkeypatch, [action])

    def reject_full_scan(_self):
        raise AssertionError("Collection must never scan the historical field/event tables for UI statistics")

    monkeypatch.setattr(ReportArchive, "stats", reject_full_scan)
    args = collect_args(work) + ([] if interrupted else ["--once"])
    code, messages = run(args, capsys)
    assert code == 0
    result = messages[-1]
    assert result["database"]["records"] == 1
    assert result["database"]["statistics_mode"] == "current_reports"
    diagnostics = [json.loads(line) for line in
                   (work / "capture/collector_diagnostics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(item["phase"] == "cycle_statistics_started" for item in diagnostics)
    finished = [item for item in diagnostics if item["phase"] == "cycle_statistics_finished"]
    assert len(finished) == 1 and finished[0]["elapsed_seconds"] >= 0
    if interrupted:
        assert result["stopped"] is True


def test_script_entrypoint_reports_nonzero_exit_for_missing_input(work):
    env = dict(os.environ, TEMP=str(work), TMP=str(work), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, str(ROOT / "battle_reports.py"), "import", str(work / "missing.json"),
                             "--db", str(work / "reports.db")], capture_output=True, text=True,
                            encoding="utf-8", env=env, cwd=ROOT)
    assert result.returncode == 2
    assert json_messages(result.stdout)[-1]["complete"] is False


def test_collect_missing_identity_fails_without_attempting_collection(work, capsys, monkeypatch):
    observed = mock_collection(monkeypatch, [])

    def unavailable(runner):
        raise RuntimeError("Game not logged in or unavailable")

    monkeypatch.setattr(cli, "read_identity", unavailable)
    code, messages = run(collect_args(work) + ["--once"], capsys)
    assert code == 2 and messages[-1]["complete"] is False
    assert observed["cycles"] == 0
    assert observed["entered"] == observed["exited"] == 1
    code, messages = run(["status", "--db", work / "reports.db"], capsys)
    assert code == 0 and messages[-1]["database"]["records"] == 0


def test_legacy_test_script_imports_attempt_no_filesystem_mutations(work):
    # Fail before any write/remove/rename rather than trusting a post-hoc snapshot.
    source = r'''
import importlib.util, os, pathlib, sys
sys.dont_write_bytecode = True
def guard(event, args):
    if event == 'open':
        path, mode, flags = args
        if (isinstance(mode,str) and any(ch in mode for ch in 'wax+')) or flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
            raise RuntimeError('Unexpected import-time write: ' + str(path))
    if event in {'os.remove','os.rename','os.rmdir','os.mkdir'}:
        raise RuntimeError('Unexpected import-time mutation: ' + event)
sys.addaudithook(guard)
for name in ('test_e2e','test_ce_e2e'):
    path = pathlib.Path(sys.argv[1]) / (name + '.py')
    spec = importlib.util.spec_from_file_location(name,path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)
print('imports performed no filesystem mutations')
'''
    env = dict(os.environ, TEMP=str(work), TMP=str(work), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-B", "-c", source, str(ROOT)], capture_output=True,
                            text=True, encoding="utf-8", env=env, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    assert "no filesystem mutations" in result.stdout


def mock_explicit_capture(monkeypatch, actions):
    from capture.game_assets.export_battle_report_runtime import validate_battle_id
    observed = mock_collection(monkeypatch, [])
    observed["captures"] = []

    def capture_report(runner, battle_id, out_dir, *, wait_seconds, expected_identity):
        directory = Path(out_dir)
        observed["captures"].append({"battle_id": battle_id, "directory": directory,
                                     "identity": expected_identity})
        assert expected_identity == {"avatarId": "123", "serverId": "9", "scenarioId": "1"}
        action = actions[battle_id]
        if isinstance(action, Exception):
            raise action
        complete = action != "partial"
        data = {"battleID": battle_id, "eventDataList": [{"eventId": 1, "intList": []}],
                "battleSettlement": {"attacker": {"army_member": []}, "defensive": {"army_member": []}}}
        components = {"basic": {"battleID": battle_id}, "detail": data, "all": data,
                      "statics": {} if complete else None,
                      "snapshots": {"isComplete": True, "battleSnapData": []}}
        payload = {"captureVersion": 2, "battleID": battle_id, "source": "all", "eventCount": 1,
                   "data": data, "components": components, "captureComplete": complete,
                   "serializationIssues": [], "runtimeIdentity": expected_identity,
                   "componentStatus": {key: {"available": value is not None, "complete": value is not None,
                                              "requested": True} for key, value in components.items()}}
        path = directory / f"battle_{battle_id}_decrypted.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        record = {"battleID": battle_id, "exported": True, "path": str(path)}
        if action != "missing_complete":
            record["complete"] = complete
        return record

    module = ModuleType("export_battle_report_runtime")
    module.capture_report = capture_report
    module.validate_battle_id = validate_battle_id
    monkeypatch.setitem(sys.modules, "export_battle_report_runtime", module)
    return observed


def explicit_args(work, *ids):
    # Exercise the actual automatic account/season/server namespace default.
    return ["collect", "--db", work / "reports.db", "--out-dir", work / "capture",
            "--once", "--battle-ids", *ids]


def test_explicit_ids_success_uses_current_identity_and_does_not_enumerate_scopes(work, capsys, monkeypatch):
    observed = mock_explicit_capture(monkeypatch, {1201: "complete"})
    code, messages = run(explicit_args(work, 1201, 1201), capsys)
    assert code == 0
    status = messages[-1]
    assert status["namespace"] == "server:9/scenario:1/avatar:123"
    assert status["result"]["scope"] == "explicit_ids" and status["result"]["complete"] is True
    assert status["database"]["records"] == 1
    assert status["database"]["current_completeness"] == {"complete": 1}
    assert observed["cycles"] == 0  # No BattleCollector.run_cycle/list pagination.
    assert [item["battle_id"] for item in observed["captures"]] == [1201]
    directory = observed["captures"][0]["directory"]
    binding = json.loads((directory / "account.json").read_text("utf-8"))
    assert binding["namespace"] == status["namespace"]
    code, messages = run(["status", "--db", work / "reports.db"], capsys)
    assert code == 0 and messages[-1]["reports"][0]["namespace"] == binding["namespace"]
    assert messages[-1]["reports"][0]["battle_uid"] == "1201"


def test_explicit_partial_is_retained_but_returns_failure(work, capsys, monkeypatch):
    observed = mock_explicit_capture(monkeypatch, {2201: "partial"})
    code, messages = run(explicit_args(work, 2201), capsys)
    assert code == 2
    assert messages[-1]["result"]["complete"] is False
    assert messages[-1]["database"]["records"] == 1
    assert messages[-1]["database"]["current_completeness"] == {"partial": 1}
    assert (observed["captures"][0]["directory"] / "battle_2201_decrypted.json").is_file()


def test_explicit_capture_failure_does_not_discard_later_success(work, capsys, monkeypatch):
    observed = mock_explicit_capture(monkeypatch, {3201: RuntimeError("detail expired"), 3202: "complete"})
    code, messages = run(explicit_args(work, 3201, 3202), capsys)
    assert code == 2
    assert [item["battle_id"] for item in observed["captures"]] == [3201, 3202]
    status = messages[-1]
    assert status["database"]["records"] == 1
    reports = status["result"]["reports"]
    assert reports[0]["exported"] is False and reports[0]["error"] == "detail expired"
    assert reports[1]["exported"] is True and reports[1]["complete"] is True
    manifest = observed["captures"][0]["directory"] / "explicit_ids_manifest.json"
    assert json.loads(manifest.read_text("utf-8")) == reports
    code, messages = run(["status", "--db", work / "reports.db"], capsys)
    assert code == 0 and [row["battle_uid"] for row in messages[-1]["reports"]] == ["3202"]


def test_explicit_missing_completeness_cannot_report_success(work, capsys, monkeypatch):
    mock_explicit_capture(monkeypatch, {4201: "missing_complete"})
    code, messages = run(explicit_args(work, 4201), capsys)
    assert code == 2
    assert messages[-1]["result"]["complete"] is False
    assert messages[-1]["database"]["records"] == 1
    assert "complete" not in messages[-1]["result"]["reports"][0]


@pytest.mark.parametrize("final_exported", [False, True])
def test_explicit_cache_checkpoints_commit_immediately_and_replay_without_duplicates(
        work, capsys, monkeypatch, final_exported):
    mock_explicit_capture(monkeypatch, {6201: "partial"})
    module = sys.modules["export_battle_report_runtime"]
    original_capture = module.capture_report
    retained = []

    def capture_with_checkpoint(*args, **kwargs):
        record = original_capture(*args, **kwargs)
        raw = Path(record["path"]).read_bytes()
        checkpoint = Path(args[2]) / "captures" / "6201" / (hashlib.sha256(raw).hexdigest() + ".json")
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_bytes(raw)
        retained.append((checkpoint, raw))
        return {"battleID": 6201, "exported": final_exported, "complete": False,
                "path": str(checkpoint), "cacheCheckpointPaths": [str(checkpoint), str(checkpoint)],
                **({} if final_exported else {"error": "not_found 6201"})}

    module.capture_report = capture_with_checkpoint
    code, messages = run(explicit_args(work, 6201), capsys)
    assert code == 2 and messages[-1]["result"]["complete"] is False
    assert messages[-1]["database"]["records"] == 1
    assert messages[-1]["database"]["snapshots"] == 1
    assert messages[-1]["database"]["current_completeness"] == {"partial": 1}
    imports = [message["database_import"] for message in messages if "database_import" in message]
    assert len(imports) == 1 and imports[0]["status"] == "imported"
    with closing(sqlite3.connect((work / "reports.db").as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute("SELECT raw_bytes FROM report_files").fetchone()[0] == retained[0][1]

    code, messages = run(explicit_args(work, 6201), capsys)
    assert code == 2
    for key in ("records", "files", "snapshots"):
        assert messages[-1]["database"][key] == 1
    assert messages[-1]["database"]["current_completeness"] == {"partial": 1}


def progress_collection(monkeypatch, work, error=None):
    """Emit progress synchronously; no real runner or game process is used."""
    observed = mock_collection(monkeypatch, [])
    observed.update(owners=[], progress_states={})
    runner_module = sys.modules["run_lua_in_game"]
    parent_runner = runner_module.GameLuaRunner

    class Runner(parent_runner):
        def __init__(self, pid=None):
            super().__init__(pid)
            self.pid = pid or 27182
            self.process = {"pid": self.pid, "name": "fake-game", "selection": "visible_main_window"}

    class Collector:
        def __init__(self, runner, out_dir, **kwargs):
            self.callback, self.progress = kwargs["on_export"], kwargs["on_progress"]
            observed["owners"].append(kwargs["owner_id"])

        def run_cycle(self):
            observed["cycles"] += 1
            def inspect(stage):
                observed["progress_states"][stage] = json.loads(
                    (work / "capture/collector_status.json").read_text(encoding="utf-8"))
            self.progress({"phase": "page_received", "scope": "personal", "index": 11, "count": 2})
            inspect("page")
            self.progress({"phase": "capture_started", "battleID": 123})
            inspect("capture")
            self.callback(SAMPLES[0])
            inspect("import")
            if observed["cycles"] == 1 and error is not None:
                self.progress({"phase": "capture_failed", "battleID": 123, "error": str(error),
                               "error_type": type(error).__name__,
                               "runtime_unavailable": bool(getattr(error, "runtime_unavailable", False))})
                inspect("failure")
                if getattr(error, "runtime_unavailable", False):
                    raise error
                return {"complete": False, "errors": [str(error)]}
            return {"complete": True, "errors": []}

    runner_module.GameLuaRunner = Runner
    sys.modules["battle_collector"].BattleCollector = Collector
    return observed


def test_progress_updates_durable_status_and_diagnostics_without_extra_json_stdout(work, capsys, monkeypatch):
    reason = "client RPC timed out; specific diagnostic detail"
    observed = progress_collection(monkeypatch, work, RuntimeError(reason))
    code, messages = run(collect_args(work) + ["--once"], capsys)
    assert code == 2
    assert len(messages) == 2  # Existing database-import and end-of-cycle messages.
    states = observed["progress_states"]
    assert states["page"]["current_scope"] == "personal" and states["page"]["current_page"] == 11
    assert states["capture"]["current_battle_id"] == "123"
    assert states["import"]["last_success_at"]
    assert states["failure"]["last_error"] == reason
    assert states["failure"]["current_stage"] == "capture_failed"
    for value in states.values():
        assert value["phase"] == "collecting" and value["running"] is True and value["complete"] is False
        assert value["namespace"] == "test" and value["identity"]["avatarId"] == "123"
        assert value["pid"] == os.getpid() and value["last_progress_at"]
        assert value["game_pid"] == 27182 and value["game_selection"] == "visible_main_window"
    diagnostics = [json.loads(line) for line in (work / "capture/collector_diagnostics.jsonl").read_text("utf-8").splitlines()]
    failed = [line for line in diagnostics if line["phase"] == "capture_failed"]
    assert failed[0]["error"] == reason and failed[0]["error_type"] == "RuntimeError"


@pytest.mark.parametrize("outcome", ["complete", "partial", "failed"])
def test_finished_capture_timing_survives_next_start_and_partial_import(work, capsys, monkeypatch, outcome):
    mock_collection(monkeypatch, [])
    observed = {}
    class Collector:
        def __init__(self, _runner, _directory, **kwargs):
            self.progress = kwargs["on_progress"]
        def run_cycle(self):
            path = work / "capture/collector_status.json"
            self.progress({"phase": "capture_started", "battleID": 123})
            self.progress({"phase": "capture_imported", "battleID": 123, "completeness": "partial"})
            assert "last_capture" not in json.loads(path.read_text("utf-8"))
            if outcome == "failed":
                self.progress({"phase": "capture_failed", "battleID": 123, "error": "connection lost",
                               "error_type": "RuntimeBridgeUnavailable", "runtime_unavailable": True})
            self.progress({"phase": "capture_finished", "battleID": 123, "elapsed_seconds": 7.25,
                           "outcome": outcome, "finished_at": "2026-11-09T14:30:00+00:00"})
            observed["finished"] = json.loads(path.read_text("utf-8"))
            self.progress({"phase": "capture_started", "battleID": 456})
            self.progress({"phase": "capture_imported", "battleID": 456, "completeness": "partial"})
            observed["during_next"] = json.loads(path.read_text("utf-8"))
            return {"complete": False, "errors": []}
    sys.modules["battle_collector"].BattleCollector = Collector
    code, messages = run(collect_args(work) + ["--once"], capsys)
    assert code == 2
    timing = observed["finished"]["last_capture"]
    assert timing == {"namespace": "test", "battleID": "123", "elapsed_seconds": 7.25,
                      "outcome": outcome, "finished_at": "2026-11-09T14:30:00+00:00",
                      "source": "collector", "approximate": False}
    final_status = json.loads((work / "capture/collector_status.json").read_text("utf-8"))
    assert observed["during_next"]["last_capture"] == final_status["last_capture"] == timing
    if outcome == "failed":
        assert observed["finished"]["last_error"] == "connection lost"
        assert observed["finished"]["reconnecting"] is True
    lines = [json.loads(line) for line in (work / "capture/collector_diagnostics.jsonl").read_text("utf-8").splitlines()]
    assert len([line for line in lines if line["phase"] == "capture_finished"]) == 1


def test_finished_capture_timing_does_not_cross_runtime_accounts(work, capsys, monkeypatch):
    mock_collection(monkeypatch, [])
    identities = iter([{"avatarId": "123", "serverId": "9", "scenarioId": "1"},
                       {"avatarId": "456", "serverId": "9", "scenarioId": "1"}])
    monkeypatch.setattr(cli, "read_identity", lambda _runner: next(identities))
    seen = []
    class Collector:
        def __init__(self, _runner, _directory, **kwargs):
            self.progress = kwargs["on_progress"]
        def run_cycle(self):
            seen.append(json.loads((work / "capture/collector_status.json").read_text("utf-8")))
            if len(seen) == 1:
                self.progress({"phase": "capture_finished", "battleID": 123, "elapsed_seconds": 7.25,
                               "outcome": "complete"})
            return {"complete": True, "errors": []}
    sys.modules["battle_collector"].BattleCollector = Collector
    args = ["collect", "--db", work / "reports.db", "--out-dir", work / "capture", "--max-cycles", 2]
    code, _ = run(args, capsys)
    assert code == 0 and len(seen) == 2
    assert seen[0]["namespace"].endswith("avatar:123") and seen[1]["namespace"].endswith("avatar:456")
    assert "last_capture" not in seen[1]


@pytest.mark.parametrize("invalid", [pytest.param(2**2000, id="overflow"),
                                    pytest.param(float("nan"), id="nan"), pytest.param(-1, id="negative")])
def test_invalid_capture_duration_does_not_replace_last_known_timing(work, capsys, monkeypatch, invalid):
    mock_collection(monkeypatch, [])
    class Collector:
        def __init__(self, _runner, _directory, **kwargs):
            self.progress = kwargs["on_progress"]
        def run_cycle(self):
            self.progress({"phase": "capture_finished", "battleID": 123, "elapsed_seconds": 7.25,
                           "outcome": "complete"})
            self.progress({"phase": "capture_finished", "battleID": 456, "elapsed_seconds": invalid,
                           "outcome": "complete"})
            return {"complete": True, "errors": []}
    sys.modules["battle_collector"].BattleCollector = Collector
    code, _ = run(collect_args(work) + ["--once"], capsys)
    assert code == 0
    saved = json.loads((work / "capture/collector_status.json").read_text("utf-8"))["last_capture"]
    assert saved["battleID"] == "123" and saved["elapsed_seconds"] == 7.25


def test_console_reports_bridge_failure_and_reconnect_keeps_one_owner(work, capsys, monkeypatch):
    from capture.game_assets.runtime_errors import RuntimeBridgeUnavailable
    reason = "Lua main thread stopped responding"
    observed = progress_collection(monkeypatch, work, RuntimeBridgeUnavailable(reason))
    code = cli.main([str(v) for v in collect_args(work) + ["--console", "--max-cycles", 2]])
    output = capsys.readouterr().out
    assert code == 0
    assert "游戏连接已失效" in output and reason in output and "重新连接" in output
    assert observed["cycles"] == 2 and len(set(observed["owners"])) == 1
    assert len(observed["owners"][0]) == 32
    assert observed["progress_states"]["failure"]["reconnecting"] is True
    final = json.loads((work / "capture/collector_status.json").read_text("utf-8"))
    assert final["phase"] == "finished" and final["running"] is False and final["complete"] is False
    assert final["last_error"] is None and final["reconnecting"] is False
    diagnostics = [json.loads(line) for line in (work / "capture/collector_diagnostics.jsonl").read_text("utf-8").splitlines()]
    assert any(line.get("error") == reason and line.get("error_type") == "RuntimeBridgeUnavailable" for line in diagnostics)


def test_fast_import_progress_is_throttled_without_losing_capture_failure(work, capsys, monkeypatch):
    observed = mock_collection(monkeypatch, [])
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: 100.0, sleep=lambda seconds: None))
    writes = []
    original_write = cli.atomic_json
    def track(path, value):
        if Path(path).name == "collector_status.json":
            writes.append(dict(value))
        return original_write(path, value)
    monkeypatch.setattr(cli, "atomic_json", track)

    class Collector:
        def __init__(self, runner, out_dir, **kwargs):
            self.progress = kwargs["on_progress"]
        def run_cycle(self):
            for _ in range(50):
                self.progress({"phase": "capture_imported", "battleID": 123})
            self.progress({"phase": "capture_failed", "battleID": 123, "error": "preserved failure"})
            return {"complete": False, "errors": ["preserved failure"]}
    sys.modules["battle_collector"].BattleCollector = Collector
    code, _ = run(collect_args(work) + ["--once"], capsys)
    assert code == 2
    assert sum(value.get("current_stage") == "capture_imported" for value in writes) == 1
    assert any(value.get("current_stage") == "capture_failed" and value["last_error"] == "preserved failure"
               for value in writes)


def test_explicit_bridge_failure_aborts_remaining_ids_and_logs_reason(work, capsys, monkeypatch):
    from capture.game_assets.runtime_errors import RuntimeBridgeUnavailable
    reason = "pending Lua execution cancelled; bridge must reconnect"
    observed = mock_explicit_capture(monkeypatch, {5201: RuntimeBridgeUnavailable(reason), 5202: "complete"})
    code, messages = run(explicit_args(work, 5201, 5202), capsys)
    assert code == 2
    assert [item["battle_id"] for item in observed["captures"]] == [5201]
    assert messages[-1]["error_type"] == "RuntimeBridgeUnavailable"
    saved = json.loads((work / "capture/collector_status.json").read_text("utf-8"))
    assert saved["current_battle_id"] == "5201" and saved["last_error"] == reason
    assert saved["namespace"] == "server:9/scenario:1/avatar:123" and saved["identity"]["avatarId"] == "123"
    log = [json.loads(line) for line in (work / "capture/collector_diagnostics.jsonl").read_text("utf-8").splitlines()]
    assert any(item["phase"] == "capture_started" and item["battleID"] == 5201 for item in log)
    assert any(item["phase"] == "capture_failed" and item["error"] == reason for item in log)
