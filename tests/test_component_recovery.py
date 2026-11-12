"""An explicit source pair can recover data without inventing a new capture."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from parser.component_recovery import ComponentRecoveryError, build_component_recovery
from parser.report_archive import ReportArchive


NS = "server:1/scenario:18/avatar:100"
UID = "42"
IDENTITY = {"serverId": "1", "scenarioId": "18", "avatarId": "100"}


def pair():
    events = [{"eventId": 100, "intList": [123, 3004], "tab": 0},
              {"eventId": 101, "intList": [0], "tab": 1}]
    full = {"battleID": UID, "battleJobId": 55, "timeStamp": 1000, "endTimeStampTime": 1010,
            "combatType": 1, "scenarioId": 18, "childNum": 0, "eventDataList": events,
            "battleSettlement": {"attacker": {"army_id": 123, "avatar_data": {"avatar_id": 100}, "army_member": []},
                                 "defensive": {"army_id": 456, "army_member": []}},
            "unknownMap": {"1": {"zero": 0, "nil": None, "empty": []}}}
    snapshot = {"isComplete": True, "receivedCount": 2, "time": 99999,
                "pages": [{"maxIDX": 1, "snapDataList": [{"preserved": "page1"}]},
                          {"maxIDX": 1, "snapDataList": [{"preserved": "page2"}]}],
                "battleSnapData": {"maxIDX": 1, "baseBattleInfoRecords": {"1": {"unknown": 0}},
                                   "diffHeroInfoRecords": {}, "unknownBinary": {"$luaBytesHex": "ff00"}}}
    statics = {"staticsRecords": [{"zero": 0}], "unknown": []}
    primary = {"battleID": UID, "namespace": NS, "captureVersion": 2, "captureComplete": False,
               "source": "all", "eventCount": 2, "exportedAt": "original-primary-time", "data": full,
               "runtimeIdentity": IDENTITY, "serializationIssues": [], "captureWarnings": [],
               "components": {"basic": copy.deepcopy(full), "detail": copy.deepcopy(full), "all": copy.deepcopy(full),
                              "statics": statics, "snapshots": None},
               "componentStatus": {name: {"available": name != "snapshots", "complete": name != "snapshots",
                                          "requested": True, **({"error": "timeout"} if name == "snapshots" else {})}
                                   for name in ("basic", "detail", "all", "statics", "snapshots")},
               "luaTableKeyTypes": {"/components/all/unknownMap": {"1": "number"},
                                    "/data/unknownMap": {"1": "number"}, "/outsideUnknown": {"7": "number"}},
               "outsideUnknown": {"7": "kept"}}
    donor = copy.deepcopy(primary)
    donor.update(source="detail", exportedAt="original-donor-time", data={"battleID": UID, "eventDataList": copy.deepcopy(events)})
    donor["components"]["all"] = None
    donor["components"]["detail"] = copy.deepcopy(donor["data"])
    donor["components"]["snapshots"] = snapshot
    donor["components"]["basic"].update(childNum=1, eventDataList="original encoded UI placeholder", oldUIOnly=True)
    donor["componentStatus"]["all"] = {"available": False, "complete": False, "requested": True, "error": "timeout"}
    donor["componentStatus"]["snapshots"] = {"available": True, "complete": True, "requested": True}
    donor["luaTableKeyTypes"] = {"/components/snapshots/battleSnapData/baseBattleInfoRecords": {"1": "number"}}
    return donor, primary


def add(archive, value, namespace=NS):
    result = archive.import_bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"), namespace=namespace)
    assert result["status"] == "imported", result
    return result["snapshot_ids"][0]


def create(tmp_path, change=None):
    donor, primary = pair()
    if change is not None:
        change(donor, primary)
    with ReportArchive(tmp_path / "archive.db") as archive:
        donor_id, primary_id = add(archive, donor), add(archive, primary)
    return tmp_path / "archive.db", donor_id, primary_id


def build(db, donor_id, primary_id, **kwargs):
    return build_component_recovery(db, namespace=kwargs.get("namespace", NS), battle_uid=UID,
                                    primary_snapshot_id=primary_id, snapshot_source_id=donor_id)


def test_deterministic_recovery_preserves_components_types_and_provenance(tmp_path):
    db, donor_id, primary_id = create(tmp_path)
    first, second = build(db, donor_id, primary_id), build(db, donor_id, primary_id)
    assert first.raw_bytes == second.raw_bytes and first.sha256 == second.sha256
    value = first.payload
    assert value["captureKind"] == "component_recovery" and value["captureComplete"] is True
    assert "exportedAt" not in value and "recoveredAt" not in value
    assert value["outsideUnknown"] == {"7": "kept"}
    assert value["components"]["snapshots"]["battleSnapData"]["unknownBinary"] == {"$luaBytesHex": "ff00"}
    assert value["data"] == value["components"]["all"]
    assert all(st["requested"] is False for st in value["componentStatus"].values())
    assert value["luaTableKeyTypes"]["/components/snapshots/battleSnapData/baseBattleInfoRecords"] == {"1": "number"}
    assert value["luaTableKeyTypes"]["/data/unknownMap"] == {"1": "number"}
    assert value["componentRecovery"]["components"]["snapshots"]["snapshot_id"] == donor_id
    assert value["componentRecovery"]["components"]["all"]["snapshot_id"] == primary_id
    assert not value["componentRecovery"]["new_network_capture"]
    value["data"]["unknownMap"].clear()
    assert first.payload["data"]["unknownMap"]


def test_write_then_normal_import_is_complete_and_repeated_import_is_duplicate(tmp_path):
    db, donor_id, primary_id = create(tmp_path)
    document = build(db, donor_id, primary_id)
    path = tmp_path / "derived.json"
    document.write(path)
    assert document.write(path)["existing_identical"]
    with ReportArchive(db) as archive:
        raw_originals = {sid: archive.original_bytes(sid) for sid in (donor_id, primary_id)}
        result = archive.import_file(path, namespace=NS)
        assert result["status"] == "imported" and result["reports"][0]["completeness"] == "complete"
        assert archive.import_file(path, namespace=NS)["status"] == "duplicate"
        assert archive.connection.execute("SELECT count(*) FROM report_records").fetchone()[0] == 1
        assert archive.connection.execute("SELECT count(*) FROM report_snapshots").fetchone()[0] == 3
        assert all(archive.original_bytes(sid) == raw for sid, raw in raw_originals.items())
        assert archive.verify()["ok"]


@pytest.mark.parametrize("change", (
    lambda old, new: old["runtimeIdentity"].update(avatarId="101"),
    lambda old, new: old["components"]["basic"].update(timeStamp=1001),
    lambda old, new: old["components"]["basic"]["battleSettlement"]["attacker"].update(army_id=999),
    lambda old, new: old["components"]["detail"]["eventDataList"][0]["intList"].append(999),
    lambda old, new: old["components"]["statics"].update(unknown=[1]),
    lambda old, new: old["components"]["snapshots"].update(isComplete=False),
    lambda old, new: old["components"]["snapshots"].pop("battleSnapData"),
    lambda old, new: old["components"]["snapshots"].update(receivedCount=3),
    lambda old, new: old["components"]["snapshots"]["battleSnapData"].update(maxIDX=2),
    lambda old, new: old["components"]["snapshots"].update(pages={"1": {"maxIDX": 1, "snapDataList": []}, "3": {"maxIDX": 1, "snapDataList": []}}),
    lambda old, new: old.update(serializationIssues=[{"kind": "max_depth"}]),
    lambda old, new: new["luaTableKeyTypes"].update({"/data/unknownMap": {"1": "string"}}),
))
def test_inconsistent_or_unverifiable_sources_are_rejected(tmp_path, change):
    db, donor_id, primary_id = create(tmp_path, change)
    with pytest.raises(ComponentRecoveryError):
        build(db, donor_id, primary_id)


def test_explicit_namespace_and_source_ids_are_required(tmp_path):
    db, donor_id, primary_id = create(tmp_path)
    with pytest.raises(ComponentRecoveryError):
        build(db, donor_id, primary_id, namespace="another-account")
    for invalid in (True, -1, donor_id):
        with pytest.raises(ComponentRecoveryError):
            build(db, donor_id, invalid)


def test_source_bytes_and_payload_must_both_remain_authoritative(tmp_path):
    db, donor_id, primary_id = create(tmp_path)
    with ReportArchive(db) as archive:
        with archive.connection:
            archive.connection.execute("UPDATE report_snapshots SET payload_json='{}' WHERE id=?", (donor_id,))
    with pytest.raises(ComponentRecoveryError, match="original"):
        build(db, donor_id, primary_id)


def test_empty_runtime_lua_type_registry_array_is_preserved(tmp_path):
    def empty_registry(old, new):
        old["luaTableKeyTypes"] = []
        new["luaTableKeyTypes"] = []
    db, donor_id, primary_id = create(tmp_path, empty_registry)
    assert build(db, donor_id, primary_id).payload["luaTableKeyTypes"] == []


def test_zero_event_component_cannot_be_promoted_to_complete(tmp_path):
    def empty_events(old, new):
        new["components"]["all"]["eventDataList"] = []
    db, donor_id, primary_id = create(tmp_path, empty_events)
    with pytest.raises(ComponentRecoveryError, match="Zero or unavailable"):
        build(db, donor_id, primary_id)


def test_output_cannot_replace_database_sidecars_originals_or_other_files(tmp_path):
    db, donor_id, primary_id = create(tmp_path)
    document = build(db, donor_id, primary_id)
    for path in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm"), Path(str(db) + "-journal")):
        with pytest.raises(ValueError):
            document.write(path)
    target = tmp_path / "existing.json"
    target.write_bytes(b"keep original")
    with pytest.raises(FileExistsError):
        document.write(target)
    assert target.read_bytes() == b"keep original"
