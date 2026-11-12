"""Bound network in-flight work while all Lua calls and commits stay serial."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from capture.game_assets.battle_collector import BattleCollector, install_lua, cleanup_lua
from capture.game_assets.export_battle_report_runtime import atomic_json, prefetch_reports as real_prefetch, run_checked
from parser.report_archive import ReportArchive

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None


TEMP = Path(".runtime/test-temp")
IDENTITY = {"avatarId": "42", "serverId": "1", "scenarioId": "14"}


def entry(battle_id, **extra):
    return {"battleID": str(battle_id), "complete": False, "attempts": 0,
            "observations": {}, "lastSeenCycle": 0, **extra}


def metadata(ids):
    return {str(bid): {name: {"battleID": int(bid), "component": name,
        "requested": True, "deadline": time.monotonic() + 20}
        for name in ("all", "statics")} for bid in ids}


def report(root, bid, *, complete=True):
    data = {"battleID": int(bid), "eventDataList": [{"eventId": 1, "intList": [int(bid)]}]}
    components = {name: data if name in ("all", "detail", "basic") else {} for name in
                  ("basic", "detail", "all", "statics", "snapshots")} if complete else {"basic": data}
    payload = {"battleID": int(bid), "source": "all" if complete else "basic", "captureVersion": 2,
        "data": data, "components": components, "captureComplete": complete, "eventCount": 1,
        "componentStatus": {name: {"available": name in components, "complete": name in components}
                            for name in ("basic", "detail", "all", "statics", "snapshots")}}
    path = root / f"battle_{bid}_decrypted.json"
    atomic_json(path, payload)
    return {"battleID": int(bid), "exported": True, "complete": complete, "path": str(path), "eventCount": 1}


class ParallelCollectionTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="parallel_collection_", dir=TEMP)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def collector(self, ids=(1, 2), **kwargs):
        collector = BattleCollector(None, self.root / "runtime", parallelism=2, **kwargs)
        collector.state["runtimeIdentity"] = dict(IDENTITY)
        collector.state["reports"] = {str(i): entry(i) for i in ids}
        return collector

    def test_parallelism_validation_before_any_file_is_created(self):
        for value in (0, 5, True, 1.5, "2"):
            target = self.root / str(value)
            with self.assertRaisesRegex(ValueError, "parallelism"):
                BattleCollector(None, target, parallelism=value)
            self.assertFalse(target.exists())
        self.assertEqual(BattleCollector(None, self.root / "default").parallelism, 1)

    def test_candidates_exclude_completed_metadata_pending_backoff_and_attempted(self):
        collector = self.collector(ids=range(1, 8))
        collector.parallelism = 4
        rows = collector.state["reports"]
        rows["2"]["complete"] = True
        rows["3"].update(metadataDirty=True, lastResult={"complete": True})
        rows["4"]["pendingImport"] = {"path": "not-requestable"}
        rows["5"]["nextRetryAt"] = time.time() + 100
        collector._cycle_attempted = {"1", "6"}
        self.assertEqual(collector._parallel_candidates(list(rows.values()), None, "1"), ["1", "7"])
        self.assertEqual(collector._parallel_candidates(list(rows.values()), {"1"}, "1"), ["1"])

    def test_groups_are_bounded_and_prefetched_metadata_is_bound_per_id(self):
        collector = self.collector(ids=range(1, 6))
        trace = []
        def prefetch(runner, ids, **kwargs):
            self.assertEqual(kwargs["min_request_interval"], 1.0)
            self.assertEqual(kwargs["min_dispatch_interval"], 1.0)
            self.assertFalse(kwargs["prefetch_snapshots"])
            trace.append(("prefetch", list(ids)))
            return metadata(ids)
        def capture(runner, bid, *args, **kwargs):
            prior = kwargs["prefetched_requests"]
            self.assertTrue(kwargs["request_missing"])
            self.assertEqual(prior["all"]["battleID"], bid)
            trace.append(("capture", bid))
            return report(collector.out_dir, bid)
        with patch("capture.game_assets.battle_collector.prefetch_reports", side_effect=prefetch), \
             patch("capture.game_assets.battle_collector.capture_report", side_effect=capture):
            result = collector.collect_pending()
        self.assertEqual([x for x in trace if x[0] == "prefetch"],
                         [("prefetch", ["1", "2"]), ("prefetch", ["3", "4"]), ("prefetch", ["5"])])
        self.assertEqual(result["complete"], 5)
        self.assertEqual(result["failed"], 0)

    def test_prefetch_gaps_follow_configured_delay_with_one_second_method_floor(self):
        for delay in (.25, 1.75):
            collector = self.collector(ids=(1, 2))
            collector.request_delay = delay
            observed_gaps = []
            def prefetch(runner, ids, **kwargs):
                observed_gaps.append((kwargs["min_request_interval"], kwargs["min_dispatch_interval"],
                                      kwargs["prefetch_snapshots"]))
                return metadata(ids)
            with patch("capture.game_assets.battle_collector.prefetch_reports", side_effect=prefetch), \
                 patch("capture.game_assets.battle_collector.capture_report",
                       side_effect=lambda r, bid, *a, **k: report(collector.out_dir, bid)):
                self.assertEqual(collector.collect_pending()["complete"], 2)
            self.assertEqual(observed_gaps, [(max(1.0, delay), max(1.0, delay), False)])

    def test_keyboard_interrupt_exports_all_prefetched_caches_without_new_requests(self):
        with ReportArchive(self.root / "reports.db") as archive:
            collector = self.collector(on_export=lambda path: archive.import_file(path, namespace="account"))
            calls = []
            def capture(runner, bid, *args, **kwargs):
                calls.append((bid, kwargs["request_missing"]))
                if kwargs["request_missing"]:
                    raise KeyboardInterrupt()
                self.assertEqual(kwargs["prefetched_requests"]["all"]["battleID"], bid)
                return report(collector.out_dir, bid, complete=False)
            with patch("capture.game_assets.battle_collector.prefetch_reports", side_effect=lambda r, ids, **k: metadata(ids)) as prefetch, \
                 patch("capture.game_assets.battle_collector.capture_report", side_effect=capture):
                with self.assertRaises(KeyboardInterrupt):
                    collector.collect_pending()
            self.assertEqual(calls, [(1, True), (1, False), (2, False)])
            self.assertEqual(prefetch.call_count, 1)
            self.assertEqual(archive.stats()["records"], 2)
            self.assertEqual(archive.stats()["current_completeness"], {"partial": 2})
            self.assertTrue(archive.verify()["ok"])
            self.assertEqual([collector.state["reports"][str(i)]["attempts"] for i in (1, 2)], [1, 1])

    def test_stop_between_reports_harvests_unprocessed_member(self):
        collector = self.collector()
        stop = threading.Event()
        collector._stop_event = stop
        calls = []
        def capture(runner, bid, *args, **kwargs):
            calls.append((bid, kwargs["request_missing"]))
            if kwargs["request_missing"]:
                stop.set()
            return report(collector.out_dir, bid, complete=kwargs["request_missing"])
        with patch("capture.game_assets.battle_collector.prefetch_reports", side_effect=lambda r, ids, **k: metadata(ids)), \
             patch("capture.game_assets.battle_collector.capture_report", side_effect=capture):
            result = collector.collect_pending()
        self.assertEqual(calls, [(1, True), (2, False)])
        self.assertEqual((result["complete"], result["partial"]), (1, 1))
        self.assertTrue(collector.state["reports"]["1"]["complete"])
        self.assertFalse(collector.state["reports"]["2"]["complete"])

    def test_interrupt_during_prefetch_still_harvests_reserved_ids(self):
        collector = self.collector()
        calls = []
        def capture(runner, bid, *args, **kwargs):
            self.assertFalse(kwargs["request_missing"])
            calls.append(bid)
            return report(collector.out_dir, bid, complete=False)
        with patch("capture.game_assets.battle_collector.prefetch_reports", side_effect=KeyboardInterrupt), \
             patch("capture.game_assets.battle_collector.capture_report", side_effect=capture):
            with self.assertRaises(KeyboardInterrupt):
                collector.collect_pending()
        self.assertEqual(calls, [1, 2])

    def test_prefetch_batch_error_does_not_redispatch_or_mix_battles(self):
        collector = self.collector()
        def capture(runner, bid, *args, **kwargs):
            self.assertFalse(kwargs["request_missing"])
            prior = kwargs["prefetched_requests"]
            self.assertEqual(prior["all"]["battleID"], bid)
            self.assertIn("prefetch disconnected", prior["all"]["error"])
            return report(collector.out_dir, bid, complete=False)
        with patch("capture.game_assets.battle_collector.prefetch_reports", side_effect=RuntimeError("prefetch disconnected")), \
             patch("capture.game_assets.battle_collector.capture_report", side_effect=capture) as capture_call:
            result = collector.collect_pending()
        self.assertEqual(capture_call.call_count, 2)
        self.assertEqual(result["partial"], 2)
        for bid in (1, 2):
            saved = json.loads(Path(collector.state["reports"][str(bid)]["lastResult"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["battleID"], bid)
            self.assertEqual(saved["data"]["eventDataList"][0]["intList"], [bid])

    def test_failed_database_commit_retries_durable_file_without_another_capture(self):
        calls = []
        def importer(path):
            bid = json.loads(Path(path).read_text(encoding="utf-8"))["battleID"]
            if bid == 1 and not calls:
                calls.append(Path(path).read_bytes())
                raise RuntimeError("database unavailable")
            return {"status": "imported"}
        collector = self.collector(on_export=importer)
        with patch("capture.game_assets.battle_collector.prefetch_reports", side_effect=lambda r, ids, **k: metadata(ids)), \
             patch("capture.game_assets.battle_collector.capture_report", side_effect=lambda r, bid, *a, **k: report(collector.out_dir, bid)) as capture:
            result = collector.collect_pending()
        self.assertEqual([c.args[1] for c in capture.call_args_list], [1, 2])
        self.assertEqual(result["failed"], 1)
        pending = collector.state["reports"]["1"]["pendingImport"]
        self.assertEqual(Path(pending["path"]).read_bytes(), calls[0])
        collector.state["reports"]["1"]["nextRetryAt"] = 0
        with patch("capture.game_assets.battle_collector.capture_report") as capture:
            self.assertEqual(collector.collect_pending([1])["complete"], 1)
            capture.assert_not_called()

    def test_state_checkpoint_failure_does_not_abandon_later_inflight_caches(self):
        collector = self.collector()
        inflight = {bid: {"metadata": meta} for bid, meta in metadata((1, 2)).items()}
        counts = collector._empty_collection_counts()
        with patch.object(collector, "save", side_effect=PermissionError("state file locked")), \
             patch("capture.game_assets.battle_collector.capture_report",
                   side_effect=lambda r, bid, *a, **k: report(collector.out_dir, bid)) as capture:
            errors = collector._harvest_inflight(inflight, counts, set())
        self.assertEqual([call.args[1] for call in capture.call_args_list], [1, 2])
        self.assertTrue(all(not call.kwargs["request_missing"] for call in capture.call_args_list))
        self.assertEqual(inflight, {})
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(error["phase"] == "prefetch_recovery_checkpoint" for error in errors))
        self.assertEqual(counts["failed"], 2)
        for bid in (1, 2):
            self.assertFalse(collector.state["reports"][str(bid)]["complete"])
            revisions = list((collector.out_dir / 'captures' / str(bid)).glob('*.json'))
            self.assertEqual(len(revisions), 1)
            self.assertEqual(json.loads(revisions[0].read_text(encoding='utf-8'))['battleID'], bid)

    def test_drain_checkpoint_error_is_aggregated_instead_of_returning_success(self):
        collector = self.collector()
        stop = threading.Event()
        collector._stop_event = stop
        original_save, saves = collector.save, []
        def save():
            saves.append(True)
            if len(saves) >= 3:
                raise PermissionError("checkpoint unavailable")
            original_save()
        def capture(runner, bid, *args, **kwargs):
            if kwargs["request_missing"]:
                stop.set()
            return report(collector.out_dir, bid, complete=kwargs["request_missing"])
        with patch.object(collector, 'save', side_effect=save), \
             patch('capture.game_assets.battle_collector.prefetch_reports', side_effect=lambda r, ids, **k: metadata(ids)), \
             patch('capture.game_assets.battle_collector.capture_report', side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, 'Prefetched cache recovery failed'):
                collector.collect_pending()
        errors = collector.state['prefetchRecoveryErrors']
        self.assertEqual(errors[0]['battleID'], '2')
        error_log = Path(collector.state['prefetchRecoveryErrorLog'])
        self.assertEqual(json.loads(error_log.read_text(encoding='utf-8'))['errors'], errors)
        self.assertTrue(list((collector.out_dir / 'captures' / '2').glob('*.json')))

    def test_ctrl_c_survives_checkpoint_failures_and_all_prefetched_ids_are_saved(self):
        collector = self.collector()
        flags = {'prefetched': False, 'cleanup': False}
        class Runner:
            def execute_on_update(self, code, timeout=10):
                if 'local capabilities' in code:
                    return {'ok': True, 'result': json.dumps({'payload': {'runtimeIdentity': IDENTITY,
                        'segmentLength': 2, 'capabilities': {}}})}
                flags['cleanup'] = True
                return {'ok': True, 'result': 'collector detached'}
        collector.raw_runner = Runner()
        original_save = collector.save
        def save():
            if flags['prefetched']:
                raise PermissionError('state remains locked during stop')
            original_save()
        def prefetch(runner, ids, **kwargs):
            flags['prefetched'] = True
            return metadata(ids)
        def capture(runner, bid, *args, **kwargs):
            if kwargs['request_missing']:
                raise KeyboardInterrupt()
            return report(collector.out_dir, bid, complete=False)
        with patch.object(collector, 'save', side_effect=save), \
             patch('capture.game_assets.battle_collector.prefetch_reports', side_effect=prefetch), \
             patch('capture.game_assets.battle_collector.capture_report', side_effect=capture) as calls:
            with self.assertRaises(KeyboardInterrupt):
                collector.run_cycle()
        self.assertEqual([(call.args[1], call.kwargs['request_missing']) for call in calls.call_args_list],
                         [(1, True), (1, False), (2, False)])
        self.assertTrue(flags['cleanup'])
        manifest = json.loads((collector.out_dir / 'collector_manifest.json').read_text(encoding='utf-8'))
        self.assertFalse(manifest['complete'])
        self.assertTrue(any(e.get('phase') == 'cycle_checkpoint' for e in manifest['errors']))
        self.assertTrue(Path(manifest['prefetchRecoveryErrorLog']).exists())
        for bid in (1, 2):
            self.assertTrue(list((collector.out_dir / 'captures' / str(bid)).glob('*.json')))


LUA_SETUP = """
CommonConst={SEG_LEN=10,BattleBaseRelationType={ALL=1000}}
ClientConst={ReqBattleDiffType={Normal=1}}
G_SCENARIO_ID=14
ClientProxy={avatarId=42,GetServerId=function() return 1 end,
 GetUnionId=function() return 1 end,all={},detail={},reportStaticsInfo={},allBattleRecordInfo={},pendingSnapshots={}}
ClientProxy.RPCGetBattleBlockList=function() end
ClientProxy.RPCGetBattleBlockListResponse=function() end
ClientProxy.RPCGetChildCombatInfoList=function() end
ClientProxy.RPCGetChildCombatInfoListResponse=function() end
ClientProxy.GetCombatInfo=function(self,id) return {battleID=id} end
ClientProxy.GetAllCombatInfo=function(self,id)
 if self.pendingSnapshots[id] then
  self.pendingSnapshots[id]=nil;self.allBattleRecordInfo[id].isComplete=true
  record('snapshot_complete:'..id)
 end
 return self.all[id]
end
ClientProxy.GetDetailCombatInfo=function(self,id) return self.detail[id] end
ClientProxy.RPCGetAllCombatInfo=function(self,id)
 record('all:'..id)
 local b={battleID=id,eventDataList={{eventId=1,intList={id},tab=1}}}
 self.all[id]=b; self.detail[id]=b
end
ClientProxy.RPCGetDetailCombatInfo=function(self,id) error('redundant detail request '..id) end
ClientProxy.RPCGetStaticsCombatInfo=function(self,id) record('statics:'..id);self.reportStaticsInfo[id]={damage=id} end
ClientProxy.RPCGetHeroBattleSnapRecords=function(self,id)
 record('snapshots:'..id);self.allBattleRecordInfo[id]={isComplete=false,battleSnapData={id=id}}
 self.pendingSnapshots[id]=true
end
"""


class LuaRunner:
    def __init__(self):
        self.events = []
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().record = self.events.append
        self.lua.execute(LUA_SETUP)

    def execute_on_update(self, code, timeout=10):
        try:
            return {"ok": True, "result": self.lua.execute(code)}
        except Exception as exc:
            return {"ok": False, "stage": "lua", "error": str(exc)}
    execute = execute_on_update


@unittest.skipIf(LuaRuntime is None, "optional Lua 5.1 test runtime")
class ParallelLuaIntegrationTests(unittest.TestCase):
    def run_pair(self, *, bad_meta=False, batch_error=False):
        TEMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="parallel_lua_", dir=TEMP) as directory:
            root = Path(directory)
            runner = LuaRunner()
            with ReportArchive(root / "reports.db") as archive:
                def ingest(path):
                    runner.events.append('import:' + str(json.loads(Path(path).read_text(encoding='utf-8'))['battleID']))
                    return archive.import_file(path, namespace="account")
                collector = BattleCollector(runner, root / "runtime", parallelism=2, on_export=ingest,
                                           wait_seconds=0, poll_seconds=.001)
                collector.state['runtimeIdentity'] = dict(IDENTITY)
                collector.state['reports'] = {str(i): entry(i) for i in (101, 102)}
                run_checked(runner, install_lua(collector.owner))
                try:
                    def prefetch(raw, ids, **kwargs):
                        result = real_prefetch(raw, ids, **kwargs)
                        if batch_error:
                            raise RuntimeError("prefetch acknowledgement lost after dispatch")
                        if bad_meta:
                            result['101']['all']['battleID'] = 102
                        return result
                    with patch('capture.game_assets.battle_collector.prefetch_reports', side_effect=prefetch):
                        counts = collector.collect_pending()
                finally:
                    run_checked(runner, cleanup_lua(collector.owner))
                self.assertEqual(runner.events[:4], ['all:101','statics:101','all:102','statics:102'])
                self.assertEqual(sum(e.startswith('all:') for e in runner.events), 2)
                self.assertEqual(sum(e.startswith('statics:') for e in runner.events), 2)
                self.assertEqual(sum(e.startswith('snapshots:') for e in runner.events),
                                 0 if batch_error else 1 if bad_meta else 2)
                if not bad_meta and not batch_error:
                    self.assertLess(runner.events.index('snapshots:101'),runner.events.index('snapshot_complete:101'))
                    self.assertLess(runner.events.index('snapshot_complete:101'),runner.events.index('import:101'))
                    self.assertLess(runner.events.index('import:101'),runner.events.index('snapshots:102'))
                    self.assertLess(runner.events.index('snapshot_complete:102'),runner.events.index('import:102'))
                self.assertEqual(archive.stats()['records'], 2)
                for bid in (101, 102):
                    row = archive.connection.execute('SELECT payload_json FROM report_current_snapshots WHERE battle_uid=?',(str(bid),)).fetchone()
                    saved = json.loads(row[0])
                    self.assertEqual(saved['battleID'],bid)
                    self.assertEqual(saved['data']['eventDataList'][0]['intList'],[bid])
                self.assertTrue(archive.verify()['ok'])
                return counts, archive.stats()['current_completeness']

    def test_two_inflight_reports_each_keep_complete_components_and_ordered_ids(self):
        counts, completeness = self.run_pair()
        self.assertEqual(counts['complete'],2)
        self.assertEqual(completeness,{'complete':2})

    def test_cross_battle_acknowledgement_is_rejected_and_own_cache_is_recovered(self):
        counts, completeness = self.run_pair(bad_meta=True)
        self.assertEqual(counts['failed'],1)
        self.assertEqual(completeness,{'complete':1,'partial':1})

    def test_lost_prefetch_acknowledgement_still_exports_received_caches_without_snapshot_rpc(self):
        counts, completeness = self.run_pair(batch_error=True)
        self.assertEqual(counts['partial'], 2)
        self.assertEqual(completeness, {'partial': 2})


if __name__ == '__main__':
    unittest.main()
