"""Economical indexes preserve current business queries and raw event pages."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import runpy
import time

import pytest

from battle_ui.daily import DailyStats
from battle_ui.data import BattleData
from battle_ui.opponents import OpponentRoster
from battle_ui.peerless import PeerlessRecords
from battle_ui.recognizer_data import EnemyDataLoader
from battle_ui.team_winrates import TeamWinrateService
from parser.report_archive import ReportArchive
from parser.report_semantics import SemanticMapper


NS = "server:1/scenario:18/avatar:100"
ROOT = Path(__file__).resolve().parents[1]
peerless_report = runpy.run_path(str(ROOT / "tests/test_battle_peerless.py"))["report"]


def _army(player, union, hero_base):
    return {"army_id": player * 100, "zgNum": 12 if player == 100 else 0,
            "avatar_data": {"avatar_id": player, "avatar_name": "己方" if player == 100 else "达成人员",
                            "avatar_union_id": union, "avatar_union_name": "同盟" + str(union)},
            "army_member": [{"heros_id": hero_base + i, "name": "武将" + str(hero_base + i),
                "index": i, "hero_lv": 40, "evolutionNum": 5, "enlighten": 4,
                "origin_troop_num": 10000, "troop_num": 9000,
                "skills": [{"skillId": 3000 + i * 10 + slot, "level": 10, "orderLevel": 0}
                           for slot in range(3)]} for i in range(3)]}


def _reports():
    for i, winner in enumerate((10000, 0, 20000, -1)):
        value = peerless_report(str(1001 + i), block="co_" + str(1001 + i))
        data = value["data"]
        data.update(battleJobId=5000 + i, childNum=0, matchType=7)
        data["battleSettlement"] = {"winer": winner, "attacker": _army(100, 10, 4000),
                                    "defensive": _army(200, 20, 5000)}
        data["eventDataList"] = [{"eventId": 0, "intList": [0], "tab": 0},
                                 {"eventId": 101, "intList": [1, 2, 0], "tab": 1}]
        value["eventCount"] = 2
        for component in ("basic", "detail", "all"):
            value["components"][component] = copy.deepcopy(data)
        value["components"]["snapshots"] = {"frames": [{"heros_id": 4000 + index,
            "properties": list(range(30))} for index in range(3)]}
        value["components"]["statics"] = {"armyStats": [{"armyId": 10000, "value": 12}]}
        value["discovery"][0]["row"]["combatInfo"] = copy.deepcopy(data)
        value["discovery"].append({"scope": "union", "parent": None,
                                   "row": {"childNum": 0, "combatInfo": copy.deepcopy(data)}})
        yield value


def _business(value):
    if isinstance(value, dict):
        return {key: _business(item) for key, item in value.items()
                if key not in {"database_path", "elapsed_seconds", "imported_at", "first_seen", "last_seen"}}
    if isinstance(value, list):
        return [_business(item) for item in value]
    return value


@pytest.fixture(params=("compact", "lean"))
def paired_archives(tmp_path, request):
    runtime = tmp_path / "runtime"
    account = runtime / hashlib.sha256(NS.encode()).hexdigest()[:20]
    account.mkdir(parents=True)
    (account / "collector_state.json").write_text(json.dumps({"namespace": NS,
        "runtimeIdentity": {"serverId": 1, "scenarioId": 18, "avatarId": 100},
        "runtimeCapabilities": {"unionId": 10, "battleMatchTypes": {"PVP": 7, "PVE": 8}},
        "runtimeCapabilitiesObservedAt": time.time()}), encoding="utf-8")
    originals = [json.dumps(value, ensure_ascii=False).encode("utf-8") for value in _reports()]
    mapper = SemanticMapper("consumer-parity-fixture", {"fixture": True},
        lambda kind, value: {"mapped": True, "status": "mapped", "name_zh": "映射" + kind + str(value),
                             "source": {"fixture": "same-name-provenance"}},
        lambda event, context: {"status": "unmapped", "parameters": event.get("intList", [])})
    paths, counts = [], []
    for mode in ("full", request.param):
        path = tmp_path / (mode + ".db")
        with ReportArchive(path, storage_mode=mode, semantic_mapper=mapper) as archive:
            for raw in originals:
                result = archive.import_bytes(raw, namespace=NS)
                assert result["status"] == "imported", result
                assert result["semantics"]["failed"] == 0, result
                assert archive.original_bytes(result["snapshot_ids"][0]) == raw
            assert archive.verify()["ok"]
            counts.append(archive.connection.execute("SELECT count(*) FROM report_fields").fetchone()[0])
        paths.append(path)
    assert 0 < counts[1] < counts[0]
    return paths, runtime


def test_daily_totals_and_current_union_filter_are_equal(paired_archives):
    paths, runtime = paired_archives
    for own_union_only in (False, True):
        results = [DailyStats(path, own_union_only=own_union_only, runtime_dir=runtime).summary(namespace=NS)
                   for path in paths]
        assert results[0]["available"] and results[0]["totals"]["reports"] == 4
        assert results[0]["players"]
        assert _business(results[0]) == _business(results[1])


def test_lineups_skills_red_quality_and_win_draw_loss_are_equal(paired_archives):
    paths, runtime = paired_archives
    types = {str(3000 + i * 10 + slot): 0 if slot == 0 else 3 for i in range(3) for slot in range(3)}
    results = [TeamWinrateService(path, runtime_dir=runtime, skill_types=types).summary(namespace=NS, battle_type="all")
               for path in paths]
    own = next(row for row in results[0]["without_skills"] if row["heroes"][0]["hero_uid"] == "4000")
    assert (own["wins"], own["draws"], own["losses"], own["unknown_results"]) == (1, 1, 1, 1)
    assert (own["total_red"], own["total_quality"]) == (15, 12)
    assert results[0]["with_skills"]
    assert _business(results[0]) == _business(results[1])


def test_opponent_peerless_and_historical_recognizer_results_are_equal(paired_archives):
    paths, _runtime = paired_archives
    opponents = [OpponentRoster(path).summary(namespace=NS) for path in paths]
    assert opponents[0]["players"] and opponents[0]["players"][0]["player_uid"] == "200"
    assert _business(opponents[0]) == _business(opponents[1])
    peerless = [PeerlessRecords(path).summary(namespace=NS) for path in paths]
    assert peerless[0]["counts"]["occurrences"] == 4
    assert _business(peerless[0]) == _business(peerless[1])
    loaders = [EnemyDataLoader(db_path=path, namespace=NS) for path in paths]
    assert loaders[0].known_player_names == ["达成人员"]
    assert loaders[0].player_teams_map == loaders[1].player_teams_map
    assert _business(loaders[0].stats) == _business(loaders[1].stats)


def test_detail_metadata_components_sides_and_raw_event_pages_are_equal(paired_archives):
    paths, _runtime = paired_archives
    details = [BattleData(path).detail(1, event_limit=1, event_offset=1) for path in paths]
    for key in ("metadata", "components", "missing_components", "sides", "event_total", "event_has_more"):
        assert details[0][key] == details[1][key], key
    assert details[0]["sides"][0]["heroes"][0]["name_zh"] == "映射hero4000"
    assert details[0]["metadata"]
    assert details[0]["events"][0]["raw"]["intList"] == [1, 2, 0]
    for key in ("sequence", "event_id", "tab", "raw"):
        assert details[0]["events"][0][key] == details[1]["events"][0][key], key
