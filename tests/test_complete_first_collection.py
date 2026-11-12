"""Complete known and newly paged reports without postponing all detail work."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from capture.game_assets.battle_collector import BattleCollector
from capture.game_assets.export_battle_report_runtime import atomic_json, atomic_bytes
from parser.report_archive import ReportArchive

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None


TEMP = Path(".runtime/test-temp")
BASE = """
CommonConst={SEG_LEN=2,BattleBaseRelationType={ALL=1000}}
ClientConst={ReqBattleDiffType={Normal=1}}
G_SCENARIO_ID=14
ClientProxy={avatarId=42,GetServerId=function() return 1 end,GetUnionId=function() return 1 end}
ClientProxy.RPCGetBattleBlockListResponse=function() end
ClientProxy.RPCGetChildCombatInfoListResponse=function() end
"""


class LuaRunner:
    def __init__(self, setup):
        self.events = []
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().record = self.events.append
        self.lua.execute(BASE + setup)

    def execute_on_update(self, code, timeout=10):
        try:
            return {"ok": True, "result": self.lua.execute(code)}
        except Exception as exc:
            return {"ok": False, "stage": "lua", "error": str(exc)}

    execute = execute_on_update


def entry(battle_id, **extra):
    return {"battleID": str(battle_id), "attempts": 0, "complete": False,
            "observations": {}, "lastSeenCycle": 0, **extra}


def report(root, battle_id, *, complete=True):
    path = root / f"battle_{battle_id}_decrypted.json"
    payload = {"battleID": battle_id, "source": "all", "data": {"battleID": battle_id,
        "eventDataList": [{"eventId": 1, "intList": []}]}, "components": {}}
    atomic_json(path, payload)
    return {"battleID": battle_id, "exported": True, "complete": complete,
            "path": str(path), "eventCount": 1}


class CompleteFirstStateTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="complete_first_", dir=TEMP)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def observe(self, collector, battle_id=123, **extra):
        row = {"combatInfo": {"battleID": battle_id}, **extra}
        return collector.remember("personal", row, {"sIndex": 1, "reqLen": 1}, self.root / "page.json")

    def test_filtered_pending_preserves_default_and_empty_filter(self):
        collector = BattleCollector(None, self.root)
        collector.state["reports"] = {str(i): entry(i) for i in (1, 2, 3)}
        with patch("capture.game_assets.battle_collector.capture_report",
                   side_effect=lambda runner, bid, *a, **k: report(self.root, bid)) as capture:
            self.assertEqual(collector.collect_pending(battle_ids=[])["exported"], 0)
            self.assertEqual(collector.collect_pending(battle_ids=[2])["complete"], 1)
            self.assertEqual([call.args[1] for call in capture.call_args_list], [2])
            self.assertEqual(collector.collect_pending()["complete"], 2)
            self.assertEqual([call.args[1] for call in capture.call_args_list], [2, 1, 3])

    def test_page_discovery_has_one_pending_checkpoint_per_changed_row(self):
        collector = BattleCollector(None, self.root, on_export=lambda path: {"status": "imported"})
        collector._collect_pages = True
        with patch.object(collector, "save", wraps=collector.save) as save:
            self.observe(collector)
            self.assertEqual(save.call_count, 1)
        saved = json.loads(collector.state_file.read_text(encoding="utf-8"))
        durable_entry = saved["reports"]["123"]
        self.assertIn("pendingDiscoveryImport", durable_entry)
        self.assertTrue(Path(durable_entry["discoveryArchivePath"]).exists())
        self.assertNotIn("pendingDiscoveryImport", collector.state["reports"]["123"])
        # The page-level save publishes all successful import flags together.
        collector.save()
        saved = json.loads(collector.state_file.read_text(encoding="utf-8"))
        self.assertNotIn("pendingDiscoveryImport", saved["reports"]["123"])

    def test_deferred_success_checkpoint_replays_committed_bytes_after_restart(self):
        with ReportArchive(self.root / "reports.db") as archive:
            imports = []
            def ingest(path):
                imports.append(Path(path).read_bytes())
                return archive.import_file(path, namespace="account")
            collector = BattleCollector(None, self.root / "captures", namespace="account", on_export=ingest)
            collector._collect_pages = True
            self.observe(collector)
            # Simulate a stop before the page checkpoint: disk still says pending,
            # while SQLite already committed the immutable capture.
            resumed = BattleCollector(None, collector.out_dir, namespace="account", on_export=ingest)
            resumed_entry = resumed.state["reports"]["123"]
            self.assertIn("pendingDiscoveryImport", resumed_entry)
            with patch("capture.game_assets.battle_collector.capture_report") as capture:
                resumed.retry_discovery_import(resumed_entry)
                capture.assert_not_called()
            self.assertEqual(len(imports), 2)
            self.assertEqual(imports[0], imports[1])
            self.assertEqual(archive.stats()["records"], 1)
            self.assertEqual(archive.stats()["snapshots"], 1)
            self.assertTrue(archive.verify()["ok"])

    def test_failed_import_is_checkpointed_even_when_success_is_deferred(self):
        collector = BattleCollector(None, self.root, on_export=lambda path: {"status": "failed", "error": "busy"})
        collector._collect_pages = True
        with patch.object(collector, "save", wraps=collector.save) as save:
            self.observe(collector)
            self.assertEqual(save.call_count, 2)  # pending, then durable failure
        saved = json.loads(collector.state_file.read_text(encoding="utf-8"))["reports"]["123"]
        self.assertIn("busy", saved["discoveryImportError"])
        self.assertIn("pendingDiscoveryImport", saved)

    def test_capture_file_failure_still_checkpoints_the_observation(self):
        collector = BattleCollector(None, self.root)
        original = atomic_bytes
        def fail_capture(path, value, **kwargs):
            if "captures" in Path(path).parts:
                raise PermissionError("capture file unavailable")
            return original(path, value, **kwargs)
        with patch("capture.game_assets.battle_collector.atomic_bytes", side_effect=fail_capture):
            with self.assertRaisesRegex(PermissionError, "capture file unavailable"):
                self.observe(collector)
        saved = json.loads(collector.state_file.read_text(encoding="utf-8"))["reports"]["123"]
        self.assertTrue(saved["observations"])
        self.assertIn("capture file unavailable", saved["discoveryImportError"])

    def test_changed_observation_does_not_erase_future_component_backoff(self):
        collector = BattleCollector(None, self.root)
        self.observe(collector)
        retry = time.time() + 3600
        collector.state["reports"]["123"]["nextRetryAt"] = retry
        self.observe(collector, changed="yes")
        self.assertEqual(collector.state["reports"]["123"]["nextRetryAt"], retry)
        with patch("capture.game_assets.battle_collector.capture_report") as capture:
            self.assertEqual(collector.collect_pending(battle_ids=[123])["skipped"], 1)
            capture.assert_not_called()

    def test_failed_new_capture_retries_same_observation_even_when_old_archive_exists(self):
        with ReportArchive(self.root / "reports.db") as archive:
            collector = BattleCollector(None, self.root / "runtime", on_export=archive.import_file)
            self.observe(collector)
            entry = collector.state["reports"]["123"]
            first_path = Path(entry["discoveryArchivePath"])
            first_bytes = first_path.read_bytes()
            original = atomic_bytes
            def fail_capture(path, value, **kwargs):
                if "captures" in Path(path).parts:
                    raise PermissionError("new immutable file blocked")
                return original(path, value, **kwargs)
            with patch("capture.game_assets.battle_collector.atomic_bytes", side_effect=fail_capture):
                with self.assertRaises(PermissionError):
                    self.observe(collector, changed="new observation")
            self.assertEqual(archive.stats()["snapshots"], 1)
            # The next identical row is not a changed observation, but its
            # earlier publication failure still needs retrying.
            self.observe(collector, changed="new observation")
            self.assertEqual(archive.stats()["snapshots"], 2)
            self.assertNotIn("discoveryImportError", entry)
            self.assertEqual(first_path.read_bytes(), first_bytes)
            self.assertTrue(archive.verify()["ok"])


@unittest.skipIf(LuaRuntime is None, "optional Lua 5.1 test runtime")
class CompleteFirstRuntimeTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="complete_first_lua_", dir=TEMP)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_current_root_and_child_pages_complete_before_old_network_batch(self):
        runner = LuaRunner("""
ClientProxy.RPCGetBattleBlockList=function(self,index,tp)
 record('page:'..index)
 local rows=index==1 and {{combatInfo={battleID=20},childNum=1},{combatInfo={battleID=21}}}
   or {{combatInfo={battleID=22}}}
 self:RPCGetBattleBlockListResponse(1,{sIndex=index,tp=tp,reqLen=#rows,blockInfoList=rows})
end
ClientProxy.RPCGetChildCombatInfoList=function(self,parent,index,tp)
 record('child:'..parent..':'..index)
 self:RPCGetChildCombatInfoListResponse(1,{sIndex=index,tp=tp,battleID=parent,reqLen=1,combatInfoList={{battleID=30}}})
end
""")
        collector = BattleCollector(runner, self.root, scopes=("personal",), wait_seconds=0, request_delay=0)
        collector.state["reports"] = {str(i): entry(i) for i in (10, 11, 12)}
        collector.state["reports"]["9"] = entry(9, complete=True)
        def capture(raw_runner, bid, *args, **kwargs):
            # Component requests begin only after list response consumption and
            # the entire page's observations/cursor have reached disk.
            self.assertIsNone(runner.lua.globals()["__codexBattleCollector"].pending)
            disk = json.loads(collector.state_file.read_text(encoding="utf-8"))
            if bid in (20, 21):
                self.assertEqual(disk["scopes"]["personal"]["index"], 3)
                self.assertTrue({"20", "21"} <= set(disk["reports"]))
            runner.events.append('capture:' + str(bid))
            return report(self.root, bid)
        collector.save()
        with patch("capture.game_assets.battle_collector.capture_report", side_effect=capture):
            summary = collector.run_cycle()
        self.assertEqual(runner.events[0], 'page:1')
        self.assertCountEqual(runner.events[1:3], ['capture:20', 'capture:21'])
        self.assertEqual(runner.events[3:], ['page:3', 'capture:22', 'child:20:1', 'capture:30',
                                            'capture:10', 'capture:11', 'capture:12'])
        self.assertEqual(summary["collection"]["complete"], 7)
        self.assertTrue(summary["complete"])
        self.assertTrue(summary["scopes"]["personal"]["children"]["20"]["complete"])
        self.assertEqual(summary["visibleReports"], 4)
        self.assertEqual(collector.state["reports"]["10"]["lastSeenCycle"], 0)
        self.assertIsNone(runner.lua.globals()["__codexBattleCollector"])

    def test_old_failure_is_not_requested_again_when_rediscovered_in_same_cycle(self):
        runner = LuaRunner("""
ClientProxy.RPCGetBattleBlockList=function(self,index,tp)
 record('page:'..index)
 self:RPCGetBattleBlockListResponse(1,{sIndex=index,tp=tp,reqLen=1,blockInfoList={{combatInfo={battleID=10},newValue=1}}})
end
ClientProxy.RPCGetChildCombatInfoList=function() end
""")
        collector = BattleCollector(runner, self.root, scopes=("personal",), wait_seconds=0, request_delay=0)
        collector.state["reports"]["10"] = entry(10)
        with patch("capture.game_assets.battle_collector.capture_report", return_value={
                "battleID": 10, "exported": False, "complete": False, "error": "timeout"}) as capture:
            summary = collector.run_cycle()
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(collector.state["reports"]["10"]["attempts"], 1)
        self.assertGreater(collector.state["reports"]["10"]["nextRetryAt"], time.time())
        self.assertEqual(summary["collection"]["failed"], 1)
        self.assertFalse(summary["complete"])
        self.assertTrue(summary["enumerationComplete"])

    def test_repeated_page_id_reuses_complete_components_without_another_request(self):
        runner = LuaRunner("""
ClientProxy.RPCGetBattleBlockList=function(self,index,tp)
 record('page:'..index)
 local rows=index==1 and {{combatInfo={battleID=20}},{combatInfo={battleID=21}}}
   or {{combatInfo={battleID=20},changedRow=true}}
 self:RPCGetBattleBlockListResponse(1,{sIndex=index,tp=tp,reqLen=#rows,blockInfoList=rows})
end
ClientProxy.RPCGetChildCombatInfoList=function() end
""")
        collector = BattleCollector(runner, self.root, scopes=("personal",), wait_seconds=0, request_delay=0)
        with patch("capture.game_assets.battle_collector.capture_report",
                   side_effect=lambda raw, bid, *a, **k: report(self.root, bid)) as capture:
            summary = collector.run_cycle()
        self.assertCountEqual([call.args[1] for call in capture.call_args_list], [20, 21])
        self.assertTrue(summary["complete"])
        self.assertEqual(len(collector.state["reports"]["20"]["observations"]), 2)

    def test_stop_after_first_page_capture_cleans_hooks_without_starting_more_work(self):
        runner = LuaRunner("""
ClientProxy.RPCGetBattleBlockList=function(self,index,tp)
 record('page')
 self:RPCGetBattleBlockListResponse(1,{sIndex=index,tp=tp,reqLen=1,blockInfoList={{combatInfo={battleID=10}}}})
end
ClientProxy.RPCGetChildCombatInfoList=function() end
""")
        collector = BattleCollector(runner, self.root, scopes=("personal",), wait_seconds=0, request_delay=0)
        collector.state["reports"] = {str(i): entry(i) for i in (10, 11)}
        stop = threading.Event()
        collector._stop_event = stop
        def capture(raw, bid, *args, **kwargs):
            stop.set()
            return report(self.root, bid)
        with patch("capture.game_assets.battle_collector.capture_report", side_effect=capture) as capture_call:
            summary = collector.run_cycle()
        self.assertEqual(capture_call.call_count, 1)
        self.assertEqual(runner.events, ['page'])
        self.assertIsNone(runner.lua.globals()["__codexBattleCollector"])
        self.assertFalse(summary["complete"])
        self.assertFalse(collector._collect_pages)
        self.assertIsNone(collector._cycle_attempted)

    def test_progress_reports_durable_pages_and_actual_capture_without_breaking_flow(self):
        runner = LuaRunner("""
ClientProxy.RPCGetBattleBlockList=function(self,index,tp)
 self:RPCGetBattleBlockListResponse(1,{sIndex=index,tp=tp,reqLen=1,blockInfoList={{combatInfo={battleID=20}}}})
end
ClientProxy.RPCGetChildCombatInfoList=function() end
""")
        events = []
        def progress(value):
            events.append(value)
            if value["phase"] == "page_received":
                disk = json.loads(collector.state_file.read_text(encoding="utf-8"))
                self.assertIn("20", disk["reports"])
                self.assertIsNone(disk["scopes"]["personal"]["index"])
            raise OSError("terminal closed")
        collector = BattleCollector(runner, self.root, scopes=("personal",), wait_seconds=0,
                                    request_delay=0, on_progress=progress)
        with patch("capture.game_assets.battle_collector.capture_report",
                   side_effect=lambda raw, bid, *a, **k: report(self.root, bid)):
            result = collector.run_cycle()
        self.assertTrue(result["complete"])
        self.assertEqual([event for event in events if event["phase"] != "capture_finished"],
                         [{"phase": "page_received", "scope": "personal", "index": 1, "count": 1},
                          {"phase": "capture_started", "battleID": "20"}])
        finished = [event for event in events if event["phase"] == "capture_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["outcome"], "complete")


if __name__ == "__main__":
    unittest.main()
