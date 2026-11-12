"""Offline collector/exporter contract tests; optional Lua 5.1 execution via lupa."""
import json
import io
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from capture.game_assets.battle_collector import (
    BattleCollector, CollectorLeaseRunner, validate_page, request_page_lua, install_lua, cleanup_lua,
)
from capture.game_assets.export_battle_report_runtime import (
    LUA_JSON, atomic_json, capture_report, decode_envelope, lua_string,
    safe_output_path, wait_for,
)
try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None


class LuaRunner:
    def __init__(self, setup=""):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(setup)

    def execute_on_update(self, code, timeout=10):
        try:
            return {"ok": True, "result": self.lua.execute(code)}
        except Exception as exc:
            return {"ok": False, "stage": "lua", "error": str(exc)}

    execute = execute_on_update


BASE = """
CommonConst={SEG_LEN=10,BattleBaseRelationType={ALL=1000}}
ClientConst={ReqBattleDiffType={Normal=1}}
G_SCENARIO_ID=14
ClientProxy={avatarId=42,GetServerId=function() return 1 end,GetUnionId=function() return 1 end}
"""


class PaginationTests(unittest.TestCase):
    def test_nonfinite_timers_are_rejected_before_any_runtime_call(self):
        with tempfile.TemporaryDirectory() as directory:
            for value in (float('nan'), float('inf'), -float('inf')):
                for keyword in ('wait_seconds', 'poll_seconds', 'request_delay'):
                    with self.assertRaises(ValueError):
                        BattleCollector(None, Path(directory), **{keyword: value})
                with self.assertRaises(ValueError):
                    capture_report(None, 123, Path(directory), wait_seconds=value)
                with self.assertRaises(ValueError):
                    wait_for(None, 123, 'all', 1, value)

    @unittest.skipUnless(os.name == "nt", "Windows drive policy")
    def test_c_drive_and_extended_c_drive_rejected_before_writing(self):
        for path in ("C:/never-created", chr(92)*2+"?"+chr(92)+"C:"+chr(92)+"never-created"):
            with self.assertRaises(ValueError):
                safe_output_path(path)
            with self.assertRaises(ValueError):
                BattleCollector(None, Path(path))

    def test_empty_full_segment_must_continue(self):
        rows, nxt = validate_page({"response":{"sIndex":1,"reqLen":10,"blockInfoList":[]}}, 1, 10)
        self.assertEqual(rows, [])
        self.assertEqual(nxt, 11)

    def test_short_segment_finishes(self):
        _, nxt = validate_page({"response":{"sIndex":11,"reqLen":2,"blockInfoList":[]}}, 11, 10)
        self.assertIsNone(nxt)

    def test_protocol_drift_cannot_look_like_empty_success(self):
        for response in ({}, {"sIndex":1,"blockInfoList":[]},
                         {"sIndex":1,"reqLen":0,"blockInfoList":[{}]}):
            with self.assertRaises(ValueError):
                validate_page({"response":response}, 1, 10)

    def test_server_error_and_oversize_segment_are_not_end_of_list(self):
        with self.assertRaises(ValueError):
            validate_page({"response":{"sIndex":1,"reqLen":0,"blockInfoList":[],"code":7}},1,10,success_code=0)
        with self.assertRaises(ValueError):
            validate_page({"response":{"sIndex":1,"reqLen":11,"blockInfoList":[]}},1,10)

    def test_favourite_parent_hash_stays_string(self):
        code = request_page_lua("owner","token","star",11,1000,"star_abc/123")
        self.assertIn("ClientProxy:RPCStarBattleChildList(11,", code)
        self.assertIn(lua_string("star_abc/123"), code)
        with self.assertRaises(ValueError):
            request_page_lua("owner","token","personal",1,1000,"star_abc")

    def test_different_namespace_refuses_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            atomic_json(root/"collector_state.json",{"version":1,"namespace":"account-a"})
            with self.assertRaises(ValueError):
                BattleCollector(None,root,namespace="account-b")


@unittest.skipIf(LuaRuntime is None, "Optional lupa.lua51 not installed")
class LuaTests(unittest.TestCase):
    def test_rejected_response_without_page_fields_is_retained_and_reported(self):
        runner=LuaRunner(BASE+"""
ErrCode={SUCCESS=0}
ClientProxy.RPCStarBattleChildList=function(self,index,parent)
  self.callbackOK=pcall(self.RPCStarBattleChildListResponse,self,91,{code=73})
end
ClientProxy.RPCStarBattleChildListResponse=function(self,msg,proto)
  error('original expected absent list fields')
end
""")
        with tempfile.TemporaryDirectory() as directory:
            collector=BattleCollector(runner,Path(directory),wait_seconds=0.01,request_delay=0)
            collector.state["runtimeCapabilities"]={"successCode":0}
            runner.lua.execute(install_lua(collector.owner))
            with self.assertRaisesRegex(ValueError,"Server rejected list request with code=73"):
                collector.page("star",1,1000,10,parent="favourite_parent")
            saved=list((Path(directory)/"list_responses").glob("*.json"))
            self.assertEqual(len(saved),1)
            value=json.loads(saved[0].read_text(encoding="utf-8"))["payload"]
            self.assertEqual(value["response"],{"code":73})
            self.assertIs(value["clientProcessingComplete"],False)

    def test_successful_unrelated_page_still_does_not_match_pending_request(self):
        runner=LuaRunner(BASE+"""
ErrCode={SUCCESS=0}
ClientProxy.RPCStarBattleBlockList=function(self,index)
  self:RPCStarBattleBlockListResponse(91,{code=0,sIndex=index+10,reqLen=0,blockInfoList={}})
end
ClientProxy.RPCStarBattleBlockListResponse=function() end
""")
        with tempfile.TemporaryDirectory() as directory:
            collector=BattleCollector(runner,Path(directory),wait_seconds=0,request_delay=0)
            runner.lua.execute(install_lua(collector.owner))
            with self.assertRaises(TimeoutError):
                collector.page("star",1,1000,10)
            self.assertEqual(list((Path(directory)/"list_responses").glob("*.json")),[])

    def test_client_callback_failure_retains_response_instead_of_timing_out(self):
        runner=LuaRunner(BASE+"""
ClientProxy.RPCStarBattleBlockList=function(self,index)
  self.callbackOK=pcall(self.RPCStarBattleBlockListResponse,self,91,
    {sIndex=index,reqLen=0,blockInfoList={}})
end
ClientProxy.RPCStarBattleBlockListResponse=function(self,msg,proto)
  proto.clientMutation='before failure'
  error('client failed while handling empty response')
end
""")
        with tempfile.TemporaryDirectory() as directory:
            collector=BattleCollector(runner,Path(directory),wait_seconds=0.01,request_delay=0)
            runner.lua.execute(install_lua(collector.owner))
            with self.assertRaisesRegex(RuntimeError,"Client response processing failed"):
                collector.page("star",1,1000,10)
            self.assertFalse(runner.lua.globals().ClientProxy.callbackOK)
            saved=list((Path(directory)/"list_responses").glob("*.json"))
            self.assertEqual(len(saved),1)
            value=json.loads(saved[0].read_text(encoding="utf-8"))["payload"]
            self.assertIs(value["clientProcessingComplete"],False)
            self.assertEqual(value["response"],{
                "sIndex":1,"reqLen":0,"blockInfoList":[],"clientMutation":"before failure"})

    def test_successful_callback_retains_mutations_and_nil_return_arity(self):
        runner=LuaRunner(BASE+"""
ClientProxy.RPCStarBattleBlockList=function(self,index)
  local function pack(...) return {n=select('#',...),...} end
  self.callbackResults=pack(self:RPCStarBattleBlockListResponse(91,
    {sIndex=index,reqLen=0,blockInfoList={}}))
end
ClientProxy.RPCStarBattleBlockListResponse=function(self,msg,proto)
  proto.clientMutation='success'
  return nil,7,nil
end
""")
        with tempfile.TemporaryDirectory() as directory:
            collector=BattleCollector(runner,Path(directory),request_delay=0)
            runner.lua.execute(install_lua(collector.owner))
            rows,nxt,response,path=collector.page("star",1,1000,10)
            self.assertEqual(rows,[])
            self.assertIsNone(nxt)
            self.assertEqual(response["clientMutation"],"success")
            value=json.loads(path.read_text(encoding="utf-8"))["payload"]
            self.assertIs(value["clientProcessingComplete"],True)
            results=runner.lua.globals().ClientProxy.callbackResults
            self.assertEqual(results.n,3)
            self.assertIsNone(results[1])
            self.assertEqual(results[2],7)
            self.assertIsNone(results[3])

    def test_expired_lease_recovers_but_active_owner_is_not_replaced(self):
        runner=LuaRunner(BASE+"""
now=1000; os.time=function() return now end
ClientProxy.RPCStarBattleBlockList=function() end
original=function() return nil,7,nil end
ClientProxy.RPCStarBattleBlockListResponse=original
""")
        first=decode_envelope(runner.lua.execute(install_lua("one", 300)))["payload"]
        self.assertNotIn("error", first)
        blocked=decode_envelope(runner.lua.execute(install_lua("two", 300)))["payload"]
        self.assertIn("active lease", blocked["error"])
        # A heartbeat extends ownership while the collector is executing work.
        runner.lua.execute("now=1200")
        heartbeat=CollectorLeaseRunner(runner,"one")
        self.assertTrue(heartbeat.execute_on_update("return 'alive'")["ok"])
        runner.lua.execute("now=1400")
        self.assertIn("error",decode_envelope(runner.lua.execute(install_lua("two",300)))["payload"])
        # Simulate an abrupt Python/Frida exit: cleanup was never executed.
        runner.lua.execute("now=1601")
        recovered=decode_envelope(runner.lua.execute(install_lua("two",300)))["payload"]
        self.assertNotIn("error", recovered)
        self.assertEqual(runner.lua.execute("return select('#',ClientProxy:RPCStarBattleBlockListResponse())"),3)
        self.assertFalse(heartbeat.execute_on_update("return 'old owner must not continue'")["ok"])
        runner.lua.execute(cleanup_lua("two"))
        self.assertTrue(runner.lua.execute("return ClientProxy.RPCStarBattleBlockListResponse == original"))

    def test_cleanup_restores_original_proxy_after_account_object_changes(self):
        runner=LuaRunner(BASE+"""
ClientProxy.RPCStarBattleBlockList=function() end
original=function() return 'old original' end
ClientProxy.RPCStarBattleBlockListResponse=original
""")
        runner.lua.execute(install_lua("one"))
        runner.lua.execute("oldProxy=ClientProxy; newHandler=function() end; ClientProxy={RPCStarBattleBlockListResponse=newHandler}")
        runner.lua.execute(cleanup_lua("one"))
        self.assertTrue(runner.lua.execute("return oldProxy.RPCStarBattleBlockListResponse==original"))
        self.assertTrue(runner.lua.execute("return ClientProxy.RPCStarBattleBlockListResponse==newHandler"))

    def test_same_owner_reinstall_recovers_failed_cleanup_on_replaced_proxy(self):
        runner=LuaRunner(BASE+"""
original=function() return 'old original' end
base={RPCStarBattleBlockList=function() end,RPCStarBattleBlockListResponse=original}
setmetatable(ClientProxy,{__index=base})
""")
        runner.lua.execute(install_lua("one"))
        runner.lua.execute("""
oldProxy=ClientProxy
newHandler=function() return 'new original' end
ClientProxy={avatarId=42,GetServerId=function() return 1 end,
 RPCStarBattleBlockList=function() end,RPCStarBattleBlockListResponse=newHandler}
""")
        # Cleanup could not run during a disconnect; the next cycle reinstalls.
        payload=decode_envelope(runner.lua.execute(install_lua("one")))["payload"]
        self.assertNotIn("error",payload)
        self.assertTrue(runner.lua.execute("return rawget(oldProxy,'RPCStarBattleBlockListResponse')==nil"))
        self.assertTrue(runner.lua.execute("return oldProxy.RPCStarBattleBlockListResponse==original"))
        self.assertTrue(runner.lua.execute("return __codexBattleCollector.proxy==ClientProxy"))
        runner.lua.execute(cleanup_lua("one"))
        self.assertTrue(runner.lua.execute("return ClientProxy.RPCStarBattleBlockListResponse==newHandler"))

    def test_identical_list_responses_share_one_durable_file(self):
        runner=LuaRunner(BASE+"""
ClientProxy.RPCStarBattleBlockList=function(self,index)
 self:RPCStarBattleBlockListResponse(1,{sIndex=index,reqLen=0,blockInfoList={}})
end
ClientProxy.RPCStarBattleBlockListResponse=function() end
""")
        with tempfile.TemporaryDirectory() as directory:
            collector=BattleCollector(runner,Path(directory),request_delay=0)
            runner.lua.execute(install_lua(collector.owner))
            first=collector.page("star",1,1000,10)[3]
            second=collector.page("star",1,1000,10)[3]
            self.assertEqual(first,second)
            self.assertEqual(len(list((Path(directory)/"list_responses").glob("*.json"))),1)
            runner.lua.execute(cleanup_lua(collector.owner))

    def test_serializer_preserves_unknown_unicode_binary_and_key_collisions(self):
        runner=LuaRunner()
        code=LUA_JSON+"""
local x={label='中文', binary=string.char(0,255,128), nested={unknown={false,0,''}}}
x.keys={[1]='numeric',['1']='string'}
return envelope(x)
"""
        envelope=decode_envelope(runner.lua.execute(code))
        payload=envelope["payload"]
        self.assertEqual(payload["label"],"中文")
        self.assertEqual(payload["binary"],{"$luaBytesHex":"00ff80"})
        self.assertEqual(payload["nested"]["unknown"],[False,0,""])
        self.assertCountEqual(payload["keys"]["$luaTableEntries"],[[1,"numeric"],["1","string"]])
        self.assertFalse(envelope["serializationIssues"])

    def test_cycles_and_functions_are_reported(self):
        runner=LuaRunner()
        out=decode_envelope(runner.lua.execute(LUA_JSON+"local x={fn=function() end}; x.self=x; return envelope(x)"))
        self.assertEqual({issue["kind"] for issue in out["serializationIssues"]},{"cycle","unsupported"})

    def test_unready_runtime_is_explicit_failure_and_cleanup_is_safe(self):
        runner=LuaRunner(BASE)
        with tempfile.TemporaryDirectory() as directory:
            collector=BattleCollector(runner,Path(directory),request_delay=0,wait_seconds=0)
            result=collector.run_cycle()
            self.assertFalse(result["complete"])
            self.assertIn("game not logged in or unavailable",result["error"])
            self.assertIsNone(runner.lua.globals()["__codexBattleCollector"])

    def test_response_wrappers_preserve_original_and_favourite_parent(self):
        runner=LuaRunner(BASE+"""
ClientProxy.RPCStarBattleBlockList=function() end
ClientProxy.RPCStarBattleChildList=function(self,index,parent)
 self:RPCStarBattleChildListResponse(1,{sIndex=index,reqLen=0,parentHashKey=parent,combatInfoList={}})
end
ClientProxy.RPCStarBattleBlockListResponse=function() end
ClientProxy.RPCStarBattleChildListResponse=function(self,msg,proto) self.originalCalled=true end
""")
        info=decode_envelope(runner.lua.execute(install_lua("one")))["payload"]
        self.assertTrue(info["capabilities"]["RPCStarBattleChildList"])
        runner.lua.execute(request_page_lua("one","t","star",1,1000,"fav_key"))
        self.assertTrue(runner.lua.globals().ClientProxy.originalCalled)
        st=runner.lua.globals()["__codexBattleCollector"]
        self.assertEqual(st.responses["t"]["response"]["parentHashKey"],"fav_key")
        runner.lua.execute(cleanup_lua("one"))
        self.assertIsNone(runner.lua.globals()["__codexBattleCollector"])

    def test_export_keeps_all_components_and_archives_previous_bytes(self):
        runner=LuaRunner(BASE+"""
local b={battleID=123,eventDataList={{eventId=1,intList={9}}},newField={x=7}}
ClientProxy.GetAllCombatInfo=function() return b end
ClientProxy.GetDetailCombatInfo=function() return b end
ClientProxy.GetCombatInfo=function() return {battleID=123,basicOnly='retained'} end
ClientProxy.reportStaticsInfo={[123]={damage=999}}
ClientProxy.allBattleRecordInfo={[123]={isComplete=true,battleSnapData={hero={v=3}},pages={{v=4}}}}
""")
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            previous=b'{ "old_unimported": true }'
            (root/"battle_123_decrypted.json").write_bytes(previous)
            result=capture_report(runner,123,root,wait_seconds=0,poll_seconds=.001)
            self.assertTrue(result["complete"])
            saved=json.loads(Path(result["path"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["components"]["basic"]["basicOnly"],"retained")
            self.assertEqual(saved["components"]["all"]["newField"],{"x":7})
            self.assertEqual(saved["components"]["snapshots"]["pages"],[{"v":4}])
            old=list((root/"captures"/"123").glob("*.json"))
            self.assertEqual(len(old),1)
            self.assertEqual(old[0].read_bytes(),previous)

    def test_cached_export_uses_one_status_read_and_one_export(self):
        runner=LuaRunner(BASE+"""
local b={battleID=123,eventDataList={{eventId=1}},unknown={x=7}}
ClientProxy.GetAllCombatInfo=function() return b end
ClientProxy.GetDetailCombatInfo=function() return b end
ClientProxy.GetCombatInfo=function() return b end
ClientProxy.reportStaticsInfo={[123]={damage=999}}
ClientProxy.allBattleRecordInfo={[123]={isComplete=true,battleSnapData={hero=3}}}
""")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(runner,"execute_on_update",wraps=runner.execute_on_update) as execute:
                result=capture_report(runner,123,Path(directory),wait_seconds=0)
            self.assertTrue(result["complete"])
            self.assertEqual(execute.call_count,2)
            saved=json.loads(Path(result["path"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["components"]["all"]["unknown"],{"x":7})

    def test_all_response_status_is_reused_for_detail_and_other_caches(self):
        runner=LuaRunner(BASE+"""
local b={battleID=123,eventDataList={{eventId=1}}}
ClientProxy.GetAllCombatInfo=function(self) return self.all end
ClientProxy.GetDetailCombatInfo=function(self) return self.detail end
ClientProxy.GetCombatInfo=function() return b end
ClientProxy.RPCGetAllCombatInfo=function(self) self.all=b; self.detail=b end
ClientProxy.RPCGetDetailCombatInfo=function(self) self.detailRequests=true; error('redundant detail request') end
ClientProxy.reportStaticsInfo={[123]={damage=999}}
ClientProxy.allBattleRecordInfo={[123]={isComplete=true,battleSnapData={hero=3}}}
""")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(runner,"execute_on_update",wraps=runner.execute_on_update) as execute:
                result=capture_report(runner,123,Path(directory),wait_seconds=0)
            self.assertTrue(result["complete"])
            self.assertEqual(execute.call_count,4)
            self.assertTrue(result["componentStatus"]["detail"]["cached"])
            self.assertIsNone(runner.lua.globals().ClientProxy.detailRequests)

    def test_request_exception_invalidates_reused_component_status(self):
        runner=LuaRunner(BASE+"""
local b={battleID=123,eventDataList={{eventId=1}}}
ClientProxy.GetAllCombatInfo=function(self) return self.all end
ClientProxy.GetDetailCombatInfo=function(self) return self.detail end
ClientProxy.GetCombatInfo=function() return b end
ClientProxy.RPCGetAllCombatInfo=function(self)
 self.all=b; self.detail=b; error('reply failed after caches changed')
end
ClientProxy.RPCGetDetailCombatInfo=function(self) self.detailRequests=true; error('redundant detail request') end
ClientProxy.reportStaticsInfo={[123]={damage=999}}
ClientProxy.allBattleRecordInfo={[123]={isComplete=true,battleSnapData={hero=3}}}
""")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(runner,"execute_on_update",wraps=runner.execute_on_update) as execute:
                result=capture_report(runner,123,Path(directory),wait_seconds=0)
            self.assertFalse(result["complete"])
            self.assertIn("reply failed after caches changed",result["componentStatus"]["all"]["error"])
            self.assertTrue(result["componentStatus"]["detail"]["cached"])
            self.assertIsNone(runner.lua.globals().ClientProxy.detailRequests)
            self.assertEqual(execute.call_count,4)

    def test_export_rechecks_actual_components_after_status_cache_degrades(self):
        runner=LuaRunner(BASE+"""
local b={battleID=123,eventDataList={{eventId=1}}}
local reads=0
ClientProxy.GetAllCombatInfo=function()
 reads=reads+1
 if reads==1 then return b end
end
ClientProxy.GetDetailCombatInfo=function() return b end
ClientProxy.GetCombatInfo=function() return b end
ClientProxy.reportStaticsInfo={[123]={damage=999}}
ClientProxy.allBattleRecordInfo={[123]={isComplete=true,battleSnapData={hero=3}}}
""")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(runner,"execute_on_update",wraps=runner.execute_on_update) as execute:
                result=capture_report(runner,123,Path(directory),wait_seconds=0)
            self.assertTrue(result["exported"])
            self.assertFalse(result["complete"])
            self.assertFalse(result["componentStatus"]["all"]["available"])
            self.assertEqual(execute.call_count,2)

    def test_all_source_with_missing_components_remains_partial(self):
        runner=LuaRunner(BASE+"""
local b={battleID=123,eventDataList={{eventId=1}}}
ClientProxy.GetAllCombatInfo=function() return b end
ClientProxy.GetDetailCombatInfo=function() return b end
ClientProxy.GetCombatInfo=function() return b end
""")
        with tempfile.TemporaryDirectory() as directory:
            result=capture_report(runner,123,Path(directory),wait_seconds=0,poll_seconds=.001)
            self.assertTrue(result["exported"])
            self.assertFalse(result["complete"])
            self.assertIn("missing RPCGetStaticsCombatInfo", result["componentStatus"]["statics"]["error"])

    def test_identity_change_blocks_page_request_and_export(self):
        runner=LuaRunner(BASE+"""
ClientProxy.RPCStarBattleBlockList=function(self) self.didRequest=true end
ClientProxy.RPCStarBattleBlockListResponse=function() end
ClientProxy.RPCStarBattleChildList=function() end
ClientProxy.RPCStarBattleChildListResponse=function() end
""")
        runner.lua.execute(install_lua("one"))
        runner.lua.execute("ClientProxy.avatarId=43")
        result=runner.lua.execute(request_page_lua("one","t","star",1,1000))
        self.assertIn("changed",result)
        self.assertIsNone(runner.lua.globals().ClientProxy.didRequest)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                capture_report(runner,123,Path(directory),wait_seconds=0,
                    expected_identity={"avatarId":"42","serverId":"1","scenarioId":"14"})
            self.assertFalse(list(Path(directory).glob("battle_*.json")))


class CollectorStateTests(unittest.TestCase):
    def make_report(self, directory):
        path=Path(directory)/"battle_123_decrypted.json"
        atomic_json(path,{"battleID":123,"data":{"battleID":123},"components":{}})
        return {"battleID":123,"exported":True,"complete":True,"path":str(path)}

    def test_failed_db_callback_retries_saved_file_without_recapture(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            calls=[]
            def importer(path):
                calls.append(path)
                if len(calls)==1:
                    raise RuntimeError("database locked")
                return {"status":"imported"}
            collector=BattleCollector(None,root,on_export=importer)
            collector.state["reports"]["123"]={"battleID":"123","attempts":0,"complete":False,"observations":{}}
            with patch("capture.game_assets.battle_collector.capture_report",return_value=self.make_report(root)) as capture:
                counts=collector.collect_pending()
                self.assertEqual(counts["failed"],1)
                self.assertFalse(collector.state["reports"]["123"]["complete"])
                self.assertTrue(collector.state["reports"]["123"]["pendingImport"])
                # Recreate the collector: persistence, not in-memory recovery.
                resumed=BattleCollector(None,root,on_export=importer)
                resumed.state["reports"]["123"]["nextRetryAt"]=0
                result=resumed.collect_pending()
                self.assertEqual(result["complete"],1)
                self.assertEqual(capture.call_count,1)
                self.assertEqual(calls[0],calls[1])

    def test_stop_prevents_new_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=BattleCollector(None,Path(directory))
            event=threading.Event()
            event.set()
            collector._stop_event=event
            collector.state["reports"]["123"]={"battleID":"123","complete":False}
            with patch("capture.game_assets.battle_collector.capture_report") as capture:
                collector.collect_pending()
                capture.assert_not_called()

    def test_collector_cli_rejects_invalid_timers_before_attach(self):
        import capture.game_assets.battle_collector as module
        with tempfile.TemporaryDirectory() as directory:
            for option,value in (("--wait","nan"),("--poll","inf"),("--interval","nan"),
                                 ("--request-delay","inf"),("--max-pages","0")):
                runner=Mock()
                runtime=ModuleType("capture.game_assets.run_lua_in_game")
                runtime.GameLuaRunner=runner
                with patch.dict(sys.modules,{"capture.game_assets.run_lua_in_game":runtime}), \
                     patch.object(sys,"argv",["collector","--out-dir",directory,option,value]), \
                     patch.object(sys,"stderr",io.StringIO()):
                    with self.assertRaises(SystemExit):
                        module.main()
                runner.assert_not_called()

    def test_new_scope_metadata_updates_complete_capture_without_game_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            imported=[]
            collector=BattleCollector(None,root,on_export=lambda path: imported.append(path))
            result=self.make_report(root)
            collector.state["reports"]["123"]={"battleID":"123","complete":True,"attempts":1,
                "observations":{},"lastResult":result}
            collector.remember("union",{"combatInfo":{"battleID":123},"unknownListField":42},
                               {"sIndex":1,"reqLen":1},root/"page.json")
            with patch("capture.game_assets.battle_collector.capture_report") as capture:
                collector.collect_pending()
                capture.assert_not_called()
            self.assertTrue(collector.state["reports"]["123"]["complete"])
            self.assertEqual(len(imported),2)
            self.assertIs(json.loads(imported[0].read_text(encoding="utf-8"))["captureComplete"],False)
            payload=json.loads(imported[1].read_text(encoding="utf-8"))
            self.assertEqual(payload["discovery"][0]["row"]["unknownListField"],42)

    def test_root_page_failure_still_collects_children_then_restarts_metadata_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            collector=BattleCollector(None,root,request_delay=0)
            caps={"capabilities":{"RPCGetBattleBlockList":True,"RPCGetChildCombatInfoList":True},
                  "segmentLength":10,"relationAll":1000}
            calls=[]
            fail=True
            def page(scope,index,relation,segment,parent=None):
                calls.append((index,parent))
                if parent is not None:
                    return [{"battleID":124}],None,{"sIndex":index,"reqLen":1},root/"child.json"
                if index==1:
                    return [{"combatInfo":{"battleID":123},"childNum":1}],11,{"sIndex":1,"reqLen":10},root/"first.json"
                if fail:
                    raise TimeoutError("page two failed")
                return [],None,{"sIndex":index,"reqLen":0},root/"last.json"
            with patch.object(collector,"page",side_effect=page):
                first=collector.discover_scope("personal",caps)
            self.assertFalse(first["complete"])
            self.assertEqual(first["index"],11)
            self.assertIn((1,"123"),calls)
            self.assertIn("124",collector.state["reports"])
            # The persistent reports survive, but page offsets restart safely.
            resumed=BattleCollector(None,root,request_delay=0)
            calls.clear()
            fail=False
            with patch.object(resumed,"page",side_effect=page):
                second=resumed.discover_scope("personal",caps)
            self.assertEqual(calls[:2],[(1,None),(11,None)])
            self.assertTrue(second["complete"])

    def test_moving_offsets_rescan_middle_pages_and_retire_invisible_parent_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            collector=BattleCollector(None,root,request_delay=0)
            collector.state["scopes"]["personal"]={"index":31,"rootComplete":False,
                "roots":{"old":{"battleID":"999","childNum":1}},
                "children":{"old":{"index":11,"complete":False}}}
            caps={"capabilities":{"RPCGetBattleBlockList":True,"RPCGetChildCombatInfoList":True},
                  "segmentLength":10,"relationAll":1000}
            calls=[]
            def page(scope,index,relation,segment,parent=None):
                calls.append((index,parent))
                self.assertIsNone(parent, "Expired parent group must not be requested")
                rows=[{"combatInfo":{"battleID":1000+index},"childNum":0}]
                return rows, (index+10 if index<31 else None), {"sIndex":index,"reqLen":10 if index<31 else 1}, root/"page.json"
            with patch.object(collector,"page",side_effect=page):
                result=collector.discover_scope("personal",caps)
            self.assertEqual(calls,[(1,None),(11,None),(21,None),(31,None)])
            self.assertTrue(result["complete"])
            self.assertIn("1011",collector.state["reports"])
            self.assertNotIn("old",result["roots"])

    def test_child_metadata_starts_at_first_page_after_previous_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            collector=BattleCollector(None,root,request_delay=0)
            collector.state["scopes"]["personal"]={"rootComplete":True,
                "children":{"123":{"index":11,"complete":False}}}
            caps={"capabilities":{"RPCGetBattleBlockList":True,"RPCGetChildCombatInfoList":True},
                  "segmentLength":10,"relationAll":1000}
            child_calls=[]
            def page(scope,index,relation,segment,parent=None):
                if parent is None:
                    return [{"combatInfo":{"battleID":123},"childNum":1}],None,{"sIndex":index,"reqLen":1},root/"root.json"
                child_calls.append(index)
                return [{"battleID":999}],None,{"sIndex":index,"reqLen":1},root/"child.json"
            with patch.object(collector,"page",side_effect=page):
                collector.discover_scope("personal",caps)
            self.assertEqual(child_calls,[1])
            self.assertIn("999",collector.state["reports"])

    def test_historical_incomplete_report_does_not_block_visible_coverage(self):
        class Runner:
            def execute_on_update(self,code,timeout=10):
                if "local capabilities" in code:
                    return {"ok":True,"result":json.dumps({"payload":{"capabilities":{},"segmentLength":10,
                        "relationAll":1000,"runtimeIdentity":{"avatarId":"42","serverId":"1","scenarioId":"14"}}})}
                return {"ok":True,"result":"collector detached"}
        with tempfile.TemporaryDirectory() as directory:
            collector=BattleCollector(Runner(),Path(directory),scopes=("personal",))
            collector.state["reports"]["old"]={"complete":False,"lastSeenCycle":0}
            def discover(scope,capabilities):
                collector.state["reports"]["new"]={"complete":True,"lastSeenCycle":collector.state["cycles"]}
                return {"complete":True}
            with patch.object(collector,"discover_scope",side_effect=discover), patch.object(collector,"collect_pending",return_value={}):
                result=collector.run_cycle()
            self.assertTrue(result["complete"])
            self.assertEqual(result["visibleReports"],1)
            self.assertEqual(result["discoveredReports"],2)

    def test_partial_scope_never_claims_coverage_even_when_reports_complete(self):
        class Runner:
            def execute_on_update(self,code,timeout=10):
                if "local capabilities" in code:
                    return {"ok":True,"result":json.dumps({"payload":{"capabilities":{},"segmentLength":10,"relationAll":1000,"runtimeIdentity":{"avatarId":"42","serverId":"1","scenarioId":"14"}}})}
                return {"ok":True,"result":"collector detached"}
        with tempfile.TemporaryDirectory() as directory:
            collector=BattleCollector(Runner(),Path(directory),request_delay=0)
            summary=collector.run_cycle()
            self.assertFalse(summary["complete"])
            self.assertEqual(set(summary["scopes"]),{"personal","union","star"})
            self.assertEqual(len(summary["errors"]),3)

    def test_empty_successful_enumeration_does_not_claim_full_collection(self):
        class Runner:
            def execute_on_update(self,code,timeout=10):
                if "local capabilities" in code:
                    return {"ok":True,"result":json.dumps({"payload":{"capabilities":{},"segmentLength":10,
                        "relationAll":1000,"runtimeIdentity":{"avatarId":"42","serverId":"1","scenarioId":"14"}}})}
                return {"ok":True,"result":"collector detached"}
        with tempfile.TemporaryDirectory() as directory:
            collector=BattleCollector(Runner(),Path(directory))
            with patch.object(collector,"discover_scope",return_value={"complete":True,"pages":1}):
                result=collector.run_cycle()
            self.assertTrue(result["enumerationComplete"])
            self.assertFalse(result["coverageComplete"])
            self.assertFalse(result["complete"])
            self.assertEqual(result["discoveredReports"],0)
            self.assertTrue(result["warnings"])

    def test_partial_export_cli_saves_manifest_and_exits_two(self):
        import capture.game_assets.export_battle_report_runtime as exporter
        class Runner:
            def __init__(self,pid): pass
            def __enter__(self): return self
            def __exit__(self,*args): pass
        module=ModuleType("capture.game_assets.run_lua_in_game")
        module.GameLuaRunner=Runner
        with tempfile.TemporaryDirectory() as directory:
            record={"battleID":123,"exported":True,"complete":False}
            with patch.dict(sys.modules,{"capture.game_assets.run_lua_in_game":module}), \
                 patch.object(sys,"argv",["export","123","--out-dir",directory]), \
                 patch.object(exporter,"capture_report",return_value=record), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    exporter.main()
            self.assertEqual(caught.exception.code,2)
            manifest=json.loads((Path(directory)/"runtime_export_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest,[record])


if __name__=="__main__":
    unittest.main()
