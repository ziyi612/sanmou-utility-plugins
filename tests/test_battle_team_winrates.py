"""Current-version, two-sided lineup statistics with proved result semantics."""
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest

from battle_ui.team_winrates import TeamWinrateService, _POINTERS
from battle_ui.winrate_sorting import known_result_count, ranked_winrate_rows
from parser.report_archive import ReportArchive


NS = "server:1/scenario:18/avatar:100"
ABSENT = object()


def army(uid=100, *, red=(4, 4, 4), quality=(4, 4, 4), hero_base=4000):
    heroes = []
    for index in range(3):
        hero = {"heros_id": hero_base + index, "index": index, "evolutionNum": red[index],
                "enlighten": quality[index], "name": "武将" + str(hero_base + index),
                "skills": [{"skillId": 3000 + index * 10 + slot, "position": 0, "level": 10, "orderLevel": 0}
                           for slot in range(3)]}
        heroes.append(hero)
    return {"army_id": uid * 100, "avatar_data": {"avatar_id": uid, "avatar_name": str(uid)},
            "army_member": heroes}


def payload(uid="1", *, winner=10000, attack=None, defend=None, match_type=7,
            day="2026-11-12T04:00:00+00:00", child_count=0, complete=True):
    settlement = {"attacker": copy.deepcopy(attack or army()),
                  "defensive": copy.deepcopy(defend or army(200, hero_base=5000))}
    if winner is not ABSENT:
        settlement["winer"] = winner
    data = {"battleID": str(uid), "battleJobId": int(uid) + 1000, "childNum": child_count,
            "matchType": match_type, "timeStamp": datetime.fromisoformat(day).timestamp(),
            "eventDataList": [{"eventId": 100, "intList": [], "tab": 0}], "battleSettlement": settlement}
    return {"battleID": str(uid), "captureVersion": 2, "source": "all", "data": data,
            "eventCount": 1, "captureComplete": complete, "serializationIssues": [],
            "components": {key: copy.deepcopy(data) if key in ("basic", "all", "detail") else {}
                           for key in ("basic", "all", "detail", "statics", "snapshots")},
            "componentStatus": {key: {"available": True, "complete": complete or key != "snapshots"}
                                for key in ("basic", "all", "detail", "statics", "snapshots")}}


def mutate(value, function):
    for data in (value["data"], value["components"]["basic"], value["components"]["all"], value["components"]["detail"]):
        function(data)
    return value


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "teams.db"
    runtime = tmp_path / "runtime"
    folder = runtime / hashlib.sha256(NS.encode()).hexdigest()[:20]
    folder.mkdir(parents=True)
    (folder / "collector_state.json").write_text(json.dumps({
        "namespace": NS, "runtimeIdentity": {"serverId": 1, "scenarioId": 18, "avatarId": 100},
        "runtimeCapabilities": {"battleMatchTypes": {"PVP": 7, "PVE": 8}}}), encoding="utf-8")
    with ReportArchive(db) as archive:
        types = {str(3000 + hero * 10 + slot): 0 if slot == 0 else 3 for hero in range(3) for slot in range(3)}
        types["8888"] = 3
        yield archive, TeamWinrateService(db, runtime_dir=runtime, skill_types=types)


def put(archive, value, namespace=NS):
    result = archive.import_bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"), namespace=namespace)
    assert result["status"] in ("imported", "duplicate")
    return result


def first(value, key="without_skills"):
    return next(row for row in value[key] if row["heroes"][0]["hero_uid"] == "4000")


def test_missing_archive_is_not_created(tmp_path):
    path = tmp_path / "absent" / "missing.db"
    result = TeamWinrateService(path).summary()
    assert not result["available"] and not path.parent.exists()


def test_draws_enter_denominator_unknown_results_do_not_and_both_sides_count(store):
    archive, service = store
    for uid, winner in enumerate((10000, 0, 20000, -1), 1):
        put(archive, payload(str(uid), winner=winner))
    value = service.summary(namespace=NS)
    row = first(value)
    assert (row["samples"], row["wins"], row["draws"], row["losses"], row["unknown_results"]) == (4, 1, 1, 1, 1)
    assert row["win_rate"] == row["draw_rate"] == pytest.approx(1 / 3)
    assert value["totals"]["samples"] == 8 and value["totals"]["reports"] == 4
    assert sum(row["samples"] for row in value["with_skills"]) == 8


def test_only_best_current_version_and_duplicate_import_count_once(store):
    archive, service = store
    put(archive, payload(winner=20000, complete=False))
    complete = payload(winner=10000)
    put(archive, complete)
    put(archive, complete)
    value = service.summary(namespace=NS)
    assert value["totals"]["samples"] == 2 and first(value)["wins"] == 1


def test_red_and_quality_use_independent_totals_and_joint_thresholds(store):
    archive, service = store
    put(archive, payload(attack=army(red=(5, 5, 5), quality=(1, 2, 3))))
    value = service.summary(namespace=NS)
    row = first(value)
    assert (row["total_red"], row["total_quality"], row["tier"]) == (15, 6, "medium")
    assert first(service.summary(namespace=NS, high_red=15, high_quality=6))["tier"] == "high"


def test_missing_or_malformed_red_quality_does_not_become_zero(store):
    archive, service = store
    attack = army(red=(5, 5, 5))
    del attack["army_member"][1]["enlighten"]
    attack["army_member"][0]["quality"] = 5  # Static card rarity is not enlighten.
    put(archive, payload(attack=attack))
    row = first(service.summary(namespace=NS))
    assert row["total_red"] == 15 and row["total_quality"] is None and row["tier"] == "unknown"


def test_hero_order_and_position_do_not_create_another_main_team(store):
    archive, service = store
    original = army()
    reordered = copy.deepcopy(original)
    reordered["army_member"].reverse()
    changed = copy.deepcopy(original)
    changed["army_member"][0]["index"], changed["army_member"][1]["index"] = 1, 0
    for uid, attack in enumerate((original, reordered, changed), 1):
        put(archive, payload(str(uid), attack=attack))
    rows = [row for row in service.summary(namespace=NS)["without_skills"]
            if {hero["hero_uid"] for hero in row["heroes"]} == {"4000", "4001", "4002"}]
    assert len(rows) == 1 and rows[0]["samples"] == 3
    with_rows = [row for row in service.summary(namespace=NS)["with_skills"]
                 if {hero["hero_uid"] for hero in row["heroes"]} == {"4000", "4001", "4002"}]
    assert len(with_rows) == 1 and with_rows[0]["skill_variants_count"] == 1


def test_first_three_skills_define_combination_extra_effects_and_positions_do_not(store):
    archive, service = store
    original = army()
    extra = copy.deepcopy(original)
    extra["army_member"][0]["skills"].append({"skillId": 987654, "position": 0})
    changed = copy.deepcopy(original)
    changed["army_member"][0]["skills"][1]["skillId"] = 8888
    for uid, attack in enumerate((original, extra, changed), 1):
        put(archive, payload(str(uid), attack=attack))
    value = service.summary(namespace=NS)
    assert first(value)["samples"] == 3
    rows = [row for row in value["with_skills"] if row["heroes"][0]["hero_uid"] == "4000"]
    assert len(rows) == 1 and rows[0]["samples"] == 3
    assert sorted(variant["samples"] for variant in rows[0]["skill_variants"]) == [1, 2]


def test_missing_skills_remain_separate_unknown_combinations(store):
    archive, service = store
    attack = army()
    del attack["army_member"][0]["skills"]
    for uid in ("1", "2"):
        put(archive, payload(uid, attack=attack))
    value = service.summary(namespace=NS)
    assert first(value)["samples"] == 2
    rows = [row for row in value["with_skills"] if row["heroes"][0]["hero_uid"] == "4000"]
    assert len(rows) == 1 and rows[0]["samples"] == 2
    assert rows[0]["skill_variants_count"] == 2
    assert all(variant["samples"] == 1 and not variant["skills_complete"] for variant in rows[0]["skill_variants"])


def test_reverse_lineup_merges_outcomes_and_tactics_stay_bound_to_the_hero(store):
    archive, service = store
    original = army()
    reverse = copy.deepcopy(original)
    reverse["army_member"].reverse()
    for index, hero in enumerate(reverse["army_member"]):
        hero["index"] = index
    moved_skills = copy.deepcopy(original)
    members = moved_skills["army_member"]
    members[0]["skills"], members[1]["skills"] = members[1]["skills"], members[0]["skills"]
    for uid, attack, winner in (("1", original, 10000), ("2", reverse, 20000), ("3", moved_skills, 0)):
        put(archive, payload(uid, attack=attack, winner=winner))
    value = service.summary(namespace=NS)
    group = first(value, "with_skills")
    assert (group["samples"], group["wins"], group["draws"], group["losses"]) == (3, 1, 1, 1)
    assert group["win_rate"] == group["draw_rate"] == pytest.approx(1 / 3)
    assert group["skill_variants_count"] == 2
    variants = group["skill_variants"]
    normal = next(variant for variant in variants if variant["samples"] == 2)
    moved = next(variant for variant in variants if variant["samples"] == 1)
    assert (normal["wins"], normal["losses"], normal["draws"]) == (1, 1, 0)
    assert normal["heroes"][0]["hero_uid"] == moved["heroes"][0]["hero_uid"] == "4000"
    assert normal["heroes"][0]["skills"][0]["skill_uid"] == "3000"
    assert moved["heroes"][0]["skills"][0]["skill_uid"] == "3010"
    assert sum(variant["samples"] for variant in variants) == group["samples"]


def test_incomplete_hero_ids_never_merge_from_matching_names(store):
    archive, service = store
    attack = army()
    del attack["army_member"][0]["heros_id"]
    for uid in ("1", "2"):
        put(archive, payload(uid, attack=attack))
    for mode in ("without_skills", "with_skills"):
        rows = [row for row in service.summary(namespace=NS)[mode] if row["tier"] == "unknown"]
        assert len(rows) == 2 and all(row["samples"] == 1 for row in rows)


def test_missing_positions_still_prove_same_three_heroes_and_each_tier_has_one_row(store):
    archive, service = store
    high = army()
    no_positions = copy.deepcopy(high)
    for hero in no_positions["army_member"]:
        del hero["index"]
    low = army(red=(0, 0, 0), quality=(0, 0, 0))
    for uid, attack in (("1", high), ("2", no_positions), ("3", low)):
        put(archive, payload(uid, attack=attack))
    for mode in ("without_skills", "with_skills"):
        rows = [row for row in service.summary(namespace=NS)[mode] if row["heroes"][0]["hero_uid"] == "4000"]
        assert sorted((row["tier_label"], row["samples"]) for row in rows) == [("低红", 1), ("高满", 2)]


def test_repeated_source_sample_does_not_duplicate_main_or_variant_counts(store):
    archive, service = store
    put(archive, payload())
    with service.data._connection() as connection:
        side = dict(connection.execute("SELECT id FROM report_sides WHERE side='attacker'").fetchone())
        sid = archive.connection.execute("SELECT current_snapshot_id FROM report_records").fetchone()[0]
        team = service._team(connection, {"snapshot_id": sid, "serialization_ok": True, "body_complete": True}, side, {})
    sample = {"namespace": NS, "battle_uid": "1", "side": "attacker", "snapshot_id": sid,
              "day": "2026-11-12", "outcome": "win", "tier": "high", "team": team}
    for with_skills in (False, True):
        groups = service._aggregate([sample, sample], with_skills)
        assert len(groups) == 1 and groups[0]["samples"] == 1 and len(groups[0]["sources"]) == 1
        if with_skills:
            assert groups[0]["skill_variants"][0]["samples"] == 1


def test_explicitly_captured_unlearned_slots_are_not_invented(store):
    archive, service = store
    attack = army()
    attack["army_member"][0]["skills"] = attack["army_member"][0]["skills"][:1]
    put(archive, payload(attack=attack))
    row = first(service.summary(namespace=NS), "with_skills")
    assert row["skills_complete"] and len(row["heroes"][0]["skills"]) == 1


def test_serialization_failure_keeps_skill_combinations_unproved(store):
    archive, service = store
    value = payload()
    value["serializationIssues"] = [{"path": "/components/all/battleSettlement/attacker/army_member", "kind": "max_depth"}]
    put(archive, value)
    assert not first(service.summary(namespace=NS), "with_skills")["skills_complete"]


def test_unverified_skill_type_is_not_guessed_and_known_warbook_is_not_a_tactic(store):
    archive, service = store
    service.skill_types.pop("3000")
    service.skill_types["3001"] = 2
    put(archive, payload())
    hero = first(service.summary(namespace=NS), "with_skills")["heroes"][0]
    assert not hero["skills_complete"]
    assert [skill["skill_uid"] for skill in hero["skills"]] == ["3000", "3002"]
    assert hero["extra_effects"][0]["skillId"] == 3001


def test_mixed_high_group_preserves_full_red_full_quality_sample_count(store):
    archive, service = store
    put(archive, payload("1", attack=army(red=(5, 5, 5), quality=(5, 5, 5))))
    put(archive, payload("2", attack=army()))
    row = first(service.summary(namespace=NS))
    assert row["samples"] == 2 and row["full_red_full_quality_samples"] == 1
    assert not row["full_red_full_quality"] and row["total_red_min"] == 12 and row["total_red_max"] == 15


@pytest.mark.parametrize("winner", [ABSENT, -1, -2, 123456, True, "error"])
def test_unproved_winner_never_guessed_from_troops(store, winner):
    archive, service = store
    attack = army()
    for hero in attack["army_member"]:
        hero.update(troop_num=0, dead_num=999, wound_num=0)
    put(archive, payload(winner=winner, attack=attack))
    row = first(service.summary(namespace=NS))
    assert row["unknown_results"] == 1 and row["win_rate"] is None


def test_conflicting_winner_alias_stays_unknown(store):
    archive, service = store
    value = payload(winner=10000)
    value["data"]["battleSettlement"]["winer"] = -1
    put(archive, value)
    assert first(service.summary(namespace=NS))["unknown_results"] == 1


@pytest.mark.parametrize("winner,outcome", [(10000, "wins"), (-100003, "losses"), (0, "draws")])
def test_all_types_support_signed_npc_army_ids_and_independent_npc_root(store, winner, outcome):
    archive, service = store
    npc = army(200, hero_base=5000)
    npc["army_id"] = -100003
    npc["avatar_data"]["avatar_id"] = 0
    put(archive, payload(winner=winner, defend=npc, match_type=8, child_count=2))
    value = service.summary(namespace=NS, battle_type="all")
    row = first(value)
    assert value["totals"]["samples"] == 2 and row[outcome] == 1 and row["unknown_results"] == 0


def test_pvp_requires_captured_enum_and_unknown_type_is_not_inferred_from_players(store):
    archive, service = store
    for uid, match in enumerate((7, 8, 12345), 1):
        put(archive, payload(str(uid), match_type=match))
    assert service.summary(namespace=NS)["totals"]["reports"] == 1
    value = service.summary(namespace=NS, battle_type="all")
    assert value["totals"]["reports"] == 3 and value["coverage"]["unknown_reports"] == 1
    missing_enum = TeamWinrateService(archive.db_path, runtime_dir=Path(service.runtime_dir) / "absent")
    assert missing_enum.summary(namespace=NS)["totals"]["samples"] == 0


def test_root_and_children_are_three_independent_battles_continuous_total_not_a_fourth(store):
    archive, service = store
    for uid in ("10", "11", "12"):
        value = payload(uid, child_count=2 if uid == "10" else 0)
        if uid == "10":
            mutate(value, lambda data: data.update(otherInfo={"continuousInfo": {"winer": 20000, "battleNum": 3}}))
        put(archive, value)
    assert service.summary(namespace=NS)["totals"]["samples"] == 6


def test_unproved_parent_list_summary_is_not_counted_as_a_battle(store):
    archive, service = store
    value = payload("10", winner=ABSENT, child_count=2, complete=False)
    mutate(value, lambda data: (data.pop("battleJobId"), data.update(eventDataList=[])))
    value["eventCount"] = 0
    put(archive, value)
    result = service.summary(namespace=NS)
    assert result["totals"]["samples"] == 0 and result["coverage"]["summary_only_reports"] == 1


def test_basic_parent_with_residual_settlement_header_is_still_not_independent(store):
    archive, service = store
    value = payload("10", child_count=2, complete=False)
    value.update(source="basic", eventCount=0)
    mutate(value, lambda data: data.update(eventDataList=[]))
    put(archive, value)
    result = service.summary(namespace=NS)
    assert result["totals"]["samples"] == 0 and result["coverage"]["unverified_parent_reports"] == 1


def test_beijing_day_filter_and_namespace_do_not_mix(store):
    archive, service = store
    put(archive, payload("1", day="2026-11-11T15:59:59+00:00"))
    put(archive, payload("2", day="2026-11-11T16:00:00+00:00"))
    put(archive, payload("3"), namespace="another-account")
    value = service.summary(namespace=NS, day="2026-11-12")
    assert value["totals"]["samples"] == 2 and value["dates"] == ["2026-11-12", "2026-11-11"]


def test_defender_alias_deduplicates_without_recounting(store):
    archive, service = store
    value = payload()
    mutate(value, lambda data: data["battleSettlement"].update(defender=copy.deepcopy(data["battleSettlement"]["defensive"])))
    put(archive, value)
    result = service.summary(namespace=NS)
    assert result["totals"]["samples"] == 2 and result["coverage"]["duplicate_side_aliases"] == 1


@pytest.mark.parametrize("kwargs", [dict(high_red=-1), dict(high_quality=16), dict(medium_red=13),
    dict(high_red=True), dict(high_quality=float("nan")), dict(high_red=None), dict(battle_type="guess"), dict(day="wrong")])
def test_invalid_filters_rejected_without_db_creation(tmp_path, kwargs):
    path = tmp_path / "missing.db"
    with pytest.raises(ValueError):
        TeamWinrateService(path).summary(**kwargs)
    assert not path.exists()


def test_metadata_plan_uses_snapshot_and_pointer_index_and_summary_is_read_only(store):
    archive, service = store
    put(archive, payload())
    placeholders = ",".join("?" for _ in _POINTERS)
    plan = [row[3] for row in archive.connection.execute(
        "EXPLAIN QUERY PLAN SELECT f.pointer FROM report_records r CROSS JOIN report_fields AS f "
        "INDEXED BY sqlite_autoindex_report_fields_1 ON f.snapshot_id=r.current_snapshot_id "
        "AND f.pointer IN (" + placeholders + ") WHERE r.namespace=?", (*_POINTERS, NS))]
    assert any("SEARCH f USING" in row and "snapshot_id=? AND pointer=?" in row for row in plan)
    before = archive.db_path.read_bytes()
    service.summary(namespace=NS)
    assert archive.db_path.read_bytes() == before


def ranking_samples(prefix, outcomes, *, hero_base=4000, skill_base=3000):
    """Small in-memory inputs; these sorting checks never create a database."""
    heroes = [{"hero_uid": str(hero_base + index), "name_zh": "武将" + str(hero_base + index),
               "position": index, "ordinal": index, "red": 4, "quality": 4,
               "skills": [{"skill_uid": str(skill_base + index)}], "skills_complete": True}
              for index in range(3)]
    team = {"heroes": heroes, "lineup_complete": True, "skills_complete": True,
            "total_red": 12, "total_quality": 12}
    return [{"namespace": NS, "battle_uid": prefix + str(index), "side": "attacker",
             "snapshot_id": index + 1, "day": "2026-11-12", "outcome": outcome,
             "tier": "high", "team": team} for index, outcome in enumerate(outcomes)]


def test_winrate_order_prefers_higher_rate_before_larger_sample_count_in_memory():
    samples = (ranking_samples("high", ["win"] * 20) +
               ranking_samples("large", ["win"] * 50 + ["loss"] * 50, hero_base=5000))
    for with_skills in (False, True):
        rows = TeamWinrateService._aggregate(samples, with_skills)
        assert [(row["samples"], row["win_rate"]) for row in rows] == [(20, 1.0), (100, .5)]
        assert len(ranked_winrate_rows(rows)) == 2


def test_winrate_order_uses_known_results_before_total_samples_when_rates_tie_in_memory():
    samples = (ranking_samples("manyunknown", ["win"] * 10 + ["loss"] * 10 + ["unknown"] * 80) +
               ranking_samples("moreknown", ["win"] * 15 + ["loss"] * 15, hero_base=5000))
    rows = TeamWinrateService._aggregate(samples, False)
    assert [row["known_results"] for row in rows] == [30, 20]
    assert [row["samples"] for row in rows] == [30, 100]
    assert [row["win_rate"] for row in rows] == [.5, .5]


def test_winrate_order_places_zero_percent_before_unknown_rate_in_memory():
    samples = (ranking_samples("unknown", ["unknown"] * 50) +
               ranking_samples("zerowins", ["loss"] * 20, hero_base=5000))
    for with_skills in (False, True):
        rows = TeamWinrateService._aggregate(samples, with_skills)
        assert [row["win_rate"] for row in rows] == [0.0, None]
        assert [row["known_results"] for row in rows] == [20, 0]
        # Filtering is a display/export step. The core still retains unknown
        # sources and all observations for the original aggregates.
        assert sum(row["samples"] for row in rows) == 70


def test_winrate_order_sorts_each_hero_bound_skill_variant_by_rate_in_memory():
    samples = (ranking_samples("largeconfig", ["win"] * 40 + ["loss"] * 40) +
               ranking_samples("highconfig", ["win"] * 20, skill_base=6000))
    rows = TeamWinrateService._aggregate(samples, True)
    assert len(rows) == 1 and rows[0]["samples"] == 100
    variants = rows[0]["skill_variants"]
    assert [(row["samples"], row["win_rate"]) for row in variants] == [(20, 1.0), (80, .5)]
    assert variants[0]["heroes"][0]["hero_uid"] == variants[1]["heroes"][0]["hero_uid"] == "4000"
    assert variants[0]["heroes"][0]["skills"][0]["skill_uid"] == "6000"
    assert sum(variant["samples"] for variant in variants) == rows[0]["samples"]


def test_winrate_eligibility_counts_nineteen_twenty_and_excludes_unknowns_in_memory():
    nineteen = {"wins": 19, "draws": 0, "losses": 0, "samples": 119,
                "unknown_results": 100, "known_results": 119, "win_rate": 1.0}
    twenty = {"wins": 12, "draws": 0, "losses": 8, "samples": 20, "win_rate": .6}
    twenty_draws = {"wins": 0, "draws": 20, "losses": 0, "samples": 20, "win_rate": 0.0}
    rows = [nineteen, twenty_draws, twenty]
    before = copy.deepcopy(rows)
    assert known_result_count(nineteen) == 19  # Never trust a stale derived count.
    assert ranked_winrate_rows(rows) == [twenty, twenty_draws]
    assert rows == before and ranked_winrate_rows(rows)[0] is twenty
