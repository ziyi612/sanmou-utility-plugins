"""Faster publication preserves original bytes and durable recovery boundaries."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from capture.game_assets import battle_collector as collector_module
from capture.game_assets import export_battle_report_runtime as exporter
from parser.report_archive import ReportArchive


def old_json_bytes(value, compact=False):
    stream = io.StringIO(newline="\n")
    options = {"separators": (",", ":")} if compact else {"indent": 2}
    json.dump(value, stream, ensure_ascii=False, allow_nan=False, **options)
    return stream.getvalue().encode("utf-8")


@pytest.mark.parametrize("compact", [False, True])
def test_json_publication_is_byte_identical_to_previous_streaming_encoder(tmp_path, compact):
    value = {"中文": "姓名\n行尾\r\n与引号\"", "zero": 0, "negative_zero": -0.0,
             "large_id": 10026778160057, "false": False, "none": None,
             "empty_list": [], "empty_object": {}, "float": 1.2345678901234567,
             "unknown": {"3": {"$luaBytesHex": "000aff"}}, "ordered": [3, 1, 2]}
    path = tmp_path / "capture.json"
    exporter.atomic_json(path, value, compact=compact)
    assert path.read_bytes() == old_json_bytes(value, compact)
    assert not path.with_name(path.name + ".tmp").exists()


def test_bytes_replacement_failure_keeps_old_target_and_complete_flushed_temporary(tmp_path):
    path = tmp_path / "capture.json"
    old, new = b'{"old":true}', old_json_bytes({"new": "字节完全保留", "flag": False})
    path.write_bytes(old)
    with patch.object(exporter, "replace_with_retry", side_effect=OSError("replace unavailable")), \
            patch.object(exporter.os, "fsync", wraps=os.fsync) as sync:
        with pytest.raises(OSError, match="replace unavailable"):
            exporter.atomic_bytes(path, new)
        assert sync.call_count == 1
    assert path.read_bytes() == old
    assert path.with_name(path.name + ".tmp").read_bytes() == new
    exporter.atomic_bytes(path, new)
    assert path.read_bytes() == new and not path.with_name(path.name + ".tmp").exists()


def test_serialization_failure_preserves_previous_target_and_existing_recovery_file(tmp_path):
    path = tmp_path / "state.json"
    temporary = path.with_name(path.name + ".tmp")
    path.write_bytes(b"previous target")
    temporary.write_bytes(b"previous complete recovery bytes")
    with pytest.raises(ValueError):
        exporter.atomic_json(path, {"invalid": float("nan")})
    assert path.read_bytes() == b"previous target"
    assert temporary.read_bytes() == b"previous complete recovery bytes"


def test_encoded_publication_rejects_c_before_any_directory_or_file_creation(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows drive paths")
    with pytest.raises(ValueError, match="Storage policy"):
        exporter.atomic_bytes(Path("C:/never-create-capture-speed-test/raw.json"), b"data")


def test_revision_alias_and_sha_share_the_same_complete_provenance_bytes(tmp_path):
    data = {"battleID": 123, "eventDataList": [{"eventId": 0, "intList": [0, 10026778160057], "flag": False}]}
    components = {name: copy.deepcopy(data) for name in ("basic", "detail", "all")}
    components.update(statics={"zero": 0}, snapshots={"isComplete": True, "battleSnapData": {"unknown": []}})
    payload = {"captureVersion": 2, "battleID": 123, "source": "all", "data": data,
        "components": components, "eventCount": 1, "captureComplete": True,
        "serializationIssues": [], "luaTableKeyTypes": {"/components/statics": {"zero": "string"}},
        "componentStatus": {name: {"available": True, "complete": True} for name in exporter.COMPONENTS}}
    observation = {"scope": "union", "parent": None, "row": {"combatInfo": {"battleID": 123}},
        "clientPeerless": [{"legend": False, "zgNum": 0, "helper_bytecode_sha256": "retained"}]}
    entry = {"battleID": "123", "observations": {"one": observation}}
    alias = tmp_path / "battle_123_decrypted.json"
    exporter.atomic_json(alias, payload)
    collector = collector_module.BattleCollector(None, tmp_path, namespace="account")
    expected = old_json_bytes({**payload, "discovery": [observation], "namespace": "account"}, compact=True)
    with patch.object(collector_module, "atomic_bytes", wraps=exporter.atomic_bytes) as publish, \
            patch.object(exporter.os, "fsync", wraps=os.fsync) as sync:
        revision = collector._capture_revision(entry, alias, update_alias=True)
        assert publish.call_count == sync.call_count == 2  # Both durable publications remain.
    assert revision.read_bytes() == alias.read_bytes() == expected
    assert revision.stem == hashlib.sha256(expected).hexdigest()
    # Immutable original and mutable alias are the same file version to SQLite.
    with ReportArchive(tmp_path / "archive.db") as archive:
        imported = archive.import_file(revision, namespace="account")
        assert imported["status"] == "imported" and imported["reports"][0]["completeness"] == "complete"
        assert archive.import_file(alias, namespace="account")["status"] == "duplicate"
        assert archive.stats()["records"] == archive.stats()["snapshots"] == 1
        assert archive.original_bytes(imported["snapshot_ids"][0]) == expected
        assert archive.verify()["ok"]


def test_selected_work_does_not_read_priorities_for_unselected_reports(tmp_path):
    class ObservedStatus(dict):
        reads = 0
        def get(self, *args, **kwargs):
            self.reads += 1
            return super().get(*args, **kwargs)
    statuses = {}
    collector = collector_module.BattleCollector(None, tmp_path)
    for index in range(1, 31):
        status = ObservedStatus()
        statuses[str(index)] = status
        collector.state["reports"][str(index)] = {"battleID": str(index), "observations": {},
            "attempts": 3 if index == 2 else 0, "lastSeenAt": index, "complete": False,
            "lastSeenCycle": 0, "lastResult": {"componentStatus": status}}
    captured = []
    def capture(_runner, bid, *_args, **_kwargs):
        captured.append(bid)
        path = tmp_path / f"battle_{bid}.json"
        exporter.atomic_json(path, {"battleID": bid, "data": {"battleID": bid}})
        return {"battleID": bid, "path": str(path), "exported": True, "complete": True}
    with patch.object(collector_module, "capture_report", side_effect=capture):
        assert collector.collect_pending([]) == collector._empty_collection_counts()
        counts = collector.collect_pending([2, 8, 8])
    assert captured == [8, 2] and counts["complete"] == 2
    assert all(status.reads == 0 for key, status in statuses.items() if key not in {"2", "8"})
    assert statuses["2"].reads and statuses["8"].reads
