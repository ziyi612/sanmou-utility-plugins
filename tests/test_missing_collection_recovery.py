"""Recover durable imports and historical children without inventing coverage."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import time
from unittest.mock import patch

import pytest

from capture.game_assets.battle_collector import BattleCollector
from parser.report_archive import ReportArchive


CAPS = {"capabilities": {"RPCGetBattleBlockList": True, "RPCGetChildCombatInfoList": True},
        "segmentLength": 10, "relationAll": 1000}


def observe(collector, root, battle_id=123, child_num=1):
    return collector.remember("personal", {"combatInfo": {"battleID": battle_id}, "childNum": child_num},
                              {"sIndex": 1, "reqLen": 1}, root / "page.json")


def captured_file(root, battle_id=123, complete=True):
    data = {"battleID": battle_id, "eventDataList": [{"eventId": 1, "intList": [0]}]}
    components = {name: copy.deepcopy(data) for name in ("basic", "detail", "all", "statics")}
    components["snapshots"] = {"isComplete": True, "battleSnapData": []} if complete else None
    payload = {"battleID": battle_id, "captureVersion": 2, "source": "all", "eventCount": 1,
               "data": data, "components": components, "captureComplete": complete,
               "serializationIssues": [], "componentStatus": {name: {"available": value is not None,
                   "complete": value is not None} for name, value in components.items()}}
    path = root / f"battle_{battle_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return {"battleID": battle_id, "exported": True, "complete": complete, "path": str(path), "eventCount": 1}


def test_post_commit_checkpoint_error_is_not_recorded_as_database_failure(tmp_path):
    calls = []
    with ReportArchive(tmp_path / "reports.db") as archive:
        def importer(path):
            calls.append(path)
            return archive.import_file(path, namespace="account")
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account", on_export=importer)
        original = collector.save
        checkpoints = 0
        def fail_only_after_commit():
            nonlocal checkpoints
            checkpoints += 1
            if checkpoints == 2:
                raise PermissionError("checkpoint temporarily locked after SQLite committed")
            original()
        with patch.object(collector, "save", side_effect=fail_only_after_commit):
            observe(collector, tmp_path)
        entry = collector.state["reports"]["123"]
        assert len(calls) == 1 and archive.stats()["records"] == 1
        assert "discoveryImportError" not in entry
        assert "checkpoint temporarily locked" in entry["discoveryCheckpointError"]
        assert "pendingDiscoveryImport" not in entry
        # Re-acknowledging the identical immutable version neither reimports it
        # nor leaves the old failed checkpoint attached to a successful import.
        collector.archive_discovery(entry)
        assert len(calls) == 1
        assert "discoveryCheckpointError" not in entry
        assert archive.verify()["ok"]


def test_legacy_import_error_clears_when_exact_version_is_already_committed(tmp_path):
    collector = BattleCollector(None, tmp_path, on_export=lambda path: {"status": "imported"})
    observe(collector, tmp_path)
    entry = collector.state["reports"]["123"]
    entry["discoveryImportError"] = "old WinError 5 after successful import"
    entry["complete"] = True
    with patch("capture.game_assets.battle_collector.capture_report") as capture:
        counts = collector.collect_pending()
    assert counts["skipped"] == 1 and capture.call_count == 0
    assert "discoveryImportError" not in entry
    saved = json.loads(collector.state_file.read_text(encoding="utf-8"))
    assert "discoveryImportError" not in saved["reports"]["123"]


@pytest.mark.parametrize("complete", [True, False])
def test_durable_pending_import_is_replayed_before_component_backoff(tmp_path, complete):
    with ReportArchive(tmp_path / "reports.db") as archive:
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account",
            on_export=lambda path: archive.import_file(path, namespace="account"))
        result = captured_file(tmp_path, complete=complete)
        deadline = time.time() + 3600
        entry = {"battleID": "123", "attempts": 4, "complete": False, "observations": {},
                 "nextRetryAt": deadline, "pendingImport": {"path": result["path"], "result": result}}
        collector.state["reports"]["123"] = entry
        assert collector._initial_pending_ids(1) == ["123"]
        with patch("capture.game_assets.battle_collector.capture_report") as capture:
            counts = collector.collect_pending()
        assert capture.call_count == 0 and "pendingImport" not in entry
        assert entry["attempts"] == 4  # Local database recovery is not a game request.
        assert counts["exported"] == 1
        assert archive.list_reports()[0]["completeness"] == ("complete" if complete else "partial")
        if complete:
            assert "nextRetryAt" not in entry
        else:
            assert entry["nextRetryAt"] == deadline
        assert archive.verify()["ok"]


@pytest.mark.parametrize("response", [{"status": "invalid"}, {"status": "duplicate", "error": "commit rejected"}])
def test_rejected_pending_import_stays_pending_without_game_request(tmp_path, response):
    result = captured_file(tmp_path)
    collector = BattleCollector(None, tmp_path / "runtime", on_export=lambda path: response)
    entry = {"battleID": "123", "attempts": 4, "complete": False, "observations": {},
             "nextRetryAt": time.time() + 3600, "pendingImport": {"path": result["path"], "result": result}}
    collector.state["reports"]["123"] = entry
    with patch("capture.game_assets.battle_collector.capture_report") as capture:
        counts = collector.collect_pending()
    assert capture.call_count == 0 and counts["failed"] == 1
    assert entry["pendingImport"] and entry["complete"] is False
    assert entry["attempts"] == 4


@pytest.mark.parametrize("keep_old_scope", [True, False])
def test_failed_root_page_recovers_old_child_without_marking_it_current_visible(tmp_path, keep_old_scope):
    with ReportArchive(tmp_path / "reports.db") as archive:
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account", request_delay=0,
            on_export=lambda path: archive.import_file(path, namespace="account"))
        collector.state["cycles"] = 1
        observe(collector, tmp_path)
        if keep_old_scope:
            collector.state["scopes"]["personal"] = {"roots": {"123": {"battleID": "123", "childNum": 1}},
                "children": {"123": {"complete": False, "index": 1, "pages": 0}}}
        else:
            collector.state["scopes"]["personal"] = {"roots": {}, "children": {}}
        collector.state["cycles"] = 2
        collector._collect_pages = True
        collector._cycle_attempted = set()
        calls = []
        def page(scope, index, relation, segment, parent=None):
            calls.append(parent)
            if parent is None:
                raise TimeoutError("head list temporarily unavailable")
            assert parent == "123"
            return [{"battleID": 456, "eventDataList": []}], None, {"sIndex": 1, "reqLen": 1}, tmp_path / "child.json"
        with patch.object(collector, "page", side_effect=page), patch(
                "capture.game_assets.battle_collector.capture_report",
                side_effect=lambda runner, bid, *a, **k: captured_file(tmp_path, bid)):
            summary = collector.discover_scope("personal", CAPS)
        assert calls == [None, "123"]
        assert not summary["complete"] and not summary["rootComplete"]
        assert summary["roots"] == {} and summary["children"] == {}
        assert summary["historicalChildren"]["123"]["complete"] is True
        restored = collector.state["reports"]["456"]
        assert restored.get("lastSeenCycle") != 2 and restored["lastRecoveryCycle"] == 2
        assert restored["complete"] is True
        assert any(obs.get("historicalRecovery") for obs in restored["observations"].values())
        assert collector.state["reports"]["123"]["lastSeenCycle"] == 1
        assert collector.state["childRecovery"]["personal"]["123"]["complete"] is True
        assert archive.stats()["records"] == 2 and archive.verify()["ok"]


def test_historical_child_recovery_has_independent_backoff(tmp_path):
    collector = BattleCollector(None, tmp_path, request_delay=0)
    observe(collector, tmp_path)
    calls = []
    def fail_page(scope, index, relation, segment, parent=None):
        calls.append(parent)
        raise TimeoutError("unavailable")
    with patch.object(collector, "page", side_effect=fail_page):
        collector.discover_scope("personal", CAPS)
        collector.discover_scope("personal", CAPS)
    assert calls == [None, "123", None]
    group = collector.state["childRecovery"]["personal"]["123"]
    assert group["attempts"] == 1 and group["nextRetryAt"] > time.time()


def test_fresh_root_completion_does_not_requery_old_invisible_children(tmp_path):
    collector = BattleCollector(None, tmp_path, request_delay=0)
    observe(collector, tmp_path)
    calls = []
    def empty_page(scope, index, relation, segment, parent=None):
        calls.append(parent)
        return [], None, {"sIndex": 1, "reqLen": 0}, tmp_path / "page.json"
    with patch.object(collector, "page", side_effect=empty_page):
        summary = collector.discover_scope("personal", CAPS)
    assert calls == [None] and summary["complete"]
    assert summary["historicalChildren"] == {}


def test_old_parent_with_all_known_children_is_not_requeried_on_failed_root(tmp_path):
    collector = BattleCollector(None, tmp_path, request_delay=0)
    observe(collector, tmp_path)
    collector.remember("personal", {"battleID": 456}, {"sIndex": 1, "reqLen": 1}, tmp_path / "child.json", "123")
    with patch.object(collector, "page", side_effect=TimeoutError("head unavailable")) as page:
        summary = collector.discover_scope("personal", CAPS)
    assert page.call_count == 1 and summary["historicalChildren"] == {}


def test_historical_child_groups_are_bounded_and_fair_across_cycles(tmp_path):
    collector = BattleCollector(None, tmp_path, request_delay=0)
    collector.state["childRecovery"] = {"personal": {str(i): {"battleID": str(i), "childNum": 1,
        "complete": False, "attempts": 0} for i in range(1, 6)}}
    calls = []
    def page(scope, index, relation, segment, parent=None):
        calls.append(parent)
        if parent is None:
            raise TimeoutError("head unavailable")
        return [], None, {"sIndex": 1, "reqLen": 0}, tmp_path / "child.json"
    caps = {**CAPS, "segmentLength": 2}
    with patch.object(collector, "page", side_effect=page):
        first = collector.discover_scope("personal", caps)
        second = collector.discover_scope("personal", caps)
    assert calls == [None, "1", "2", None, "3", "4"]
    assert len(first["historicalChildren"]) == len(second["historicalChildren"]) == 2
    assert first["historicalPendingGroups"] == 5 and second["historicalPendingGroups"] == 3


@pytest.mark.parametrize("fail_stage", [None, "checkpoint", "final"])
def test_retry_cache_is_imported_before_final_capture_and_replayed_without_rpc(tmp_path, fail_stage):
    result = captured_file(tmp_path, complete=False)
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_bytes(Path(result["path"]).read_bytes())
    original_bytes = checkpoint.read_bytes()
    result = captured_file(tmp_path, complete=True)
    result["cacheCheckpointPaths"] = [str(checkpoint), str(checkpoint)]
    calls = []
    failed = False
    with ReportArchive(tmp_path / "reports.db") as archive:
        def importer(path):
            nonlocal failed
            payload = json.loads(path.read_text(encoding="utf-8"))
            stage = "final" if payload["captureComplete"] else "checkpoint"
            calls.append(stage)
            if fail_stage == stage and not failed:
                failed = True
                raise OSError("temporary database interruption")
            return archive.import_file(path, namespace="account")
        collector = BattleCollector(None, tmp_path / "runtime", namespace="account", on_export=importer)
        entry = {"battleID": "123", "complete": False, "attempts": 2, "observations": {}}
        collector.state["reports"]["123"] = entry
        counts = collector._empty_collection_counts()
        if fail_stage:
            with pytest.raises(OSError):
                collector._store_captured_result(entry, result, counts)
            # Restart from the durable checkpoint. Its local import must precede
            # both the future network backoff and any fresh game request.
            entry["nextRetryAt"] = time.time() + 3600
            collector.save()
            collector = BattleCollector(None, tmp_path / "runtime", namespace="account", on_export=importer)
            with patch("capture.game_assets.battle_collector.capture_report") as capture:
                counts = collector.collect_pending()
            assert capture.call_count == 0
            entry = collector.state["reports"]["123"]
        else:
            collector._store_captured_result(entry, result, counts)
        assert calls == (["checkpoint", "checkpoint", "final"] if fail_stage == "checkpoint" else
                         ["checkpoint", "final", "final"] if fail_stage == "final" else ["checkpoint", "final"])
        assert counts["exported"] == counts["complete"] == 1
        assert counts["partial"] == counts["failed"] == 0
        assert "pendingImport" not in entry and entry["complete"]
        assert entry["attempts"] == 2
        assert checkpoint.read_bytes() == original_bytes
        assert archive.stats()["records"] == 1
        assert archive.connection.execute("SELECT count(*) FROM report_snapshots").fetchone()[0] == 2
        assert archive.list_reports()[0]["completeness"] == "complete"
        assert archive.verify()["ok"]


def test_cache_checkpoint_from_other_battle_is_never_attributed_to_this_entry(tmp_path):
    checkpoint = captured_file(tmp_path, battle_id=456)
    result = captured_file(tmp_path, battle_id=123)
    result["cacheCheckpointPaths"] = [checkpoint["path"]]
    calls = []
    collector = BattleCollector(None, tmp_path / "runtime", namespace="account", on_export=calls.append)
    entry = {"battleID": "123", "observations": {}}
    with pytest.raises(ValueError, match="another battle"):
        collector._store_captured_result(entry, result, collector._empty_collection_counts())
    assert not calls and "pendingImport" not in entry
