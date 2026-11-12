"""Do not discard captured statistics/snapshots when report/event caches are absent."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from lupa.lua51 import LuaRuntime

from capture.game_assets import export_battle_report_runtime as e
from parser.report_archive import ReportArchive


class Runner:
    def __init__(self, setup=""):
        self.calls = 0
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(r'''
ClientConst={BattleOutOfTime=10,ReqBattleDiffType={Normal=1}}
GameTime={svrTime=100};ErrCode={SUCCESS=0}
GetErrCodeByNum=function(code) return code==41 and 'ERR_TOOFAST' or 'GLOBAL_NOTICE' end
ClientProxy={_basic={},_all={},_detail={},reportStaticsInfo={},allBattleRecordInfo={}}
local cp=ClientProxy
cp.avatarId=42;cp.GetServerId=function() return 1 end;G_SCENARIO_ID=18
cp.GetCombatInfo=function(self,id) return self._basic[id] end
cp.GetAllCombatInfo=function(self,id) return self._all[id] end
cp.GetDetailCombatInfo=function(self,id) return self._detail[id] end
local methods={all='RPCGetAllCombatInfo',detail='RPCGetDetailCombatInfo',statics='RPCGetStaticsCombatInfo',snapshots='RPCGetHeroBattleSnapRecords'}
for _,method in pairs(methods) do
 local name=method
 cp[name]=function(self,id,tp) return self:CallServer(name,{battleID=id,tp=tp}) end
 cp[name..'Response']=function(self,msg,proto) return end
end
NetMgr={SendPacket=function(self,method,proto)
 cp[method..'Response'](cp,1,{battleID=0,code=99})
 cp[method..'Response'](cp,1,{battleID=proto.battleID,code=41})
end}
cp.CallServer=function(self,method,proto) return NetMgr:SendPacket(method,proto) end
__originalSend=NetMgr.SendPacket;__originalCall=cp.CallServer
''')
        self.lua.execute(setup)

    def info(self):
        return {}

    def execute_on_update(self, code, timeout=10):
        self.calls += 1
        try:
            return {"ok": True, "result": self.lua.execute(code)}
        except Exception as exc:
            return {"ok": False, "stage": "lua", "error": str(exc)}

    execute = execute_on_update


@pytest.mark.parametrize("component,setup,expected", [
    ("statics", "ClientProxy.reportStaticsInfo[123]={damage=0,unknown={x='kept'}}", {"damage": 0, "unknown": {"x": "kept"}}),
    ("snapshots", "ClientProxy.allBattleRecordInfo[123]={isComplete=true,receivedCount=1,pages={{raw='kept'}},battleSnapData={zero=0}}",
        {"isComplete": True, "receivedCount": 1, "pages": [{"raw": "kept"}], "battleSnapData": {"zero": 0}}),
    ("statics", "ClientProxy.reportStaticsInfo[123]={}", []),
    ("statics", "ClientProxy.reportStaticsInfo[123]=0", 0),
    ("statics", "ClientProxy.reportStaticsInfo[123]=false", False),
    ("basic", "ClientProxy._basic[123]=false", False),
])
def test_only_secondary_component_is_saved_and_indexes_all_original_values(tmp_path, component, setup, expected):
    runner = Runner(setup)
    result = e.capture_report(runner, 123, tmp_path / "capture", request_missing=False)
    assert result["exported"] is True and result["complete"] is False
    path = Path(result["path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source"] == component
    assert payload["components"] == {component: expected}
    assert isinstance(payload["data"], dict)
    assert (component == "basic" or "basic" not in payload["components"]) and "eventDataList" not in payload["data"]
    assert payload["captureComplete"] is False and payload["eventCount"] <= 0
    with ReportArchive(tmp_path / "archive.db") as archive:
        imported = archive.import_file(path, namespace="test")
        assert imported["status"] == "imported"
        current = archive.connection.execute("SELECT battle_uid,completeness,event_count FROM report_current_snapshots").fetchone()
        assert tuple(current) == ("123", "partial", 0)
        original = archive.connection.execute("SELECT raw_bytes FROM report_files").fetchone()[0]
        assert original == path.read_bytes()
        fields = {row[0]: row[1] for row in archive.connection.execute("SELECT pointer,value_json FROM report_fields")}
        if component == "snapshots":
            assert json.loads(fields["/components/snapshots/battleSnapData/zero"]) == 0
            assert json.loads(fields["/components/snapshots/pages/0/raw"]) == "kept"
        elif isinstance(expected, dict):
            assert json.loads(fields["/components/statics/damage"]) == 0
            assert json.loads(fields["/components/statics/unknown/x"]) == "kept"
        else:
            assert json.loads(fields[f"/components/{component}"]) == expected
        assert archive.verify()["ok"]


@pytest.mark.parametrize("invalid", ["false", "0", "'invalid-scalar'"])
def test_scalar_statics_stays_available_but_incomplete_with_other_four_complete(tmp_path, invalid):
    runner = Runner("""
local data={battleID=123,eventDataList={{eventId=0,intList={},tab=0}}}
ClientProxy._basic[123]={battleID=123};ClientProxy._all[123]=data;ClientProxy._detail[123]=data
ClientProxy.allBattleRecordInfo[123]={isComplete=true,battleSnapData={zero=0}}
ClientProxy.reportStaticsInfo[123]=""" + invalid)
    status = e.read_status(runner, 123)
    assert status["statics"]["available"] is True and status["statics"]["complete"] is False
    result = e.capture_report(runner, 123, tmp_path, request_missing=False)
    assert result["exported"] and not result["complete"] and result["eventCount"] == 1
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert payload["componentStatus"]["statics"]["available"] is True
    assert payload["componentStatus"]["statics"]["complete"] is False
    with ReportArchive(tmp_path / "archive.db") as archive:
        assert archive.import_file(result["path"], namespace="test")["status"] == "imported"
        assert archive.connection.execute("SELECT completeness FROM report_current_snapshots").fetchone()[0] == "partial"


def test_sparse_numeric_keys_keep_original_and_wrapped_data_key_types(tmp_path):
    runner = Runner("ClientProxy.reportStaticsInfo[123]={[2]='kept-numeric-key'}")
    result = e.capture_report(runner, 123, tmp_path, request_missing=False)
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert payload["components"] == {"statics": {"2": "kept-numeric-key"}}
    assert payload["data"] == {"statics": {"2": "kept-numeric-key"}}
    assert payload["luaTableKeyTypes"]["/components/statics"] == {"2": "number"}
    assert payload["luaTableKeyTypes"]["/data/statics"] == {"2": "number"}
    assert "/data" not in payload["luaTableKeyTypes"]
    with ReportArchive(tmp_path / "archive.db") as archive:
        assert archive.import_file(result["path"], namespace="test")["status"] == "imported"
        assert archive.verify()["ok"]


def test_all_nil_keeps_not_found_compatibility_and_returns_matched_and_global_audit(tmp_path):
    runner = Runner()
    now = [100.0]
    with patch.object(e.time, "monotonic", side_effect=lambda: now[0]), \
            patch.object(e.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
        result = e.capture_report(runner, 123, tmp_path)
    assert result["exported"] is False and result["complete"] is False
    assert result["error"] == "not_found 123"
    assert not list(tmp_path.glob("battle_*"))
    audit = result["requestAudit"]
    assert len(audit["responses"]) == 4 and all(row["battleID"] == 123 and row["code"] == 41 for row in audit["responses"])
    assert len(audit["notices"]) == 4 and all(row["battleID"] == 0 for row in audit["notices"])
    assert all(item["callServer"] == item["sendPacket"] == 1 for item in audit["dispatches"].values())
    assert all("ERR_TOOFAST" in result["componentStatus"][name]["error"] for name in ("all", "detail", "statics", "snapshots"))
    assert runner.lua.eval("__codexBattleReportAudit==nil and NetMgr.SendPacket==__originalSend and ClientProxy.CallServer==__originalCall")


def test_zero_events_stays_partial_and_complete_cached_path_keeps_two_calls(tmp_path):
    setup = """
local data={battleID=123,eventDataList={}}
ClientProxy._basic[123]={battleID=123};ClientProxy._all[123]=data;ClientProxy._detail[123]=data
ClientProxy.reportStaticsInfo[123]={damage=0}
ClientProxy.allBattleRecordInfo[123]={isComplete=true,battleSnapData={zero=0}}
"""
    runner = Runner(setup)
    result = e.capture_report(runner, 123, tmp_path / "empty")
    assert result["exported"] and not result["complete"] and result["eventCount"] == 0 and runner.calls == 2
    runner = Runner(setup + "ClientProxy._all[123].eventDataList={{eventId=0,intList={},tab=0}}")
    result = e.capture_report(runner, 123, tmp_path / "full")
    assert result["complete"] and result["eventCount"] == 1 and runner.calls == 2


def test_invalid_scalar_all_does_not_hide_real_detail_events(tmp_path):
    runner = Runner("ClientProxy._all[123]=false;ClientProxy._detail[123]={battleID=123,eventDataList={{eventId=0,intList={},tab=0}}}")
    result = e.capture_report(runner, 123, tmp_path, request_missing=False)
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert payload["source"] == "detail" and payload["components"]["all"] is False
    assert result["eventCount"] == 1 and not result["complete"]
    with ReportArchive(tmp_path / "archive.db") as archive:
        assert archive.import_file(result["path"], namespace="test")["status"] == "imported"
        assert archive.connection.execute("SELECT event_count FROM report_current_snapshots").fetchone()[0] == 1
