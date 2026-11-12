"""Opponent identities, latest-day scope and observed-team deduplication."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

import pytest

from battle_ui.data import BattleQueryTimeout
from battle_ui.opponents import OpponentRoster
from parser.report_archive import ReportArchive
from parser.report_semantics import SemanticArchive, SemanticMapper


NS = "server:1/scenario:18/avatar:100"
OTHER = "server:2/scenario:18/avatar:300"
CHINA = timezone(timedelta(hours=8))


def stamp(day="2026-11-08", hour=12):
    return datetime.fromisoformat(day).replace(tzinfo=CHINA, hour=hour).timestamp()


def army(uid=100, name="自己", union=10, grades=(0, 0, 0), hero_ids=(4012, 4003, 4020)):
    members = []
    for ordinal, (grade, hero) in enumerate(zip(grades, hero_ids)):
        value = {"heros_id": hero, "index": ordinal, "hero_lv": 0,
                 "skills": [{"skillId": 3001, "level": 0, "orderLevel": 2}]}
        if grade != "missing":
            value["evolutionNum"] = grade
        members.append(value)
    return {"army_id": 100000 + uid, "avatar_data": {"avatar_id": uid, "avatar_name": name,
            "avatar_union_id": union}, "army_member": members}


def payload(bid="1001", *, timestamp=None, owner=None, enemy=None, partial=False, own_defender=False):
    owner = owner if owner is not None else army()
    enemy = enemy if enemy is not None else army(200, "对手", 20, (5, 3, 1))
    settlement = {"attacker": enemy if own_defender else owner,
                  "defensive": owner if own_defender else enemy}
    data = {"battleID": bid, "endTimeStampTime": timestamp or stamp(),
            "timeStamp": (timestamp or stamp()) - 10, "battleSettlement": settlement,
            "eventDataList": [{"eventId": 0, "intList": [], "tab": 0}]}
    return {"battleID": bid, "captureVersion": 2, "source": "all", "eventCount": 1,
            "data": data, "components": {"basic": copy.deepcopy(data), "detail": copy.deepcopy(data),
                "all": copy.deepcopy(data), "statics": {},
                "snapshots": None if partial else {"isComplete": True, "battleSnapData": {}}},
            "componentStatus": {key: {"available": not (partial and key == "snapshots"),
                "complete": not (partial and key == "snapshots")} for key in
                ("basic", "detail", "all", "statics", "snapshots")}, "serializationIssues": []}


def put(archive, value, namespace=NS):
    return archive.import_bytes(json.dumps(value, ensure_ascii=False).encode(), namespace=namespace)


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "opponents.db"
    archive = ReportArchive(path)
    yield path, archive
    archive.close()


def test_missing_database_does_not_create_it(tmp_path):
    path = tmp_path / "missing" / "archive.db"
    result = OpponentRoster(path).summary()
    assert result["available"] is False and result["players"] == []
    assert not path.exists() and not path.parent.exists()


def test_latest_battle_day_uses_beijing_date_not_import_order(store):
    path, archive = store
    put(archive, payload("1002", timestamp=stamp("2026-11-09", 0)))
    put(archive, payload("1001", timestamp=stamp("2026-11-08", 23)))
    result = OpponentRoster(path).summary(NS)
    assert result["day"] == "2026-11-09"
    assert result["counts"]["latest_day_reports"] == 1
    assert result["players"][0]["teams"][0]["battle_uid"] == "1002"


def test_end_time_is_preferred_to_start_time_across_components(store):
    path, archive = store
    value = payload()
    value["data"]["endTimeStampTime"] = None
    value["data"]["timeStamp"] = stamp("2026-11-07")
    value["components"]["all"]["endTimeStampTime"] = stamp("2026-11-09")
    put(archive, value)
    result = OpponentRoster(path).summary(NS)
    assert result["day"] == "2026-11-09"
    assert result["players"][0]["teams"][0]["source_time_pointer"] == "/components/all/endTimeStampTime"


def test_thresholds_keep_observed_heroes_skills_zero_levels_and_sources(store):
    path, archive = store
    put(archive, payload())
    result = OpponentRoster(path).summary(NS)
    player = result["players"][0]
    assert player["player_uid"] == "200" and player["player_name"] == "对手"
    team = player["teams"][0]
    assert set(team["tags"]) == {"高品（进阶≥3）", "单将满红"}
    assert team["team_complete"] and team["side"] == "defensive"
    assert team["heroes"][0]["hero_uid"] == "4012"
    assert team["heroes"][0]["evolutionNum"] == 5
    assert team["heroes"][0]["level"] == 0
    assert team["heroes"][0]["skills"][0] == {"skill_uid": "3001", "name_zh": None,
                                            "level": 0, "order_level": 2}


def test_whole_team_requires_exactly_three_identified_five_grade_heroes(store):
    path, archive = store
    put(archive, payload("1001", enemy=army(200, "全满", 20, (5, 5, 5))))
    put(archive, payload("1002", enemy=army(201, "单将", 20, (5,), (4012,))))
    result = OpponentRoster(path).summary(NS)
    players = {row["player_uid"]: row for row in result["players"]}
    assert "整队满红" in players["200"]["tags"]
    assert "整队满红" not in players["201"]["tags"]
    assert result["counts"]["whole_team_full_red_teams"] == 1
    assert result["coverage"]["incomplete_teams"] == 1


def test_unknown_and_boolean_evolution_are_not_zero_or_high_grade(store):
    path, archive = store
    put(archive, payload(enemy=army(200, "未知", 20, (0, "missing", True))))
    result = OpponentRoster(path).summary(NS)
    assert result["players"] == []
    assert result["coverage"]["unknown_evolution_heroes"] == 2
    assert result["counts"]["opponents_observed"] == 1


def test_same_team_retains_latest_observation_before_applying_threshold(store):
    path, archive = store
    put(archive, payload("1001", timestamp=stamp(hour=10), enemy=army(200, "旧名", 20, (5, 5, 5))))
    put(archive, payload("1002", timestamp=stamp(hour=11), enemy=army(200, "新名", 20, (1, 1, 1))))
    result = OpponentRoster(path).summary(NS)
    assert result["players"] == []  # Cannot call the older rank its latest team.
    assert result["coverage"]["deduplicated_team_observations"] == 1


def test_same_player_distinct_compositions_remain_distinct_teams(store):
    path, archive = store
    put(archive, payload("1001", timestamp=stamp(hour=10)))
    put(archive, payload("1002", timestamp=stamp(hour=11), enemy=army(200, "新名", 20, (4, 4, 4), (5001, 5002, 5003))))
    result = OpponentRoster(path).summary(NS)
    assert len(result["players"]) == 1 and result["players"][0]["team_count"] == 2
    assert result["players"][0]["player_name"] == "新名"
    assert result["players"][0]["teams"][0]["battle_uid"] == "1002"


def test_current_snapshot_only_prevents_historical_version_duplication(store):
    path, archive = store
    put(archive, payload(partial=True, enemy=army(200, "旧", 20, (5, 5, 5))))
    put(archive, payload(enemy=army(200, "新", 20, (3, 3, 3))))
    result = OpponentRoster(path).summary(NS)
    assert result["counts"]["reports"] == 1 and result["counts"]["teams"] == 1
    assert "单将满红" not in result["players"][0]["tags"]


def test_owner_on_defending_side_makes_attacker_the_opponent(store):
    path, archive = store
    put(archive, payload(own_defender=True, owner=army(grades=(5, 5, 5))))
    result = OpponentRoster(path).summary(NS)
    assert [row["player_uid"] for row in result["players"]] == ["200"]
    assert result["players"][0]["teams"][0]["side"] == "attacker"


def test_unique_same_day_owner_union_identifies_allied_side(store):
    path, archive = store
    put(archive, payload("1001", enemy=army(-1, "系统", 0, (5, 5, 5))))
    put(archive, payload("1002", owner=army(101, "同盟成员", 10)))
    result = OpponentRoster(path).summary(NS)
    assert [row["player_uid"] for row in result["players"]] == ["200"]
    assert result["players"][0]["teams"][0]["opponent_basis"] == "same_day_unique_owner_union"
    assert result["coverage"]["npc_opponent_reports"] == 1


def test_conflicting_owner_unions_do_not_guess_enemy_in_union_report(store):
    path, archive = store
    put(archive, payload("1001", enemy=army(-1, "系统", 0)))
    put(archive, payload("1002", owner=army(union=11), enemy=army(-1, "系统", 0)))
    put(archive, payload("1003", owner=army(101, "同盟成员", 10)))
    result = OpponentRoster(path).summary(NS)
    assert result["players"] == []
    assert result["coverage"]["unresolved_side_reports"] == 1


def test_unrelated_battle_without_owner_evidence_is_not_enemy_list(store):
    path, archive = store
    put(archive, payload(owner=army(101, "不明阵营", 10)))
    result = OpponentRoster(path).summary(NS)
    assert result["players"] == [] and result["coverage"]["unresolved_side_reports"] == 1


def test_owner_on_both_sides_is_unresolved_even_if_union_matches_one_side(store):
    path, archive = store
    put(archive, payload("1001", enemy=army(-1, "系统", 0)))
    put(archive, payload("1002", enemy=army(100, "自己另一侧", 20, (5, 5, 5))))
    result = OpponentRoster(path).summary(NS)
    assert result["players"] == []
    assert result["coverage"]["unresolved_side_reports"] == 1


@pytest.mark.parametrize("raw_uid", [0, -1, "0", "-12"])
def test_npc_is_excluded_even_with_five_grade_heroes(store, raw_uid):
    path, archive = store
    enemy = army(0, "NPC", 0, (5, 5, 5))
    enemy["avatar_data"]["avatar_id"] = raw_uid
    put(archive, payload(enemy=enemy))
    result = OpponentRoster(path).summary(NS)
    assert result["players"] == [] and result["coverage"]["npc_opponent_reports"] == 1
    assert result["coverage"]["unknown_opponent_id_reports"] == 0


@pytest.mark.parametrize("raw_uid", [None, "", "missing-id", "1.5", True])
def test_unknown_opponent_id_is_not_counted_as_npc(store, raw_uid):
    path, archive = store
    enemy = army(200, "未知ID", 20, (5, 5, 5))
    enemy["avatar_data"]["avatar_id"] = raw_uid
    put(archive, payload(enemy=enemy))
    result = OpponentRoster(path).summary(NS)
    assert result["players"] == []
    assert result["coverage"]["unknown_opponent_id_reports"] == 1
    assert result["coverage"]["npc_opponent_reports"] == 0


@pytest.mark.parametrize("raw_ids", [(0, None), (0, -1), (None, "broken-id")])
def test_defender_aliases_keep_npc_and_unknown_id_states_distinct(store, raw_ids):
    path, archive = store
    enemy = army(200, "冲突ID", 20, (5, 5, 5))
    enemy["avatar_data"]["avatar_id"] = raw_ids[0]
    alias = copy.deepcopy(enemy)
    alias["avatar_data"]["avatar_id"] = raw_ids[1]
    value = payload(enemy=enemy)
    for container in [value["data"], *[value["components"][key] for key in ("basic", "detail", "all")]]:
        container["battleSettlement"]["defender"] = copy.deepcopy(alias)
    put(archive, value)
    result = OpponentRoster(path).summary(NS)
    assert result["players"] == []
    assert result["coverage"]["unresolved_side_reports"] == 1
    assert result["coverage"]["npc_opponent_reports"] == 0
    assert result["coverage"]["unknown_opponent_id_reports"] == 0


def test_namespace_scopes_and_same_numeric_player_ids_are_separate(store):
    path, archive = store
    put(archive, payload())
    put(archive, payload(owner=army(300), enemy=army(200, "另一服", 20, (5, 3, 1))), OTHER)
    result = OpponentRoster(path).summary()
    assert len(result["players"]) == 2
    assert len(OpponentRoster(path).summary(NS)["players"]) == 1


def test_conflicting_defender_aliases_are_unresolved_not_two_opponents(store):
    path, archive = store
    value = payload()
    for container in [value["data"], *[value["components"][key] for key in ("basic", "detail", "all")]]:
        container["battleSettlement"]["defender"] = army(201, "冲突", 21, (5, 5, 5))
    put(archive, value)
    result = OpponentRoster(path).summary(NS)
    assert result["players"] == [] and result["coverage"]["unresolved_side_reports"] == 1


def test_summary_is_read_only_and_does_not_change_database(store):
    path, archive = store
    put(archive, payload())
    archive.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    OpponentRoster(path).summary(NS)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_invalid_time_never_falls_back_to_import_date(store):
    path, archive = store
    value = payload()
    for container in [value["data"], *[value["components"][key] for key in ("basic", "detail", "all")]]:
        container["endTimeStampTime"] = None
        container["timeStamp"] = 0
    put(archive, value)
    result = OpponentRoster(path).summary(NS)
    assert result["day"] is None and result["players"] == []
    assert result["coverage"]["invalid_time_reports"] == 1


def test_query_deadline_is_bounded(store):
    path, archive = store
    put(archive, payload())
    with pytest.raises(BattleQueryTimeout):
        OpponentRoster(path, timeout_seconds=1e-9).summary(NS)


def test_metadata_uses_current_pointer_index_when_large_payload_columns_are_forbidden(store, monkeypatch):
    path, archive = store
    put(archive, payload("1001", timestamp=stamp("2026-11-08")))
    put(archive, payload("1002", timestamp=stamp("2026-11-09"), enemy=army(201, "最新对手", 20, (5, 5, 5))))
    service = OpponentRoster(path)
    original = service.data._connection
    statements = []
    @contextmanager
    def guarded():
        with original() as connection:
            def authorize(action, table, column, _database, _trigger):
                if action == sqlite3.SQLITE_READ and ((table == "report_snapshots" and column == "payload_json")
                        or (table == "report_files" and column == "raw_bytes")):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            connection.set_trace_callback(statements.append)
            yield connection
    monkeypatch.setattr(service.data, "_connection", guarded)
    result = service.summary(NS)
    assert result["day"] == "2026-11-09"
    assert [row["player_uid"] for row in result["players"]] == ["201"]
    field_queries = [statement.lower() for statement in statements if "report_fields" in statement.lower()]
    assert field_queries and all("sqlite_autoindex_report_fields_1" in statement for statement in field_queries)
    assert all("snapshot_id" in statement and "pointer" in statement for statement in field_queries)


def test_names_are_resolved_only_after_latest_composition_deduplication(store, monkeypatch):
    path, archive = store
    older = put(archive, payload("1001", timestamp=stamp(hour=10)))
    latest = put(archive, payload("1002", timestamp=stamp(hour=11)))
    calls = []
    def names(_connection, snapshot_id, _cache, **_kwargs):
        calls.append(snapshot_id)
        return {("hero", "4012"): "最新武将名"}
    monkeypatch.setattr(OpponentRoster, "_names", staticmethod(names))
    result = OpponentRoster(path).summary(NS)
    assert calls == latest["snapshot_ids"] and calls != older["snapshot_ids"]
    assert result["players"][0]["teams"][0]["heroes"][0]["name_zh"] == "最新武将名"


def test_filtered_low_grade_teams_never_read_name_meanings(store, monkeypatch):
    path, archive = store
    put(archive, payload(enemy=army(200, "低红", 20, (1, 1, 1))))
    def names(*_args, **_kwargs):
        raise AssertionError("Excluded team must not load semantic names")
    monkeypatch.setattr(OpponentRoster, "_names", staticmethod(names))
    assert OpponentRoster(path).summary(NS)["players"] == []


def test_names_use_exact_settlement_pointers_and_keep_conflicting_names_unknown(store):
    _path, archive = store
    sid = put(archive, payload())["snapshot_ids"][0]
    mapper = SemanticMapper("opponent-names", {},
        lambda kind, value: {"mapped": True, "status": "mapped", "name_zh": f"{kind}-{value}"},
        lambda *_args: {"status": "unmapped"})
    assert SemanticArchive(archive.connection, mapper).enrich_snapshot(sid)["status"] == "ready"
    # The same observed hero has conflicting mappings in two retained
    # settlement components. No event-history scan is needed to detect it.
    with archive.connection:
        archive.connection.execute("UPDATE report_id_meanings SET name_zh='冲突名字' WHERE snapshot_id=? AND pointer=?",
            (sid, "/components/basic/battleSettlement/defensive/army_member/0/heros_id"))
    statements = []
    archive.connection.set_trace_callback(statements.append)
    names = OpponentRoster._names(archive.connection, sid, {})
    archive.connection.set_trace_callback(None)
    assert ("hero", "4012") not in names
    assert names[("hero", "4003")] == "hero-4003"
    assert names[("skill", "3001")] == "skill-3001"
    lookups = [statement.lower() for statement in statements if "from report_id_meanings" in statement.lower()]
    assert lookups and all("pointer in (" in statement and "sqlite_autoindex_report_id_meanings_1" in statement for statement in lookups)
