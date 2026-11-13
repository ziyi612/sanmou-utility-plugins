"""Compact indexes preserve authoritative bytes, complete events and metadata."""
import copy
import hashlib
import json
import sqlite3

import pytest

from battle_ui.current_metadata import read_indexed_subtree
from parser.report_archive import COMPONENTS, ReportArchive, iter_fields


def report(uid="2001", *, event_value=42, marker="one"):
    army = {"army_id": "10", "avatar_data": {"avatar_id": "11", "avatar_name": "甲"},
            "army_member": [{"heros_id": 4012, "hero_lv": 50, "index": 0,
                             "skills": [{"skillId": 301, "level": 10}]}]}
    data = {"battleID": uid, "timeStamp": 1794500000, "battleJobId": 2,
            "eventDataList": [{"eventId": 211, "intList": [event_value, None, 2**90], "tab": 0},
                              {"eventId": 99999, "intList": [], "tab": 1}],
            "battleSettlement": {"winer": "10", "attacker": army,
                                 "defensive": {"army_id": "20", "army_member": []}},
            "otherInfo": {"continuousInfo": {"zgNum": 0, "winBattleNum": 3}}}
    return {"battleID": uid, "captureVersion": 2, "eventCount": 2, "source": "all",
            "marker": marker, "data": data,
            "runtimeIdentity": {"avatarId": "11", "serverId": "57", "scenarioId": "1"},
            "discovery": [{"scope": "union", "parent": None, "row": {"childNum": 0}}],
            "components": {name: copy.deepcopy(data) if name in ("basic", "detail", "all")
                           else {"heroSnapshots": [{"heros_id": 4012, "attribute": [1, 2, 3]}]}
                           for name in COMPONENTS},
            "componentStatus": {name: {"requested": True, "available": True, "complete": True}
                                for name in COMPONENTS},
            "serializationIssues": [], "unknown": {"eventDataList": [{"retain": True}]}}


def encoded(value):
    return b"\xef\xbb\xbf" + json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\r\n"


def import_report(archive, payload, namespace="one"):
    result = archive.import_bytes(encoded(payload), namespace=namespace)
    assert result["status"] == "imported", result
    return result["snapshot_ids"][0], result["reports"][0]


def test_compact_preserves_raw_sha_components_events_and_metadata(tmp_path):
    value = report()
    raw = encoded(value)
    with ReportArchive(tmp_path / "compact.db", storage_mode="compact") as archive:
        sid, result = import_report(archive, value)
        assert result["completeness"] == "complete"
        assert result["storage_mode"] == "compact"
        assert result["field_count"] == len(list(iter_fields(value)))
        assert result["indexed_field_count"] < result["field_count"]
        assert archive.original_bytes(sid) == raw
        assert archive.get_payload(sid) == value
        original = archive.connection.execute("SELECT sha256,raw_bytes FROM report_files").fetchone()
        assert original["sha256"] == hashlib.sha256(raw).hexdigest()
        assert original["raw_bytes"] == raw
        target = tmp_path / "export.json"
        archive.export_original(sid, target)
        assert target.read_bytes() == raw
        pointers = {row[0] for row in archive.connection.execute("SELECT pointer FROM report_fields")}
        for root in ("/data/eventDataList", *("/components/" + name + "/eventDataList"
                                             for name in ("basic", "detail", "all"))):
            assert root in pointers
            assert not any(pointer.startswith(root + "/") for pointer in pointers)
        assert "/unknown/eventDataList/0/retain" in pointers
        assert "/components/snapshots/heroSnapshots/0/attribute/2" in pointers
        for pointer, expected in (("/data/otherInfo/continuousInfo", {"zgNum": 0, "winBattleNum": 3}),
                                  ("/discovery", value["discovery"]),
                                  ("/runtimeIdentity", value["runtimeIdentity"])):
            actual, truncated = read_indexed_subtree(archive.connection, sid, pointer)
            assert (actual, truncated) == (expected, False)
        events = [json.loads(row[0]) for row in archive.connection.execute(
            "SELECT raw_json FROM report_events WHERE snapshot_id=? ORDER BY sequence", (sid,))]
        assert events == value["data"]["eventDataList"]
        for row in archive.connection.execute("SELECT name,payload_json FROM report_components"):
            assert json.loads(row["payload_json"]) == value["components"][row["name"]]
        assert archive.stats()["heroes"] == 1
        assert archive.stats()["skills"] == 1
        verified = archive.verify()
        assert verified["ok"], verified
        assert verified["fields_checked"] == result["field_count"]
        assert verified["indexed_fields_checked"] == result["indexed_field_count"]


def test_full_and_compact_snapshots_coexist_without_duplicate_reindex(tmp_path):
    path = tmp_path / "mixed.db"
    full_value = report(marker="full")
    with ReportArchive(path) as archive:
        full_sid, full = import_report(archive, full_value)
        assert full["storage_mode"] == "full"
        assert full["field_count"] == full["indexed_field_count"]
    with ReportArchive(path, storage_mode="compact") as archive:
        duplicate = archive.import_bytes(encoded(full_value), namespace="one")
        assert duplicate["status"] == "duplicate"
        assert duplicate["snapshot_ids"] == [full_sid]
        compact_sid, compact = import_report(archive, report(marker="compact"))
        assert archive.connection.execute("SELECT storage_mode FROM report_snapshots WHERE id=?",
                                          (full_sid,)).fetchone()[0] == "full"
        assert archive.connection.execute("SELECT current_snapshot_id FROM report_records").fetchone()[0] == compact_sid
        assert archive.collection_stats()["current_fields"] == compact["indexed_field_count"]
        assert archive.collection_stats()["current_fields"] == archive.stats()["current_fields"]
        assert archive.verify()["ok"]
    with ReportArchive(path, read_only=True) as reader:
        assert reader.verify()["ok"]
        assert reader.get_payload(full_sid) == full_value


def test_hidden_event_serialization_issue_still_rejects_completeness(tmp_path):
    with ReportArchive(tmp_path / "marker.db", storage_mode="compact") as archive:
        value = report(event_value="<cycle>")
        _, result = import_report(archive, value)
        assert result["completeness"] == "partial"
        assert any(issue.get("path", "").startswith("/data/eventDataList/")
                   for issue in result["issues"])
        assert archive.verify()["ok"]


def test_compact_verify_detects_missing_kept_field_and_changed_event(tmp_path):
    with ReportArchive(tmp_path / "tampered.db", storage_mode="compact") as archive:
        sid, _ = import_report(archive, report())
        with archive.connection:
            archive.connection.execute("DELETE FROM report_fields WHERE snapshot_id=? AND pointer='/data/timeStamp'", (sid,))
            archive.connection.execute("UPDATE report_events SET raw_json='{}' WHERE snapshot_id=? AND sequence=0", (sid,))
        verified = archive.verify()
        assert not verified["ok"]
        checks = {item["check"] for item in verified["errors"]}
        assert {"all_fields", "ordered_events"} <= checks


def test_old_schema_is_read_without_writes_then_safely_migrated(tmp_path):
    path = tmp_path / "old.db"
    value = report()
    with ReportArchive(path) as archive:
        old_sid, original = import_report(archive, value)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE report_snapshots DROP COLUMN storage_mode")
        connection.execute("ALTER TABLE report_snapshots DROP COLUMN indexed_field_count")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = path.read_bytes()
    with ReportArchive(path, read_only=True, storage_mode="compact") as reader:
        assert reader.verify()["ok"]
        assert reader.collection_stats()["current_fields"] == original["field_count"]
        assert "storage_mode" not in {row["name"] for row in reader.connection.execute("PRAGMA table_info(report_snapshots)")}
    assert path.read_bytes() == before
    with ReportArchive(path, storage_mode="compact") as writer:
        old = writer.connection.execute("SELECT storage_mode,indexed_field_count FROM report_snapshots WHERE id=?", (old_sid,)).fetchone()
        assert tuple(old) == ("full", None)
        new_sid, _ = import_report(writer, report("2002"))
        assert new_sid != old_sid
        assert writer.verify()["ok"]
        assert writer.collection_stats()["current_fields"] == writer.stats()["current_fields"]


def test_invalid_storage_policy_fails_before_creating_database(tmp_path):
    path = tmp_path / "invalid.db"
    with pytest.raises(ValueError, match="storage_mode"):
        ReportArchive(path, storage_mode="unknown")
    assert not path.exists()
