"""Time windows gate body requests, including resumed and prefetched work."""
from __future__ import annotations

import json
from pathlib import Path
import runpy
from unittest.mock import patch

import pytest

from capture.game_assets import battle_collector as module
from capture.game_assets import export_battle_report_runtime as exporter
from capture.game_assets.collection_window import (
    DEFAULT_TIME_RANGE, TIME_RANGE_HOURS, battle_timestamp, validate_time_range,
)
from capture.game_assets.export_battle_report_runtime import atomic_json


NOW = 1_800_000_000.0


@pytest.fixture
def root(tmp_path):
    return tmp_path


def collector(root, **kwargs):
    return module.BattleCollector(None, root, time_range=kwargs.pop("time_range", "24h"),
                                  request_delay=0, **kwargs)


def observe(service, bid, end=None, *, start=None, parent=None, kind=71):
    combat = {"battleID": bid, "matchType": kind}
    if end is not None:
        combat["endTimeStampTime"] = end
    if start is not None:
        combat["timeStamp"] = start
    service.remember("personal", combat if parent is not None else {"combatInfo": combat},
                     {}, service.out_dir / "page.json", parent)
    return service.state["reports"][str(bid)]


def export(service, bid, complete=True):
    path = service.out_dir / f"battle_{bid}_decrypted.json"
    atomic_json(path, {"battleID": bid, "data": {"battleID": bid}, "source": "basic"})
    return {"battleID": bid, "exported": True, "complete": complete, "path": str(path)}


@pytest.mark.parametrize("time_range,hours", [("24h", 24), ("72h", 72), ("7d", 168)])
def test_exact_lower_boundary_is_included_but_older_and_future_are_excluded(root, time_range, hours):
    service = collector(root, time_range=time_range)
    threshold = NOW - hours * 3600
    entries = [observe(service, bid, end) for bid, end in
               ((101, threshold), (102, threshold - .001), (103, NOW), (104, NOW + 1))]
    with patch.object(module.time, "time", return_value=NOW):
        assert [service._network_selected(entry) for entry in entries] == [True, False, True, False]
        summary = service.filter_summary(entries)
    assert summary["timeRange"] == time_range
    assert summary["timeCutoff"] == threshold
    assert summary["timeExcludedReports"] == summary["excludedReports"] == 2


def test_end_time_has_priority_over_start_and_observation_time(root):
    service = collector(root)
    recent_end = observe(service, 101, NOW - 1, start=NOW - 3 * 86400)
    old_end = observe(service, 102, NOW - 3 * 86400, start=NOW - 1)
    start_only = observe(service, 103, start=NOW - 1)
    missing = observe(service, 104)
    missing.update(firstSeenAt=NOW, lastSeenAt=NOW)
    with patch.object(module.time, "time", return_value=NOW):
        assert [service._network_selected(entry) for entry in (recent_end, old_end, start_only, missing)] == [True, False, True, False]
    assert battle_timestamp(recent_end) == NOW - 1
    assert battle_timestamp(missing) is None


@pytest.mark.parametrize("bad", [False, True, 0, -1, "1794500000", float("nan"), float("inf"), NOW * 1000])
def test_invalid_end_is_unknown_without_start_and_can_fall_back_to_valid_start(root, bad):
    service = collector(root)
    missing = observe(service, 101)
    # Mutate only in memory: JSON originals correctly reject nonfinite numbers.
    next(iter(missing["observations"].values()))["row"]["combatInfo"]["endTimeStampTime"] = bad
    assert battle_timestamp(missing) is None
    next(iter(missing["observations"].values()))["row"]["combatInfo"]["timeStamp"] = NOW - 1
    assert battle_timestamp(missing) == NOW - 1


def test_conflicting_observed_end_times_are_unknown_and_all_mode_includes_them(root):
    service = collector(root)
    entry = observe(service, 101, NOW - 1)
    observe(service, 101, NOW - 3 * 86400, parent="900")
    assert battle_timestamp(entry) is None
    with patch.object(module.time, "time", return_value=NOW):
        assert not service._network_selected(entry)
        assert service.filter_summary([entry])["timeUnknownReports"] == 1
        service.time_range = "all"
        assert service._network_selected(entry)
        assert service.filter_summary([entry])["timeUnknownReports"] == 0


def test_children_use_their_own_timestamps_instead_of_the_parent(root):
    service = collector(root)
    observe(service, 101, NOW - 4 * 86400)
    observe(service, 102, NOW - 1, parent="101")
    observe(service, 103, NOW - 4 * 86400, parent="101")
    observe(service, 104, parent="101")
    with patch.object(module.time, "time", return_value=NOW), \
            patch.object(module, "capture_report", side_effect=lambda runner, bid, *a, **k: export(service, bid)) as capture:
        counts = service.collect_pending()
    assert [call.args[1] for call in capture.call_args_list] == [102]
    assert counts["skipped"] == 3


def test_resumed_old_queue_stays_pending_until_all_time_is_selected(root):
    service = collector(root)
    entry = observe(service, 101, NOW - 4 * 86400)
    entry.update(attempts=3, nextRetryAt=0)
    service.save()
    resumed = collector(root)
    with patch.object(module.time, "time", return_value=NOW), patch.object(module, "capture_report") as capture:
        resumed.collect_pending()
        capture.assert_not_called()
    assert resumed.state["reports"]["101"]["attempts"] == 3
    assert not resumed.state["reports"]["101"]["complete"]
    resumed.time_range = "all"
    with patch.object(module, "capture_report", side_effect=lambda runner, bid, *a, **k: export(resumed, bid)) as capture:
        resumed.collect_pending()
    assert capture.call_count == 1
    assert resumed.state["reports"]["101"]["complete"]


def test_pending_old_bytes_are_imported_without_requesting_missing_components(root):
    imported = []
    service = collector(root, on_export=lambda path: imported.append(str(path)))
    entry = observe(service, 101, NOW - 4 * 86400)
    saved = export(service, 101, complete=False)
    entry["pendingImport"] = {"path": saved["path"], "result": saved}
    entry["attempts"] = 2
    with patch.object(module.time, "time", return_value=NOW), patch.object(module, "capture_report") as capture:
        service.collect_pending()
        capture.assert_not_called()
    assert saved["path"] in imported
    assert "pendingImport" not in entry
    assert entry["attempts"] == 2
    assert not entry["complete"]


def test_parallel_candidates_apply_both_type_and_time_and_summary_counts_union(root):
    service = collector(root, parallelism=4, pvp_only=True)
    service.state["runtimeCapabilities"] = {"battleMatchTypes": {"PVP": 71, "PVE": 72}}
    observe(service, 101, NOW - 1)
    observe(service, 102, NOW - 4 * 86400)
    observe(service, 103, NOW - 1, kind=72)
    observe(service, 104, NOW - 4 * 86400, kind=72)
    observe(service, 105)
    entries = list(service.state["reports"].values())
    with patch.object(module.time, "time", return_value=NOW):
        assert service._parallel_candidates(entries, None, "101") == ["101"]
        summary = service.filter_summary(entries)
    assert summary["excludedReports"] == 4  # Overlapping PVE/age exclusions counted once.
    assert summary["timeExcludedReports"] == 2
    assert summary["timeUnknownReports"] == 1


def test_long_running_pending_work_ages_out_before_next_request(root):
    service = collector(root)
    entry = observe(service, 101, NOW - 86400 + 1)
    with patch.object(module.time, "time", return_value=NOW):
        assert service._network_selected(entry)
        assert service._parallel_candidates([entry], None, "101") == ["101"]
    with patch.object(module.time, "time", return_value=NOW + 2), patch.object(module, "capture_report") as capture:
        service.collect_pending()
        capture.assert_not_called()
        assert service._parallel_candidates([entry], None, "101") == []
    assert entry["attempts"] == 0


def test_report_aging_out_during_checkpoint_does_not_start_a_request(root):
    service = collector(root)
    entry = observe(service, 101, NOW - 86400 + 1)
    clock = [NOW]
    original_save = service.save
    def delayed_save():
        original_save()
        if entry["attempts"]:
            clock[0] = NOW + 2
    with patch.object(module.time, "time", side_effect=lambda: clock[0]), \
            patch.object(service, "save", side_effect=delayed_save), \
            patch.object(module, "capture_report") as capture:
        service.collect_pending()
    capture.assert_not_called()
    assert entry["attempts"] == 0
    assert "lastAttemptAt" not in entry


def test_prefetched_report_aging_out_still_saves_bytes_without_more_requests(root):
    service = collector(root, parallelism=2)
    observe(service, 101, NOW - 86400 + 1)
    observe(service, 102, NOW - 86400 + 1)
    clock = [NOW]
    def prefetch(runner, ids, **kwargs):
        clock[0] = NOW + 2
        return {str(bid): {} for bid in ids}
    with patch.object(module.time, "time", side_effect=lambda: clock[0]), \
            patch.object(module, "prefetch_reports", side_effect=prefetch), \
            patch.object(module, "capture_report", side_effect=lambda runner, bid, *a, **k: export(service, bid)) as capture:
        service.collect_pending()
    assert {call.args[1] for call in capture.call_args_list} == {101, 102}
    assert all(call.kwargs.get("request_missing") is False for call in capture.call_args_list)
    assert all(entry["complete"] for entry in service.state["reports"].values())


@pytest.mark.parametrize("timestamps,visible,complete", [
    ([NOW - 1, NOW - 4 * 86400], 1, True),
    ([NOW - 4 * 86400], 0, True),
    ([NOW - 1, None], 1, False),
])
def test_cycle_coverage_uses_current_time_selection(root, timestamps, visible, complete):
    service = collector(root, scopes=("personal",))
    capabilities = {"runtimeIdentity": {"avatarId": "42", "serverId": "1", "scenarioId": "14"}}
    def discover(scope, capabilities):
        for bid, stamp in enumerate(timestamps, 101):
            observe(service, bid, stamp)
        return {"complete": True}
    with patch.object(module.time, "time", return_value=NOW), \
            patch.object(service, "discover_scope", side_effect=discover), \
            patch.object(module, "run_checked", return_value=json.dumps({"payload": capabilities})), \
            patch.object(module, "capture_report", side_effect=lambda runner, bid, *a, **k: export(service, bid)) as capture:
        result = service.run_cycle()
    assert result["complete"] is complete
    assert result["visibleReports"] == visible
    assert result["allVisibleReports"] == len(timestamps)
    assert capture.call_count == visible


def test_discovery_does_not_stop_at_an_old_page_without_sortedness_evidence(root):
    service = collector(root, scopes=("personal",))
    capabilities = {"segmentLength": 1, "relationAll": 1000,
                    "capabilities": {"RPCGetBattleBlockList": True, "RPCGetChildCombatInfoList": True}}
    calls = []
    def page(scope, index, relation, segment, parent=None):
        calls.append(index)
        stamp = NOW - 4 * 86400 if index == 1 else NOW - 1
        rows = [{"combatInfo": {"battleID": 100 + index, "endTimeStampTime": stamp}}]
        return rows, 2 if index == 1 else None, {"sIndex": index, "reqLen": 1}, root / "page.json"
    service.page = page
    service._collect_pages = True
    with patch.object(module.time, "time", return_value=NOW), \
            patch.object(module, "capture_report", side_effect=lambda runner, bid, *a, **k: export(service, bid)) as capture:
        service.discover_scope("personal", capabilities)
    assert calls == [1, 2]
    assert [call.args[1] for call in capture.call_args_list] == [102]


def test_library_default_stays_all_and_invalid_range_fails_before_output_creation(root):
    assert module.BattleCollector(None, root).time_range == "all"
    assert DEFAULT_TIME_RANGE == "24h"
    assert TIME_RANGE_HOURS == {"all": None, "24h": 24, "72h": 72, "7d": 168}
    for value in (None, False, 24, "1d", ""):
        with pytest.raises(ValueError, match="下载时段"):
            validate_time_range(value)
        destination = root / "must_not_be_created"
        with pytest.raises(ValueError, match="下载时段"):
            module.BattleCollector(None, destination, time_range=value)
        assert not destination.exists()


def runtime_with_expiring_window(root):
    pytest.importorskip("lupa.lua51")
    helpers = runpy.run_path(str(Path(__file__).with_name("test_parallel_prefetch.py")))
    clock = helpers["Clock"]()
    runner = helpers["DelayedLuaRunner"](clock, latency=3)
    service = collector(root)
    service.runner.runner = runner
    runner.lua.execute("__codexBattleCollector={owner=" + json.dumps(service.owner) + "}")
    for bid in (101, 102):
        observe(service, bid, NOW - 86400 + .5)
    return service, runner, clock, helpers["virtual_time"]


def test_component_pacing_rechecks_window_before_each_actual_prefetch_rpc(root):
    service, runner, clock, virtual_time = runtime_with_expiring_window(root)
    with virtual_time(clock), patch.object(module.time, "time", side_effect=lambda: NOW + clock.now - 100):
        result = exporter.prefetch_reports(service.runner, [101, 102], wait_seconds=4,
            min_request_interval=1, min_dispatch_interval=1, prefetch_snapshots=False)
    assert runner.requests() == [(101, "all")]
    assert result["101"]["all"]["requested"] is True
    for bid, name in (("101", "statics"), ("102", "all"), ("102", "statics")):
        assert result[bid][name]["requested"] is False
        assert result[bid][name]["dispatched"] is False
        assert result[bid][name]["skipReason"] == "outside_selected_time_range"


def test_component_waiting_does_not_request_expired_missing_parts_or_drop_arrived_bytes(root):
    service, runner, clock, virtual_time = runtime_with_expiring_window(root)
    with virtual_time(clock), patch.object(module.time, "time", side_effect=lambda: NOW + clock.now - 100):
        result = exporter.capture_report(service.runner, 101, root, wait_seconds=4, poll_seconds=.1)
    assert runner.requests() == [(101, "all")]
    assert result["exported"] is True
    assert result["complete"] is False
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert payload["components"]["all"]["marker"] == "battle-101"
    assert payload["components"]["all"]["eventDataList"]
    for name in ("statics", "snapshots"):
        assert result["componentStatus"][name]["requested"] is False
        assert result["componentStatus"][name]["dispatched"] is False
        assert result["componentStatus"][name]["skipReason"] == "outside_selected_time_range"
