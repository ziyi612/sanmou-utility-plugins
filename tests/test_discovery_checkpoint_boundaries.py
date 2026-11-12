"""Child-page receipts and completed cursors share a recoverable checkpoint."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from battle_reports import recover_captures
from capture.game_assets import battle_collector as module
from capture.game_assets.battle_collector import BattleCollector
from parser.report_archive import ReportArchive


CAPS = {"capabilities": {"RPCGetBattleBlockList": True, "RPCGetChildCombatInfoList": True},
        "segmentLength": 10, "relationAll": 1000}
IDENTITY = {"avatarId": "42", "serverId": "1", "scenarioId": "18"}


def collector_at(root, importer):
    collector = BattleCollector(None, root / "runtime", namespace="account", scopes=("personal",), on_export=importer)
    collector.state["runtimeIdentity"] = IDENTITY
    collector._collect_pages = True
    return collector


def page_fixture(root):
    def page(scope, index, relation, segment, parent=None):
        row = ({"battleID": 2, "zero": 0, "flag": False, "unknown": {"raw": {"$luaBytesHex": "00ff41"}}}
               if parent else {"combatInfo": {"battleID": 1, "zero": 0, "flag": False}, "childNum": 1})
        response = {"sIndex": 1, "reqLen": 1, "unknownHeader": {"zero": 0, "flag": False}}
        path = root / "list_responses" / ("child.json" if parent else "root.json")
        module.atomic_json(path, {"rows": [row], "response": response})
        return [row], None, response, path
    return page


def assert_two_original_reports(archive):
    assert archive.stats()["records"] == archive.stats()["snapshots"] == 2
    assert archive.connection.execute("SELECT count(*) FROM report_fields WHERE pointer='/data/zero' AND value_json='0'").fetchone()[0] == 2
    assert archive.connection.execute("SELECT count(*) FROM report_fields WHERE pointer='/data/flag' AND value_json='false'").fetchone()[0] == 2
    assert archive.connection.execute("SELECT count(*) FROM report_fields WHERE pointer='/data/unknown/raw/$luaBytesHex' AND value_json='\"00ff41\"'").fetchone()[0] == 1
    assert archive.verify()["ok"]


def assert_completed_child_on_disk(collector):
    state = json.loads(collector.state_file.read_bytes())
    child = state["scopes"]["personal"]["children"]["1"]
    group = state["childRecovery"]["personal"]["1"]
    assert child["index"] is group["index"] is None
    assert child["complete"] is group["complete"] is True
    assert "pendingDiscoveryImport" not in state["reports"]["2"]


def test_single_child_page_has_only_pending_and_completed_receipt_checkpoints(tmp_path):
    with ReportArchive(tmp_path / "archive.db") as archive:
        collector = collector_at(tmp_path, lambda path: archive.import_file(path, namespace="account"))
        saved, save = [], collector.save
        def watch_save():
            save()
            saved.append(copy.deepcopy(collector.state))
        def collect(ids, **_kwargs):
            if "2" in ids:
                assert_completed_child_on_disk(collector)
                assert_two_original_reports(archive)
            return collector._empty_collection_counts()
        with patch.object(collector, "save", side_effect=watch_save), \
                patch.object(collector, "page", side_effect=page_fixture(tmp_path)), \
                patch.object(collector, "_collect_batch", side_effect=collect):
            assert collector.discover_scope("personal", CAPS)["complete"]
        # Root pending + root page receipt, child pending + child receipt and
        # completed group together, then the final scope summary.
        assert len(saved) == 5
        child_checkpoints = [state for state in saved if state["scopes"]["personal"].get("children")
                             and "finishedAt" not in state["scopes"]["personal"]]
        assert len(child_checkpoints) == 2
        first, last = child_checkpoints
        assert first["reports"]["2"].get("pendingDiscoveryImport")
        assert not first["childRecovery"]["personal"]["1"]["complete"]
        assert last["childRecovery"]["personal"]["1"]["complete"]
        assert_completed_child_on_disk(collector)


@pytest.mark.parametrize("fault", ["raw_before_pending", "committed_before_ack", "before_completion_save", "after_completion_save"])
def test_each_child_receipt_crash_recovers_exact_bytes_without_duplicate_reports(tmp_path, fault):
    with ReportArchive(tmp_path / "archive.db") as archive:
        tripped = False
        def importer(path):
            nonlocal tripped
            result = archive.import_file(path, namespace="account")
            assert result["status"] in ("imported", "duplicate")
            payload = json.loads(Path(path).read_bytes())
            if fault == "committed_before_ack" and payload["battleID"] == 2 and not tripped:
                tripped = True
                raise SystemExit("child import committed before its acknowledgement")
            return result
        collector = collector_at(tmp_path, importer)
        save = collector.save
        def faulting_save():
            nonlocal tripped
            child = collector.state["scopes"]["personal"].get("children", {}).get("1", {})
            entry = collector.state["reports"].get("2", {})
            hit = ((fault == "raw_before_pending" and entry.get("pendingDiscoveryImport")) or
                   (fault in ("before_completion_save", "after_completion_save") and child.get("complete")))
            if hit and not tripped:
                tripped = True
                if fault == "after_completion_save":
                    save()
                raise SystemExit(fault)
            save()
        with patch.object(collector, "save", side_effect=faulting_save), \
                patch.object(collector, "page", side_effect=page_fixture(tmp_path)), \
                patch.object(collector, "_collect_batch", return_value=collector._empty_collection_counts()):
            with pytest.raises(SystemExit):
                collector.discover_scope("personal", CAPS)
        assert tripped
        originals = {path: path.read_bytes() for path in (collector.out_dir / "captures").glob("*/*.json")}
        assert len(originals) == 2
        if fault == "after_completion_save":
            assert_completed_child_on_disk(collector)
        else:
            stored = json.loads(collector.state_file.read_bytes())
            group = stored.get("childRecovery", {}).get("personal", {}).get("1", {})
            assert not group.get("complete")
        recovered = recover_captures(archive, collector.out_dir, "account", expected_identity=IDENTITY)
        assert not recovered["errors"]
        resumed = collector_at(tmp_path, lambda path: archive.import_file(path, namespace="account"))
        for entry in list(resumed.state["reports"].values()):
            if entry.get("pendingDiscoveryImport"):
                assert resumed.retry_discovery_import(entry)["status"] == "imported"
        with patch.object(resumed, "page", side_effect=page_fixture(tmp_path)), \
                patch.object(resumed, "_collect_batch", return_value=resumed._empty_collection_counts()), \
                patch.object(module, "capture_report") as capture:
            assert resumed.discover_scope("personal", CAPS)["complete"]
        assert not capture.called
        assert_completed_child_on_disk(resumed)
        assert_two_original_reports(archive)
        assert not any(entry.get("discoveryImportError") for entry in resumed.state["reports"].values())
        for path, raw in originals.items():
            assert path.read_bytes() == raw
            stored = archive.connection.execute("SELECT raw_bytes FROM report_files WHERE source_path=?", (str(path.resolve()),)).fetchone()
            assert bytes(stored[0]) == raw


def test_child_completion_state_write_failure_is_not_an_import_failure(tmp_path):
    with ReportArchive(tmp_path / "archive.db") as archive:
        collector = collector_at(tmp_path, lambda path: archive.import_file(path, namespace="account"))
        save, interrupted = collector.save, False
        def fail_completion_once():
            nonlocal interrupted
            child = collector.state["scopes"]["personal"].get("children", {}).get("1", {})
            if child.get("complete") and not interrupted:
                interrupted = True
                raise OSError("page completion state was temporarily locked")
            save()
        with patch.object(collector, "save", side_effect=fail_completion_once), \
                patch.object(collector, "page", side_effect=page_fixture(tmp_path)), \
                patch.object(collector, "_collect_batch", return_value=collector._empty_collection_counts()):
            assert not collector.discover_scope("personal", CAPS)["complete"]
        assert interrupted and "discoveryImportError" not in collector.state["reports"]["2"]
        assert not json.loads(collector.state_file.read_bytes())["childRecovery"]["personal"]["1"]["complete"]
        assert_two_original_reports(archive)
        with patch.object(collector, "page", side_effect=page_fixture(tmp_path)), \
                patch.object(collector, "_collect_batch", return_value=collector._empty_collection_counts()):
            assert collector.discover_scope("personal", CAPS)["complete"]
        assert_completed_child_on_disk(collector)
        assert_two_original_reports(archive)
