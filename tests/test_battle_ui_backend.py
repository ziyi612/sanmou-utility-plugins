"""GUI process control against isolated project-local workers, never the game."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import psutil
import pytest

from battle_ui import backend


STUB = r'''
import argparse,datetime,json,os,pathlib,signal,time
p=argparse.ArgumentParser();p.add_argument('command');p.add_argument('--out-dir',type=pathlib.Path)
a,_=p.parse_known_args();a.out_dir.mkdir(parents=True,exist_ok=True)
def status(running):
 now=datetime.datetime.now(datetime.timezone.utc).isoformat()
 value={'at':now,'pid':os.getpid(),'running':running,'phase':'collecting' if running else 'stopped',
        'current_battle_id':'TEST-123','last_progress_at':now,'complete':False}
 (a.out_dir/'collector_status.json').write_text(json.dumps(value),encoding='utf-8')
def stop(*_):raise KeyboardInterrupt
signal.signal(signal.SIGINT,stop)
try:
 status(True);print('READY 中文',flush=True)
 while True:
  time.sleep(.1);print('tick',flush=True)
except KeyboardInterrupt:
 status(False);print('SAVED_STOP',flush=True)
'''


@pytest.fixture
def work():
    base = Path(".runtime/test-temp").resolve()
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="battle-gui-controller-", dir=base) as folder:
        root = Path(folder).resolve()
        assert root.parent == base
        (root / "battle_reports.py").write_text(STUB, encoding="utf-8")
        yield root
        # Only synthetic fixture workers belong to this exact temporary root.
        for process in psutil.process_iter(["name"]):
            if (process.info.get("name") or "").lower().startswith("python"):
                signature = backend.collector_signature(process, root, root / "test.db")
                if signature:
                    try:
                        backend.signal_verified_collector(signature["pid"], signature["created_at"], root,
                            root / "test.db", Path(signature["out_dir"]))
                        process.wait(timeout=5)
                    except (psutil.NoSuchProcess, psutil.TimeoutExpired, OSError, RuntimeError):
                        if process.is_running():
                            process.kill()  # A failed synthetic fixture only; never production workers.
                            process.wait(timeout=5)


def controller(work):
    return backend.Controller(work, work / "test.db", work / "capture", sys.executable)


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError("Controlled fixture did not reach the expected state")


class FakeProcess:
    pid = 123
    def __init__(self, root, args=None, created=100, running=True):
        self.root, self.args, self.created, self.running = root, args, created, running
    def cmdline(self):
        return self.args or [sys.executable, "-B", "battle_reports.py", "collect", "--db", "test.db", "--out-dir", "capture"]
    def cwd(self):
        return str(self.root)
    def create_time(self):
        return self.created
    def is_running(self):
        return self.running


def test_signature_checks_entry_command_and_database(work):
    expected = work / "test.db"
    signature = backend.collector_signature(FakeProcess(work), work, expected)
    assert signature["pid"] == 123 and Path(signature["out_dir"]) == work / "capture"
    other = [sys.executable, str(work / "other/battle_reports.py"), "collect", "--db", str(expected)]
    assert backend.collector_signature(FakeProcess(work, other), work, expected) is None
    query = [sys.executable, "battle_reports.py", "status", "--db", "test.db"]
    assert backend.collector_signature(FakeProcess(work, query), work, expected) is None
    wrong_db = [sys.executable, "battle_reports.py", "collect", "--db", "another.db"]
    assert backend.collector_signature(FakeProcess(work, wrong_db), work, expected) is None
    assert backend.collector_signature(FakeProcess(work, running=False), work, expected) is None


def test_module_and_equals_flags_are_verified(work):
    args = [sys.executable, "-m", "battle_reports", "collect", "--db=test.db", "--out-dir=capture"]
    value = backend.collector_signature(FakeProcess(work, args), work, work / "test.db")
    assert Path(value["db_path"]) == work / "test.db"
    assert Path(value["out_dir"]) == work / "capture"


def test_active_collection_options_follow_verified_cli_not_gui_defaults(work, monkeypatch):
    c = controller(work)
    args = [sys.executable, "-B", "battle_reports.py", "collect", "--db=test.db", "--out-dir=capture",
            "--scope=personal", "--interval=180", "--pid=12345"]
    signature = backend.collector_signature(FakeProcess(work, args), work, c.db_path)
    monkeypatch.setattr(c, "_find_collectors", lambda: [signature])
    assert c.snapshot()["collection_options"] == {"scopes": ["personal"], "interval": 180, "pid": 12345, "pvp_only": False, "parallel": 1}
    monkeypatch.setattr(c, "_find_collectors", lambda: [])
    assert c.snapshot()["collection_options"] is None


def test_collection_options_support_separate_values_and_last_argparse_value(work):
    args = [sys.executable, "battle_reports.py", "collect", "--db", "test.db", "--scope", "personal",
            "--interval", "10", "--scope=union,star", "--interval=180", "--parallel", "1", "--parallel=2"]
    signature = backend.collector_signature(FakeProcess(work, args), work, work / "test.db")
    assert signature["collection_options"] == {"scopes": ["union", "star"], "interval": 180, "pvp_only": False, "parallel": 2}
    defaults = backend.collector_signature(FakeProcess(work), work, work / "test.db")
    assert defaults["collection_options"] == {"scopes": ["personal", "union", "star"], "interval": 60, "pvp_only": False, "parallel": 1}


def test_unrelated_python_entry_cannot_spoof_collector_with_data_argument(work):
    for prefix in ([sys.executable, "other_worker.py"], [sys.executable, "-c", "pass"]):
        args = prefix + [str(work / "battle_reports.py"), "collect", "--db", "test.db"]
        assert backend.collector_signature(FakeProcess(work, args), work, work / "test.db") is None


def test_stale_running_status_is_not_a_live_collector(work, monkeypatch):
    c = controller(work)
    c.out_dir.mkdir()
    c.status_path.write_text(json.dumps({"running": True, "phase": "collecting", "pid": 999,
        "at": datetime.now(timezone.utc).isoformat(), "current_battle_id": "old"}), encoding="utf-8")
    monkeypatch.setattr(c, "_find_collectors", lambda: [])
    state = c.snapshot()
    assert state["running"] is False and state["can_start"] is True
    assert state["status_stale"] is True and state["current_battle_id"] is None


def test_reused_pid_does_not_attach_old_status_or_progress(work, monkeypatch):
    c = controller(work)
    c.out_dir.mkdir()
    c.status_path.write_text(json.dumps({"running": True, "pid": 123,
        "at": "1970-01-01T00:00:10+00:00", "current_battle_id": "old"}), encoding="utf-8")
    signature = backend.collector_signature(FakeProcess(work, created=100), work, c.db_path)
    monkeypatch.setattr(c, "_find_collectors", lambda: [signature])
    state = c.snapshot()
    assert state["external"] and state["running"] and state["status_stale"]
    assert state["current_battle_id"] is None


TIMING_NAMESPACE = "server:760057/scenario:18/avatar:123456789"


def timing_controller(work, monkeypatch):
    c = controller(work)
    c.out_dir.mkdir()
    signature = backend.collector_signature(FakeProcess(work, created=100), work, c.db_path)
    monkeypatch.setattr(c, "_find_collectors", lambda: [signature])
    raw = {"running": True, "pid": 123, "at": "1970-01-01T00:03:20Z", "phase": "collecting",
           "namespace": TIMING_NAMESPACE, "current_battle_id": "next", "cycle": 1}
    return c, signature, raw


def capture_receipt(**updates):
    return {"battleID": "10071018760057", "elapsed_seconds": 7.42, "outcome": "complete",
            "finished_at": "1970-01-01T00:02:00Z", "source": "collector",
            "namespace": TIMING_NAMESPACE, **updates}


def write_timing_status(c, raw):
    c.status_path.write_text(json.dumps(raw), encoding="utf-8")


def test_capture_receipt_is_a_state_change_and_survives_next_report_and_stop(work, monkeypatch):
    c, _, raw = timing_controller(work, monkeypatch)
    raw["last_capture"] = capture_receipt()
    write_timing_status(c, raw)
    first = c.snapshot()["last_capture"]
    key = c._last_state_key
    raw["last_capture"] = capture_receipt(elapsed_seconds=8.25, outcome="partial")
    write_timing_status(c, raw)
    assert c.snapshot()["last_capture"]["elapsed_seconds"] == 8.25
    assert c._last_state_key != key
    # An older worker has no formal field; starting the next report must not
    # erase the completed attempt already verified in this same session.
    raw.pop("last_capture")
    raw["current_battle_id"] = "another"
    write_timing_status(c, raw)
    assert c.snapshot()["last_capture"]["outcome"] == "partial"
    monkeypatch.setattr(c, "_find_collectors", lambda: [])
    raw.update(running=False, phase="stopped")
    write_timing_status(c, raw)
    stopped = c.snapshot()
    assert not stopped["running"] and stopped["last_capture"]["elapsed_seconds"] == 8.25
    assert first["outcome"] == "complete"


@pytest.mark.parametrize("invalid", [
    {"elapsed_seconds": True}, {"elapsed_seconds": float("nan")}, {"elapsed_seconds": -1},
    {"elapsed_seconds": 10**1000},
    {"outcome": {}}, {"source": []},
    {"battleID": ""}, {"outcome": "unknown"}, {"namespace": "another-account"},
    {"finished_at": "1970-01-01T00:00:50Z"}, {"elapsed_seconds": 300},
])
def test_invalid_or_other_session_capture_receipts_are_unknown(work, monkeypatch, invalid):
    c, _, raw = timing_controller(work, monkeypatch)
    raw["last_capture"] = capture_receipt(**invalid)
    write_timing_status(c, raw)
    assert c.snapshot()["last_capture"] is None


def test_new_process_or_account_never_reuses_previous_capture(work, monkeypatch):
    c, signature, raw = timing_controller(work, monkeypatch)
    raw["last_capture"] = capture_receipt()
    write_timing_status(c, raw)
    assert c.snapshot()["last_capture"]
    raw["namespace"] = "server:760058/scenario:18/avatar:222"
    write_timing_status(c, raw)
    assert c.snapshot()["last_capture"] is None
    raw["namespace"] = TIMING_NAMESPACE
    write_timing_status(c, raw)
    assert c.snapshot()["last_capture"]
    reused = {**signature, "created_at": 190}
    monkeypatch.setattr(c, "_find_collectors", lambda: [reused])
    assert c.snapshot()["last_capture"] is None
    # A fresh controller cannot authenticate old stopped-session history merely
    # because the persisted status file still contains the old PID.
    fresh = controller(work)
    monkeypatch.setattr(fresh, "_find_collectors", lambda: [])
    assert fresh.snapshot()["last_capture"] is None


def test_legacy_log_timing_ignores_partial_carriers_and_prefers_formal_receipt(work, monkeypatch):
    c, _, raw = timing_controller(work, monkeypatch)
    write_timing_status(c, raw)
    def event(seconds, phase, bid, **extra):
        return {"at": datetime.fromtimestamp(seconds, timezone.utc).isoformat(), "pid": 123,
                "namespace": TIMING_NAMESPACE, "cycle": 1, "phase": phase, "battleID": bid, **extra}
    rows = [event(105, "capture_started", "finished"),
            event(106, "capture_imported", "finished", completeness="partial"),
            event(111, "capture_imported", "finished", completeness="complete"),
            event(112, "capture_started", "next"),
            event(113, "capture_imported", "next", completeness="partial"),
            {**event(114, "capture_imported", "next", completeness="complete"), "pid": 999},
            {**event(115, "capture_imported", "next", completeness="complete"), "namespace": "other"}]
    (c.out_dir / "collector_diagnostics.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    recovered = c.snapshot()["last_capture"]
    assert recovered["battleID"] == "finished" and recovered["elapsed_seconds"] == 6
    assert recovered["source"] == "recovered_log" and recovered["approximate"]
    raw["last_capture"] = capture_receipt(outcome="failed")
    write_timing_status(c, raw)
    assert c.snapshot()["last_capture"]["outcome"] == "failed"
    assert c.snapshot()["last_capture"]["source"] == "collector"


def test_different_output_same_db_blocks_start_but_not_stopped_by_wrong_gui(work, monkeypatch):
    c = controller(work)
    args = [sys.executable, "battle_reports.py", "collect", "--db", "test.db", "--out-dir", "other-output"]
    signature = backend.collector_signature(FakeProcess(work, args), work, c.db_path)
    monkeypatch.setattr(c, "_find_collectors", lambda: [signature])
    assert c.snapshot()["can_start"] is False and c.snapshot()["can_stop"] is False
    with pytest.raises(RuntimeError, match="重复启动"):
        c.start()
    with pytest.raises(RuntimeError, match="无法唯一确认"):
        c.request_stop()


@pytest.mark.parametrize("options", [
    {"scopes": []}, {"scopes": ["other"]}, {"interval": 0}, {"interval": float("nan")},
    {"interval": True}, {"pid": 0}, {"pid": True}, {"parallel": 0}, {"parallel": 5},
    {"parallel": True}, {"parallel": "2"}, {"parallel": 2.5}, {"namespace": ""},
    {"pvp_only": "true"}, {"pvp_only": 1},
])
def test_bad_options_never_spawn(work, monkeypatch, options):
    c = controller(work)
    calls = []
    monkeypatch.setattr(backend.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    with pytest.raises(ValueError):
        c.start(options)
    assert not calls


def test_start_close_reopen_and_graceful_stop_real_isolated_worker(work):
    c = controller(work)
    first = c.start({"scopes": ["personal", "star"], "interval": 60, "pvp_only": True, "parallel": 2})
    assert first["owned"] and first["running"]
    assert first["collection_options"]["pvp_only"] is True
    assert "--pvp-only" in psutil.Process(first["pid"]).cmdline()
    worker_args = psutil.Process(first["pid"]).cmdline()
    assert worker_args[worker_args.index("--parallel") + 1] == "2"
    assert first["collection_options"]["parallel"] == 2
    ready = wait_until(lambda: c.snapshot() if c.snapshot()["current_battle_id"] == "TEST-123" else None)
    assert ready["can_stop"] and ready["last_progress_at"]
    log = Path(ready["log_path"])
    wait_until(lambda: "READY 中文" in log.read_text(encoding="utf-8"))
    with pytest.raises(RuntimeError, match="重复启动"):
        controller(work).start()
    size_before_close = log.stat().st_size
    pid = ready["pid"]
    c.close()
    wait_until(lambda: log.stat().st_size > size_before_close)
    assert psutil.Process(pid).is_running()
    reopened = controller(work)
    assert reopened.snapshot()["external"] and reopened.snapshot()["can_stop"]
    assert reopened.snapshot()["collection_options"]["parallel"] == 2
    started = time.monotonic()
    reopened.request_stop()
    assert time.monotonic() - started < .5
    wait_until(lambda: not reopened.snapshot()["running"])
    reopened._stop_thread.join(timeout=3)
    assert not reopened._stop_thread.is_alive()
    assert "SAVED_STOP" in log.read_text(encoding="utf-8")
    saved = json.loads(c.status_path.read_text(encoding="utf-8"))
    assert saved["running"] is False and saved["phase"] == "stopped"
    assert c.db_path.exists() is False  # The controller never creates or writes a DB.


def test_log_decoder_keeps_split_utf8_and_bounded_events(work):
    c = controller(work)
    c._log_path = work / "output.log"
    line = "战报已保存\n".encode("utf-8")
    c._log_path.write_bytes(line[:2])
    assert c.drain_events() == []
    with c._log_path.open("ab") as handle:
        handle.write(line[2:] + b"second\nthird\n")
    events = c.drain_events(limit=1)
    assert events == [{"type": "log", "text": "战报已保存"}]
    assert [x["text"] for x in c.drain_events()] == ["second", "third"]


def test_stop_signature_mismatch_never_signals(work, monkeypatch):
    called = []
    process = FakeProcess(work)
    process.send_signal = lambda value: called.append(value)
    monkeypatch.setattr(backend.psutil, "Process", lambda pid: process)
    with pytest.raises(RuntimeError, match="校验失败"):
        backend.signal_verified_collector(123, 200, work, work / "test.db", work / "capture")
    assert called == []


def test_external_diagnostics_tail_is_incremental(work):
    c = controller(work)
    c.out_dir.mkdir()
    p = c.out_dir / "collector_diagnostics.jsonl"
    p.write_text(json.dumps({"phase": "capture_imported", "battleID": "123", "completeness": "complete"}) + "\n", encoding="utf-8")
    assert "123" in c.drain_events()[0]["text"]
    assert c.drain_events() == []
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"phase": "capture_failed", "error": "specific reason"}) + "\n")
    assert c.drain_events() == [{"type": "error", "text": "specific reason"}]


def test_read_status_permits_atomic_replacement(work):
    path = work / "status.json"
    path.write_text('{"generation":1}', encoding="utf-8")
    assert backend.read_status(path) == {"generation": 1}
    replacement = work / "replacement.json"
    replacement.write_text('{"generation":2}', encoding="utf-8")
    replacement.replace(path)
    assert backend.read_status(path) == {"generation": 2}


def test_console_broadcast_rejects_unrelated_attached_processes():
    backend._validate_console_members({100, 200}, {100, 200})
    with pytest.raises(RuntimeError, match="共享控制台"):
        backend._validate_console_members({100, 200, 300}, {100, 200})
    with pytest.raises(RuntimeError, match="共享控制台"):
        backend._validate_console_members(set(), {100, 200})


def test_only_explicit_launcher_ancestors_can_share_signal(work):
    def shell(pid, name, args):
        return SimpleNamespace(pid=pid, name=lambda: name, cmdline=lambda: args)
    project = shell(10, "powershell.exe", ["powershell.exe", "-File", str(work / "start_battle_archive.ps1")])
    interactive = shell(20, "powershell.exe", ["powershell.exe"])
    outer = shell(30, "cmd.exe", ["cmd.exe", "/c", str(work / "start_battle_archive.cmd")])
    process = SimpleNamespace(parents=lambda: [project, interactive, outer])
    assert backend._verified_launcher_ancestors(process, work) == {10}
