"""The GUI reads current reports without mutating or expanding the archive."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from battle_ui.data import BattleData, BattleDataError, BattleQueryTimeout
from parser.report_archive import ReportArchive
from parser.report_semantics import SemanticArchive, SemanticMapper


def payload(battle_id="1001", complete=True, player="张三100%_x"):
    data = {
        "battleID": battle_id, "timeStamp": 1794041000, "locName": "江陵",
        "eventDataList": [{"eventId": 211, "tab": 0, "intList": [168, 4012, 0]},
                          {"eventId": 99999, "intList": [2**90, None, False]},
                          {"eventId": 212, "intList": []}],
        "battleSettlement": {
            "attacker": {"army_id": 168, "formationId": 1, "morale": 0,
                "avatar_data": {"avatar_id": 0, "avatar_name": player},
                "army_member": [{"heros_id": 4012, "index": 0, "hero_lv": 0,
                    "origin_troop_num": 0, "troop_num": None,
                    "skills": [{"skillId": 300401, "level": 0}]}]},
            "defensive": {"army_id": 99, "avatar_data": {"avatar_name": "李四"},
                          "army_member": [{"heros_id": 55555, "skills": [{"skillId": 99999}]}]},
        },
    }
    result = {"battleID": battle_id, "captureVersion": 2, "source": "all", "eventCount": 3,
              "data": data, "components": {"basic": {}, "detail": {}, "all": copy.deepcopy(data),
                "statics": {}, "snapshots": {"isComplete": True, "battleSnapData": {}}},
              "componentStatus": {name: {"available": True, "complete": True, "requested": True}
                for name in ("basic", "detail", "all", "statics", "snapshots")},
              "serializationIssues": []}
    if not complete:
        result["components"]["snapshots"] = None
        result["componentStatus"]["snapshots"] = {"available": False, "complete": False, "error": "timeout"}
    return result


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def lookup(kind, value):
    names = {("hero", "4012"): "大乔", ("skill", "300401"): "测试战法", ("formation", "1"): "测试阵型"}
    name = names.get((kind, str(value)))
    return {"name_zh": name, "mapped": name is not None, "status": "mapped" if name else "unmapped",
            "source": {"fixture": True}}


def decode(event, context):
    known = event.get("eventId") == 211
    return {"event_name_zh": "测试事件" if known else None,
            "summary_zh": "大乔发生测试事件" if known else None,
            "parameters": [{"name_zh": "武将", "raw_value": 4012, "name_value_zh": "大乔"}]
                if known else event.get("intList"),
            "status": "mapped" if known else "unmapped", "source": {"fixture": True}}


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "archive.db"
    a = ReportArchive(path)
    a.import_bytes(encoded(payload(complete=False)), namespace="one")
    complete = a.import_bytes(encoded(payload()), namespace="one")["snapshot_ids"][0]
    a.import_bytes(encoded(payload("1002", complete=False, player="孙五")), namespace="one")
    a.import_bytes(encoded(payload(player="其他账号")), namespace="two")
    helper = SemanticArchive(a.connection, SemanticMapper("gui-fixture", {"fixture": True}, lookup, decode))
    helper.enrich_snapshot(complete)
    a.close()
    return path, complete


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing" / "archive.db"
    data = BattleData(path)
    result = data.overview()
    assert result["available"] is False and result["total"] == 0
    assert not path.exists() and not path.parent.exists()
    with pytest.raises(BattleDataError):
        data.list_reports()


def test_overview_counts_each_account_report_once_and_is_read_only(archive):
    path, _ = archive
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    data = BattleData(path)
    result = data.overview()
    assert (result["total"], result["complete"], result["partial"], result["events"]) == (3, 2, 1, 9)
    assert next(x for x in result["accounts"] if x["namespace"] == "one")["total"] == 2
    with data._connection() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA temp_store").fetchone()[0] == 2
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM report_records")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_list_filters_current_versions_accounts_and_pages(archive):
    path, sid = archive
    data = BattleData(path)
    first = data.list_reports(namespace="one", limit=1)
    assert len(first["items"]) == 1 and first["has_more"]
    second = data.list_reports(namespace="one", limit=1, offset=1)
    assert len(second["items"]) == 1 and not second["has_more"]
    full = data.list_reports(namespace="one", status="complete", search="1001")
    assert full["search_mode"] == "exact_id" and len(full["items"]) == 1
    row = full["items"][0]
    assert row["snapshot_id"] == sid and row["attacker_name"] == "张三100%_x"
    assert row["defender_name"] == "李四" and row["battle_time"] == 1794041000
    assert row["location"] == "江陵" and row["result"] is None
    assert data.list_reports(search="100")["items"] == []  # full numeric ID, not a substring
    assert len(data.list_reports(search="张三100%_x")["items"]) == 1
    assert data.list_reports(search="' OR 1=1 --")["items"] == []


def test_detail_reads_components_sides_names_and_paged_events(archive):
    path, sid = archive
    data = BattleData(path)
    result = data.detail(sid, event_limit=1)
    assert result["report"]["is_current"] and result["report"]["completeness"] == "complete"
    assert not result["missing_components"] and result["components"]["snapshots"]["complete"]
    assert result["event_total"] == 3 and result["event_has_more"]
    attacker = next(side for side in result["sides"] if side["side"] == "attacker")
    hero = attacker["heroes"][0]
    assert hero["name_zh"] == "大乔" and hero["hero_uid"] == "4012"
    assert hero["level"] == hero["troops_initial"] == 0 and hero["troops_remaining"] is None
    assert hero["skills"][0]["name_zh"] == "测试战法" and hero["skills"][0]["level"] == 0
    event = result["events"][0]
    assert event["event_name_zh"] == "测试事件" and event["parameters"][0]["raw_value"] == 4012
    later = data.detail(sid, event_limit=2, event_offset=1)
    assert not later["event_has_more"]
    assert later["events"][0]["raw"]["intList"] == [2**90, None, False]
    assert later["events"][0]["parameters"] == [2**90, None, False]
    assert later["events"][0]["status"] == "unmapped"


def test_partial_components_and_missing_semantics_are_not_claimed_ready(archive):
    path, _ = archive
    data = BattleData(path)
    sid = data.list_reports(namespace="one", search="1002")["items"][0]["snapshot_id"]
    detail = data.detail(sid)
    assert detail["missing_components"] == ["snapshots"]
    assert detail["components"]["snapshots"]["error"] == "timeout"
    assert detail["semantics"]["status"] == "pending"
    assert detail["events"][0]["event_name_zh"] is None
    assert detail["events"][0]["status"] == "not_generated"
    assert detail["sides"][0]["heroes"][0]["name_zh"] is None


def test_no_semantics_tables_still_displays_raw_events(tmp_path):
    path = tmp_path / "plain.db"
    with ReportArchive(path) as archive:
        sid = archive.import_bytes(encoded(payload()))["snapshot_ids"][0]
    result = BattleData(path).detail(sid)
    assert not result["semantics"]["available"]
    assert result["events"][1]["raw"]["intList"][0] == 2**90


def test_large_fields_are_explicit_previews_and_full_export_is_exact(tmp_path):
    path = tmp_path / "large.db"
    value = payload()
    value["data"]["eventDataList"][0]["extra"] = "中" * 30000
    raw = encoded(value)
    with ReportArchive(path) as archive:
        sid = archive.import_bytes(raw)["snapshot_ids"][0]
    data = BattleData(path)
    detail = data.detail(sid, event_limit=1)
    assert detail["events"][0]["raw"]["preview_truncated"] is True
    preview = data.original_preview(sid, max_bytes=100)
    assert preview["truncated"] and preview["bytes"] == len(raw)
    target = tmp_path / "original.json"
    result = data.export_original(sid, target)
    assert target.read_bytes() == raw and result["sha256"] == hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError):
        data.export_original(sid, path)
    with pytest.raises(ValueError):
        data.export_original(sid, str(path) + "-wal")


def test_hero_and_skill_preview_limits_are_explicit(tmp_path):
    path = tmp_path / "many.db"
    value = payload()
    members = value["data"]["battleSettlement"]["attacker"]["army_member"]
    members[0]["skills"] = [{"skillId": number} for number in range(101)]
    members.extend({"heros_id": 6000 + number} for number in range(100))
    with ReportArchive(path) as archive:
        sid = archive.import_bytes(encoded(value))["snapshot_ids"][0]
    result = BattleData(path).detail(sid)
    attacker = next(side for side in result["sides"] if side["side"] == "attacker")
    assert attacker["heroes_preview_truncated"] and len(attacker["heroes"]) == 100
    assert attacker["heroes"][0]["skills_preview_truncated"]
    assert len(attacker["heroes"][0]["skills"]) == 100


def test_queries_never_select_snapshot_or_component_full_json(archive, monkeypatch):
    path, sid = archive
    traced = []
    original = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(traced.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    data = BattleData(path)
    data.overview()
    data.list_reports()
    data.detail(sid)
    lowered = "\n".join(traced).lower()
    assert "payload_json" not in lowered
    assert "select count(*) from report_fields" not in lowered
    assert "select count(*) from report_events" not in lowered
    assert not any("from report_fields" in query.lower() and "pointer in" not in query.lower() for query in traced)


def test_query_deadline_interrupts_read_without_mutation(archive, monkeypatch):
    path, _ = archive
    data = BattleData(path, timeout_seconds=.01)
    from battle_ui import data as module
    original = module.time.monotonic
    calls = 0

    def expiring_clock():
        nonlocal calls
        calls += 1
        return original() if calls == 1 else original() + 100

    monkeypatch.setattr(module.time, "monotonic", expiring_clock)
    with pytest.raises(BattleQueryTimeout):
        with data._connection() as connection:
            connection.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n").fetchone()


def test_reader_is_usable_on_independent_worker_threads(archive):
    path, sid = archive
    data = BattleData(path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        values = list(pool.map(lambda _: data.detail(sid, event_limit=1)["report"]["battle_uid"], range(6)))
    assert values == ["1001"] * 6


@pytest.mark.parametrize("arguments", [
    {"limit": 0}, {"limit": 201}, {"limit": True}, {"offset": -1},
    {"status": "complete' OR 1=1"}, {"search": "x" * 201},
    {"namespace": ["not a namespace"]},
])
def test_invalid_list_parameters_are_rejected_before_query(archive, arguments):
    with pytest.raises(ValueError):
        BattleData(archive[0]).list_reports(**arguments)


def test_bad_archive_and_snapshot_ids_fail_cleanly(tmp_path, archive):
    path = tmp_path / "wrong.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE something_else(value)")
    with pytest.raises(BattleDataError):
        BattleData(path).overview()
    data = BattleData(archive[0])
    for sid in (0, -1, True, "1", 2**64):
        with pytest.raises(ValueError):
            data.detail(sid)
    with pytest.raises(KeyError):
        data.detail(2**62)


def test_newer_archive_schema_is_not_interpreted_silently(archive):
    path, _ = archive
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE report_meta SET value='999' WHERE key='schema_version'")
    with pytest.raises(BattleDataError, match="版本"):
        BattleData(path).overview()


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows drive policy")
@pytest.mark.parametrize("path", [r"C:\gui-no-write-check.db", r"\\?\C:\gui-no-write-check.db", r"\\.\C:\gui-no-write-check.db"])
def test_c_drive_and_extended_paths_are_rejected_without_opening(path):
    with pytest.raises(ValueError, match="C 盘"):
        BattleData(path)
