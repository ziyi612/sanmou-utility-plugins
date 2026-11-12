"""Old network retries yield between reports; durable cache pages never vanish."""
from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest

from capture.game_assets import battle_collector as module
from capture.game_assets.battle_collector import BattleCollector
from parser.report_archive import ReportArchive


class Clock:
    def __init__(self):
        self.elapsed = 0.0
    def monotonic(self):
        return self.elapsed
    def time(self):
        return 10000 + self.elapsed
    def advance(self, seconds):
        self.elapsed += seconds


def old_entry(bid, attempts=4, missing_snapshot=False):
    statuses = {name: {"available": missing_snapshot and name != "snapshots",
                      "complete": missing_snapshot and name != "snapshots"}
                for name in ("basic", "detail", "all", "statics", "snapshots")}
    return {"battleID": str(bid), "attempts": attempts, "complete": False,
            "observations": {}, "lastSeenCycle": 0, "lastResult": {"componentStatus": statuses}}


def timeout(bid):
    return {"battleID": bid, "exported": False, "complete": False, "error": "not_found " + str(bid),
            "componentStatus": {name: {"available": False, "complete": False,
                **({"requested": True, "dispatched": True, "sendPacketCount": 1, "error": "timeout"}
                   if name != "basic" else {})} for name in ("basic", "detail", "all", "statics", "snapshots")}}


def captured(root, bid=123, *, complete=True):
    data = {"battleID": bid, "eventDataList": [{"eventId": 0, "intList": [0], "tab": 0}]}
    components = {name: copy.deepcopy(data) for name in ("basic", "detail", "all", "statics")}
    components["snapshots"] = {"isComplete": complete, "battleSnapData": {"zero": 0}} if complete else {
        "isComplete": False, "receivedCount": 1, "pages": [{"maxIDX": 0, "snapDataList": [{"flag": False}]}]}
    payload = {"captureVersion": 2, "battleID": bid, "source": "all", "data": data,
               "components": components, "captureComplete": complete, "eventCount": 1,
               "componentStatus": {name: {"available": True, "complete": complete or name != "snapshots"}
                                   for name in components}, "serializationIssues": []}
    path = root / (f"complete_{bid}.json" if complete else f"checkpoint_{bid}.json")
    module.atomic_json(path, payload)
    return {"battleID": bid, "path": str(path), "exported": True, "complete": complete,
            "eventCount": 1, "componentStatus": payload["componentStatus"]}


def test_budget_defers_only_old_retries_without_shortening_an_inflight_attempt(tmp_path):
    clock, events, calls = Clock(), [], []
    collector = BattleCollector(None, tmp_path, wait_seconds=20, on_progress=events.append)
    collector.state["reports"] = {str(bid): old_entry(bid) for bid in (1, 2, 3)}
    before_retry = 9999
    collector.state["reports"]["2"]["nextRetryAt"] = before_retry
    def capture(_runner, bid, _folder, wait, poll, **kwargs):
        assert wait == 20 and poll == .5
        calls.append(bid)
        clock.advance(80)  # Four full component budgets, not a truncated 20s report.
        return timeout(bid)
    with patch.object(module.time, "monotonic", clock.monotonic), patch.object(module.time, "time", clock.time), \
            patch.object(module, "capture_report", side_effect=capture):
        counts = collector.collect_pending(retry_budget_seconds=20)
    assert calls == [1] and clock.elapsed == 80
    assert counts == {"exported": 0, "complete": 0, "partial": 0, "failed": 1, "skipped": 2}
    assert collector.state["reports"]["1"]["attempts"] == 5
    assert collector.state["reports"]["2"]["attempts"] == 4
    assert collector.state["reports"]["2"]["nextRetryAt"] == before_retry
    assert all(not entry["complete"] for entry in collector.state["reports"].values())
    deferred = [event for event in events if event["phase"] == "retry_deferred"]
    assert {event["battleID"] for event in deferred} == {"2", "3"}
    assert all(event["reason"] == "old_retry_start_budget_exhausted" for event in deferred)


def test_new_report_still_runs_after_old_retry_budget_is_used(tmp_path):
    clock, calls = Clock(), []
    collector = BattleCollector(None, tmp_path)
    collector.state["reports"] = {"1": old_entry(1, missing_snapshot=True),
                                  "2": old_entry(2, missing_snapshot=True), "3": old_entry(3, attempts=0)}
    def capture(_runner, bid, *_args, **_kwargs):
        calls.append(bid)
        clock.advance(80 if bid == 1 else 1)
        return timeout(bid) if bid == 1 else captured(tmp_path, bid)
    # New reports normally sort first. Force the reverse order here to verify
    # their first attempt is exempt even when an older attempt used the budget.
    with patch.object(collector, "_ordered_pending_entries", return_value=list(collector.state["reports"].values())), \
            patch.object(module.time, "monotonic", clock.monotonic), patch.object(module.time, "time", clock.time), \
            patch.object(module, "capture_report", side_effect=capture):
        counts = collector.collect_pending(retry_budget_seconds=20)
    assert calls == [1, 3]
    assert counts["complete"] == 1 and counts["failed"] == 1 and counts["skipped"] == 1
    assert collector.state["reports"]["2"]["attempts"] == 4
    assert collector.state["reports"]["3"]["complete"]


def test_recovery_metadata_and_new_reports_precede_fair_old_retry_rotation(tmp_path):
    collector = BattleCollector(None, tmp_path)
    reports = {str(bid): old_entry(bid) for bid in range(1, 9)}
    reports["1"].update(lastAttemptAt=100, lastSeenCycle=collector.state["cycles"])
    reports["1"]["lastResult"] = old_entry(1, missing_snapshot=True)["lastResult"]
    reports["2"].update(lastAttemptAt=90, lastSeenCycle=-1)
    reports["3"].update(attempts=3, lastAttemptAt=200)
    reports["4"]["attempts"] = 0
    reports["5"].update(attempts=10, metadataDirty=True, lastResult={"complete": True})
    reports["6"].update(attempts=10, pendingImport={"path": "saved.json", "result": {"complete": True}})
    reports["7"].update(attempts=10, pendingDiscoveryImport="discovery.json")
    reports["8"].update(lastAttemptAt=90)
    reports["8"]["lastResult"] = old_entry(8, missing_snapshot=True)["lastResult"]
    collector.state["reports"] = reports
    assert [row["battleID"] for row in collector._ordered_pending_entries()] == ["6", "7", "5", "4", "3", "8", "2", "1"]


def test_mixed_old_backlog_does_not_starve_a_report_missing_more_components(tmp_path):
    clock, calls = Clock(), []
    collector = BattleCollector(None, tmp_path, scopes=("personal",), wait_seconds=20)
    collector.state["reports"] = {str(bid): old_entry(bid, missing_snapshot=True) for bid in range(1, 240)}
    collector.state["reports"]["999"] = old_entry(999)
    caps = {"runtimeIdentity": {"avatarId": "1", "serverId": "1", "scenarioId": "1"},
            "segmentLength": 10, "capabilities": {}, "relationAll": 1000}
    def capture(_runner, bid, *_args, **_kwargs):
        calls.append(bid)
        clock.advance(20 if bid != 999 else 80)
        result = timeout(bid)
        if bid != 999:
            result["componentStatus"] = old_entry(bid, missing_snapshot=True)["lastResult"]["componentStatus"]
            result["componentStatus"]["snapshots"]["error"] = "timeout"
        return result
    def discover(scope, _caps):
        result = {"complete": True, "children": {}, "roots": {}}
        collector.state["scopes"][scope] = result
        return result
    with patch.object(module.time, "monotonic", clock.monotonic), patch.object(module.time, "time", clock.time), \
            patch.object(module, "run_checked", return_value=json.dumps({"payload": caps})), \
            patch.object(module, "capture_report", side_effect=capture), \
            patch.object(collector, "discover_scope", side_effect=discover), \
            patch.object(collector, "save"), patch.object(module, "atomic_json"):
        for _ in range(500):
            collector.run_cycle()
    attempts = Counter(calls)
    # All 240 reports get a turn before any old retry is repeated, despite the
    # original 239 one-component retries becoming eligible again during that turn.
    assert len(calls) > 240 and len(set(calls[:240])) == 240
    assert calls.index(999) == 239
    assert min(attempts.values()) >= 2 and max(attempts.values()) - min(attempts.values()) <= 1
    assert len(attempts) == 240 and not any(row["complete"] for row in collector.state["reports"].values())


def test_default_explicit_collection_keeps_unbounded_legacy_behavior(tmp_path):
    clock, calls = Clock(), []
    collector = BattleCollector(None, tmp_path)
    collector.state["reports"] = {str(bid): old_entry(bid) for bid in (1, 2, 3)}
    def capture(_runner, bid, *_args, **_kwargs):
        calls.append(bid); clock.advance(80); return timeout(bid)
    with patch.object(module.time, "monotonic", clock.monotonic), patch.object(module.time, "time", clock.time), \
            patch.object(module, "capture_report", side_effect=capture):
        assert collector.collect_pending()["failed"] == 3
    assert calls == [1, 2, 3] and clock.elapsed == 240


def test_cycles_discover_before_working_through_all_old_failures_and_rotate_fairly(tmp_path):
    clock, calls, discoveries = Clock(), [], []
    collector = BattleCollector(None, tmp_path, scopes=("personal",), wait_seconds=20)
    collector.state["reports"] = {str(bid): old_entry(bid) for bid in range(1, 7)}
    caps = {"runtimeIdentity": {"avatarId": "1", "serverId": "1", "scenarioId": "1"},
            "segmentLength": 10, "capabilities": {}, "relationAll": 1000}
    def checked(*_args, **_kwargs): return json.dumps({"payload": caps})
    def capture(_runner, bid, *_args, **_kwargs):
        calls.append(bid); clock.advance(80); return timeout(bid)
    def discover(scope, _caps):
        discoveries.append(clock.elapsed)
        result = {"complete": True, "children": {}, "roots": {}}
        collector.state["scopes"][scope] = result
        return result
    with patch.object(module.time, "monotonic", clock.monotonic), patch.object(module.time, "time", clock.time), \
            patch.object(module, "run_checked", side_effect=checked), \
            patch.object(module, "capture_report", side_effect=capture), patch.object(collector, "discover_scope", side_effect=discover):
        first = collector.run_cycle()
        assert discoveries == [0]  # No historical network attempt precedes the first list.
        assert len(calls) == 1 and first["deferredRetries"]["count"] == 5
        assert not first["complete"] and first["collection"]["failed"] == 1
        for _ in range(5):
            collector.run_cycle()
    assert calls == [1, 2, 3, 4, 5, 6]
    assert all(entry["attempts"] == 5 and not entry["complete"] for entry in collector.state["reports"].values())


def test_first_new_page_and_report_precede_old_network_work_without_losing_pending_import(tmp_path):
    clock, discoveries, calls = Clock(), [], []
    with ReportArchive(tmp_path / "archive.db") as archive:
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account", scopes=("personal",),
            wait_seconds=20, on_export=lambda path: archive.import_file(path, namespace="account"))
        partial = captured(tmp_path, 1, complete=False)
        collector.state["reports"] = {"1": old_entry(1, attempts=0), "2": old_entry(2)}
        collector.state["reports"]["1"].update(pendingImport={"path": partial["path"], "result": partial}, nextRetryAt=10**12)
        collector.save()
        caps = {"runtimeIdentity": {"avatarId": "1", "serverId": "1", "scenarioId": "1"},
                "segmentLength": 10, "capabilities": {}, "relationAll": 1000}
        def capture(_runner, bid, *_args, **_kwargs):
            calls.append(bid)
            clock.advance(1 if bid == 999 else 80)
            return captured(tmp_path, bid) if bid == 999 else timeout(bid)
        def discover(scope, _caps):
            discoveries.append(clock.elapsed)
            collector.state["reports"]["999"] = old_entry(999, attempts=0)
            collector._collect_batch(["999"], retry_budget_seconds=20)
            result = {"complete": True, "children": {}, "roots": {}}
            collector.state["scopes"][scope] = result
            return result
        with patch.object(module.time, "monotonic", clock.monotonic), patch.object(module.time, "time", clock.time), \
                patch.object(module, "run_checked", return_value=json.dumps({"payload": caps})), \
                patch.object(module, "capture_report", side_effect=capture), \
                patch.object(collector, "discover_scope", side_effect=discover):
            collector.run_cycle()
        assert discoveries == [0] and calls == [999, 2]
        restored = collector.state["reports"]["1"]
        assert "pendingImport" not in restored and restored["attempts"] == 0
        assert restored["nextRetryAt"] == 10**12 and not restored["complete"]
        assert archive.stats()["records"] == 2 and archive.verify()["ok"]


def test_pending_complete_import_and_metadata_refresh_bypass_zero_retry_budget(tmp_path):
    with ReportArchive(tmp_path / "archive.db") as archive:
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account", on_export=archive.import_file)
        first = captured(tmp_path, 1)
        second = captured(tmp_path, 2)
        collector.state["reports"] = {"1": old_entry(1), "2": old_entry(2)}
        collector.state["reports"]["1"].update(pendingImport={"path": first["path"], "result": first}, nextRetryAt=10**12)
        collector.state["reports"]["2"].update(metadataDirty=True, lastResult=second)
        with patch.object(module, "capture_report") as capture:
            counts = collector.collect_pending(retry_budget_seconds=0)
        assert capture.call_count == 0
        assert counts["complete"] == 2 and counts["exported"] == 2
        assert all(entry["complete"] for entry in collector.state["reports"].values())
        assert archive.stats()["records"] == 2
        assert archive.verify()["ok"]


@pytest.mark.parametrize("future_backoff", [False, True])
def test_complete_pending_recovery_immediately_refreshes_dirty_metadata_without_network(tmp_path, future_backoff):
    complete = captured(tmp_path, 1)
    with ReportArchive(tmp_path / "archive.db") as archive:
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account",
            on_export=lambda path: archive.import_file(path, namespace="account"))
        entry = old_entry(1)
        entry.update(lastResult=timeout(1), metadataDirty=True,
                     pendingImport={"path": complete["path"], "result": complete})
        if future_backoff:
            entry["nextRetryAt"] = 10**12
        collector.state["reports"]["1"] = entry
        collector.save()
        with patch.object(module, "capture_report") as capture:
            counts = collector.collect_pending(retry_budget_seconds=0)
        disk = json.loads(collector.state_file.read_bytes())["reports"]["1"]
        assert not capture.called and counts["complete"] == counts["exported"] == 1 and counts["skipped"] == 0
        assert disk["complete"] is True and "metadataDirty" not in disk and "pendingImport" not in disk
        assert "nextRetryAt" not in disk
        assert archive.stats()["records"] == 1 and archive.list_reports()[0]["completeness"] == "complete"
        assert archive.verify()["ok"]


def test_partial_pending_recovery_is_checkpointed_when_zero_budget_defers_old_network(tmp_path):
    partial = captured(tmp_path, 1, complete=False)
    with ReportArchive(tmp_path / "archive.db") as archive:
        imports = []
        def ingest(path):
            imports.append(str(path))
            return archive.import_file(path, namespace="account")
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account", on_export=ingest)
        entry = old_entry(1)
        entry.update(lastResult=timeout(1), pendingImport={"path": partial["path"], "result": partial})
        collector.state["reports"]["1"] = entry
        collector.save()
        with patch.object(module, "capture_report") as capture:
            counts = collector.collect_pending(retry_budget_seconds=0)
        assert not capture.called and counts["skipped"] == 1
        disk = json.loads(collector.state_file.read_bytes())["reports"]["1"]
        assert "pendingImport" not in disk and disk["lastResult"] == partial and not disk["complete"]
        assert disk["attempts"] == 4
        resumed = BattleCollector(None, collector.out_dir, namespace="account", on_export=ingest)
        with patch.object(module, "capture_report") as capture:
            assert resumed.collect_pending(retry_budget_seconds=0)["skipped"] == 1
        assert not capture.called and len(imports) == 1
        assert archive.stats()["records"] == archive.stats()["snapshots"] == 1
        assert archive.verify()["ok"]


@pytest.mark.parametrize("bad", [True, -1, float("inf"), float("nan"), "20"])
def test_bad_retry_budget_rejected_without_game_access(tmp_path, bad):
    collector = BattleCollector(None, tmp_path)
    with patch.object(module, "capture_report") as capture:
        with pytest.raises(ValueError, match="retry_budget_seconds"):
            collector.collect_pending(retry_budget_seconds=bad)
    assert capture.call_count == 0


def test_failed_final_export_imports_pre_request_pages_without_losing_best_or_duplicates(tmp_path):
    with ReportArchive(tmp_path / "archive.db") as archive:
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account",
            on_export=lambda path: archive.import_file(path, namespace="account"))
        entry = old_entry(123)
        collector.state["reports"]["123"] = entry
        full = captured(tmp_path)
        collector._store_captured_result(entry, full, collector._empty_collection_counts())
        complete_sid = archive.list_reports()[0]["current_snapshot_id"]
        original_full = archive.original_bytes(complete_sid)
        checkpoint = captured(tmp_path, complete=False)
        checkpoint_bytes = Path(checkpoint["path"]).read_bytes()
        failed = {**timeout(123), "cacheCheckpointPaths": [checkpoint["path"], checkpoint["path"]]}
        for _ in range(2):
            counts = collector._empty_collection_counts()
            collector._store_captured_result(entry, copy.deepcopy(failed), counts)
            assert counts == {"exported": 0, "complete": 0, "partial": 0, "failed": 1, "skipped": 0}
        assert not entry["complete"] and entry["lastResult"]["exported"] is False
        assert Path(checkpoint["path"]).read_bytes() == checkpoint_bytes
        assert archive.stats()["records"] == 1 and archive.stats()["snapshots"] == 2
        assert archive.list_reports()[0]["current_snapshot_id"] == complete_sid
        assert archive.original_bytes(complete_sid) == original_full
        assert archive.connection.execute("SELECT count(*) FROM report_fields WHERE pointer='/components/snapshots/pages/0/snapDataList/0/flag' AND value_json='false'").fetchone()[0] == 1
        assert archive.verify()["ok"]


def test_checkpoint_only_import_failure_replays_after_restart_without_rpc_or_false_success(tmp_path):
    with ReportArchive(tmp_path / "archive.db") as archive:
        checkpoint = captured(tmp_path, complete=False)
        failed = {**timeout(123), "cacheCheckpointPaths": [checkpoint["path"]]}
        def commit_then_lose_ack(path):
            archive.import_file(path, namespace="account")
            raise OSError("database committed before acknowledgement was interrupted")
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account", on_export=commit_then_lose_ack)
        entry = old_entry(123)
        entry["nextRetryAt"] = 10**12
        collector.state["reports"]["123"] = entry
        with pytest.raises(OSError):
            collector._store_captured_result(entry, failed, collector._empty_collection_counts())
        assert entry["pendingImport"]["path"] is None
        assert len(entry["pendingImport"]["checkpointPaths"]) == 1
        assert archive.stats()["snapshots"] == 1
        resumed = BattleCollector(None, collector.out_dir, namespace="account",
            on_export=lambda path: archive.import_file(path, namespace="account"))
        with patch.object(module, "capture_report") as capture:
            counts = resumed.collect_pending(retry_budget_seconds=0)
        assert capture.call_count == 0
        assert counts == {"exported": 0, "complete": 0, "partial": 0, "failed": 1, "skipped": 0}
        restored = resumed.state["reports"]["123"]
        assert "pendingImport" not in restored and not restored["complete"]
        assert restored["attempts"] == 4 and restored["nextRetryAt"] == 10**12
        assert archive.stats()["records"] == archive.stats()["snapshots"] == 1
        assert archive.verify()["ok"]


def three_checkpoint_result(root):
    paths = []
    for index in range(3):
        checkpoint = captured(root, complete=False)
        payload = json.loads(Path(checkpoint["path"]).read_bytes())
        payload["components"]["snapshots"]["pages"][0]["snapDataList"][0]["page"] = index
        path = root / f"received_page_{index}.json"
        module.atomic_json(path, payload)
        paths.append(str(path))
    return {**captured(root), "cacheCheckpointPaths": paths}


@pytest.mark.parametrize("interrupt_after_commit", [False, True])
@pytest.mark.parametrize("interrupt_import", [1, 2, 3, 4])
def test_batched_checkpoint_acknowledgements_survive_each_import_crash_without_duplicate_data(
        tmp_path, interrupt_import, interrupt_after_commit):
    result = three_checkpoint_result(tmp_path)
    original_pages = {path: Path(path).read_bytes() for path in result["cacheCheckpointPaths"]}
    with ReportArchive(tmp_path / "archive.db") as archive:
        imports = 0
        def ingest_then_interrupt(path):
            nonlocal imports
            imports += 1
            if imports == interrupt_import and not interrupt_after_commit:
                raise SystemExit("process interrupted before this database commit")
            answer = archive.import_file(path, namespace="account")
            assert answer["status"] == "imported"
            if imports == interrupt_import:
                raise SystemExit("database committed but the process lost its acknowledgement")
            return answer
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account", on_export=ingest_then_interrupt)
        collector.state["reports"]["123"] = old_entry(123)
        with patch.object(module, "capture_report", return_value=copy.deepcopy(result)):
            with pytest.raises(SystemExit):
                collector.collect_pending()
        pending = json.loads(collector.state_file.read_bytes())["reports"]["123"]["pendingImport"]
        # A single durable list covers every page, including already committed
        # pages: unacknowledged originals can never disappear between saves.
        assert len(pending["checkpointPaths"]) == 3 and pending["path"]
        committed = interrupt_import if interrupt_after_commit else interrupt_import - 1
        assert archive.stats()["snapshots"] == committed
        resumed = BattleCollector(None, collector.out_dir, namespace="account",
            on_export=lambda path: archive.import_file(path, namespace="account"))
        with patch.object(module, "capture_report") as capture:
            counts = resumed.collect_pending(retry_budget_seconds=0)
        assert capture.call_count == 0 and counts["complete"] == counts["exported"] == 1
        assert resumed.state["reports"]["123"]["attempts"] == 5
        assert "pendingImport" not in resumed.state["reports"]["123"]
        assert archive.stats()["records"] == 1 and archive.stats()["snapshots"] == 4
        assert archive.list_reports()[0]["completeness"] == "complete"
        for path, raw in original_pages.items():
            assert Path(path).read_bytes() == raw
        assert archive.verify()["ok"]


def test_final_state_checkpoint_failure_replays_all_committed_pages_once(tmp_path):
    result = three_checkpoint_result(tmp_path)
    with ReportArchive(tmp_path / "archive.db") as archive:
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account",
            on_export=lambda path: archive.import_file(path, namespace="account"))
        collector.state["reports"]["123"] = old_entry(123)
        saves, save = 0, collector.save
        def interrupt_final_save():
            nonlocal saves
            saves += 1
            if saves == 3:
                raise OSError("final per-battle state checkpoint interrupted")
            save()
        with patch.object(collector, "save", side_effect=interrupt_final_save), \
                patch.object(module, "capture_report", return_value=copy.deepcopy(result)):
            with pytest.raises(OSError, match="final per-battle"):
                collector.collect_pending()
        assert saves == 3  # Attempt, durable full import list, final result; no per-page state rewrite.
        assert archive.stats()["snapshots"] == 4
        resumed = BattleCollector(None, collector.out_dir, namespace="account",
            on_export=lambda path: archive.import_file(path, namespace="account"))
        with patch.object(module, "capture_report") as capture:
            counts = resumed.collect_pending(retry_budget_seconds=0)
        assert not capture.called and counts["complete"] == 1
        assert "pendingImport" not in resumed.state["reports"]["123"]
        assert archive.stats()["records"] == 1 and archive.stats()["snapshots"] == 4
        assert archive.verify()["ok"]


def test_four_new_reports_do_not_spend_the_fifth_reports_old_retry_budget(tmp_path):
    clock, calls = Clock(), []
    collector = BattleCollector(None, tmp_path, wait_seconds=20)
    collector.state["reports"] = {str(bid): old_entry(bid, attempts=0 if bid < 5 else 1) for bid in range(1, 6)}
    def capture(_runner, bid, *_args, **_kwargs):
        calls.append((bid, clock.elapsed))
        clock.advance(7.75 if bid < 5 else 6)
        return captured(tmp_path, bid)
    with patch.object(module.time, "monotonic", clock.monotonic), patch.object(module.time, "time", clock.time), \
            patch.object(module, "capture_report", side_effect=capture):
        counts = collector.collect_pending(retry_budget_seconds=20)
    assert calls == [(1, 0), (2, 7.75), (3, 15.5), (4, 23.25), (5, 31)]
    assert counts["complete"] == counts["exported"] == 5 and counts["skipped"] == 0
    assert clock.elapsed == 37 and all(row["complete"] for row in collector.state["reports"].values())


def test_local_recovery_metadata_and_new_report_do_not_use_old_network_allowance(tmp_path):
    clock, calls = Clock(), []
    partial, metadata = captured(tmp_path, 1, complete=False), captured(tmp_path, 2)
    collector = BattleCollector(None, tmp_path / "runtime", on_export=lambda _path: clock.advance(50))
    collector.state["reports"] = {str(bid): old_entry(bid) for bid in range(1, 6)}
    collector.state["reports"]["1"].update(pendingImport={"path": partial["path"], "result": partial}, nextRetryAt=10**12)
    collector.state["reports"]["2"].update(metadataDirty=True, lastResult=metadata)
    collector.state["reports"]["3"]["attempts"] = 0
    def capture(_runner, bid, *_args, **_kwargs):
        calls.append(bid)
        clock.advance(31 if bid == 3 else 2)
        return captured(tmp_path, bid)
    with patch.object(module.time, "monotonic", clock.monotonic), patch.object(module.time, "time", clock.time), \
            patch.object(module, "capture_report", side_effect=capture):
        counts = collector.collect_pending(retry_budget_seconds=1)
    assert calls == [3, 4]  # The first old report still gets its own full turn.
    assert counts["skipped"] == 1 and collector.state["reports"]["5"]["attempts"] == 4
    assert "pendingImport" not in collector.state["reports"]["1"]


def prefetch_metadata(ids):
    return {str(bid): {name: {"battleID": int(bid), "component": name, "requested": True,
                              "deadline": 1000.0}
                       for name in ("all", "statics")} for bid in ids}


def test_zero_retry_budget_never_prefetches_old_reports_alongside_new_reports(tmp_path):
    collector = BattleCollector(None, tmp_path, parallelism=4)
    collector.state["reports"] = {str(bid): old_entry(bid, attempts=0 if bid < 3 else 4) for bid in (1, 2, 3)}
    groups, calls = [], []
    def prefetch(_runner, ids, **_kwargs):
        groups.append(ids)
        return prefetch_metadata(ids)
    def capture(_runner, bid, *_args, **_kwargs):
        calls.append(bid)
        return captured(tmp_path, bid)
    with patch.object(module, "prefetch_reports", side_effect=prefetch), \
            patch.object(module, "capture_report", side_effect=capture):
        counts = collector.collect_pending(retry_budget_seconds=0)
    assert groups == [["1", "2"]] and calls == [1, 2]
    assert counts["complete"] == 2 and counts["skipped"] == 1
    assert collector.state["reports"]["3"]["attempts"] == 4


@pytest.mark.parametrize("stop_after_first", [False, True])
def test_expired_retry_budget_finishes_or_harvests_all_prefetched_old_reports(tmp_path, stop_after_first):
    import threading
    clock, groups, calls = Clock(), [], []
    collector = BattleCollector(None, tmp_path, parallelism=2)
    collector.state["reports"] = {str(bid): old_entry(bid) for bid in (1, 2, 3)}
    stop = threading.Event()
    collector._stop_event = stop
    def prefetch(_runner, ids, **_kwargs):
        groups.append(ids)
        clock.advance(30)  # Budget expires only after both requests are in flight.
        return prefetch_metadata(ids)
    def capture(_runner, bid, *_args, **kwargs):
        calls.append((bid, kwargs["request_missing"]))
        clock.advance(1)
        if stop_after_first and bid == 1:
            stop.set()
        return captured(tmp_path, bid, complete=kwargs["request_missing"])
    with patch.object(module.time, "monotonic", clock.monotonic), patch.object(module.time, "time", clock.time), \
            patch.object(module, "prefetch_reports", side_effect=prefetch), \
            patch.object(module, "capture_report", side_effect=capture):
        counts = collector.collect_pending(retry_budget_seconds=20)
    assert groups == [["1", "2"]] and calls == [(1, True), (2, not stop_after_first)]
    assert counts["exported"] == 2 and collector.state["reports"]["3"]["attempts"] == 4
    assert collector.state["reports"]["2"]["attempts"] == 5
