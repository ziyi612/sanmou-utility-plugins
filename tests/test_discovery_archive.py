"""List discoveries survive later paging/runtime failures before detail export."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from capture.game_assets.battle_collector import BattleCollector
from parser.report_archive import ReportArchive


IDENTITY = {"avatarId": "42", "serverId": "1", "scenarioId": "14"}


class DiscoveryArchiveTests(unittest.TestCase):
    def make_collector(self, root, archive, importer=None):
        collector = BattleCollector(None, root / "captures", namespace="account", request_delay=0,
                                   on_export=importer or (lambda path: archive.import_file(path, namespace="account")))
        collector.state["runtimeIdentity"] = dict(IDENTITY)
        collector.state["cycles"] = 4
        return collector

    def observe(self, collector, root, extra=None):
        row = {"combatInfo": {"battleID": 123, "unknownBasic": {"zero": 0, "empty": [], "null": None}},
               "unknownRow": {"flag": False}}
        if extra:
            row.update(extra)
        return collector.remember("personal", row,
                                  {"sIndex": 1, "reqLen": 1, "unknownResponse": {"value": 99}}, root / "page.json")

    def test_discovery_is_immediate_partial_lossless_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory, ReportArchive(Path(directory) / "archive.db") as archive:
            root = Path(directory)
            calls = []
            def importer(path):
                calls.append(path)
                return archive.import_file(path, namespace="account")
            collector = self.make_collector(root, archive, importer)
            self.observe(collector, root)
            entry = collector.state["reports"]["123"]
            self.assertFalse(entry["complete"])
            self.assertEqual(archive.stats()["records"], 1)
            saved = json.loads(Path(entry["discoveryArchivePath"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["captureKind"], "list_discovery")
            self.assertIs(saved["captureComplete"], False)
            self.assertEqual(saved["runtimeIdentity"], IDENTITY)
            self.assertEqual(saved["components"]["basic"], saved["data"])
            self.assertEqual(saved["discovery"][0]["row"]["unknownRow"], {"flag": False})
            self.assertEqual(saved["discovery"][0]["responseMetadata"]["unknownResponse"], {"value": 99})
            self.assertEqual(set(saved["componentStatus"]), {"basic", "detail", "all", "statics", "snapshots"})
            self.assertFalse(saved["componentStatus"]["all"]["available"])
            self.assertEqual(archive.list_reports()[0]["completeness"], "partial")
            self.observe(collector, root)
            self.assertEqual(len(calls), 1)
            self.assertEqual(archive.stats()["snapshots"], 1)
            self.observe(collector, root, {"changedUnknown": "retained"})
            self.assertEqual(archive.stats()["snapshots"], 2)
            saved = json.loads(Path(entry["discoveryArchivePath"]).read_text(encoding="utf-8"))
            self.assertEqual(len(saved["discovery"]), 2)
            self.assertTrue(archive.verify()["ok"])

    def test_next_page_disconnect_does_not_lose_discovered_database_record(self):
        with tempfile.TemporaryDirectory() as directory, ReportArchive(Path(directory) / "archive.db") as archive:
            root = Path(directory)
            collector = self.make_collector(root, archive)
            caps = {"capabilities": {"RPCGetBattleBlockList": True, "RPCGetChildCombatInfoList": True},
                    "segmentLength": 10, "relationAll": 1000}
            def page(scope, index, relation, segment, parent=None):
                if index != 1:
                    raise ConnectionError("disconnected on following page")
                return [{"combatInfo": {"battleID": 123}}], 11, {"sIndex": 1, "reqLen": 10}, root / "page.json"
            with patch.object(collector, "page", side_effect=page):
                summary = collector.discover_scope("personal", caps)
            self.assertFalse(summary["complete"])
            self.assertEqual(archive.stats()["records"], 1)
            self.assertEqual(archive.list_reports()[0]["battle_uid"], "123")

    def test_failed_discovery_import_is_durable_and_retried_without_game(self):
        with tempfile.TemporaryDirectory() as directory, ReportArchive(Path(directory) / "archive.db") as archive:
            root = Path(directory)
            collector = self.make_collector(root, archive, lambda path: {"status": "failed", "error": "database busy"})
            self.observe(collector, root)
            entry = collector.state["reports"]["123"]
            self.assertIn("database busy", entry["discoveryImportError"])
            durable = Path(entry["pendingDiscoveryImport"]["path"])
            self.assertTrue(durable.exists())
            original = durable.read_bytes()
            resumed = BattleCollector(None, collector.out_dir, namespace="account", request_delay=0,
                                     on_export=lambda path: archive.import_file(path, namespace="account"))
            with patch("capture.game_assets.battle_collector.capture_report") as capture:
                self.observe(resumed, root)
                capture.assert_not_called()
            entry = resumed.state["reports"]["123"]
            self.assertNotIn("pendingDiscoveryImport", entry)
            self.assertNotIn("discoveryImportError", entry)
            self.assertFalse(entry["complete"])
            self.assertEqual(durable.read_bytes(), original)
            self.assertEqual(archive.stats()["records"], 1)

    def test_basic_discovery_never_replaces_existing_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as directory, ReportArchive(Path(directory) / "archive.db") as archive:
            root = Path(directory)
            data = {"battleID": 123, "eventDataList": [{"eventId": 1}]}
            complete = {"battleID": 123, "source": "all", "eventCount": 1, "data": data, "captureVersion": 2,
                        "components": {name: copy.deepcopy(data) for name in ("basic", "detail", "all", "statics", "snapshots")},
                        "componentStatus": {name: {"available": True, "complete": True} for name in ("basic", "detail", "all", "statics", "snapshots")},
                        "serializationIssues": [], "captureComplete": True}
            imported = archive.import_bytes(json.dumps(complete).encode(), namespace="account")
            current = imported["snapshot_ids"][0]
            collector = self.make_collector(root, archive)
            self.observe(collector, root)
            report = archive.list_reports()[0]
            self.assertEqual(report["current_snapshot_id"], current)
            self.assertEqual(report["completeness"], "complete")
            self.assertEqual(archive.stats()["snapshots"], 2)
            self.assertTrue(archive.verify()["ok"])

    def test_offline_replay_does_not_change_seen_cycle_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory, ReportArchive(Path(directory) / "archive.db") as archive:
            root = Path(directory)
            collector = self.make_collector(root, archive)
            entry = {"battleID": "123", "complete": False, "lastSeenCycle": 2,
                     "observations": {"old": {"scope": "union", "parent": "999", "row": {"battleID": 123},
                                               "responseMetadata": {"reqLen": 1}}}}
            collector.state["reports"]["123"] = entry
            collector.archive_discovery(entry)
            collector.archive_discovery(entry)
            self.assertEqual(entry["lastSeenCycle"], 2)
            self.assertFalse(entry["complete"])
            self.assertEqual(archive.stats()["snapshots"], 1)

    def test_old_state_observations_are_archived_before_retry_backoff_skip(self):
        with tempfile.TemporaryDirectory() as directory, ReportArchive(Path(directory) / "archive.db") as archive:
            root = Path(directory)
            initial = self.make_collector(root, archive)
            initial.state["reports"]["123"] = {
                "battleID": "123", "complete": False, "lastSeenCycle": 2,
                "nextRetryAt": time.time() + 3600,
                "observations": {"saved-before-interruption": {
                    "scope": "personal", "parent": None,
                    "row": {"combatInfo": {"battleID": 123, "unknown": {"retained": 0}}},
                    "responseMetadata": {"reqLen": 1}}},
            }
            initial.save()
            resumed = BattleCollector(None, initial.out_dir, namespace="account",
                on_export=lambda path: archive.import_file(path, namespace="account"))
            with patch("capture.game_assets.battle_collector.capture_report") as capture:
                result = resumed.collect_pending()
                capture.assert_not_called()
            entry = resumed.state["reports"]["123"]
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(entry["lastSeenCycle"], 2)
            self.assertFalse(entry["complete"])
            self.assertTrue(Path(entry["discoveryArchivePath"]).exists())
            self.assertNotIn("pendingDiscoveryImport", entry)
            self.assertEqual(archive.stats()["records"], 1)
            report = archive.list_reports()[0]
            self.assertEqual(report["completeness"], "partial")
            payload = archive.get_payload(report["current_snapshot_id"])
            self.assertEqual(payload["components"]["basic"]["unknown"], {"retained": 0})

    def test_import_failure_cannot_look_like_complete_coverage(self):
        class Runner:
            def execute_on_update(self, code, timeout=10):
                if "local capabilities" in code:
                    return {"ok": True, "result": json.dumps({"payload": {"capabilities": {}, "runtimeIdentity": IDENTITY}})}
                return {"ok": True, "result": "collector detached"}
        with tempfile.TemporaryDirectory() as directory:
            collector = BattleCollector(Runner(), Path(directory), scopes=("personal",))
            def discover(scope, caps):
                collector.state["reports"]["123"] = {"battleID": "123", "complete": True,
                    "lastSeenCycle": collector.state["cycles"], "discoveryImportError": "database busy"}
                return {"complete": True}
            with patch.object(collector, "discover_scope", side_effect=discover), patch.object(collector, "collect_pending", return_value={}):
                summary = collector.run_cycle()
            self.assertFalse(summary["coverageComplete"])
            self.assertFalse(summary["complete"])
            self.assertEqual(summary["errors"][0]["phase"], "discovery_import")
