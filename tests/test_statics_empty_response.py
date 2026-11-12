"""A matched statistics reply rejected by ImpBattle need not wait for a cache."""
import json
from pathlib import Path
import runpy
from unittest.mock import patch

import pytest

from capture.game_assets import export_battle_report_runtime as exporter
from capture.game_assets import runtime_response_audit as audit
from parser.report_archive import ReportArchive


BASE = runpy.run_path(str(Path(__file__).with_name("test_component_response_audit.py")))
Clock, Runner = BASE["Clock"], BASE["Runner"]


@pytest.fixture
def clock():
    value = Clock()
    with patch.object(exporter.time, "monotonic", side_effect=value.monotonic), \
            patch.object(exporter.time, "sleep", side_effect=value.sleep):
        yield value


def statics_runner(clock, response, *, after=1):
    runner = Runner(clock, replies=[])
    runner.lua.execute(r'''
__complete(123);ClientProxy.reportStaticsInfo[123]=nil
ClientProxy.RPCGetStaticsCombatInfo=function(self,id)
  if self.reportStaticsInfo[id] then return end
  return self:CallServer('RPCGetStaticsCombatInfo',{battleID=id})
end
ClientProxy.RPCGetStaticsCombatInfoResponse=function(self,msg,proto)
  if proto.code~=ErrCode.SUCCESS then return end
  -- The exact early-return guard in the shipped ImpBattle client.
  if not proto.binStatics or #proto.binStatics<4 then return end
  self.reportStaticsInfo[proto.battleID]={damage=0,unknown='valid-decoded-statics'}
end
__originalStaticsResponse=ClientProxy.RPCGetStaticsCombatInfoResponse
local oldSend=NetMgr.SendPacket
NetMgr.SendPacket=function(self,method,proto)
  if method~='RPCGetStaticsCombatInfo' then return oldSend(self,method,proto) end
  __sent[#__sent+1]={method=method,battleID=proto.battleID,at=GameTime.svrTime}
  __staticsPending={at=GameTime.svrTime+''' + str(after) + ",proto=" + response + r'''}
end
__originalSend=NetMgr.SendPacket
local oldDeliver=__deliver
__deliver=function()
  oldDeliver()
  if __staticsPending and GameTime.svrTime>=__staticsPending.at then
    local item=__staticsPending;__staticsPending=nil
    ClientProxy:RPCGetStaticsCombatInfoResponse(1,item.proto)
  end
end
''')
    return runner


def saved_checkpoints(result):
    return [json.loads(Path(path).read_bytes()) for path in result["cacheCheckpointPaths"]]


@pytest.mark.parametrize("value,length,reason", [
    ("''", 0, "short_bin_statics"), ("'x'", 1, "short_bin_statics"),
    ("string.char(0,255)", 2, "short_bin_statics"), ("'abc'", 3, "short_bin_statics"),
    ("nil", None, "missing_bin_statics"), ("false", None, "missing_bin_statics"),
])
def test_empty_or_short_current_target_statics_stops_wait_and_preserves_original(clock, tmp_path, value, length, reason):
    runner = statics_runner(clock, "{battleID=123,code=0,binStatics=" + value + "}")
    result = exporter.capture_report(runner, 123, tmp_path)
    assert clock.now == 101 and len(runner.requests()) == 1
    assert result["exported"] and not result["complete"]
    status = result["componentStatus"]["statics"]
    assert status["error"] == "server_empty_component" and not status["complete"]
    proof = status["responseCoverage"]
    assert proof["empty_reason"] == reason and proof["complete"] is False
    response = proof["server_response"]
    assert response["battleID"] == 123 and response["generation"] == 1 and response["code"] == 0
    assert response.get("binStaticsBytes") == length
    raw = next(item for item in saved_checkpoints(result) if item.get("responseFragments", {}).get("statics"))
    assert not raw["captureComplete"] and raw["responseFragments"]["statics"]["code"] == 0
    if value == "''":
        assert raw["responseFragments"]["statics"]["binStatics"] == ""
    elif value == "string.char(0,255)":
        assert raw["responseFragments"]["statics"]["binStatics"] == {"$luaBytesHex": "00ff"}
    assert runner.restored() and runner.lua.eval("ClientProxy.RPCGetStaticsCombatInfoResponse==__originalStaticsResponse")


@pytest.mark.parametrize("value,length", [("'abcd'", 4), ("string.char(0,255,65,0)", 4), ("'汉汉'", 6)])
def test_valid_statics_keep_full_twenty_second_response_budget(clock, tmp_path, value, length):
    runner = statics_runner(clock, "{battleID=123,code=0,binStatics=" + value + "}", after=19.5)
    result = exporter.capture_report(runner, 123, tmp_path)
    assert result["complete"] and clock.now == 119.5
    status = result["componentStatus"]["statics"]
    assert status["complete"] and "error" not in status and status["responseCoverage"]["complete"]
    payload = json.loads(Path(result["path"]).read_bytes())
    observed = next(row for row in payload["requestAudit"]["responses"] if row["component"] == "statics")
    assert observed["binStaticsBytes"] == length and not observed.get("emptyComponent")
    assert payload["components"]["statics"]["unknown"] == "valid-decoded-statics" and runner.restored()


@pytest.mark.parametrize("response", [
    "{battleID=124,code=0,binStatics=''}",
    "{battleID=0,code=36064,binStatics=''}",
    "{battleID=0,code=35484,binStatics=''}",
])
def test_other_battle_and_zero_id_notices_cannot_end_target_wait(clock, tmp_path, response):
    runner = statics_runner(clock, response)
    result = exporter.capture_report(runner, 123, tmp_path)
    assert clock.now == 120
    assert result["componentStatus"]["statics"]["error"] == "response_page_coverage_incomplete"
    payload = json.loads(Path(result["path"]).read_bytes())
    assert not payload["requestAudit"]["responses"]
    if "battleID=0" in response:
        assert len(payload["requestAudit"]["notices"]) == 1
    assert runner.restored()


def test_server_rejection_precedes_empty_payload_classification(clock, tmp_path):
    runner = statics_runner(clock, "{battleID=123,code=41,binStatics=''}")
    result = exporter.capture_report(runner, 123, tmp_path)
    assert clock.now == 101 and result["componentStatus"]["statics"]["error"] == "server_rejected:ERR_TOOFAST"


def test_valid_length_without_decoded_cache_still_keeps_normal_wait_and_partial_gate(clock, tmp_path):
    runner = statics_runner(clock, "{battleID=123,code=0,binStatics='abcd'}")
    runner.lua.execute("ClientProxy.RPCGetStaticsCombatInfoResponse=function() return end;__originalStaticsResponse=ClientProxy.RPCGetStaticsCombatInfoResponse")
    result = exporter.capture_report(runner, 123, tmp_path)
    assert not result["complete"] and clock.now == 120
    assert result["componentStatus"]["statics"]["error"] == "timeout" and runner.restored()


def test_current_observed_callserver_can_bind_empty_reply_when_packet_hook_is_unavailable():
    proof = audit.coverage("statics", {"battleID": 123, "sendPacketObserved": False,
        "dispatches": {"statics": {"callServer": 1, "sendPacket": 0}},
        "responses": [{"component": "statics", "battleID": 123, "generation": 1,
                       "success": True, "emptyComponent": True, "emptyReason": "short_bin_statics"}]})
    assert proof["error"] == "server_empty_component" and proof["complete"] is False


@pytest.mark.parametrize("generation,target,rows,expected", [
    (2, 123, [(1, 123, True), (2, 123, False)], True),
    (2, 123, [(1, 123, True)], None),
    (1, 123, [(0, 123, True)], None),
    (1, 123, [(1, 124, True)], None),
    (1, 123, [(1, 0, True)], None),
    (0, 123, [(0, 123, True)], True),
    (1, None, [(1, 123, True)], True),
])
def test_only_matching_current_send_and_target_can_reject_empty_statics(generation, target, rows, expected):
    data = {"dispatches": {"statics": {"sendPacket": generation}}, "sendPacketObserved": True,
            "responses": [{"component": "statics", "success": True, "battleID": bid,
                           "generation": era, "emptyComponent": empty, "emptyReason": "short_bin_statics"}
                          for era, bid, empty in rows]}
    if target is not None:
        data["battleID"] = target
    proof = audit.coverage("statics", data)
    assert proof["complete"] is expected and "error" not in proof


def test_empty_statics_checkpoint_is_partial_and_reimport_adds_no_logical_report(clock, tmp_path):
    runner = statics_runner(clock, "{battleID=123,code=0,binStatics=''}")
    result = exporter.capture_report(runner, 123, tmp_path / "runtime")
    path = next(path for path in result["cacheCheckpointPaths"]
                if json.loads(Path(path).read_bytes()).get("responseFragments", {}).get("statics"))
    with ReportArchive(tmp_path / "archive.db") as archive:
        imported = archive.import_file(path, namespace="test")
        assert imported["reports"][0]["completeness"] == "partial"
        assert archive.import_file(path, namespace="test")["status"] == "duplicate"
        assert archive.stats()["records"] == 1 and archive.verify()["ok"]
