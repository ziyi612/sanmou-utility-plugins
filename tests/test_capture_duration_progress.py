"""Last-report duration describes one formal capture, through durable ingestion."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import runpy
from unittest.mock import patch

import pytest

from capture.game_assets import battle_collector as module
from capture.game_assets.battle_collector import BattleCollector
from capture.game_assets.runtime_errors import RuntimeBridgeUnavailable
from parser.report_archive import ReportArchive


BASE = runpy.run_path(str(Path(__file__).with_name("test_timeout_retry_fairness.py")))
Clock, captured, old_entry, timeout = BASE["Clock"], BASE["captured"], BASE["old_entry"], BASE["timeout"]


@pytest.fixture
def clock():
    value = Clock()
    with patch.object(module.time, "monotonic", side_effect=value.monotonic), \
            patch.object(module.time, "time", side_effect=value.time):
        yield value


def timed_saves(collector, clock):
    save = collector.save
    def measured():
        save()
        clock.advance(.25)
    collector.save = measured


def finished(events):
    return [event for event in events if event["phase"] == "capture_finished"]


def assert_finished(event, battle_id, outcome, elapsed):
    assert str(event["battleID"]) == str(battle_id) and event["outcome"] == outcome
    assert event["elapsed_seconds"] == pytest.approx(elapsed)
    started_at, finished_at = datetime.fromisoformat(event["started_at"]), datetime.fromisoformat(event["finished_at"])
    assert started_at.tzinfo is not None and finished_at.tzinfo is not None and started_at <= finished_at


@pytest.mark.parametrize("complete", [True, False])
def test_duration_includes_ingestion_and_final_state_confirmation_without_changing_originals(tmp_path, clock, complete):
    result, events, states_at_finish = captured(tmp_path, complete=complete), [], []
    with ReportArchive(tmp_path / "archive.db") as archive:
        def ingest(path):
            answer = archive.import_file(path, namespace="account")
            clock.advance(3)
            return answer
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account", on_export=ingest)
        collector.state["reports"]["123"] = old_entry(123, attempts=0)
        timed_saves(collector, clock)
        def progress(event):
            events.append(event)
            if event["phase"] == "capture_finished":
                disk = json.loads(collector.state_file.read_bytes())["reports"]["123"]
                states_at_finish.append((disk, archive.stats()["records"]))
        collector.on_progress = progress
        def capture(*_args, **_kwargs):
            clock.advance(2)
            return result
        with patch.object(module, "capture_report", side_effect=capture):
            counts = collector.collect_pending()
        assert len(finished(events)) == 1
        assert len(states_at_finish) == 1
        disk, records = states_at_finish[0]
        assert disk["complete"] is complete and "pendingImport" not in disk and records == 1
        assert_finished(finished(events)[0], 123, "complete" if complete else "partial", 5.5)
        assert counts["exported"] == 1
        payload = json.loads(archive.original_bytes(archive.list_reports()[0]["current_snapshot_id"]))
        assert not {"elapsed_seconds", "started_at", "finished_at", "captureDuration"} & payload.keys()
        before = archive.stats()["snapshots"]
        assert archive.import_file(result["archivePath"], namespace="account")["status"] == "duplicate"
        assert archive.stats()["snapshots"] == before and archive.verify()["ok"]


@pytest.mark.parametrize("failure_kind", ["exception", "not_found", "database_rejected"])
def test_ordinary_failures_finish_once_and_retain_their_reason(tmp_path, clock, failure_kind):
    events = []
    collector = BattleCollector(None, tmp_path / "runtime", on_progress=events.append,
        on_export=(lambda _path: {"status": "failed", "error": "database rejected"}) if failure_kind == "database_rejected" else None)
    collector.state["reports"]["123"] = old_entry(123, attempts=0)
    timed_saves(collector, clock)
    def capture(*_args, **_kwargs):
        clock.advance(4)
        if failure_kind == "exception":
            raise ValueError("component decoder failed")
        return timeout(123) if failure_kind == "not_found" else captured(tmp_path)
    with patch.object(module, "capture_report", side_effect=capture):
        counts = collector.collect_pending()
    assert counts["failed"] == 1 and len(finished(events)) == 1
    expected = 4.5 if failure_kind == "database_rejected" else 4.25
    event = finished(events)[0]
    assert_finished(event, 123, "failed", expected)
    assert event["error"] and event["error_type"] == ("ValueError" if failure_kind == "exception" else "RuntimeError")


def test_fatal_bridge_failure_finishes_failed_and_preserves_original_circuit_break(tmp_path, clock):
    events = []
    collector = BattleCollector(None, tmp_path, on_progress=events.append)
    collector.state["reports"] = {str(bid): old_entry(bid, attempts=0) for bid in (1, 2)}
    timed_saves(collector, clock)
    failure = RuntimeBridgeUnavailable("native bridge unavailable")
    failure.result = {"nativeOperation": "lua_gettop", "rawL": "0x123"}
    def capture(*_args, **_kwargs):
        clock.advance(4)
        raise failure
    with patch.object(module, "capture_report", side_effect=capture) as capture_call:
        with pytest.raises(RuntimeBridgeUnavailable) as caught:
            collector.collect_pending()
    assert caught.value is failure and capture_call.call_count == 1
    assert failure.result == {"nativeOperation": "lua_gettop", "rawL": "0x123"}
    assert collector.state["reports"]["2"]["attempts"] == 0
    assert len(finished(events)) == 1
    assert_finished(finished(events)[0], 1, "failed", 4.25)
    assert finished(events)[0]["error_type"] == "RuntimeBridgeUnavailable"


def test_final_state_failure_finishes_failed_without_replacing_the_exception(tmp_path, clock):
    result, events = captured(tmp_path), []
    with ReportArchive(tmp_path / "archive.db") as archive:
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account", on_progress=events.append,
            on_export=lambda path: archive.import_file(path, namespace="account"))
        collector.state["reports"]["123"] = old_entry(123, attempts=0)
        save, saves = collector.save, 0
        failure = OSError("final state checkpoint locked")
        def save_then_fail():
            nonlocal saves
            saves += 1
            clock.advance(.25)
            if saves == 3:
                raise failure
            save()
        collector.save = save_then_fail
        def capture(*_args, **_kwargs):
            clock.advance(2)
            return result
        with patch.object(module, "capture_report", side_effect=capture):
            with pytest.raises(OSError) as caught:
                collector.collect_pending()
        assert caught.value is failure and archive.stats()["records"] == 1
        assert len(finished(events)) == 1
        assert_finished(finished(events)[0], 123, "failed", 2.5)
        assert finished(events)[0]["error_type"] == "OSError"
        assert json.loads(collector.state_file.read_bytes())["reports"]["123"].get("pendingImport")


def test_intermediate_checkpoints_do_not_end_the_duration_before_final_import(tmp_path, clock):
    result = BASE["three_checkpoint_result"](tmp_path)
    originals = {path: Path(path).read_bytes() for path in result["cacheCheckpointPaths"]}
    events = []
    with ReportArchive(tmp_path / "archive.db") as archive:
        def ingest(path):
            answer = archive.import_file(path, namespace="account")
            clock.advance(1)
            assert not finished(events)
            return answer
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account", on_export=ingest, on_progress=events.append)
        collector.state["reports"]["123"] = old_entry(123, attempts=0)
        timed_saves(collector, clock)
        def capture(*_args, **_kwargs):
            clock.advance(2)
            return result
        with patch.object(module, "capture_report", side_effect=capture):
            collector.collect_pending()
        assert len([event for event in events if event["phase"] == "cache_checkpoint_imported"]) == 3
        assert len(finished(events)) == 1 and events[-1]["phase"] == "capture_finished"
        assert_finished(finished(events)[0], 123, "complete", 6.5)
        assert archive.stats()["records"] == 1 and archive.stats()["snapshots"] == 4
        assert all(Path(path).read_bytes() == raw for path, raw in originals.items())


def test_local_pending_metadata_complete_and_backoff_skips_do_not_emit_capture_duration(tmp_path, clock):
    first, second = captured(tmp_path, 1), captured(tmp_path, 2)
    events = []
    with ReportArchive(tmp_path / "archive.db") as archive:
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account", on_progress=events.append,
            on_export=lambda path: archive.import_file(path, namespace="account"))
        collector.state["reports"] = {str(bid): old_entry(bid) for bid in range(1, 5)}
        collector.state["reports"]["1"]["pendingImport"] = {"path": first["path"], "result": first}
        collector.state["reports"]["2"].update(metadataDirty=True, lastResult=second)
        collector.state["reports"]["3"]["complete"] = True
        collector.state["reports"]["4"]["nextRetryAt"] = 10**12
        with patch.object(module, "capture_report") as capture_call:
            counts = collector.collect_pending(retry_budget_seconds=0)
        assert not capture_call.called and counts["complete"] == 2 and counts["skipped"] == 2
        assert not [event for event in events if event["phase"] in ("capture_started", "capture_finished")]


def test_finished_notification_failure_cannot_invalidate_a_committed_capture(tmp_path, clock):
    events, result = [], captured(tmp_path)
    def progress(event):
        events.append(event)
        if event["phase"] == "capture_finished":
            raise OSError("optional status renderer unavailable")
    with ReportArchive(tmp_path / "archive.db") as archive:
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account", on_progress=progress,
            on_export=lambda path: archive.import_file(path, namespace="account"))
        collector.state["reports"]["123"] = old_entry(123, attempts=0)
        with patch.object(module, "capture_report", return_value=result):
            assert collector.collect_pending()["complete"] == 1
            assert collector.collect_pending()["skipped"] == 1
        assert len(finished(events)) == 1 and collector.state["reports"]["123"]["complete"]
        assert archive.stats()["records"] == archive.stats()["snapshots"] == 1


def test_parallel_prefetch_bridge_failure_finishes_only_the_formally_started_attempt(tmp_path, clock):
    events = []
    collector = BattleCollector(None, tmp_path, parallelism=2, on_progress=events.append)
    collector.state["reports"] = {str(bid): old_entry(bid, attempts=0) for bid in (1, 2)}
    failure = RuntimeBridgeUnavailable("prefetch bridge failed")
    def prefetch(*_args, **_kwargs):
        clock.advance(3)
        raise failure
    with patch.object(module, "prefetch_reports", side_effect=prefetch), \
            patch.object(module, "capture_report", side_effect=failure):
        with pytest.raises(RuntimeBridgeUnavailable) as caught:
            collector.collect_pending()
    assert caught.value is failure
    assert [str(event["battleID"]) for event in events if event["phase"] == "capture_started"] == ["1"]
    assert len(finished(events)) == 1
    assert_finished(finished(events)[0], 1, "failed", 3)
    assert finished(events)[0]["error_type"] == "RuntimeBridgeUnavailable"


def test_consecutive_attempts_report_separate_durations_in_completion_order(tmp_path, clock):
    events = []
    collector = BattleCollector(None, tmp_path, on_progress=events.append)
    collector.state["reports"] = {str(bid): old_entry(bid, attempts=0) for bid in (1, 2)}
    def capture(_runner, bid, *_args, **_kwargs):
        clock.advance(2 if bid == 1 else 5)
        return captured(tmp_path, bid)
    with patch.object(module, "capture_report", side_effect=capture):
        assert collector.collect_pending()["complete"] == 2
    assert [event["phase"] for event in events] == ["capture_started", "capture_finished", "capture_started", "capture_finished"]
    assert_finished(finished(events)[0], 1, "complete", 2)
    assert_finished(finished(events)[1], 2, "complete", 5)
