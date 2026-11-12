"""Cache expiry retries keep partial bytes and bind coverage to one send era."""
from pathlib import Path
import json
import runpy
from unittest.mock import patch

import pytest

from capture.game_assets import export_battle_report_runtime as e
from capture.game_assets import runtime_response_audit as audit
from parser.report_archive import ReportArchive


BASE = runpy.run_path(str(Path(__file__).with_name("test_component_response_audit.py")))
Runner, Clock = BASE["Runner"], BASE["Clock"]


@pytest.fixture
def clock():
    value = Clock()
    with patch.object(e.time, "monotonic", side_effect=value.monotonic), patch.object(e.time, "sleep", side_effect=value.sleep):
        yield value


def document(result):
    return json.loads(Path(result["path"]).read_text(encoding="utf-8"))


def checkpoints(result):
    return [json.loads(Path(path).read_text(encoding="utf-8")) for path in result["cacheCheckpointPaths"]]


ALL_SETUP = r'''
ClientProxy._all={};ClientProxy._allCombatSlices={}
ClientProxy.GetAllCombatInfo=function(self,id) return self._all[id] end
ClientProxy.RPCGetAllCombatInfo=function(self,id)
 if self._all[id] then return end
 local partial=self._allCombatSlices[id]
 if partial then
  if GameTime.svrTime-(partial.time or 0)<ClientConst.BattleOutOfTime then return end
  self._allCombatSlices[id]=nil
 end
 return self:CallServer('RPCGetAllCombatInfo',{battleID=id})
end
ClientProxy.RPCGetAllCombatInfoResponse=function(self,msg,proto)
 self._allCombatSlices[proto.battleID]=nil
 self._all[proto.battleID]={battleID=proto.battleID,eventDataList={{eventId=1,intList={123},tab=0}},unknown='full-after-retry'}
end
local snapshotSend=NetMgr.SendPacket
NetMgr.SendPacket=function(self,method,proto)
 if method=='RPCGetAllCombatInfo' then
  __sent[#__sent+1]={method=method,battleID=proto.battleID,at=GameTime.svrTime}
  __pendingAll={at=GameTime.svrTime+1,battleID=proto.battleID}
 else return snapshotSend(self,method,proto) end
end
local snapshotDeliver=__deliver
__deliver=function()
 snapshotDeliver()
 if __pendingAll and GameTime.svrTime>=__pendingAll.at then
  local value=__pendingAll;__pendingAll=nil
  ClientProxy:RPCGetAllCombatInfoResponse(1,{battleID=value.battleID,code=0,sliceIndex=1,sliceCount=1})
 end
end
__originalSend=NetMgr.SendPacket
'''


def test_expired_snapshot_cache_is_saved_before_first_rpc_clears_it(clock, tmp_path):
    runner = Runner(clock, replies=[], partial=True)
    runner.lua.execute("ClientProxy.allBattleRecordInfo[123].time=90")
    result = e.capture_report(runner, 123, tmp_path)
    assert not result["complete"] and len(runner.requests()) == 1
    assert runner.requests()[0]["at"] == 100
    assert checkpoints(result)[0]["components"]["snapshots"]["pages"][0]["marker"] == "old-partial"
    assert runner.lua.eval("ClientProxy.allBattleRecordInfo[123]==nil")
    assert runner.restored()


def test_failed_checkpoint_prevents_destructive_request(clock, tmp_path):
    runner = Runner(clock, replies=[], partial=True)
    runner.lua.execute("ClientProxy.allBattleRecordInfo[123].time=90")
    publish = e.atomic_bytes
    def fail_capture(path, raw):
        if path.parent.name == "123":
            raise OSError("durable checkpoint unavailable")
        return publish(path, raw)
    with patch.object(e, "atomic_bytes", side_effect=fail_capture):
        result = e.capture_report(runner, 123, tmp_path)
    assert not result["complete"] and not runner.requests()
    assert "durable checkpoint unavailable" in result["componentStatus"]["snapshots"]["error"]
    assert document(result)["components"]["snapshots"]["pages"][0]["marker"] == "old-partial"
    assert runner.restored()


def test_all_slices_wait_for_their_own_expiry_even_with_complete_snapshots(clock, tmp_path):
    runner = Runner(clock, replies=[])
    runner.lua.execute(ALL_SETUP)
    runner.lua.execute("ClientProxy._allCombatSlices[123]={time=100,receivedCount=1,[1]=string.char(0,255,65)};__complete(123)")
    result = e.capture_report(runner, 123, tmp_path)
    assert result["complete"] and clock.now == 111
    assert len(runner.requests()) == 1 and runner.requests()[0]["at"] == 110
    assert result["componentStatus"]["all"]["natural_expiry_retry"]
    old = checkpoints(result)
    assert any(value["componentFragments"]["all"]["1"]["$luaBytesHex"] == "00ff41" for value in old)
    assert document(result)["components"]["all"]["unknown"] == "full-after-retry"
    assert runner.restored()


def test_only_raw_slices_are_archived_without_inventing_complete_events(clock, tmp_path):
    runner = Runner(clock, replies=[])
    runner.lua.execute(r'''
ClientProxy.GetCombatInfo=function() return nil end
ClientProxy.GetAllCombatInfo=function() return nil end
ClientProxy.GetDetailCombatInfo=function() return nil end
ClientProxy.reportStaticsInfo={}
ClientProxy._allCombatSlices={[123]={time=90,receivedCount=1,[2]=string.char(0,255,65)}}
''')
    result = e.capture_report(runner, 123, tmp_path / "captures", request_missing=False)
    payload = document(result)
    assert result["exported"] and not result["complete"]
    assert payload["source"] == "all_fragments" and not payload.get("hasAll")
    assert payload["componentFragments"]["all"]["2"] == {"$luaBytesHex": "00ff41"}
    assert payload["luaTableKeyTypes"]["/componentFragments/all"]["2"] == "number"
    assert payload["luaTableKeyTypes"]["/data/all_fragments"]["2"] == "number"
    with ReportArchive(tmp_path / "archive.db") as archive:
        imported = archive.import_file(result["path"], namespace="account")
        assert imported["status"] == "imported" and imported["reports"][0]["completeness"] == "partial"
        assert archive.import_file(result["path"], namespace="account")["status"] == "duplicate"
        assert archive.verify()["ok"]


def test_detail_slices_use_detail_expiry_and_preserve_raw_fragment(clock, tmp_path):
    runner = Runner(clock, replies=[])
    runner.lua.execute(ALL_SETUP.replace("_all", "_detail").replace("GetAllCombatInfo", "GetDetailCombatInfo"))
    runner.lua.execute("ClientProxy._detailCombatSlices[123]={time=100,receivedCount=1,[1]='raw-detail'};__complete(123)")
    result = e.capture_report(runner, 123, tmp_path)
    assert result["complete"] and clock.now == 111
    assert runner.requests()[0]["method"] == "RPCGetDetailCombatInfo"
    assert runner.requests()[0]["at"] == 110
    assert any(value["componentFragments"]["detail"]["1"] == "raw-detail" for value in checkpoints(result))
    assert runner.restored()


def test_final_not_found_still_returns_saved_partial_snapshot_paths(clock, tmp_path):
    runner = Runner(clock, replies=[], partial=True)
    runner.lua.execute(r'''
ClientProxy.GetCombatInfo=function() return nil end
ClientProxy.GetAllCombatInfo=function() return nil end
ClientProxy.GetDetailCombatInfo=function() return nil end
ClientProxy.reportStaticsInfo={}
ClientProxy.allBattleRecordInfo[123].time=90
''')
    result = e.capture_report(runner, 123, tmp_path)
    assert result["exported"] is False and result["error"] == "not_found 123"
    assert result["cacheCheckpointPaths"]
    assert checkpoints(result)[0]["components"]["snapshots"]["pages"][0]["marker"] == "old-partial"
    assert not (tmp_path / "battle_123_decrypted.json").exists()
    assert runner.restored()


def test_new_resend_coverage_does_not_mix_the_previous_stream(clock, tmp_path):
    runner = Runner(clock, partial=True, replies=[{"after": 1, "page": 1, "maxPage": 2}, {"after": 2, "page": 2, "maxPage": 2}])
    runner.lua.execute("__pending={{at=105,battleID=123,page=2,maxPage=3,code=0}}")
    result = e.capture_report(runner, 123, tmp_path)
    assert result["complete"] and clock.now == 112
    proof = result["componentStatus"]["snapshots"]["responseCoverage"]
    assert proof["received"] == [1, 2] and proof["expected"] == 2
    assert proof["historical_response_count"] == 1 and not proof["conflicting_totals"]
    preserved = checkpoints(result)
    assert any(len(value["components"]["snapshots"]["pages"]) == 2 for value in preserved)
    assert runner.restored()


@pytest.mark.parametrize("invalid", ["nil", "false", "0", "'invalid-decoded-scalar'"])
def test_cached_complete_flag_without_decoded_data_fails_without_empty_wait(clock, tmp_path, invalid):
    runner = Runner(clock, replies=[])
    runner.lua.execute("ClientProxy.allBattleRecordInfo[123]={isComplete=true,pages={{marker='not-decoded'}},receivedCount=1,battleSnapData="+invalid+"}")
    result = e.capture_report(runner, 123, tmp_path)
    assert not result["complete"] and clock.now == 100 and not runner.requests()
    assert result["componentStatus"]["snapshots"]["error"] == "client_cached_incomplete_component"
    assert checkpoints(result)[0]["components"]["snapshots"]["pages"][0]["marker"] == "not-decoded"
    assert runner.restored()


def test_snapshot_completed_before_rpc_is_not_misclassified_as_bad_cached_value(clock, tmp_path):
    runner = Runner(clock, replies=[], partial=True)
    checkpoint = e._checkpoint_before_cache_retry
    def complete_after_checkpoint(*args, **kwargs):
        path = checkpoint(*args, **kwargs)
        runner.lua.globals().__complete(123)
        return path
    with patch.object(e, "_checkpoint_before_cache_retry", side_effect=complete_after_checkpoint):
        result = e.capture_report(runner, 123, tmp_path)
    assert result["complete"] and clock.now == 100 and not runner.requests()
    assert not result["componentStatus"]["snapshots"].get("error")
    assert runner.restored()


@pytest.mark.parametrize("component", ["all", "detail", "statics"])
def test_confirmed_invalid_cached_scalar_does_not_wait_for_unsent_rpc(clock, tmp_path, component):
    runner = Runner(clock, replies=[])
    runner.lua.globals().__complete(123)
    method = {"all": "RPCGetAllCombatInfo", "detail": "RPCGetDetailCombatInfo", "statics": "RPCGetStaticsCombatInfo"}[component]
    if component == "statics":
        runner.lua.execute("ClientProxy.reportStaticsInfo[123]=0")
    else:
        getter = "GetAllCombatInfo" if component == "all" else "GetDetailCombatInfo"
        runner.lua.execute(f"ClientProxy.{getter}=function() return 0 end")
    runner.lua.execute(f"ClientProxy.{method}=function() return end")
    result = e.capture_report(runner, 123, tmp_path)
    assert not result["complete"] and clock.now == 100 and not runner.requests()
    assert result["componentStatus"][component]["error"] == "client_cached_incomplete_component"
    assert runner.restored()


def test_prefetch_defers_partial_cache_until_capture_can_checkpoint(clock, tmp_path):
    runner = Runner(clock, replies=[], partial=True)
    prefetched = e.prefetch_reports(runner, [123])
    assert not runner.requests()
    assert prefetched["123"]["snapshots"]["deferred"]
    assert prefetched["123"]["snapshots"]["reason"] == "partial_cache_requires_checkpoint"
    result = e.capture_report(runner, 123, tmp_path, prefetched_requests=prefetched["123"])
    assert not result["complete"] and result["cacheCheckpointPaths"]
    assert checkpoints(result)[0]["components"]["snapshots"]["pages"][0]["marker"] == "old-partial"


def test_previous_generation_error_cannot_poison_new_complete_coverage():
    data = {"responses": [{"component": "snapshots", "success": False, "code": 41, "generation": 0},
                           {"component": "snapshots", "success": True, "page": 1, "maxPage": 1, "generation": 1}],
            "dispatches": {"snapshots": {"sendPacket": 1}}, "sendPacketObserved": True}
    result = audit.coverage("snapshots", data)
    assert result["complete"] and result["historical_response_count"] == 1


def test_previous_generation_complete_cannot_satisfy_new_request():
    data = {"responses": [{"component": "snapshots", "success": True, "page": 1, "maxPage": 1, "generation": 0}],
            "dispatches": {"snapshots": {"sendPacket": 1}}, "sendPacketObserved": True}
    result = audit.coverage("snapshots", data)
    assert result["complete"] is None and not result["observed"]


def test_coverage_uses_observed_callserver_generation_when_packet_hook_unavailable():
    data = {"responses": [{"component": "snapshots", "success": True, "page": 1, "maxPage": 3, "generation": 1},
                          {"component": "snapshots", "success": True, "page": 1, "maxPage": 2, "generation": 2},
                          {"component": "snapshots", "success": True, "page": 2, "maxPage": 2, "generation": 2}],
            "dispatches": {"snapshots": {"sendPacket": 0, "callServer": 2}},
            "sendPacketObserved": False, "callServerObserved": True}
    result = audit.coverage("snapshots", data)
    assert result["complete"] and result["expected"] == 2 and result["historical_response_count"] == 1


def pending_count(runner):
    return runner.lua.eval("(function() local n=0;for _ in pairs((__codexBattleReportRetained or {}).entries or {}) do n=n+1 end;return n end)()")


def test_pacing_arrival_is_frozen_in_actual_dispatch_before_cache_clear(clock, tmp_path):
    runner = Runner(clock, replies=[], partial=True)
    runner.lua.execute("ClientProxy.allBattleRecordInfo[123].time=90")
    runner._battle_prefetch_dispatch_times = {"__all_methods__": 100.0}
    arrived = []
    def arrive_during_pacing(seconds):
        clock.sleep(seconds)
        if not arrived:
            arrived.append(True)
            runner.lua.globals().GameTime.svrTime = clock.now
            runner.lua.execute("ClientProxy:RPCGetHeroBattleSnapRecordsResponse(1,{battleID=123,page=2,maxPage=3,code=0})")
    with patch.object(e.time, "sleep", side_effect=arrive_during_pacing):
        result = e.capture_report(runner, 123, tmp_path)
    assert arrived and runner.requests()[0]["at"] == 101
    assert runner.lua.eval("ClientProxy.allBattleRecordInfo[123]==nil")
    assert any(len(value.get("components", {}).get("snapshots", {}).get("pages", [])) == 2
               for value in checkpoints(result))
    assert not result["complete"] and runner.restored() and pending_count(runner) == 0


def test_arrival_in_ipc_window_is_frozen_before_original_rpc(clock, tmp_path):
    runner = Runner(clock, replies=[], partial=True)
    runner.lua.execute("ClientProxy.allBattleRecordInfo[123].time=90")
    run = e.run_checked
    arrived = []
    def incoming(runner_arg, code, *args, **kwargs):
        if "ClientProxy:RPCGetHeroBattleSnapRecords(" in code and not arrived:
            arrived.append(True)
            runner.lua.execute("ClientProxy:RPCGetHeroBattleSnapRecordsResponse(1,{battleID=123,page=2,maxPage=3,code=0})")
        return run(runner_arg, code, *args, **kwargs)
    with patch.object(e, "run_checked", side_effect=incoming):
        result = e.capture_report(runner, 123, tmp_path)
    assert arrived
    assert any(value.get("captureKind") == "cache_at_dispatch"
               and len(value["components"]["snapshots"]["pages"]) == 2 for value in checkpoints(result))
    assert not result["complete"] and runner.restored()


def test_response_error_keeps_prior_page_and_both_original_protos(clock, tmp_path):
    runner = Runner(clock, replies=[{"after": 1, "page": 1, "maxPage": 3}, {"after": 2, "code": 41}])
    result = e.capture_report(runner, 123, tmp_path)
    values = checkpoints(result)
    raw = [value["responseFragments"]["snapshots"] for value in values if value.get("responseFragments")]
    assert any(item.get("page") == 1 for item in raw) and any(item["code"] == 41 for item in raw)
    assert any(value.get("components", {}).get("snapshots", {}).get("pages", [{}])[0].get("marker") == "new"
               for value in values)
    assert "ERR_TOOFAST" in result["componentStatus"]["snapshots"]["error"]
    assert all(value["captureComplete"] is False for value in values)
    assert runner.lua.eval("ClientProxy.allBattleRecordInfo[123]==nil") and runner.restored()


def test_response_before_mutation_keeps_binary_and_typed_numeric_keys(clock, tmp_path):
    runner = Runner(clock, replies=[{"after": 1, "page": 1, "maxPage": 1}])
    runner.lua.execute(r'''
local originalDeliver=__deliver
__deliver=function()
  for _,item in ipairs(__pending) do
    item.unknown={[3]=string.char(0,255,65),tag='original'}
    item.collision={[1]='numeric',['1']='text'}
  end
  return originalDeliver()
end
local original=ClientProxy.RPCGetHeroBattleSnapRecordsResponse
ClientProxy.RPCGetHeroBattleSnapRecordsResponse=function(self,msg,proto)
  proto.unknown[3]='mutated';proto.unknown.tag='changed';proto.collision[1]=nil
  return original(self,msg,proto)
end
__originalResponse=ClientProxy.RPCGetHeroBattleSnapRecordsResponse
''')
    result = e.capture_report(runner, 123, tmp_path)
    value = next(value for value in checkpoints(result) if value.get("source") == "response_fragments")
    raw = value["responseFragments"]["snapshots"]
    assert raw["unknown"] == {"3": {"$luaBytesHex": "00ff41"}, "tag": "original"}
    assert value["luaTableKeyTypes"]["/responseFragments/snapshots/unknown"]["3"] == "number"
    assert sorted(raw["collision"]["$luaTableEntries"], key=lambda item: str(type(item[0]))) == [[1, "numeric"], ["1", "text"]]
    assert not value["captureComplete"] and result["complete"] and runner.restored()


def test_write_failure_retains_frozen_original_then_full_cache_recovers_it(clock, tmp_path):
    runner = Runner(clock)
    publish = e.atomic_bytes
    def unavailable(path, raw):
        if path.parent.name == "123":
            raise OSError("retained-original disk unavailable")
        return publish(path, raw)
    with patch.object(e, "atomic_bytes", side_effect=unavailable), pytest.raises(OSError, match="disk unavailable"):
        e.capture_report(runner, 123, tmp_path)
    assert runner.restored() and pending_count(runner) == 1
    before = runner.calls
    result = e.capture_report(runner, 123, tmp_path)
    assert result["complete"] and result["cacheCheckpointPaths"] and pending_count(runner) == 0
    assert len(runner.requests()) == 1 and runner.calls - before == 3  # status, export+recover, durable ack
    before = runner.calls
    assert e.capture_report(runner, 123, tmp_path)["complete"] and runner.calls - before == 2


def test_bridge_loss_leaves_independent_journal_for_cached_reconnect(clock, tmp_path):
    runner = Runner(clock)
    execute = runner.execute_on_update
    disconnected = []
    def disconnect_before_export(code, timeout=10):
        if "local source,data='none',nil" in code:
            disconnected.append(True)
        if disconnected:
            return {"ok": False, "stage": "transport", "error": "test bridge disconnected"}
        return execute(code, timeout=timeout)
    with patch.object(runner, "execute_on_update", side_effect=disconnect_before_export):
        with pytest.raises(e.RuntimeBridgeUnavailable, match="disconnected"):
            e.capture_report(runner, 123, tmp_path)
    assert pending_count(runner) == 1 and not list(tmp_path.rglob("*.json"))
    result = e.capture_report(runner, 123, tmp_path)
    assert result["complete"] and result["cacheCheckpointPaths"]
    assert pending_count(runner) == 0 and runner.restored()


def test_wrong_account_and_other_battle_cannot_consume_pending_originals(clock, tmp_path):
    runner = Runner(clock)
    with patch.object(e, "atomic_bytes", side_effect=OSError("unavailable")), pytest.raises(OSError):
        e.capture_report(runner, 123, tmp_path)
    assert pending_count(runner) == 1 and runner.restored()
    runner.lua.execute("ClientProxy.avatarId=43")
    result = e.capture_report(runner, 123, tmp_path, expected_identity={"avatarId": "43", "serverId": "1", "scenarioId": "18"})
    assert not result["cacheCheckpointPaths"] and pending_count(runner) == 1
    runner.lua.execute("ClientProxy.avatarId=42;__complete(124)")
    result = e.capture_report(runner, 124, tmp_path)
    assert not result["cacheCheckpointPaths"] and pending_count(runner) == 1
    result = e.capture_report(runner, 123, tmp_path, expected_identity={"avatarId": "42", "serverId": "1", "scenarioId": "18"})
    assert result["cacheCheckpointPaths"] and pending_count(runner) == 0
    assert all(value["runtimeIdentity"]["avatarId"] == "42" for value in checkpoints(result))


def test_export_exception_still_detaches_and_fsyncs_received_body(clock, tmp_path):
    runner = Runner(clock)
    run = e.run_checked
    def invalid_export(runner_arg, code, *args, **kwargs):
        if "local source,data='none',nil" in code:
            raise ValueError("invalid final envelope")
        return run(runner_arg, code, *args, **kwargs)
    with patch.object(e, "run_checked", side_effect=invalid_export), pytest.raises(ValueError, match="final envelope"):
        e.capture_report(runner, 123, tmp_path)
    assert runner.restored() and pending_count(runner) == 0
    saved = [json.loads(path.read_text(encoding="utf-8")) for path in tmp_path.rglob("*.json")]
    assert any(item["responseFragments"]["snapshots"]["page"] == 1 for item in saved)
