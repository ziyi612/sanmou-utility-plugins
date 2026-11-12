"""Daily statistics preserve source zeros, date provenance and family boundaries."""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import time
import sqlite3
from contextlib import contextmanager

import pytest

from battle_ui.daily import DailyStats
from battle_ui.data import BattleQueryTimeout
from parser.report_archive import ReportArchive


NS = "server:1/scenario:18/avatar:100"
ABSENT = object()


def stamp(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def report(uid="1", *, player=100, name="甲", merit=0, child_count=0, parent=None,
           scopes=("personal",), end="2026-11-07T15:00:00Z", start=None, complete=True):
    army = {"army_id": int(player) * 10 if isinstance(player, int) else 10,
            "avatar_data": {"avatar_id": player, "avatar_name": name}, "army_member": [],
            "originZgNum": 999, "barbarianPoint": 777}
    if merit is not ABSENT:
        army["zgNum"] = merit
    data = {"battleID": str(uid), "eventDataList": [{"eventId": 100, "intList": [], "tab": 0}],
            "battleSettlement": {"attacker": army}}
    if child_count is not ABSENT:
        data["childNum"] = child_count
    if end is not ABSENT:
        data["endTimeStampTime"] = stamp(end) if isinstance(end, str) else end
    if start is not None:
        data["timeStamp"] = stamp(start) if isinstance(start, str) else start
    observations = [{"scope": scope, "parent": parent, "row": {"childNum": child_count}}
                    for scope in scopes if child_count is not ABSENT]
    value = {"battleID": str(uid), "source": "all", "data": data, "captureVersion": 2,
             "eventCount": 1, "captureComplete": complete, "serializationIssues": [], "discovery": observations,
             "components": {name: copy.deepcopy(data) if name in ("basic", "all", "detail") else {}
                            for name in ("basic", "all", "detail", "statics", "snapshots")},
             "componentStatus": {name: {"available": True, "complete": True}
                                 for name in ("basic", "all", "detail", "statics", "snapshots")}}
    return value


@pytest.fixture
def archive(tmp_path):
    with ReportArchive(tmp_path / "daily.db") as value:
        yield value


def add(archive, value, namespace=NS):
    result = archive.import_bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"), namespace=namespace)
    assert result["status"] == "imported", result
    return result["snapshot_ids"][0]


def summary(archive, **kwargs):
    return DailyStats(archive.db_path).summary(**kwargs)


def test_uses_beijing_end_time_not_import_date_and_scopes_can_overlap(archive):
    add(archive, report(end="2026-11-07T16:00:01Z", start="2026-11-07T15:59:50Z", scopes=("personal", "union"), merit=8))
    value = summary(archive)
    assert value["dates"] == ["2026-11-08"]
    assert value["days"][0]["total"] == 1
    assert value["days"][0]["personal"] == value["days"][0]["union"] == 1
    assert value["players"][0]["date"] == "2026-11-08"
    assert value["records"][0]["date_source_pointer"] == "/data/endTimeStampTime"


def test_current_best_snapshot_only_and_namespace_identity(archive):
    add(archive, report(merit=2, complete=False))
    add(archive, report(merit=5, complete=True))
    add(archive, report(merit=9), namespace="another-account")
    value = summary(archive)
    assert value["totals"]["reports"] == 2 and value["totals"]["known_merit"] == 14
    assert len(value["players"]) == 2
    assert summary(archive, namespace=NS)["totals"]["known_merit"] == 5


def test_zero_is_known_but_missing_does_not_use_origin_or_barbarian_points(archive):
    add(archive, report("1", merit=0))
    add(archive, report("2", merit=ABSENT))
    value = summary(archive)
    player = value["players"][0]
    assert player["battles"] == 2 and player["known_merit"] == 0
    assert player["known_merit_reports"] == 1 and player["unknown_merit_reports"] == 1
    assert not player["data_complete"]


@pytest.mark.parametrize("merit", (None, True, "12"))
def test_unrecognized_merit_remains_unknown(archive, merit):
    add(archive, report(merit=merit))
    value = summary(archive)
    assert value["players"][0]["known_merit"] is None
    assert value["players"][0]["unknown_merit_reports"] == 1


def test_parent_summary_does_not_duplicate_child_merit(archive):
    add(archive, report("100", merit=999, child_count=2))
    add(archive, report("101", merit=5, parent="100"))
    add(archive, report("102", merit=7, parent="100"))
    value = summary(archive)
    assert value["days"][0]["total"] == 3 and value["days"][0]["settlement_reports"] == 2
    player = value["players"][0]
    assert player["battles"] == 2 and player["known_merit"] == 12
    assert player["excluded_parent_reports"] == 1
    assert value["coverage"]["incomplete_families"] == 0
    assert player["data_complete"]


def individual_root(uid="100", *, merit=2142, child_count=2, parent=None, union=False):
    value = (union_report if union else report)(uid, merit=merit, child_count=child_count, parent=parent)
    for component in (value["data"], *(value["components"][key] for key in ("basic", "detail", "all"))):
        component["battleJobId"] = int(uid) * 17
        settlement = component["battleSettlement"]
        settlement["winer"] = settlement["attacker"]["army_id"]
        settlement["defensive"] = {"army_id": 3000, "zgNum": 0, "army_member": [],
            "avatar_data": {"avatar_id": 300, "avatar_name": "对手", "avatar_union_id": 3}}
    return value


def test_individual_continuous_root_counts_its_own_merit_not_rolling_total(archive):
    root = individual_root()
    root["discovery"][0]["row"]["otherInfo"] = {"continuousInfo": {"zgNum": 11037}}
    add(archive, root)
    add(archive, report("101", merit=5043, parent="100"))
    add(archive, report("102", merit=3852, parent="100"))
    value = summary(archive, player_search="100")
    player = value["players"][0]
    assert player["battles"] == 3 and player["known_merit"] == 11037
    assert player["known_merit_reports"] == 3 and player["excluded_parent_reports"] == 0
    assert value["days"][0]["settlement_reports"] == 3
    assert value["coverage"]["excluded_parent_reports"] == 0
    assert value["coverage"]["missing_children"] == 0
    assert player["data_complete"]


def test_a_fight_seen_as_both_root_and_child_is_counted_once(archive):
    add(archive, individual_root("100", merit=2142, child_count=2))
    middle = individual_root("101", merit=5043, child_count=1, parent="100")
    middle["discovery"].append({"scope": "personal", "parent": None, "row": {"childNum": 1}})
    add(archive, middle)
    child = report("102", merit=3852, parent="101")
    child["discovery"].append({"scope": "union", "parent": "100", "row": {"childNum": 0}})
    add(archive, child)
    value = summary(archive, player_search="100")
    assert value["players"][0]["battles"] == 3
    assert value["players"][0]["known_merit"] == 11037
    assert value["coverage"]["incomplete_families"] == 0
    assert len({(r["namespace"], r["battle_uid"], r["player_uid"])
                for r in value["records"] if r["player_uid"] == "100"}) == 3


@pytest.mark.parametrize("missing_proof", ("events", "job", "winner", "basic_only", "conflict"))
def test_parent_requires_actual_independent_settlement_evidence(archive, missing_proof):
    value = individual_root(merit=999, child_count=1)
    components = [value["data"], *(value["components"][key] for key in ("basic", "detail", "all"))]
    for component in components:
        if missing_proof == "events":
            component["eventDataList"] = []
        elif missing_proof == "job":
            component.pop("battleJobId")
        elif missing_proof == "winner":
            component["battleSettlement"]["winer"] = -1
    if missing_proof == "events":
        value["eventCount"] = 0
    elif missing_proof == "basic_only":
        value["source"] = "basic"
    elif missing_proof == "conflict":
        value["components"]["basic"]["battleJobId"] += 1
    add(archive, value)
    add(archive, report("101", merit=5, parent="100"))
    result = summary(archive, player_search="100")
    assert result["players"][0]["battles"] == 1
    assert result["players"][0]["known_merit"] == 5
    assert result["players"][0]["excluded_parent_reports"] == 1


def test_individual_root_with_missing_children_keeps_known_merit_and_missing_coverage(archive):
    add(archive, individual_root(merit=2142, child_count=2))
    add(archive, report("101", merit=5043, parent="100"))
    result = summary(archive, player_search="100")
    assert result["players"][0]["known_merit"] == 7185
    assert result["players"][0]["missing_children"] == 1
    assert not result["players"][0]["data_complete"]


def test_individual_root_can_have_an_event_only_detail_component(archive):
    value = individual_root(merit=2142, child_count=1)
    value["components"]["detail"] = {"battleID": "100", "eventDataList": value["data"]["eventDataList"]}
    add(archive, value)
    add(archive, report("101", merit=5043, parent="100"))
    result = summary(archive, player_search="100")
    assert result["players"][0]["battles"] == 2
    assert result["players"][0]["known_merit"] == 7185


def test_missing_children_are_explicit_not_assumed_zero(archive):
    add(archive, report("100", merit=100, child_count=3))
    add(archive, report("101", merit=5, parent="100"))
    value = summary(archive)
    assert value["players"][0]["known_merit"] == 5
    assert value["players"][0]["missing_children"] == 2
    assert not value["players"][0]["data_complete"]
    assert value["coverage"]["missing_children"] == 2
    assert value["coverage"]["account_wide_complete"] is False


def test_missing_parent_reference_is_not_full_family_coverage(archive):
    add(archive, report("101", merit=5, parent="100"))
    value = summary(archive)
    assert value["players"][0]["known_merit"] == 5
    assert value["players"][0]["missing_parent_reports"] == 1
    assert not value["players"][0]["data_complete"]


def test_npc_and_missing_player_id_are_excluded(archive):
    add(archive, report("1", player=0, merit=100))
    add(archive, report("2", player=-1, merit=200))
    add(archive, report("3", player="unknown", merit=300))
    value = summary(archive)
    assert value["players"] == [] and value["totals"]["known_merit"] is None
    assert value["coverage"]["npc_sides_excluded"] == 2
    assert value["coverage"]["unidentified_sides_excluded"] == 1


def test_missing_or_zero_timestamp_is_not_replaced_by_import_time(archive):
    add(archive, report("1", end=0, start=0))
    add(archive, report("2", end=ABSENT))
    value = summary(archive)
    assert value["dates"] == [] and value["days"] == []
    assert value["coverage"]["unknown_date_reports"] == 2


def test_unknown_family_metadata_is_explicit(archive):
    add(archive, report(child_count=ABSENT, merit=10))
    value = summary(archive)
    assert value["players"][0]["battles"] == 0 and value["players"][0]["known_merit"] is None
    assert value["players"][0]["unknown_family_reports"] == 1


def test_same_player_alias_side_deduplicates_but_distinct_armies_are_unknown(archive):
    value = report("1", merit=9)
    for component in (value["data"], value["components"]["basic"], value["components"]["all"], value["components"]["detail"]):
        component["battleSettlement"]["defensive"] = copy.deepcopy(component["battleSettlement"]["attacker"])
    add(archive, value)
    first = summary(archive)
    assert first["players"][0]["battles"] == 1 and first["players"][0]["known_merit"] == 9
    second = report("2", merit=9)
    for component in (second["data"], second["components"]["basic"], second["components"]["all"], second["components"]["detail"]):
        army = copy.deepcopy(component["battleSettlement"]["attacker"])
        army["army_id"] += 1
        component["battleSettlement"]["defensive"] = army
    add(archive, second)
    result = summary(archive)
    assert result["players"][0]["battles"] == 2
    assert result["players"][0]["known_merit"] == 9
    assert result["players"][0]["unknown_merit_reports"] == 1


def test_same_nickname_different_ids_do_not_merge_and_search_is_read_only(archive):
    add(archive, report("1", player=100, name="同名", merit=5))
    add(archive, report("2", player=200, name="同名", merit=7))
    before = archive.connection.total_changes
    value = summary(archive, player_search="同名")
    assert {row["player_uid"] for row in value["players"]} == {"100", "200"}
    assert len(summary(archive, player_search="200")["players"]) == 1
    assert archive.connection.total_changes == before


def test_day_filter_validates_and_missing_database_is_not_created(tmp_path):
    data = DailyStats(tmp_path / "absent.db")
    assert not data.summary()["available"] and not data.data.db_path.exists()
    for invalid in ("20261107", "2026-02-30", "bad", 1):
        with pytest.raises(ValueError):
            data.summary(day=invalid)


def test_query_deadline_is_bounded(archive):
    add(archive, report())
    with pytest.raises(BattleQueryTimeout):
        DailyStats(archive.db_path, timeout_seconds=.000001).summary()


def test_all_days_keeps_one_player_identity_and_counts_player_day_rows(archive):
    add(archive, report("1", merit=5, end="2026-11-07T10:00:00Z"))
    add(archive, report("2", merit=7, end="2026-11-08T10:00:00Z"))
    value = summary(archive)
    assert value["totals"]["players"] == 1 and value["totals"]["player_days"] == 2
    assert value["totals"]["known_merit"] == 12
    only = summary(archive, day="2026-11-08")
    assert len(only["days"]) == 1 and only["days"][0]["total"] == 1
    assert only["players"][0]["known_merit"] == 7


def test_end_time_in_basic_is_preferred_to_start_time_in_main_data(archive):
    value = report("1", end=ABSENT, start="2026-11-07T15:00:00Z")
    value["components"]["basic"]["endTimeStampTime"] = stamp("2026-11-07T16:00:00Z")
    add(archive, value)
    result = summary(archive)
    assert result["dates"] == ["2026-11-08"]
    assert result["records"][0]["date_source_pointer"] == "/components/basic/endTimeStampTime"


def test_excess_discovery_history_and_cycles_are_explicitly_uncertain(archive):
    value = report("1", merit=5)
    value["discovery"] *= 130
    add(archive, value)
    result = summary(archive)
    assert result["coverage"]["observations_truncated_reports"] == 1
    assert result["players"][0]["known_merit"] is None
    cyclic = report("2", merit=7, parent="2")
    add(archive, cyclic)
    result = summary(archive)
    assert result["warnings"] and not result["coverage"]["account_wide_complete"]


def test_partial_report_does_not_claim_complete_merit_coverage(archive):
    add(archive, report(merit=5, complete=False))
    result = summary(archive)
    assert result["players"][0]["known_merit"] == 5
    assert result["players"][0]["partial_reports"] == 1
    assert not result["players"][0]["data_complete"]


def union_report(uid="1", *, union=2, **kwargs):
    value = report(uid, **kwargs)
    for component in (value["data"], *(value["components"][key] for key in ("basic", "detail", "all"))):
        avatar = component["battleSettlement"]["attacker"]["avatar_data"]
        if union is not ABSENT:
            avatar.update(avatar_union_id=union, avatar_union_name="我盟" if union == 2 else "其他盟")
    return value


def union_service(archive, tmp_path, *, union=2, namespace=NS, updated_at=None, identity=None):
    runtime = tmp_path / "runtime"
    folder = runtime / hashlib.sha256(namespace.encode()).hexdigest()[:20]
    folder.mkdir(parents=True, exist_ok=True)
    state = {"namespace": namespace, "runtimeIdentity": identity or
             {"serverId": "1", "scenarioId": "18", "avatarId": "100"},
             "runtimeCapabilities": {"unionId": union},
             "updatedAt": time.time(),
             "runtimeCapabilitiesObservedAt": time.time() if updated_at is None else updated_at}
    (folder / "collector_state.json").write_text(json.dumps(state), encoding="utf-8")
    return DailyStats(archive.db_path, own_union_only=True, runtime_dir=runtime)


def test_own_union_uses_numeric_membership_and_excludes_enemy_merit_and_reports(archive, tmp_path):
    value = union_report("1", player=200, name="同名", merit=7, scopes=("personal", "star"))
    for component in (value["data"], *(value["components"][key] for key in ("basic", "detail", "all"))):
        enemy = copy.deepcopy(component["battleSettlement"]["attacker"])
        enemy.update(army_id=3000, zgNum=99)
        enemy["avatar_data"].update(avatar_id=300, avatar_union_id=3, avatar_union_name="其他盟")
        component["battleSettlement"]["defensive"] = enemy
    add(archive, value)
    add(archive, union_report("2", player=300, name="同名", union=3, merit=100, scopes=("union",)))
    add(archive, union_report("3", player=100, union=0, merit=50))
    before = archive.connection.total_changes
    result = union_service(archive, tmp_path).summary(namespace=NS)
    assert result["totals"]["reports"] == 1 and result["totals"]["known_merit"] == 7
    assert {row["player_uid"] for row in result["players"]} == {"200"}
    assert result["days"][0]["personal"] == result["days"][0]["star"] == 1
    assert result["union_filter"]["enemy_sides_excluded"] == 3
    assert result["union_filter"]["excluded_reports"] == 2
    assert result["union_filter"]["contexts"][NS]["union_id"] == "2"
    assert all(row["player_uid"] == "200" for row in result["records"])
    assert archive.connection.total_changes == before


@pytest.mark.parametrize("union", (0, True, -1, "2"))
def test_departed_or_invalid_runtime_union_never_uses_historical_membership(archive, tmp_path, union):
    add(archive, union_report(merit=9))
    result = union_service(archive, tmp_path, union=union).summary(namespace=NS)
    assert result["players"] == [] and result["totals"]["known_merit"] is None
    assert result["totals"]["reports"] == 0
    assert result["union_filter"]["contexts"][NS]["status"] == ("no_union" if union == 0 and union is not False else "unknown")


def test_missing_stale_or_wrong_account_context_does_not_guess_from_union_scope(archive, tmp_path):
    add(archive, union_report(merit=9, scopes=("union",)))
    assert DailyStats(archive.db_path, own_union_only=True).summary(namespace=NS)["players"] == []
    stale = union_service(archive, tmp_path, updated_at=time.time() - 601).summary(namespace=NS)
    assert stale["players"] == []
    other = union_service(archive, tmp_path, identity={"serverId": "1", "scenarioId": "18", "avatarId": "200"}).summary(namespace=NS)
    assert other["players"] == []


def test_generic_state_save_time_cannot_refresh_an_old_union_observation(archive, tmp_path):
    add(archive, union_report(merit=9))
    service = union_service(archive, tmp_path)
    path = service.runtime_dir / hashlib.sha256(NS.encode()).hexdigest()[:20] / "collector_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state.pop("runtimeCapabilitiesObservedAt")
    state.update(updatedAt=time.time(), lastCycle={"startedAt": time.time() - 601})
    path.write_text(json.dumps(state), encoding="utf-8")
    assert service.summary(namespace=NS)["players"] == []
    state["lastCycle"]["startedAt"] = time.time()
    path.write_text(json.dumps(state), encoding="utf-8")
    assert service.summary(namespace=NS)["totals"]["known_merit"] == 9


@pytest.mark.parametrize("union", (ABSENT, True, "2", None))
def test_missing_or_invalid_side_membership_is_excluded_not_guessed(archive, tmp_path, union):
    add(archive, union_report(union=union, merit=9))
    result = union_service(archive, tmp_path).summary(namespace=NS)
    assert result["players"] == []
    assert result["union_filter"]["unknown_union_sides_excluded"] == 1


def test_conflicting_membership_aliases_for_one_player_are_not_counted(archive, tmp_path):
    value = union_report(merit=9)
    for component in (value["data"], *(value["components"][key] for key in ("basic", "detail", "all"))):
        enemy = copy.deepcopy(component["battleSettlement"]["attacker"])
        enemy["avatar_data"]["avatar_union_id"] = 3
        component["battleSettlement"]["defensive"] = enemy
    add(archive, value)
    result = union_service(archive, tmp_path).summary(namespace=NS)
    assert result["players"] == [] and result["union_filter"]["unknown_union_sides_excluded"] == 2


def test_own_union_keeps_zero_merit_and_parent_child_deduplication(archive, tmp_path):
    add(archive, union_report("100", merit=999, child_count=2))
    add(archive, union_report("101", merit=0, parent="100"))
    add(archive, union_report("102", merit=7, parent="100"))
    result = union_service(archive, tmp_path).summary(namespace=NS)
    assert result["players"][0]["battles"] == 2
    assert result["players"][0]["known_merit"] == 7
    assert result["players"][0]["known_merit_reports"] == 2
    assert result["players"][0]["excluded_parent_reports"] == 1
    assert result["players"][0]["data_complete"]


def test_own_union_counts_individual_root_and_still_excludes_enemy(archive, tmp_path):
    add(archive, individual_root(merit=2142, union=True))
    add(archive, union_report("101", merit=5043, parent="100"))
    add(archive, union_report("102", merit=3852, parent="100"))
    result = union_service(archive, tmp_path).summary(namespace=NS)
    assert result["totals"]["known_merit"] == 11037
    assert {row["player_uid"] for row in result["players"]} == {"100"}
    assert result["players"][0]["battles"] == 3
    assert result["players"][0]["excluded_parent_reports"] == 0
    assert result["union_filter"]["enemy_sides_excluded"] == 1
    assert result["players"][0]["data_complete"]


def test_own_union_identity_is_separate_for_each_account(archive, tmp_path):
    other_ns = "server:1/scenario:18/avatar:999"
    add(archive, union_report("1", player=200, union=2, merit=5))
    add(archive, union_report("1", player=300, union=2, merit=90), namespace=other_ns)
    service = union_service(archive, tmp_path)
    union_service(archive, tmp_path, namespace=other_ns, union=7,
                  identity={"serverId": "1", "scenarioId": "18", "avatarId": "999"})
    result = service.summary()
    assert result["totals"]["known_merit"] == 5
    assert {row["player_uid"] for row in result["players"]} == {"200"}


def test_daily_uses_indexed_metadata_when_original_payload_reads_are_forbidden(archive, monkeypatch):
    add(archive, report("1", merit=7))
    service = DailyStats(archive.db_path)
    original_connection = service.data._connection
    statements = []

    @contextmanager
    def guarded_connection():
        with original_connection() as connection:
            def authorize(action, table, column, *_):
                if action == sqlite3.SQLITE_READ and ((table == "report_snapshots" and column == "payload_json")
                        or table in ("report_events", "report_files")):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(service.data, "_connection", guarded_connection)
    result = service.summary(namespace=NS)
    assert result["totals"]["known_merit"] == 7
    assert not any("json_each" in statement.lower() or "payload_json" in statement.lower() for statement in statements)
    field_reads = [statement for statement in statements if "FROM report_fields" in statement or "JOIN report_fields" in statement]
    assert field_reads and all("INDEXED BY sqlite_autoindex_report_fields_1" in statement for statement in field_reads)


@pytest.mark.parametrize("dictionary", (False, True))
def test_daily_observation_cutoff_uses_source_ordinal_including_dictionary_values(archive, dictionary):
    value = report("1", merit=7)
    source = [{"scope": "personal", "parent": None, "row": {"childNum": 0}} for _ in range(130)]
    source[127]["scope"] = "star"
    source[128]["scope"] = "union"  # The 129th proves truncation, but is not classified.
    value["discovery"] = {str(999 - index): item for index, item in enumerate(source)} if dictionary else source
    add(archive, value)
    result = summary(archive, namespace=NS)
    day = result["days"][0]
    assert (day["personal"], day["star"], day["union"]) == (1, 1, 0)
    assert result["coverage"]["observations_truncated_reports"] == 1
    assert result["totals"]["known_merit"] is None
