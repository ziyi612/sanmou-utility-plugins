"""Recover captured headers without fabricating event paths or replacing rows."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from parser.report_archive import ReportArchive, _settlement_source


def payload():
    events = [{"eventId": 100, "intList": [0], "tab": 0}]
    header = {"battleID": "42", "battleSettlement": {
        "attacker": {"army_id": 123, "avatar_data": {"avatar_id": 456, "avatar_name": "甲"},
                     "army_member": [{"heros_id": 3004, "hero_lv": 0, "origin_troop_num": 0,
                                      "troop_num": 0, "skills": [{"skillId": 300401, "level": 0}]}]},
        "defensive": {"army_id": 0, "avatar_data": {"avatar_id": -1, "avatar_name": "守军"}, "army_member": []}}}
    return {"battleID": "42", "source": "detail", "captureVersion": 2, "captureComplete": False,
            "eventCount": 1, "data": {"battleID": "42", "eventDataList": events},
            "components": {"basic": header, "detail": {"battleID": "42", "eventDataList": events},
                           "all": None, "statics": {}, "snapshots": {}},
            "componentStatus": {name: {"available": name != "all", "complete": name != "all"}
                                for name in ("basic", "detail", "all", "statics", "snapshots")},
            "serializationIssues": []}


def import_partial(archive):
    raw = json.dumps(payload(), ensure_ascii=False).encode("utf-8")
    result = archive.import_bytes(raw)
    assert result["status"] == "imported"
    return raw, result["snapshot_ids"][0], result


def drop_projection(archive, sid):
    with archive.connection:
        for table in ("skills", "heroes", "sides"):
            archive.connection.execute(f"DELETE FROM report_{table} WHERE snapshot_id=?", (sid,))


def immutable_state(archive, sid):
    return {"raw": archive.original_bytes(sid),
            "snapshot": tuple(archive.connection.execute("SELECT * FROM report_snapshots WHERE id=?", (sid,)).fetchone()),
            "counts": {name: archive.connection.execute(f"SELECT count(*) FROM report_{name}").fetchone()[0]
                       for name in ("files", "snapshots", "fields", "events", "records", "imports")},
            "selection": archive.connection.execute("SELECT current_snapshot_id FROM report_records").fetchone()[0]}


@pytest.fixture
def archive(tmp_path):
    with ReportArchive(tmp_path / "archive.db") as value:
        yield value


def test_detail_events_keep_original_pointer_while_header_uses_basic(archive):
    raw, sid, result = import_partial(archive)
    assert result["reports"][0]["completeness"] == "partial"
    assert result["reports"][0]["settlement_source_pointer"] == "/components/basic/battleSettlement"
    assert archive.original_bytes(sid) == raw
    assert archive.connection.execute("SELECT source_pointer FROM report_events").fetchone()[0] == "/data/eventDataList/0"
    assert archive.connection.execute("SELECT count(*) FROM report_fields WHERE pointer LIKE '/data/battleSettlement%'").fetchone()[0] == 0
    assert archive.connection.execute("SELECT player_name FROM report_sides WHERE side='attacker'").fetchone()[0] == "甲"
    assert tuple(archive.connection.execute("SELECT level,troops_initial,troops_remaining FROM report_heroes").fetchone()) == (0, 0, 0)
    assert archive.verify()["ok"]


@pytest.mark.parametrize("chosen", ("data", "all", "detail", "basic"))
def test_source_priority_and_real_json_pointer(chosen):
    value = payload()
    settlement = copy.deepcopy(value["components"]["basic"]["battleSettlement"])
    value["data"]["battleSettlement"] = {}
    value["components"]["basic"]["battleSettlement"] = settlement
    if chosen == "data":
        value["data"]["battleSettlement"] = settlement
    elif chosen in ("all", "detail"):
        value["components"][chosen] = {"battleID": "42", "battleSettlement": settlement}
    _, pointer = _settlement_source(value)
    assert pointer == ("/data/battleSettlement" if chosen == "data" else f"/components/{chosen}/battleSettlement")


def test_repair_adds_only_relations_and_is_idempotent(archive):
    raw, sid, _ = import_partial(archive)
    drop_projection(archive, sid)
    before = immutable_state(archive, sid)
    assert not archive.verify()["ok"]
    repaired = archive.repair_settlement_projections([sid, sid])
    assert repaired["repaired"] == 1 and repaired["failed"] == 0
    assert repaired["snapshots"][0]["inserted"] == {"sides": 2, "heroes": 1, "skills": 1}
    assert repaired["snapshots"][0]["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert immutable_state(archive, sid) == before
    assert archive.repair_settlement_projections([sid])["unchanged"] == 1
    assert archive.verify()["ok"]


def test_repair_preserves_existing_side_and_hero_ids(archive):
    _, sid, _ = import_partial(archive)
    existing_sides = [tuple(row) for row in archive.connection.execute("SELECT * FROM report_sides")]
    existing_heroes = [tuple(row) for row in archive.connection.execute("SELECT * FROM report_heroes")]
    with archive.connection:
        archive.connection.execute("DELETE FROM report_skills WHERE snapshot_id=?", (sid,))
    result = archive.repair_settlement_projections([sid])
    assert result["snapshots"][0]["inserted"] == {"sides": 0, "heroes": 0, "skills": 1}
    assert [tuple(row) for row in archive.connection.execute("SELECT * FROM report_sides")] == existing_sides
    assert [tuple(row) for row in archive.connection.execute("SELECT * FROM report_heroes")] == existing_heroes


def test_conflicting_existing_projection_is_not_overwritten(archive):
    _, sid, _ = import_partial(archive)
    with archive.connection:
        archive.connection.execute("UPDATE report_sides SET player_name='keep existing' WHERE side='attacker'")
    before = [tuple(row) for row in archive.connection.execute("SELECT * FROM report_sides")]
    result = archive.repair_settlement_projections([sid])
    assert result["failed"] == 1 and "conflicts" in result["snapshots"][0]["error"]
    assert [tuple(row) for row in archive.connection.execute("SELECT * FROM report_sides")] == before


def test_failed_insert_rolls_back_new_relations_for_that_snapshot(archive):
    _, sid, _ = import_partial(archive)
    drop_projection(archive, sid)
    archive.connection.executescript("CREATE TRIGGER reject_repair BEFORE INSERT ON report_skills BEGIN SELECT RAISE(ABORT,'blocked'); END;")
    result = archive.repair_settlement_projections([sid])
    assert result["failed"] == 1
    for table in ("sides", "heroes", "skills"):
        assert archive.connection.execute(f"SELECT count(*) FROM report_{table}").fetchone()[0] == 0


def test_caller_transaction_and_read_only_mode_are_preserved(archive):
    _, sid, _ = import_partial(archive)
    archive.connection.execute("BEGIN IMMEDIATE")
    archive.connection.execute("INSERT INTO report_meta VALUES('caller','uncommitted')")
    with pytest.raises(RuntimeError, match="caller transaction"):
        archive.repair_settlement_projections([sid])
    assert archive.connection.in_transaction
    archive.connection.rollback()
    assert archive.connection.execute("SELECT value FROM report_meta WHERE key='caller'").fetchone() is None
    with ReportArchive(archive.db_path, read_only=True) as reader:
        with pytest.raises(PermissionError):
            reader.repair_settlement_projections([sid])


def test_corrupted_payload_or_missing_source_pointer_is_not_repaired(archive):
    _, sid, _ = import_partial(archive)
    drop_projection(archive, sid)
    with archive.connection:
        archive.connection.execute("DELETE FROM report_fields WHERE snapshot_id=? AND pointer='/components/basic/battleSettlement'", (sid,))
    result = archive.repair_settlement_projections([sid])
    assert result["failed"] == 1 and "pointer" in result["snapshots"][0]["error"]
    assert archive.connection.execute("SELECT count(*) FROM report_sides").fetchone()[0] == 0


def test_repair_validates_explicit_ids_before_writing(archive):
    _, sid, _ = import_partial(archive)
    for invalid in ([sid, True], "123", [-1], [1.5]):
        with pytest.raises(ValueError):
            archive.repair_settlement_projections(invalid)
