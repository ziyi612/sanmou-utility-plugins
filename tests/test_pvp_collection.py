"""PVP selection must gate requests, including resumed and prefetched work."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from capture.game_assets.battle_collector import BattleCollector, install_lua
from capture.game_assets.export_battle_report_runtime import atomic_json, decode_envelope
import battle_reports as cli


@pytest.fixture
def root():
    with tempfile.TemporaryDirectory(prefix="pvp_collection_", dir=".runtime/test-temp") as folder:
        yield Path(folder)


def collector(root, **kwargs):
    result = BattleCollector(None, root, pvp_only=True, request_delay=0, **kwargs)
    # Deliberately differ from the historical PVP=1 to detect hardcoded rules.
    result.state["runtimeCapabilities"] = {"battleMatchTypes": {"PVP": 71, "PVBE": 72, "PVAR": 77}}
    return result


def observe(service, bid, kind=None, *, parent=None, **extra):
    combat = {"battleID": bid, **extra}
    if kind is not None:
        combat["matchType"] = kind
    row = combat if parent is not None else {"combatInfo": combat}
    service.remember("personal", row, {"sIndex": 1, "reqLen": 1}, service.out_dir / "page.json", parent)
    return service.state["reports"][str(bid)]


def export(service, bid, complete=True):
    path = service.out_dir / f"battle_{bid}_decrypted.json"
    atomic_json(path, {"battleID": bid, "data": {"battleID": bid}, "source": "basic"})
    return {"battleID": bid, "exported": True, "complete": complete, "path": str(path)}


def test_current_matchtype_controls_selection_not_names_ids_or_old_numeric_constant(root):
    service = collector(root)
    pvp = observe(service, 101, 71, attacker={"avatarId": 0})
    npc = observe(service, 102, 72, attacker={"avatarId": 42}, defender={"avatarId": 43})
    arena = observe(service, 103, 77)
    historical_numeric = observe(service, 104, 1)
    missing = observe(service, 105)
    for value in (historical_numeric, missing):
        assert service.battle_class(value) == "unknown"
    assert service.battle_class(pvp) == "pvp"
    assert service.battle_class(npc) == service.battle_class(arena) == "pve"
    with patch("capture.game_assets.battle_collector.capture_report",
               side_effect=lambda runner, bid, *a, **k: export(service, bid)) as capture:
        result = service.collect_pending()
    assert [call.args[1] for call in capture.call_args_list] == [101]
    assert result["complete"] == 1
    assert result["skipped"] == 4
    assert npc["attempts"] == missing["attempts"] == 0


def test_children_are_classified_independently_of_the_parent(root):
    service = collector(root)
    observe(service, 101, 71)
    observe(service, 102, 72, parent="101")
    observe(service, 103, parent="101")
    observe(service, 104, 71, parent="101")
    with patch("capture.game_assets.battle_collector.capture_report",
               side_effect=lambda runner, bid, *a, **k: export(service, bid)) as capture:
        service.collect_pending()
    assert {call.args[1] for call in capture.call_args_list} == {101, 104}


def test_parallel_prefetch_does_not_admit_pve_or_unknown(root):
    service = collector(root, parallelism=4)
    observe(service, 101, 71)
    observe(service, 102, 72)
    observe(service, 103)
    observe(service, 104, 71)
    rows = list(service.state["reports"].values())
    assert service._parallel_candidates(rows, None, "101") == ["101", "104"]


def test_filter_switch_resumes_existing_pve_without_destroying_or_completing_it(root):
    service = collector(root)
    old = observe(service, 101, 72)
    old.update(attempts=3, complete=False, nextRetryAt=0)
    service.save()
    resumed = BattleCollector(None, root, pvp_only=True)
    with patch("capture.game_assets.battle_collector.capture_report") as capture:
        resumed.collect_pending()
        capture.assert_not_called()
    assert resumed.state["reports"]["101"]["attempts"] == 3
    assert not resumed.state["reports"]["101"]["complete"]
    resumed.pvp_only = False
    with patch("capture.game_assets.battle_collector.capture_report",
               side_effect=lambda runner, bid, *a, **k: export(resumed, bid)) as capture:
        resumed.collect_pending()
    assert capture.call_count == 1
    assert resumed.state["reports"]["101"]["complete"]


def test_pending_pve_bytes_import_locally_but_do_not_request_missing_components(root):
    imports = []
    service = collector(root, on_export=lambda path: imports.append(str(path)))
    old = observe(service, 101, 72)
    saved = export(service, 101, complete=False)
    old["pendingImport"] = {"path": saved["path"], "result": saved}
    old["attempts"] = 2
    with patch("capture.game_assets.battle_collector.capture_report") as capture:
        service.collect_pending()
        capture.assert_not_called()
    assert saved["path"] in imports
    assert "pendingImport" not in old
    assert old["attempts"] == 2
    assert not old["complete"]


def test_missing_runtime_enum_never_falls_back_to_historical_number(root):
    service = collector(root)
    entry = observe(service, 101, 1)
    service.state["runtimeCapabilities"] = {}
    assert service.battle_class(entry) == "unknown"
    with patch("capture.game_assets.battle_collector.capture_report") as capture:
        service.collect_pending()
        capture.assert_not_called()


def test_conflicting_observations_remain_unknown(root):
    service = collector(root)
    entry = observe(service, 101, 71)
    observe(service, 101, 72, parent="900")
    assert service.battle_class(entry) == "unknown"


@pytest.mark.parametrize("scope,relation", [("personal", 27), ("union", 27), ("star", 1000)])
def test_runtime_pvp_relation_reduces_server_list_requests_except_favourites(root, scope, relation):
    service = collector(root)
    capabilities = {"segmentLength": 10, "relationAll": 1000, "relationPvp": 27,
                    "capabilities": {name: True for name in (
                        "RPCGetBattleBlockList", "RPCGetChildCombatInfoList",
                        "RPCGetUnionBattleBlockList", "RPCGetUnionChildCombatInfoList",
                        "RPCStarBattleBlockList", "RPCStarBattleChildList")}}
    calls = []
    def page(scope, index, selected_relation, segment, parent=None):
        calls.append(selected_relation)
        return [], None, {"sIndex": 1, "reqLen": 0}, root / "page.json"
    service.page = page
    service.discover_scope(scope, capabilities)
    assert calls == [relation]


def test_filter_summary_exposes_exclusions_without_claiming_unknown_as_pvp(root):
    service = collector(root)
    observe(service, 101, 71)
    observe(service, 102, 72)
    observe(service, 103)
    summary = service.filter_summary(service.state["reports"].values())
    assert summary["mode"] == "pvp_only"
    assert summary["pvpReports"] == summary["pveReports"] == summary["unknownReports"] == 1
    assert summary["excludedReports"] == 2


def test_filtered_historical_child_page_does_not_complete_the_all_mode_backlog(root):
    service = collector(root)
    service.pvp_only = False
    service.remember("personal", {"combatInfo": {"battleID": 101, "matchType": 71}, "childNum": 2},
                     {}, root / "page.json")
    service.state["scopes"]["personal"] = {
        "roots": {"101": {"battleID": "101", "childNum": 2}},
        "children": {"101": {"complete": False}}, "selectionMode": "all"}
    original = service._known_child_groups("personal", service.state["scopes"]["personal"])
    assert not original["101"]["complete"]
    capabilities = {"segmentLength": 10, "relationAll": 1000, "relationPvp": 27,
                    "capabilities": {"RPCGetBattleBlockList": True, "RPCGetChildCombatInfoList": True}}
    calls = []
    def page(scope, index, relation, segment, parent=None):
        calls.append((relation, parent))
        if parent is None:
            raise TimeoutError("head unavailable")
        return [], None, {"sIndex": 1, "reqLen": 0}, root / "page.json"
    service.page = page
    service.pvp_only = True
    service.discover_scope("personal", capabilities)
    assert (27, "101") in calls
    assert service.state["childRecovery"]["personal:pvp_only"]["101"]["complete"]
    assert not service.state["childRecovery"]["personal"]["101"]["complete"]
    service.pvp_only = False
    service.discover_scope("personal", capabilities)
    assert (1000, "101") in calls


@pytest.mark.parametrize("kinds,complete", [([71, 72], True), ([71, 72, None], False), ([72], True)])
def test_cycle_coverage_counts_selected_pvp_and_reports_unknown_exclusions(root, kinds, complete):
    service = collector(root, scopes=("personal",))
    capabilities = {**service.state["runtimeCapabilities"],
                    "runtimeIdentity": {"avatarId": "42", "serverId": "1", "scenarioId": "14"}}
    def discover(scope, capabilities):
        for bid, kind in enumerate(kinds, 101):
            observe(service, bid, kind)
        return {"complete": True}
    with patch.object(service, "discover_scope", side_effect=discover), \
            patch("capture.game_assets.battle_collector.run_checked", return_value=json.dumps({"payload": capabilities})), \
            patch("capture.game_assets.battle_collector.capture_report",
                  side_effect=lambda runner, bid, *a, **k: export(service, bid)) as capture:
        result = service.run_cycle()
    assert result["complete"] is complete
    assert result["visibleReports"] == kinds.count(71)
    assert result["allVisibleReports"] == len(kinds)
    assert result["filter"]["unknownReports"] == kinds.count(None)
    assert all(call.args[1] == 101 for call in capture.call_args_list)


def test_cli_explicit_ids_cannot_bypass_the_pvp_metadata_filter(root):
    args = cli.build_parser().parse_args(["collect", "--pvp-only", "--battle-ids", "101", "--out-dir", str(root)])
    with pytest.raises(ValueError, match="explicit IDs lack verified list matchType"):
        cli.run_collect(args, None)


def test_installer_exports_the_loaded_runtime_matchtype_and_relation():
    lupa = pytest.importorskip("lupa.lua51")
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute("""
CommonConst={SEG_LEN=10,BattleBaseRelationType={ALL=1000,PVP=27},BATTLEOBJECTMATCHTYPE={PVP=71,PVBE=72}}
G_SCENARIO_ID=14
ClientProxy={avatarId=42,GetServerId=function() return 1 end,
RPCStarBattleBlockList=function() end,RPCStarBattleBlockListResponse=function() end}
""")
    value = decode_envelope(lua.execute(install_lua("pvp-test")))["payload"]
    assert value["relationPvp"] == 27
    assert value["battleMatchTypes"] == {"PVP": 71, "PVBE": 72}
