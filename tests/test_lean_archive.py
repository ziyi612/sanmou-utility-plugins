"""Lean projections retain authoritative components and conservative quality."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import runpy

import pytest

from battle_ui.current_metadata import read_indexed_subtree
from parser.report_archive import ReportArchive, iter_fields


BASE = runpy.run_path(str(Path(__file__).with_name("test_report_compact.py")))
report = BASE["report"]
encoded = BASE["encoded"]


def _put(archive, value):
    result = archive.import_bytes(encoded(value), namespace="lean-fixture")
    assert result["status"] == "imported", result
    return result["snapshot_ids"][0], result["reports"][0]


@pytest.mark.parametrize("snapshot_component", [
    {"heroSnapshots": [{"heros_id": 4012, "attributes": [1, 2, 3]}]},
    [{"heros_id": 4012, "attributes": [1, 2, 3]}],
])
def test_lean_keeps_component_roots_raw_components_events_and_metadata(tmp_path, snapshot_component):
    value = report()
    value["components"]["snapshots"] = copy.deepcopy(snapshot_component)
    value["unknown"]["components"] = {"snapshots": {"retain": [1, 2, 3]},
                                       "statics": {"retain": 0}}
    raw = encoded(value)
    with ReportArchive(tmp_path / "lean.db", storage_mode="lean") as archive:
        sid, imported = _put(archive, value)
        assert imported["storage_mode"] == "lean"
        assert imported["completeness"] == "complete"
        assert imported["field_count"] == len(list(iter_fields(value)))
        assert imported["indexed_field_count"] < imported["field_count"]
        assert archive.original_bytes(sid) == raw
        assert archive.get_payload(sid) == value
        assert archive.connection.execute("SELECT sha256 FROM report_files").fetchone()[0] == hashlib.sha256(raw).hexdigest()
        pointers = {row[0] for row in archive.connection.execute("SELECT pointer FROM report_fields")}
        for name in ("snapshots", "statics"):
            root = "/components/" + name
            assert root in pointers
            assert not any(pointer.startswith(root + "/") for pointer in pointers)
        assert "/unknown/components/snapshots/retain/2" in pointers
        assert "/unknown/components/statics/retain" in pointers
        for pointer, expected in (("/data/otherInfo/continuousInfo", value["data"]["otherInfo"]["continuousInfo"]),
                                  ("/discovery", value["discovery"]),
                                  ("/runtimeIdentity", value["runtimeIdentity"])):
            assert read_indexed_subtree(archive.connection, sid, pointer) == (expected, False)
        components = {row["name"]: json.loads(row["payload_json"])
                      for row in archive.connection.execute("SELECT name,payload_json FROM report_components")}
        assert components == value["components"]
        events = [json.loads(row[0]) for row in archive.connection.execute(
            "SELECT raw_json FROM report_events WHERE snapshot_id=? ORDER BY sequence", (sid,))]
        assert events == value["data"]["eventDataList"]
        exported = tmp_path / "exact-original.json"
        archive.export_original(sid, exported)
        assert exported.read_bytes() == raw
        verification = archive.verify()
        assert verification["ok"], verification
        assert verification["fields_checked"] == imported["field_count"]
        assert verification["indexed_fields_checked"] == imported["indexed_field_count"]


def test_compact_and_lean_coexist_and_duplicates_keep_their_original_policy(tmp_path):
    path = tmp_path / "mixed.db"
    compact_value = report(marker="compact-before-lean")
    with ReportArchive(path, storage_mode="compact") as archive:
        compact_sid, compact_result = _put(archive, compact_value)
    with ReportArchive(path, storage_mode="lean") as archive:
        duplicate = archive.import_bytes(encoded(compact_value), namespace="lean-fixture")
        assert duplicate["status"] == "duplicate"
        assert duplicate["snapshot_ids"] == [compact_sid]
        lean_sid, lean_result = _put(archive, report(marker="lean-after-compact"))
        assert lean_result["indexed_field_count"] < compact_result["indexed_field_count"]
        modes = list(archive.connection.execute("SELECT id,storage_mode FROM report_snapshots ORDER BY id"))
        assert [tuple(row) for row in modes] == [(compact_sid, "compact"), (lean_sid, "lean")]
        assert archive.collection_stats()["current_fields"] == lean_result["indexed_field_count"]
        assert archive.stats()["current_fields"] == lean_result["indexed_field_count"]
        assert archive.verify()["ok"]
    with ReportArchive(path, read_only=True) as reader:
        assert reader.get_payload(compact_sid) == compact_value
        assert reader.verify()["ok"]


@pytest.mark.parametrize("branch", ("snapshots", "statics"))
def test_unindexed_component_placeholders_still_prevent_complete_status(tmp_path, branch):
    value = report()
    value["components"][branch] = {"hidden": [0, "<cycle>"]}
    with ReportArchive(tmp_path / "placeholder.db", storage_mode="lean") as archive:
        _, imported = _put(archive, value)
        assert imported["completeness"] == "partial"
        assert any(issue.get("path") == "/components/" + branch + "/hidden/1"
                   and issue.get("reason") == "serialization_placeholder" for issue in imported["issues"])
        assert archive.verify()["ok"]


def test_lean_verify_detects_corrupt_preserved_component_and_metadata(tmp_path):
    with ReportArchive(tmp_path / "corrupt.db", storage_mode="lean") as archive:
        sid, _ = _put(archive, report())
        with archive.connection:
            archive.connection.execute("UPDATE report_components SET payload_json='{}' WHERE snapshot_id=? AND name='snapshots'", (sid,))
            archive.connection.execute("DELETE FROM report_fields WHERE snapshot_id=? AND pointer='/data/battleJobId'", (sid,))
        verification = archive.verify()
        assert not verification["ok"]
        assert {"all_fields", "components_and_statuses"} <= {item["check"] for item in verification["errors"]}
