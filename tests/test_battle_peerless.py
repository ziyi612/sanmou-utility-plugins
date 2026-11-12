"""Client-evaluated peerless occurrences, with identities and source boundaries."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import sqlite3

import pytest

from battle_ui.data import BattleQueryTimeout
from battle_ui.peerless import PeerlessRecords
from parser.report_archive import ReportArchive


NS = "server:1/scenario:18/avatar:100"
OTHER = "server:2/scenario:18/avatar:300"


def stamp(value="2026-11-08T04:00:00Z"):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def army(uid, name):
    return {"army_id": 100000 + (uid if isinstance(uid, int) else 0), "zgNum": 0,
            "avatar_data": {"avatar_id": uid, "avatar_name": name,
                            "avatar_union_id": 20, "avatar_union_name": "同盟甲",
                            "avatar_icon": 123, "avatar_frame": 0}, "army_member": []}


def report(bid="1001", *, achiever=200, legend=True, block="co_1001", timestamp=None,
           namespace=NS, category="normal", ci_type=0, partial=False):
    timestamp = stamp() if timestamp is None else timestamp
    ci = {"avatarId": achiever, "zgNum": 20000, "winBattleNum": 5,
          "killNum": 999999, "isMuti": True, "continuousInfoType": ci_type}
    data = {"battleID": str(bid), "endTimeStampTime": timestamp, "timeStamp": timestamp - 10,
            "battleSettlement": {"attacker": army(100, "己方"),
                                 "defensive": army(achiever, "达成人员")},
            "eventDataList": [{"eventId": 0, "intList": [], "tab": 0}]}
    evaluation = {"version": 1, "evaluator": "client_BattleReportHelper.IsLegend",
                  "status": "evaluated", "legend": legend, "battleID": str(bid),
                  "avatarId": achiever, "isMuti": True, "continuousInfoType": ci_type,
                  "zgNum": 20000, "winBattleNum": 5, "endTimeStamp": timestamp,
                  "category": category, "category_state": "runtime_enum",
                  "rule": {"normalType": 0, "pvBarbarianArmyType": 1,
                           "maxWarContribute": 20000, "maxGame": 5,
                           "bannerMinFeat": 5000, "bannerMinBattles": 2}}
    row = {"combatInfo": copy.deepcopy(data), "childNum": 4,
           "otherInfo": {"continuousInfo": ci}}
    if block is not None:
        row["blockHash"] = block
    discovery = {"scope": "personal", "parent": None, "row": row,
                 "clientPeerless": [evaluation]}
    identity = dict(part.split(":", 1) for part in namespace.split("/"))
    return {"namespace": namespace, "battleID": str(bid), "captureVersion": 2,
            "source": "all", "eventCount": 1, "data": data,
            "runtimeIdentity": {"serverId": identity["server"],
                                "scenarioId": identity["scenario"], "avatarId": identity["avatar"]},
            "components": {"basic": copy.deepcopy(data), "detail": copy.deepcopy(data),
                           "all": copy.deepcopy(data), "statics": {},
                           "snapshots": None if partial else {"isComplete": True, "battleSnapData": {}}},
            "componentStatus": {name: {"available": not (partial and name == "snapshots"),
                                       "complete": not (partial and name == "snapshots")}
                                for name in ("basic", "detail", "all", "statics", "snapshots")},
            "serializationIssues": [], "captureComplete": not partial, "discovery": [discovery]}


def observation(value):
    return value["discovery"][0]


def ci(value):
    return observation(value)["row"]["otherInfo"]["continuousInfo"]


def evaluation(value):
    return observation(value)["clientPeerless"][0]


def put(archive, value, namespace=None):
    result = archive.import_bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"),
                                  namespace=namespace or value["namespace"])
    assert result["status"] in {"imported", "duplicate"}, result
    return result


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "peerless.db"
    with ReportArchive(path) as archive:
        yield path, archive


def summary(store, namespace=NS):
    return PeerlessRecords(store[0]).summary(namespace=namespace)


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing" / "archive.db"
    result = PeerlessRecords(path).summary()
    assert result["available"] is False and result["records"] == []
    assert not path.exists() and not path.parent.exists()


def test_client_true_records_only_the_named_player_and_source_battle(store):
    value = report()
    imported = put(store[1], value)
    result = summary(store)
    assert result["available"] is True and result["namespace"] == NS
    assert len(result["records"]) == 1
    row = result["records"][0]
    assert row["namespace"] == NS and row["battle_uid"] == "1001"
    assert row["snapshot_id"] == imported["snapshot_ids"][0]
    assert row["player_uid"] == "200" and row["player_name"] == "达成人员"
    assert row["side"] in {"defensive", "defender"}
    assert row["day"] == "2026-11-08" and row["timestamp"] == stamp()
    assert row["avatar_data"]["avatar_frame"] == 0
    assert row["completeness"] == "complete"
    assert row["evidence"] and all({"pointer", "value", "kind"} <= set(item) for item in row["evidence"])
    assert any("clientPeerless" in item["pointer"] for item in row["evidence"])


def test_reported_date_source_pointer_resolves_to_the_actual_saved_timestamp(store):
    value = report()
    put(store[1], value)
    row = summary(store)["records"][0]
    current = value
    for token in row["date_source_pointer"].split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    assert current == row["timestamp"]


@pytest.mark.parametrize("legend", [False, None, 0, 1, "true", "false"])
def test_only_strict_boolean_true_can_confirm_peerless(store, legend):
    value = report(legend=legend)
    value["data"]["battleSettlement"]["defensive"]["avatar_data"]["avatar_name"] = "天下无双"
    put(store[1], value)
    assert summary(store)["records"] == []


def test_nickname_skill_or_large_single_battle_values_are_not_peerless_evidence(store):
    value = report()
    observation(value).pop("clientPeerless")
    observation(value)["row"].pop("otherInfo")
    for node in [value["data"], value["components"]["basic"], value["components"]["all"], value["components"]["detail"]]:
        node["battleSettlement"]["defensive"]["zgNum"] = 999999
        node["battleSettlement"]["defensive"]["avatar_data"]["avatar_name"] = "天下无双"
        node["eventDataList"] = [{"eventId": 0, "intList": [1000], "skillName": "无双"}]
    put(store[1], value)
    assert summary(store)["records"] == []


@pytest.mark.parametrize("change", [
    lambda x: x.update(version=2),
    lambda x: x.update(evaluator="guessed_threshold"),
    lambda x: x.update(status="unavailable"),
    lambda x: x.update(category="unknown"),
    lambda x: x.update(category_state="missing_enum"),
    lambda x: x.update(category_state="unknown_value"),
])
def test_unverifiable_evaluator_metadata_never_confirms(store, change):
    value = report()
    change(evaluation(value))
    put(store[1], value)
    assert summary(store)["records"] == []


@pytest.mark.parametrize("field,changed", [("battleID", "9999"), ("avatarId", 100),
    ("zgNum", 20001), ("winBattleNum", 6), ("isMuti", False),
    ("continuousInfoType", 1), ("endTimeStamp", stamp() + 1)])
def test_evaluation_must_match_that_observation_inputs_exactly(store, field, changed):
    value = report()
    evaluation(value)[field] = changed
    put(store[1], value)
    assert summary(store)["records"] == []


def test_runtime_category_enum_not_a_hardcoded_zero_can_identify_normal(store):
    value = report(ci_type=7)
    evaluation(value)["rule"].update(normalType=7, pvBarbarianArmyType=8)
    put(store[1], value)
    assert len(summary(store)["records"]) == 1


def test_absent_ci_type_can_use_the_captured_normal_enum(store):
    value = report()
    ci(value).pop("continuousInfoType")
    put(store[1], value)
    assert len(summary(store)["records"]) == 1


@pytest.mark.parametrize("uid", [0, -1, None, "", "broken", True])
def test_npc_unknown_or_boolean_player_id_is_not_a_person(store, uid):
    value = report(achiever=uid)
    put(store[1], value)
    assert summary(store)["records"] == []


def test_barbarian_legend_is_not_counted_as_normal_peerless(store):
    value = report(category="barbarian", ci_type=1)
    evaluation(value)["status"] = "excluded"
    put(store[1], value)
    assert summary(store)["records"] == []


def test_type_one_cannot_be_labeled_normal_when_captured_enum_says_barbarian(store):
    value = report(ci_type=1)
    put(store[1], value)
    assert summary(store)["records"] == []


def test_continuous_without_client_evaluation_stays_unverified(store):
    value = report()
    observation(value).pop("clientPeerless")
    put(store[1], value)
    result = summary(store)
    assert result["records"] == []
    assert result["coverage"]["unverified_occurrences"] == 1


def test_component_only_continuous_metadata_is_not_reconstructed_from_thresholds(store):
    value = report()
    info = copy.deepcopy(ci(value))
    value["discovery"] = []
    value["components"]["basic"]["otherInfo"] = {"continuousInfo": info}
    put(store[1], value)
    result = summary(store)
    assert result["records"] == []
    assert result["coverage"]["unverified_occurrences"] == 1


def test_nested_row_combat_metadata_is_a_valid_observed_source(store):
    value = report()
    node = observation(value)["row"]
    node["combatInfo"]["otherInfo"] = node.pop("otherInfo")
    put(store[1], value)
    assert len(summary(store)["records"]) == 1


def test_same_occurrence_from_multiple_sources_is_one_with_all_evidence(store):
    value = report()
    second = copy.deepcopy(observation(value))
    second["scope"] = "union"
    third = copy.deepcopy(observation(value))
    third["scope"] = "star"
    value["discovery"] += [second, third]
    put(store[1], value)
    rows = summary(store)["records"]
    assert len(rows) == 1
    pointers = {item["pointer"] for item in rows[0]["evidence"]}
    assert any("/discovery/0/" in item for item in pointers)
    assert any("/discovery/1/" in item for item in pointers)
    assert any("/discovery/2/" in item for item in pointers)


def test_same_person_two_blocks_is_two_occurrences_not_lifetime_player_summary(store):
    put(store[1], report("1001", block="co_1001"))
    put(store[1], report("1002", block="co_1002"))
    rows = summary(store)["records"]
    assert len(rows) == 2 and {x["battle_uid"] for x in rows} == {"1001", "1002"}
    assert {x["player_uid"] for x in rows} == {"200"}


def test_same_block_continuously_updated_across_roots_is_counted_once(store):
    put(store[1], report("1001", block="co_1001"))
    put(store[1], report("1002", block="co_1001", timestamp=stamp() + 20))
    rows = summary(store)["records"]
    assert len(rows) == 1
    assert rows[0]["battle_uid"] == "1002"
    assert len(rows[0]["evidence"]) >= 2


def test_continuous_progress_from_not_peerless_to_peerless_is_not_a_contradiction(store):
    value = report()
    earlier = copy.deepcopy(observation(value))
    earlier["row"]["otherInfo"]["continuousInfo"].update(zgNum=15000, winBattleNum=4)
    earlier["clientPeerless"][0].update(zgNum=15000, winBattleNum=4, legend=False)
    value["discovery"].insert(0, earlier)
    put(store[1], value)
    rows = summary(store)["records"]
    assert len(rows) == 1 and rows[0]["player_uid"] == "200"
    assert rows[0]["merit"] == 20000 and rows[0]["win_battle_num"] == 5


def test_same_report_old_partial_version_and_best_complete_version_do_not_duplicate(store):
    put(store[1], report(partial=True))
    best = put(store[1], report())
    rows = summary(store)["records"]
    assert len(rows) == 1 and rows[0]["snapshot_id"] == best["snapshot_ids"][0]


def test_conflicting_evaluations_for_same_observation_are_not_resolved_by_picking_true(store):
    value = report()
    contradictory = copy.deepcopy(evaluation(value))
    contradictory["legend"] = False
    observation(value)["clientPeerless"].append(contradictory)
    put(store[1], value)
    assert summary(store)["records"] == []


def test_child_reports_do_not_inherit_the_parent_occurrence(store):
    put(store[1], report())
    child = report("1002")
    observation(child)["parent"] = "1001"
    observation(child)["row"].pop("otherInfo")
    observation(child).pop("clientPeerless")
    put(store[1], child)
    rows = summary(store)["records"]
    assert len(rows) == 1 and rows[0]["battle_uid"] == "1001"


def test_another_players_false_evaluation_does_not_mark_them_true(store):
    value = report()
    other = copy.deepcopy(observation(value))
    other["row"]["otherInfo"]["continuousInfo"]["avatarId"] = 100
    other["clientPeerless"][0].update(avatarId=100, legend=False)
    value["discovery"].append(other)
    put(store[1], value)
    rows = summary(store)["records"]
    assert len(rows) == 1 and rows[0]["player_uid"] == "200"


def test_same_numeric_occurrence_on_different_accounts_does_not_merge(store):
    put(store[1], report())
    put(store[1], report(namespace=OTHER))
    all_rows = summary(store, namespace=None)["records"]
    assert len(all_rows) == 2 and {row["namespace"] for row in all_rows} == {NS, OTHER}
    assert len(summary(store)["records"]) == 1


def test_declared_runtime_account_mismatch_does_not_attribute_peerless(store):
    value = report()
    value["runtimeIdentity"]["avatarId"] = "999"
    put(store[1], value)
    assert summary(store)["records"] == []


def test_missing_name_keeps_the_exact_achiever_uid_without_borrowing_other_name(store):
    value = report()
    for node in [value["data"], value["components"]["basic"], value["components"]["all"], value["components"]["detail"], observation(value)["row"]["combatInfo"]]:
        node["battleSettlement"]["defensive"]["avatar_data"].pop("avatar_name")
    put(store[1], value)
    row = summary(store)["records"][0]
    assert row["player_uid"] == "200" and row["player_name"] in {None, "", "姓名未采集", "姓名未记录"}
    assert row["player_name"] != "己方"


def test_fallback_to_root_battle_id_without_block_hash_stays_separate(store):
    put(store[1], report("1001", block=None))
    put(store[1], report("1002", block=None))
    result = summary(store)
    assert len(result["records"]) == 2
    assert result["coverage"]["fallback_battle_id_groups"] == 2


def test_beijing_midnight_and_multiple_days_use_battle_time(store):
    put(store[1], report("1001", timestamp=stamp("2026-11-07T15:59:59Z")))
    put(store[1], report("1002", block="co_1002", timestamp=stamp("2026-11-07T16:00:00Z")))
    result = summary(store)
    assert set(result["dates"]) == {"2026-11-07", "2026-11-08"}
    by_id = {row["battle_uid"]: row["day"] for row in result["records"]}
    assert by_id == {"1001": "2026-11-07", "1002": "2026-11-08"}


def test_zero_merit_and_false_assessment_do_not_become_unknown_or_positive(store):
    value = report(legend=False)
    ci(value).update(zgNum=0, winBattleNum=0)
    evaluation(value).update(zgNum=0, winBattleNum=0)
    put(store[1], value)
    result = summary(store)
    assert result["records"] == []
    assert result["coverage"]["unverified_occurrences"] == 0


def test_invalid_null_input_does_not_match_a_missing_value_as_zero(store):
    value = report()
    ci(value)["zgNum"] = None
    evaluation(value)["zgNum"] = 0
    put(store[1], value)
    assert summary(store)["records"] == []


def test_summary_never_changes_database_bytes(store):
    path, archive = store
    put(archive, report())
    archive.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = PeerlessRecords(path).summary(NS)
    assert result["records"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_query_deadline_is_bounded(store):
    put(store[1], report())
    with pytest.raises(BattleQueryTimeout):
        PeerlessRecords(store[0], timeout_seconds=1e-9).summary(NS)


def test_later_block_hash_resolves_older_same_root_fallback_without_double_count(store):
    value = report()
    older = copy.deepcopy(observation(value))
    older["row"].pop("blockHash")
    value["discovery"].insert(0, older)
    put(store[1], value)
    result = summary(store)
    assert result["counts"]["occurrences"] == 1
    assert result["records"][0]["block_hash"] == "co_1001"
    assert result["coverage"]["fallback_battle_id_groups"] == 0
    pointers = {entry["pointer"] for entry in result["records"][0]["evidence"]}
    assert any("/discovery/0/" in pointer for pointer in pointers)
    assert any("/discovery/1/" in pointer for pointer in pointers)


def test_indexed_metadata_and_participant_fallback_preserve_all_small_evidence_fields(store, monkeypatch):
    path, archive = store
    value = report()
    ci(value)["unknown/a~b"] = {"zero": 0, "false": False, "null": None, "values": list(range(600))}
    avatar = observation(value)["row"]["combatInfo"]["battleSettlement"]["defensive"]["avatar_data"]
    avatar["unknown/a~b"] = {"large_id": 2**90, "zero": 0, "false": False, "null": None, "empty": [[], {}]}
    expected_avatar, expected_ci = copy.deepcopy(avatar), copy.deepcopy(ci(value))
    for node in [value["data"], *[value["components"][key] for key in ("basic", "all", "detail")]]:
        node.pop("battleSettlement")
    put(archive, value)
    service = PeerlessRecords(path)
    original = service.data._connection
    statements = []
    @contextmanager
    def guarded():
        with original() as connection:
            def authorize(action, table, column, _database, _trigger):
                if action == sqlite3.SQLITE_READ and ((table == "report_snapshots" and column == "payload_json")
                        or (table == "report_files" and column == "raw_bytes") or table == "report_events"):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            connection.set_trace_callback(statements.append)
            yield connection
    monkeypatch.setattr(service.data, "_connection", guarded)
    result = service.summary(NS)
    assert result["counts"]["occurrences"] == 1
    row = result["records"][0]
    assert row["avatar_data"] == expected_avatar
    assert any(item["kind"] == "continuous_info" and item["value"] == expected_ci for item in row["evidence"])
    assert row["player_uid"] == "200" and row["player_name"] == "达成人员"
    field_queries = [statement.lower() for statement in statements if "report_fields" in statement.lower()]
    assert field_queries and all("sqlite_autoindex_report_fields_1" in statement for statement in field_queries)
    assert all("snapshot_id" in statement and "pointer" in statement for statement in field_queries)


def test_observations_beyond_128_are_read_and_array_indices_keep_numeric_source_order(store):
    value = report(legend=False)
    original = copy.deepcopy(observation(value))
    value["discovery"] = []
    for index in range(131):
        item = copy.deepcopy(original)
        item["row"]["otherInfo"]["continuousInfo"]["zgNum"] += index
        item["clientPeerless"][0].update(zgNum=20000 + index, legend=index in (9, 130))
        value["discovery"].append(item)
    put(store[1], value)
    result = summary(store)
    assert result["counts"]["occurrences"] == 1
    assert result["records"][0]["merit"] == 20130
    pointers = {item["pointer"] for item in result["records"][0]["evidence"]}
    assert any(pointer.startswith("/discovery/9/") for pointer in pointers)
    assert any(pointer.startswith("/discovery/130/") for pointer in pointers)
