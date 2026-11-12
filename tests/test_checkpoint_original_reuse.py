"""Intermediate responses keep one exact original across metadata refreshes."""
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from battle_reports import recover_captures
from capture.game_assets.battle_collector import BattleCollector
from parser.report_archive import ReportArchive


def capture_files(root):
    components = {key: {"battleID": 123, "eventDataList": [{"eventId": 1, "intList": [0]}]}
                  for key in ("basic", "detail", "all")}
    components.update(statics={"zero": 0}, snapshots={"isComplete": True, "battleSnapData": {"kept": []}})
    full = {"captureVersion": 2, "battleID": 123, "source": "all", "data": components["all"],
            "components": components, "captureComplete": True, "eventCount": 1,
            "serializationIssues": [], "captureWarnings": [],
            "componentStatus": {key: {"available": True, "complete": True} for key in components}}
    partial = copy.deepcopy(full)
    partial.update(captureKind="response_before_handler", captureComplete=False,
                   unknown={"zero": 0, "false": False, "bytes": {"$luaBytesHex": "00ff"}})
    partial["components"]["snapshots"] = None
    partial["componentStatus"]["snapshots"] = {"available": False, "complete": False}
    raw = json.dumps(partial, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    checkpoint = root / "captures/123" / (hashlib.sha256(raw).hexdigest() + ".json")
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(raw)
    final = root / "battle_123_decrypted.json"
    final.write_text(json.dumps(full), encoding="utf-8")
    return checkpoint, raw, {"battleID": 123, "exported": True, "complete": True,
                             "path": str(final), "cacheCheckpointPaths": [str(checkpoint)]}


def test_received_original_is_committed_once_without_extra_copy_or_startup_backlog(tmp_path):
    runtime = tmp_path / "runtime"
    checkpoint, raw, result = capture_files(runtime)
    with ReportArchive(tmp_path / "archive.db") as archive:
        calls = []
        def ingest(path):
            calls.append(path)
            return archive.import_file(path, namespace="account")
        collector = BattleCollector(None, runtime, namespace="account", on_export=ingest)
        entry = {"battleID": "123", "observations": {"list": {"scope": "union", "parent": None,
                  "row": {"combatInfo": {"battleID": 123}, "marker": "first"}}}}
        with patch("capture.game_assets.battle_collector.atomic_bytes") as publish:
            assert collector._capture_checkpoint(entry, checkpoint) == checkpoint
            publish.assert_not_called()
        collector._store_captured_result(entry, result, collector._empty_collection_counts())
        assert calls[0] == checkpoint
        received = archive.connection.execute("SELECT raw_bytes FROM report_files WHERE namespace=? AND sha256=?",
                    ("account", hashlib.sha256(raw).hexdigest())).fetchone()
        assert bytes(received[0]) == raw and checkpoint.read_bytes() == raw
        assert len(list((runtime / "captures/123").glob("*.json"))) == 2
        before = archive.stats()
        recovered = recover_captures(archive, runtime, "account")
        assert recovered == {"recovered": 0, "errors": []}
        assert archive.stats() == before
        assert archive.verify()["ok"]


def test_metadata_only_refresh_does_not_reimport_or_rewrite_old_responses(tmp_path):
    runtime = tmp_path / "runtime"
    checkpoint, raw, result = capture_files(runtime)
    with ReportArchive(tmp_path / "archive.db") as archive:
        calls = []
        def ingest(path):
            calls.append(path)
            return archive.import_file(path, namespace="account")
        collector = BattleCollector(None, runtime, namespace="account", on_export=ingest)
        entry = {"battleID": "123", "attempts": 1, "observations": {}}
        collector.state["reports"]["123"] = entry
        collector._store_captured_result(entry, result, collector._empty_collection_counts())
        assert entry["lastResult"]["cacheCheckpointPaths"] == [str(checkpoint)]
        calls.clear()
        entry["metadataDirty"] = True
        entry["observations"]["new"] = {"scope": "star", "parent": None,
                                        "row": {"combatInfo": {"battleID": 123}, "marker": "updated"}}
        # This discovery was already durably archived by remember().
        entry["discoveryArchivePath"] = "already-committed"
        with patch("capture.game_assets.battle_collector.capture_report") as game:
            counts = collector.collect_pending([123])
        game.assert_not_called()
        assert counts["complete"] == 1 and len(calls) == 1
        assert checkpoint.read_bytes() == raw
        assert entry["lastResult"]["cacheCheckpointPaths"] == []
        assert archive.connection.execute("SELECT count(*) FROM report_records").fetchone()[0] == 1
        kinds = [json.loads(row[0]).get("captureKind") for row in archive.connection.execute("SELECT payload_json FROM report_snapshots")]
        assert kinds.count("response_before_handler") == 1
        assert recover_captures(archive, runtime, "account") == {"recovered": 0, "errors": []}
        assert archive.verify()["ok"]


@pytest.mark.parametrize("change", [
    {"battleID": 456}, {"namespace": "another-account"},
    {"runtimeIdentity": {"avatarId": "999", "serverId": "1", "scenarioId": "18"}},
])
def test_original_checkpoint_identity_is_checked_before_import(tmp_path, change):
    runtime = tmp_path / "runtime"
    checkpoint, raw, _ = capture_files(runtime)
    payload = {**json.loads(raw), **change}
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    collector = BattleCollector(None, runtime, namespace="account")
    collector.state["runtimeIdentity"] = {"avatarId": "42", "serverId": "1", "scenarioId": "18"}
    with pytest.raises(ValueError):
        collector._capture_checkpoint({"battleID": "123"}, checkpoint)
