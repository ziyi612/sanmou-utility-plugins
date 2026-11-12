"""Log recovery is scoped, conservative about partial carriers, and bounded."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

import pytest

from battle_ui import capture_timing as timing


EPOCH = datetime(2026, 11, 9, tzinfo=timezone.utc)


def instant(seconds):
    return (EPOCH + timedelta(seconds=seconds)).isoformat()


def event(phase, seconds, battle="A", **values):
    return {"phase": phase, "at": instant(seconds), "battleID": battle,
            "pid": 17, "namespace": "account", "cycle": 1, **values}


def save(path, *rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def read(path, reader=None, **scope):
    return (reader or timing.LastCaptureTimingReader()).read(path, namespace="account", pid=17,
        process_created_at=EPOCH.timestamp(), **scope)


def test_complete_import_finishes_and_next_in_progress_carrier_does_not_replace_it(tmp_path):
    path = tmp_path / "diagnostic.jsonl"
    rows = [event("capture_started", 0), event("capture_imported", 2, completeness="partial")]
    save(path, *rows)
    reader = timing.LastCaptureTimingReader()
    assert read(path, reader) is None
    rows += [event("capture_imported", 5, completeness="complete"),
             event("capture_started", 6, "B"), event("capture_imported", 7, "B", completeness="partial")]
    save(path, *rows)
    result = read(path, reader)
    assert result["battleID"] == "A" and result["elapsed_seconds"] == 5
    assert result["finished_at"] == instant(5) and result["outcome"] == "complete"
    assert result["source"] == "recovered_log" and result["approximate"] is True


def test_failed_capture_is_a_finished_attempt_with_its_own_error(tmp_path):
    path = tmp_path / "diagnostic.jsonl"
    save(path, event("capture_started", 3), event("capture_failed", 3.25, error="bridge unavailable"))
    result = read(path)
    assert result["elapsed_seconds"] == .25 and result["outcome"] == "failed"
    assert result["error"] == "bridge unavailable" and result["complete"] is False


@pytest.mark.parametrize("boundary", ["capture_started", "page_received"])
def test_partial_needs_a_later_sequential_boundary_to_confirm_end(tmp_path, boundary):
    path = tmp_path / "diagnostic.jsonl"
    rows = [event("capture_started", 0), event("capture_imported", 2, completeness="partial"),
            event("capture_imported", 4, completeness="partial")]
    save(path, *rows)
    assert read(path) is None
    save(path, *rows, event(boundary, 8, "B"))
    result = read(path)
    assert result["outcome"] == "partial" and result["elapsed_seconds"] == 4
    assert result["finished_at"] == instant(4) and result["approximate"]


@pytest.mark.parametrize("foreign", [{"pid": 18}, {"namespace": "other"}, {"cycle": 2},
                                      {"cycle": None}, {"cycle": True}, {"battleID": "B"}])
def test_endpoints_do_not_cross_process_namespace_cycle_or_battle(tmp_path, foreign):
    path = tmp_path / "diagnostic.jsonl"
    save(path, event("capture_started", 0), event("capture_imported", 5, completeness="complete", **foreign))
    assert read(path) is None


def test_process_creation_excludes_reused_pid_history_and_old_start(tmp_path):
    path = tmp_path / "diagnostic.jsonl"
    save(path, event("capture_started", -5), event("capture_imported", -1, completeness="complete"),
         event("capture_started", -1), event("capture_imported", 1, completeness="complete"))
    assert read(path) is None
    save(path, event("capture_started", 2), event("capture_imported", 5, completeness="complete"))
    assert read(path)["elapsed_seconds"] == 3


def test_formal_event_uses_outcome_and_measured_elapsed_even_without_a_start_row(tmp_path):
    path = tmp_path / "diagnostic.jsonl"
    save(path, event("capture_finished", 8, outcome="partial", complete=True,
                     elapsed_seconds=6.5, finished_at=instant(8)))
    result = read(path)
    assert result["outcome"] == "partial" and result["complete"] is False
    assert result["elapsed_seconds"] == 6.5 and result["source"] == "collector"
    assert result["approximate"] is False and result["started_at"] == instant(1.5)
    save(path, event("capture_finished", 8, outcome="complete", elapsed_seconds=9, finished_at=instant(8)))
    assert read(path) is None  # Derived start predates the current collector.


@pytest.mark.parametrize("duration", [-1, float("nan"), float("inf"), True, "3", None, 10**400])
def test_invalid_formal_duration_never_appears_as_a_timing(tmp_path, duration):
    path = tmp_path / "diagnostic.jsonl"
    save(path, event("capture_finished", 8, outcome="complete", elapsed_seconds=duration,
                     started_at=instant(1), finished_at=instant(8)))
    assert read(path) is None


def test_incomplete_line_and_malformed_record_cannot_pair_false_endpoints(tmp_path):
    path = tmp_path / "diagnostic.jsonl"
    save(path, event("capture_started", 0))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event("capture_imported", 4, completeness="complete")))
    reader = timing.LastCaptureTimingReader()
    assert read(path, reader) is None
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert read(path, reader)["elapsed_seconds"] == 4
    path.write_text(json.dumps(event("capture_started", 0)) + "\ninvalid json\n" +
                    json.dumps(event("capture_imported", 4, completeness="complete")) + "\n", encoding="utf-8")
    assert read(path, reader) is None


def test_tail_is_bounded_unchanged_files_are_cached_and_truncation_is_seen(tmp_path):
    path = tmp_path / "diagnostic.jsonl"
    prefix = json.dumps({"padding": "x" * 12000}) + "\n"
    path.write_text(prefix + json.dumps(event("capture_started", 0)) + "\n" +
                    json.dumps(event("capture_imported", 5, completeness="complete")) + "\n", encoding="utf-8")
    reader = timing.LastCaptureTimingReader(max_tail_bytes=512)
    with patch.object(timing, "_recover", wraps=timing._recover) as recover:
        result = read(path, reader)
        assert result["elapsed_seconds"] == 5 and len(recover.call_args.args[0]) <= 512
        result["elapsed_seconds"] = 999
        assert read(path, reader)["elapsed_seconds"] == 5 and recover.call_count == 1
        path.write_text("", encoding="utf-8")
        assert read(path, reader) is None and recover.call_count == 2
    # A start outside the bounded tail is unknown, even with a completion row.
    save(path, event("capture_started", 0), {"padding": "x" * 12000},
         event("capture_imported", 5, completeness="complete"))
    assert read(path, reader) is None


def test_missing_file_and_invalid_scope_are_unknown_and_cache_has_a_bound(tmp_path):
    path = tmp_path / "diagnostic.jsonl"
    reader = timing.LastCaptureTimingReader(cache_entries=2)
    assert read(path, reader) is None
    save(path, event("capture_started", 0), event("capture_imported", 5, completeness="complete"))
    assert reader.read(path, namespace="account", pid=True) is None
    assert reader.read(path, namespace="account", pid=17, process_created_at=float("nan")) is None
    for pid in (17, 18, 19):
        reader.read(path, namespace="account", pid=pid)
    assert len(reader._cache) == 2


@pytest.mark.parametrize("finish", ["not-a-date", "2026-11-09T00:00:05", instant(-1)])
def test_invalid_or_backwards_wall_clock_is_not_a_duration(tmp_path, finish):
    path = tmp_path / "diagnostic.jsonl"
    save(path, event("capture_started", 0), event("capture_imported", 5, at=finish, completeness="complete"))
    assert read(path) is None
