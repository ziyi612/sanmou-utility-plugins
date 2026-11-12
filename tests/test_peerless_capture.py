"""Client verdict sidecars do not mutate protocol rows or add runtime calls."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from lupa.lua51 import LuaRuntime

from capture.game_assets.battle_collector import BattleCollector, install_lua
from capture.game_assets.peerless_capture import EVALUATOR, evaluation_metadata
from parser.report_archive import ReportArchive


BASE = r'''
CommonConst={SEG_LEN=10,BattleBaseRelationType={ALL=1000},
  BattleContinousReportType={Normal=42,PVBarbarianArmy=43}}
ClientConst={BattleHFMinBattleNum=2}
game_config_battle_config={battle_broadcast_max_war_contribute={content='33'},
  battle_broadcast_max_game={content='2'},killstreaks_banner_min_feat={content='1'}}
G_SCENARIO_ID=18
ClientProxy={avatarId=100,GetServerId=function() return 1 end}
ErrCode={SUCCESS=0}
helperCalls=0;requestCalls=0;requireCalls=0
require=function() requireCalls=requireCalls+1;error('must not require any module') end
local helper={IsLegend=function(zg,wins,stamp)
  helperCalls=helperCalls+1;lastInputs={zg,wins,stamp}
  if wins==77 then error('current helper error') end
  if wins==88 then return 'unexpected' end
  return zg>=tonumber(game_config_battle_config.battle_broadcast_max_war_contribute.content)
    and wins>=tonumber(game_config_battle_config.battle_broadcast_max_game.content)
end}
package.loaded['common_lib.battlelog_lib.BattleReportHelper']=helper
rows={}
ClientProxy.RPCGetBattleBlockList=function(self,index,relation)
  requestCalls=requestCalls+1
  self:RPCGetBattleBlockListResponse(1,{code=0,sIndex=index,reqLen=#rows,blockInfoList=rows})
end
ClientProxy.RPCGetBattleBlockListResponse=function() end
'''


class Runner:
    def __init__(self, setup=""):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(BASE + setup)
        self.calls = 0

    def execute_on_update(self, code, timeout=10):
        self.calls += 1
        try:
            return {"ok": True, "result": self.lua.execute(code)}
        except Exception as exc:
            return {"ok": False, "stage": "lua", "error": str(exc)}

    execute = execute_on_update


def row(bid=123, uid=200, **kwargs):
    info = {"isMuti": True, "avatarId": uid, "zgNum": 33, "winBattleNum": 2,
            "continuousInfoType": 42}
    info.update(kwargs)
    return {"combatInfo": {"battleID": bid, "endTimeStampTime": 1794100000,
                           "unknown": {"zero": 0, "flag": False, "empty": []}},
            "blockHash": "block-" + str(bid), "otherInfo": {"continuousInfo": info}}


def put_rows(runner, values):
    # JSON text is embedded as an ordinary Lua string; the tiny converter keeps
    # fake server data separate from the client-under-test serializer.
    def lua(value):
        if isinstance(value, dict):
            return "{" + ",".join("["+lua(key)+"]="+lua(item) for key,item in value.items()) + "}"
        if isinstance(value, list):
            return "{" + ",".join(lua(item) for item in value) + "}"
        if value is None:
            return "nil"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            # Lua source accepts UTF-8 directly; Lua 5.1 does not decode JSON's
            # \uXXXX escape, so ASCII JSON escaping would corrupt real names.
            return json.dumps(value, ensure_ascii=False)
        return str(value)
    runner.lua.execute("rows=" + lua(values))


def page(tmp_path, runner, values):
    put_rows(runner, values)
    collector = BattleCollector(runner, tmp_path, request_delay=0, namespace="account")
    collector.state["runtimeCapabilities"] = {"successCode": 0}
    runner.lua.execute(install_lua(collector.owner))
    return collector, collector.page("personal", 1, 1000, 10)


def test_actual_loaded_helper_and_runtime_enums_are_used_without_extra_calls(tmp_path):
    original = row()
    runner = Runner()
    collector, (rows, nxt, response, path) = page(tmp_path, runner, [original])
    assert runner.calls == 2  # Existing request and first response poll only.
    assert runner.lua.eval("requestCalls") == runner.lua.eval("helperCalls") == 1
    assert runner.lua.eval("requireCalls") == 0
    assert rows == [original] and nxt is None
    values = response.client_peerless
    assert len(values) == 1
    item = values[0]
    assert item["legend"] is True and item["status"] == "evaluated"
    assert item["evaluator"] == EVALUATOR and item["category"] == "normal"
    assert item["category_state"] == "runtime_enum"
    assert item["rule"]["normalType"] == 42 and item["rule"]["pvBarbarianArmyType"] == 43
    assert item["rule"]["maxWarContribute"] == 33 and item["rule"]["maxGame"] == 2
    assert [runner.lua.eval(f"lastInputs[{n}]") for n in (1, 2, 3)] == [33, 2, 1794100000]
    assert len(item["helper_bytecode_sha256"]) == 64
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["payload"]["response"]["blockInfoList"] == [original]
    assert "function_bytecode" in envelope["payload"]["clientPeerlessEvaluator"]
    assert "clientPeerless" not in rows[0]
    collector.remember("personal", rows[0], response, path)
    observation = next(iter(collector.state["reports"]["123"]["observations"].values()))
    assert observation["clientPeerless"] == values
    assert "_clientPeerlessEvaluations" not in observation["responseMetadata"]
    assert observation["row"] == original


@pytest.mark.parametrize("patch,status,reason", [
    ({"zgNum": 32}, "evaluated", None),
    ({"winBattleNum": 1}, "evaluated", None),
    ({"continuousInfoType": 43}, "excluded", "barbarian_legend_is_not_peerless"),
    ({"isMuti": False}, "excluded", "not_multi_battle_summary"),
    ({"isMuti": 1}, "invalid", "isMuti_is_not_boolean"),
    ({"avatarId": 0}, "invalid", "battle_or_player_id_invalid"),
    ({"winBattleNum": 2.5}, "invalid", "continuous_settlement_values_invalid"),
    ({"continuousInfoType": 99}, "unavailable", "continuous_type_unverified"),
    ({"winBattleNum": 77}, "unavailable", "client_helper_failed"),
    ({"winBattleNum": 88}, "unavailable", "client_helper_returned_nonboolean"),
])
def test_false_excluded_and_unknown_are_distinct(tmp_path, patch, status, reason):
    runner = Runner()
    _, (_, _, response, _) = page(tmp_path, runner, [row(**patch)])
    item = response.client_peerless[0]
    assert item["status"] == status
    if reason:
        assert item["reason"] == reason and "legend" not in item
    else:
        assert item["legend"] is False


@pytest.mark.parametrize("setup,reason", [
    ("package.loaded['common_lib.battlelog_lib.BattleReportHelper']=nil", "current_client_helper_not_loaded"),
    ("CommonConst.BattleContinousReportType=nil", "continuous_type_unverified"),
])
def test_missing_runtime_rule_or_helper_never_requires_or_guesses(tmp_path, setup, reason):
    runner = Runner(setup)
    _, (_, _, response, _) = page(tmp_path, runner, [row()])
    item = response.client_peerless[0]
    assert item["status"] == "unavailable" and item["reason"] == reason and "legend" not in item
    assert runner.lua.eval("helperCalls") == runner.lua.eval("requireCalls") == 0


def test_missing_end_time_does_not_guess_today_or_change_page_hash(tmp_path):
    value = row()
    value["combatInfo"].pop("endTimeStampTime")
    runner = Runner()
    collector, (_, _, response, first) = page(tmp_path, runner, [value])
    item = response.client_peerless[0]
    assert item["status"] == "unavailable" and item["reason"] == "battle_end_time_unavailable"
    assert "legend" not in item and runner.lua.eval("helperCalls") == 0
    _, _, _, second = collector.page("personal", 1, 1000, 10)
    assert first == second and len(list(first.parent.glob("*.json"))) == 1


def test_absent_continuous_metadata_and_child_pages_do_not_add_sidecars(tmp_path):
    runner = Runner()
    original = row()
    original.pop("otherInfo")
    _, (_, _, response, path) = page(tmp_path, runner, [original])
    assert not response.client_peerless
    payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
    assert "clientPeerlessEvaluations" not in payload and "clientPeerlessEvaluator" not in payload
    assert runner.lua.eval("helperCalls") == runner.lua.eval("requireCalls") == 0


def test_discovery_archive_is_lossless_durable_account_scoped_and_idempotent(tmp_path):
    runner = Runner()
    original = row()
    with ReportArchive(tmp_path / "reports.db") as archive:
        collector, (rows, _, response, path) = page(tmp_path / "runtime", runner, [original])
        collector.on_export = lambda source: archive.import_file(source, namespace="account")
        collector.remember("personal", rows[0], response, path)
        first = collector.state["reports"]["123"]["discoveryArchivePath"]
        collector.remember("personal", rows[0], response, path)
        assert collector.state["reports"]["123"]["discoveryArchivePath"] == first
        assert archive.stats()["records"] == archive.stats()["snapshots"] == 1
        payload = json.loads(Path(first).read_text(encoding="utf-8"))
        assert payload["discovery"][0]["row"] == original
        assert payload["discovery"][0]["clientPeerless"][0]["legend"] is True
        sid = archive.connection.execute("SELECT id FROM report_snapshots").fetchone()[0]
        assert archive.original_bytes(sid) == Path(first).read_bytes()
        assert archive.verify()["ok"]
        restarted = BattleCollector(None, collector.out_dir, namespace="account")
        assert restarted.state["reports"]["123"]["observations"] == collector.state["reports"]["123"]["observations"]


def test_same_battle_different_player_observations_only_keep_their_own_evaluation(tmp_path):
    originals = [row(uid=200), row(uid=300)]
    runner = Runner()
    collector, (rows, _, response, path) = page(tmp_path, runner, originals)
    for value in rows:
        collector.remember("personal", value, response, path)
    saved = list(collector.state["reports"]["123"]["observations"].values())
    assert len(saved) == 2
    for observation in saved:
        assert len(observation["clientPeerless"]) == 1
        assert observation["clientPeerless"][0]["avatarId"] == observation["row"]["otherInfo"]["continuousInfo"]["avatarId"]


def test_normal_type_default_is_explicit_and_unverified_serialization_never_invented(tmp_path):
    original = row()
    original["otherInfo"]["continuousInfo"].pop("continuousInfoType")
    runner = Runner()
    _, (_, _, response, _) = page(tmp_path, runner, [original])
    item = response.client_peerless[0]
    assert item["continuousInfoType"] == 42 and item["continuousInfoTypeDefaulted"] is True
    assert item["legend"] is True
    # A malformed synthetic bytecode annotation cannot break page persistence.
    assert evaluation_metadata({"clientPeerlessEvaluations": [{}],
                                "clientPeerlessEvaluator": {"function_bytecode": {"$luaBytesHex": "bad"}}}) == [{}]


def test_same_battle_same_player_distinct_summary_versions_do_not_cross_attach(tmp_path):
    originals = [row(), row(zgNum=0)]
    runner = Runner()
    collector, (rows, _, response, path) = page(tmp_path, runner, originals)
    for value in rows:
        collector.remember("personal", value, response, path)
    saved = list(collector.state["reports"]["123"]["observations"].values())
    assert len(saved) == 2
    for observation in saved:
        assert len(observation["clientPeerless"]) == 1
        actual = observation["clientPeerless"][0]
        assert actual["zgNum"] == observation["row"]["otherInfo"]["continuousInfo"]["zgNum"]
        assert actual["legend"] is (actual["zgNum"] == 33)


def test_evaluated_unchanged_pages_deduplicate_even_while_client_clock_changes(tmp_path):
    runner = Runner()
    collector, (_, _, response, first) = page(tmp_path, runner, [row()])
    runner.lua.execute("GameTime={svrTime=9999999999};os.time=function() return 2000000000 end")
    _, _, response_again, second = collector.page("personal", 1, 1000, 10)
    assert first == second and response.client_peerless == response_again.client_peerless
    assert len(list(first.parent.glob("*.json"))) == 1


def test_child_response_never_evaluates_or_inherits_its_parent_achievement(tmp_path):
    runner = Runner(r'''
ClientProxy.RPCGetChildCombatInfoList=function(self,parent,index,relation)
  requestCalls=requestCalls+1
  self:RPCGetChildCombatInfoListResponse(1,{code=0,sIndex=index,reqLen=1,parentHashKey=parent,
    combatInfoList={{battleID=456,endTimeStampTime=1794100000}}})
end
ClientProxy.RPCGetChildCombatInfoListResponse=function() end
''')
    collector = BattleCollector(runner, tmp_path, request_delay=0, namespace="account")
    runner.lua.execute(install_lua(collector.owner))
    collector.state["runtimeCapabilities"] = {"successCode": 0}
    values, _, response, path = collector.page("personal", 1, 1000, 10, parent="123")
    assert not response.client_peerless and runner.lua.eval("helperCalls") == 0
    collector.remember("personal", values[0], response, path, parent="123")
    observation = next(iter(collector.state["reports"]["456"]["observations"].values()))
    assert "clientPeerless" not in observation


def test_original_response_fields_cannot_collide_with_local_sidecar(tmp_path):
    runner = Runner(r'''
ClientProxy.RPCGetBattleBlockList=function(self,index,relation)
  requestCalls=requestCalls+1
  self:RPCGetBattleBlockListResponse(1,{code=0,sIndex=index,reqLen=#rows,blockInfoList=rows,
    _clientPeerlessEvaluations='original-network-value'})
end
''')
    collector, (values, _, response, path) = page(tmp_path, runner, [row()])
    collector.remember("personal", values[0], response, path)
    observation = next(iter(collector.state["reports"]["123"]["observations"].values()))
    assert observation["responseMetadata"]["_clientPeerlessEvaluations"] == "original-network-value"
    assert observation["clientPeerless"][0]["legend"] is True
