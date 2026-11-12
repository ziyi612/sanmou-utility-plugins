"""Real Lua client-shaped requests and responses; no game/database access."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from lupa.lua51 import LuaRuntime

from capture.game_assets import export_battle_report_runtime as e
from capture.game_assets import runtime_response_audit as audit


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Runner:
    def __init__(self, clock, replies=None, partial=False):
        self.clock, self.calls = clock, 0
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(r'''
GameTime={svrTime=100};G_SCENARIO_ID=18
ErrCode={SUCCESS=0};ClientConst={BattleOutOfTime=10,ReqBattleDiffType={Normal=1}}
GetErrCodeByNum=function(code) return code==41 and 'ERR_TOOFAST' or tostring(code) end
ClientProxy={avatarId=42,GetServerId=function() return 1 end,allBattleRecordInfo={},reportStaticsInfo={}}
local cp=ClientProxy
local function battle(id) return {battleID=id,eventDataList={{eventId=1,intList={123},tab=0}},unknown='retained'} end
cp.GetCombatInfo=function(self,id) return {battleID=id} end
cp.GetAllCombatInfo=function(self,id) return battle(id) end
cp.GetDetailCombatInfo=function(self,id) return battle(id) end
cp.reportStaticsInfo[123]={damage=3};cp.reportStaticsInfo[124]={damage=4}
__sent={};__pending={};__replySpecs={}
NetMgr={}
NetMgr.SendPacket=function(self,method,proto)
 __sent[#__sent+1]={method=method,battleID=proto.battleID,at=GameTime.svrTime}
 for _,spec in ipairs(__replySpecs) do
  __pending[#__pending+1]={at=GameTime.svrTime+spec.after,battleID=spec.battleID or proto.battleID,
   page=spec.page,maxPage=spec.maxPage,code=spec.code or 0}
 end
end
cp.CallServer=function(self,method,proto) return NetMgr:SendPacket(method,proto) end
cp.RPCGetHeroBattleSnapRecords=function(self,id,tp)
 local info=self.allBattleRecordInfo[id]
 if info then
  if info.isComplete then return end
  if GameTime.svrTime-(info.time or 0)<ClientConst.BattleOutOfTime then return end
  self.allBattleRecordInfo[id]=nil
 end
 return self:CallServer('RPCGetHeroBattleSnapRecords',{battleID=id,tp=tp})
end
cp.RPCGetHeroBattleSnapRecordsResponse=function(self,msg,proto)
 if __decoder_error then error('snapshot decoder failed') end
 if proto.code~=0 then
  if proto.battleID and proto.battleID>0 then self.allBattleRecordInfo[proto.battleID]=nil end
  return
 end
 local value=self.allBattleRecordInfo[proto.battleID] or {isComplete=false,pages={},receivedCount=0,time=GameTime.svrTime}
 self.allBattleRecordInfo[proto.battleID]=value
 if proto.page==nil then value.isComplete=true;value.battleSnapData={unchecked=true};return end
 if not value.pages[proto.page] then value.pages[proto.page]={page=proto.page,marker='new'};value.receivedCount=value.receivedCount+1 end
 if value.receivedCount>=proto.maxPage then value.isComplete=true;value.battleSnapData={received=value.receivedCount} end
end
__originalResponse=cp.RPCGetHeroBattleSnapRecordsResponse
__originalSend=NetMgr.SendPacket;__originalCall=cp.CallServer
function __deliver()
 for i=# __pending,1,-1 do
  local item=__pending[i]
  if GameTime.svrTime>=item.at then
   table.remove(__pending,i)
   ClientProxy:RPCGetHeroBattleSnapRecordsResponse(1,item)
   if __change_identity then ClientProxy.avatarId=43 end
  end
 end
end
function __complete(id) cp.allBattleRecordInfo[id]={isComplete=true,battleSnapData={whole=true},pages={{marker='cached'}},receivedCount=1} end
''')
        sequence = replies if replies is not None else [{"after": 1, "page": 1, "maxPage": 1}]
        self.lua.globals()["__replySpecs"] = self.lua.table_from([self.lua.table_from(item) for item in sequence])
        if partial:
            self.lua.execute("ClientProxy.allBattleRecordInfo[123]={isComplete=false,time=100,receivedCount=1,pages={{marker='old-partial'}}}")

    def info(self):
        return {"test": True}

    def execute_on_update(self, code, timeout=10):
        self.calls += 1
        self.lua.globals().GameTime.svrTime = self.clock.now
        try:
            self.lua.globals()["__deliver"]()
            return {"ok": True, "result": self.lua.execute(code)}
        except Exception as exc:
            return {"ok": False, "stage": "lua", "error": str(exc)}

    execute = execute_on_update

    def requests(self):
        return [dict(row.items()) for _, row in self.lua.globals()["__sent"].items()]

    def restored(self):
        return self.lua.eval("__codexBattleReportAudit==nil and ClientProxy.RPCGetHeroBattleSnapRecordsResponse==__originalResponse and NetMgr.SendPacket==__originalSend and ClientProxy.CallServer==__originalCall")


@pytest.fixture
def clock():
    value = Clock()
    with patch.object(e.time, "monotonic", side_effect=value.monotonic), patch.object(e.time, "sleep", side_effect=value.sleep):
        yield value


def saved(result):
    return json.loads(Path(result["path"]).read_text(encoding="utf-8"))


def test_cached_complete_keeps_two_calls_and_installs_no_observer(clock, tmp_path):
    runner = Runner(clock)
    runner.lua.globals().__complete(123)
    result = e.capture_report(runner, 123, tmp_path)
    assert result["complete"] and runner.calls == 2 and not runner.requests() and runner.restored()


def test_default_serial_requests_are_paced_across_battles(clock, tmp_path):
    runner = Runner(clock, replies=[{"after": 0, "page": 1, "maxPage": 1}])
    first = e.capture_report(runner, 123, tmp_path)
    second = e.capture_report(runner, 124, tmp_path)
    assert first["complete"] and second["complete"]
    assert [row["at"] for row in runner.requests()] == [100, 101]
    assert runner.restored()


def test_fresh_partial_is_checkpointed_then_retried_once_after_natural_expiry(clock, tmp_path):
    runner = Runner(clock, partial=True)
    result = e.capture_report(runner, 123, tmp_path)
    assert result["complete"] and len(runner.requests()) == 1
    assert runner.requests()[0]["at"] == 110
    meta = result["componentStatus"]["snapshots"]
    assert meta["natural_expiry_retry"] and meta["retryHistory"][0]["dispatched"] is False
    assert meta["dispatched"] is True
    checkpoint = json.loads(Path(meta["cache_checkpoint_path"]).read_text(encoding="utf-8"))
    assert checkpoint["captureComplete"] is False
    assert checkpoint["componentStatus"]["all"]["complete"] is True
    assert checkpoint["componentStatus"]["snapshots"]["complete"] is False
    assert checkpoint["components"]["snapshots"]["pages"][0]["marker"] == "old-partial"
    assert saved(result)["components"]["all"]["unknown"] == "retained"
    assert runner.restored()


def test_late_remaining_page_uses_same_generation_cached_page_without_new_request(clock, tmp_path):
    runner = Runner(clock, partial=True)
    runner.lua.execute("__pending={{at=105,battleID=123,page=2,maxPage=2,code=0}}")
    result = e.capture_report(runner, 123, tmp_path)
    assert result["complete"] and clock.now == 105 and not runner.requests()
    proof = result["componentStatus"]["snapshots"]["responseCoverage"]
    assert proof["received"] == [1, 2] and proof["complete"]
    assert runner.restored()


def test_matched_server_rejection_is_preserved_without_waiting_twenty_seconds(clock, tmp_path):
    runner = Runner(clock, replies=[{"after": 1, "code": 41}])
    result = e.capture_report(runner, 123, tmp_path)
    assert not result["complete"] and clock.now == 101
    assert "ERR_TOOFAST" in result["componentStatus"]["snapshots"]["error"]
    assert saved(result)["requestAudit"]["responses"][0]["code"] == 41
    assert runner.restored()


def test_battle_zero_error_is_only_a_global_notice(clock, tmp_path):
    runner = Runner(clock, replies=[{"after": 1, "battleID": 0, "code": 41}, {"after": 2, "page": 1, "maxPage": 1}])
    result = e.capture_report(runner, 123, tmp_path)
    assert result["complete"]
    document = saved(result)
    assert document["requestAudit"]["notices"][0]["battleID"] == 0
    assert all(row["battleID"] == 123 for row in document["requestAudit"]["responses"])
    assert runner.restored()


def test_new_pages_extend_idle_budget_but_missing_page_stays_partial(clock, tmp_path):
    runner = Runner(clock, replies=[{"after": 15, "page": 1, "maxPage": 3},
                                   {"after": 30, "page": 2, "maxPage": 3},
                                   {"after": 45, "page": 3, "maxPage": 3}])
    result = e.capture_report(runner, 123, tmp_path)
    assert result["complete"] and clock.now == 145
    assert result["componentStatus"]["snapshots"]["responseCoverage"]["received"] == [1, 2, 3]
    assert runner.restored()


def test_duplicate_pages_do_not_keep_extending_wait(clock, tmp_path):
    runner = Runner(clock, replies=[{"after": 15, "page": 1, "maxPage": 2},
                                   {"after": 30, "page": 1, "maxPage": 2},
                                   {"after": 45, "page": 1, "maxPage": 2}])
    result = e.capture_report(runner, 123, tmp_path)
    assert not result["complete"] and clock.now == 135
    assert result["componentStatus"]["snapshots"]["responseCoverage"]["missing"] == [2]
    assert runner.restored()


def test_progress_still_has_a_total_wait_ceiling(clock, tmp_path):
    runner = Runner(clock, replies=[{"after": i * 15, "page": i, "maxPage": 5} for i in range(1, 6)])
    result = e.capture_report(runner, 123, tmp_path)
    assert not result["complete"] and clock.now == 160
    assert runner.restored()


def test_missing_page_metadata_never_becomes_complete_from_client_flag_alone(clock, tmp_path):
    runner = Runner(clock, replies=[{"after": 1}])
    result = e.capture_report(runner, 123, tmp_path)
    assert not result["complete"]
    assert saved(result)["components"]["snapshots"]["isComplete"] is True
    assert "invalid_or_conflicting" in result["componentStatus"]["snapshots"]["error"]
    assert runner.restored()


def test_unknown_success_enum_cannot_prove_new_response_complete(clock, tmp_path):
    runner = Runner(clock)
    runner.lua.execute("ErrCode=nil")
    result = e.capture_report(runner, 123, tmp_path)
    assert not result["complete"]
    assert result["componentStatus"]["snapshots"]["error"] == "response_page_coverage_incomplete"
    assert runner.restored()


def test_hooks_restore_inherited_methods_without_shadow_properties(clock, tmp_path):
    runner = Runner(clock)
    runner.lua.execute("""
ClientProxy.RPCGetHeroBattleSnapRecordsResponse=nil;ClientProxy.CallServer=nil
setmetatable(ClientProxy,{__index={RPCGetHeroBattleSnapRecordsResponse=__originalResponse,CallServer=__originalCall}})
NetMgr.SendPacket=nil;setmetatable(NetMgr,{__index={SendPacket=__originalSend}})
""")
    result = e.capture_report(runner, 123, tmp_path)
    assert result["complete"] and runner.restored()
    assert runner.lua.eval("rawget(ClientProxy,'RPCGetHeroBattleSnapRecordsResponse')==nil and rawget(ClientProxy,'CallServer')==nil and rawget(NetMgr,'SendPacket')==nil")


def test_zero_wait_does_not_add_ttl_or_progress_wait(clock, tmp_path):
    runner = Runner(clock, partial=True)
    result = e.capture_report(runner, 123, tmp_path, wait_seconds=0)
    assert not result["complete"] and clock.now == 100 and not runner.requests()
    assert runner.restored()


def test_identity_change_and_decoder_error_restore_exact_hooks(clock, tmp_path):
    runner = Runner(clock)
    runner.lua.globals().__change_identity = True
    with pytest.raises(RuntimeError, match="account/server/scenario changed"):
        e.capture_report(runner, 123, tmp_path, expected_identity={"avatarId": "42", "serverId": "1", "scenarioId": "18"})
    assert runner.restored()
    runner = Runner(clock)
    runner.lua.globals().__decoder_error = True
    result = e.capture_report(runner, 123, tmp_path)
    assert not result["complete"] and runner.restored()


def test_coverage_rejects_conflicting_totals_and_foreign_metadata():
    proof = audit.coverage("snapshots", {"responses": [
        {"component": "snapshots", "success": True, "page": 1, "maxPage": 2},
        {"component": "snapshots", "success": True, "page": 2, "maxPage": 3}]})
    assert proof["complete"] is False and proof["conflicting_totals"]
