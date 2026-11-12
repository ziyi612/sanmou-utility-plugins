"""Real Lua captures retain pre-handler/pre-dispatch bodies in an isolated archive."""
from __future__ import annotations

import json
from pathlib import Path
import runpy
from unittest.mock import patch

import pytest

from capture.game_assets import export_battle_report_runtime as exporter
from parser.report_archive import ReportArchive, iter_fields


BASE = runpy.run_path(str(Path(__file__).with_name("test_snapshot_partial_recovery.py")))
Clock, Runner, ALL_SETUP = BASE["Clock"], BASE["Runner"], BASE["ALL_SETUP"]
IDENTITY = {"avatarId": "42", "serverId": "1", "scenarioId": "18"}
NAMESPACE = "server:1/scenario:18/avatar:42"


@pytest.fixture
def clock():
    value = Clock()
    with patch.object(exporter.time, "monotonic", side_effect=value.monotonic), \
            patch.object(exporter.time, "sleep", side_effect=value.sleep):
        yield value


def generated_documents(result):
    paths = list(dict.fromkeys(result.get("cacheCheckpointPaths", []) +
                               ([result["path"]] if result.get("exported") else [])))
    return [(Path(path), json.loads(Path(path).read_bytes())) for path in paths]


def assert_archive_preserves_all(root, clock, result):
    """Import actual generated files, check every projected node, then re-import."""
    runner = Runner(clock)
    runner.lua.globals().__complete(123)
    baseline = exporter.capture_report(runner, 123, root / "complete", expected_identity=IDENTITY)
    assert baseline["complete"] and runner.restored()
    documents = generated_documents(result)
    assert documents
    with ReportArchive(root / "archive.db") as archive:
        best = archive.import_file(baseline["path"], namespace=NAMESPACE)
        assert best["status"] == "imported" and best["reports"][0]["completeness"] == "complete"
        best_id = best["snapshot_ids"][0]
        for path, payload in documents:
            raw = path.read_bytes()
            imported = archive.import_file(path, namespace=NAMESPACE)
            assert imported["status"] in ("imported", "duplicate")
            snapshot_id = imported["snapshot_ids"][0]
            assert archive.original_bytes(snapshot_id) == raw
            actual = [tuple(row) for row in archive.connection.execute(
                "SELECT pointer,parent_pointer,field_key,ordinal,node_type,value_json,scalar_text "
                "FROM report_fields WHERE snapshot_id=? ORDER BY pointer", (snapshot_id,))]
            assert actual == sorted(iter_fields(payload), key=lambda field: field[0])
            assert payload.get("runtimeIdentity") == IDENTITY
            if payload.get("captureKind") in ("cache_at_dispatch", "response_before_handler"):
                assert payload["captureComplete"] is False
                assert imported.get("reports", [{"completeness": "partial"}])[0]["completeness"] == "partial"
        counts = tuple(archive.connection.execute(
            "SELECT (SELECT count(*) FROM report_files),(SELECT count(*) FROM report_snapshots)").fetchone())
        unique_originals = {Path(baseline["path"]).read_bytes(), *(path.read_bytes() for path, _payload in documents)}
        assert counts == (len(unique_originals), len(unique_originals))
        for path, _payload in reversed(documents):
            assert archive.import_bytes(path.read_bytes(), source_path="replayed-copy.json", namespace=NAMESPACE)["status"] == "duplicate"
        assert tuple(archive.connection.execute(
            "SELECT (SELECT count(*) FROM report_files),(SELECT count(*) FROM report_snapshots)").fetchone()) == counts
        assert archive.stats()["records"] == 1
        current = archive.list_reports()[0]
        assert current["completeness"] == "complete"
        if not result["complete"]:
            assert current["current_snapshot_id"] == best_id
        assert archive.verify()["ok"]
    return documents


@pytest.mark.parametrize("other_components_available", [True, False])
def test_page_then_error_preserves_original_zero_false_binary_and_typed_keys(clock, tmp_path, other_components_available):
    runner = Runner(clock, replies=[])
    runner.lua.execute(r'''
__replySpecs={
 {after=1,page=1,maxPage=3,code=0,zero=0,flag=false,raw=string.char(0,255,65),
  numericKeys={[3]=0,flag=false},mixed={[1]='numeric',['1']='text',[false]=0}},
 {after=2,code=41,zero=0,flag=false}}
NetMgr.SendPacket=function(self,method,proto)
 __sent[#__sent+1]={method=method,battleID=proto.battleID,at=GameTime.svrTime}
 for _,spec in ipairs(__replySpecs) do
  local item={};for key,value in pairs(spec) do item[key]=value end
  item.at=GameTime.svrTime+item.after;item.battleID=proto.battleID
  __pending[#__pending+1]=item
 end
end
__originalSend=NetMgr.SendPacket
''')
    if not other_components_available:
        runner.lua.execute('''
ClientProxy.GetCombatInfo=function() return nil end
ClientProxy.GetAllCombatInfo=function() return nil end
ClientProxy.GetDetailCombatInfo=function() return nil end
ClientProxy.reportStaticsInfo={}
''')
    result = exporter.capture_report(runner, 123, tmp_path / "runtime", expected_identity=IDENTITY)
    assert not result["complete"] and runner.lua.eval("ClientProxy.allBattleRecordInfo[123]==nil")
    if not other_components_available:
        assert result["exported"] is False and result["error"] == "not_found 123"
    assert runner.restored()
    documents = assert_archive_preserves_all(tmp_path, clock, result)
    received = [payload for _path, payload in documents if payload.get("source") == "response_fragments"
                and payload.get("responseFragments", {}).get("snapshots", {}).get("page") == 1]
    assert received
    payload = received[0]
    response = payload["responseFragments"]["snapshots"]
    assert response["zero"] == 0 and response["flag"] is False
    assert response["raw"] == {"$luaBytesHex": "00ff41"}
    assert response["numericKeys"] == {"3": 0, "flag": False}
    assert payload["luaTableKeyTypes"]["/responseFragments/snapshots/numericKeys"]["3"] == "number"
    entries = response["mixed"]["$luaTableEntries"]
    assert any(type(key) is int and key == 1 and value == "numeric" for key, value in entries)
    assert any(type(key) is str and key == "1" and value == "text" for key, value in entries)
    assert any(key is False and type(value) is int and value == 0 for key, value in entries)


def test_page_arriving_during_dispatch_pacing_survives_cache_clear_and_archive(clock, tmp_path):
    runner = Runner(clock, replies=[], partial=True)
    runner.lua.execute("ClientProxy.allBattleRecordInfo[123].time=90")
    runner._battle_prefetch_dispatch_times = {"__all_methods__": clock.now}
    arrived = False
    def sleep(seconds):
        nonlocal arrived
        clock.sleep(seconds)
        if not arrived:
            arrived = True
            runner.lua.globals().GameTime.svrTime = clock.now
            runner.lua.execute("ClientProxy:RPCGetHeroBattleSnapRecordsResponse(1,{battleID=123,page=2,maxPage=3,code=0,zero=0,flag=false})")
    with patch.object(exporter.time, "sleep", side_effect=sleep):
        result = exporter.capture_report(runner, 123, tmp_path / "runtime", expected_identity=IDENTITY)
    assert arrived and runner.requests()[0]["at"] == 101
    assert not result["complete"] and runner.lua.eval("ClientProxy.allBattleRecordInfo[123]==nil")
    assert runner.restored()
    documents = assert_archive_preserves_all(tmp_path, clock, result)
    caches = [payload for _path, payload in documents if payload.get("captureKind") == "cache_at_dispatch"]
    assert any(len(payload["components"]["snapshots"]["pages"]) == 2 for payload in caches)
    responses = [payload["responseFragments"]["snapshots"] for _path, payload in documents
                 if payload.get("captureKind") == "response_before_handler"]
    assert any(value.get("page") == 2 and value["zero"] == 0 and value["flag"] is False for value in responses)


@pytest.mark.parametrize("component", ["all", "detail"])
def test_expiring_compressed_slices_preserve_actual_carrier_and_archive(clock, tmp_path, component):
    runner = Runner(clock, replies=[])
    setup = ALL_SETUP if component == "all" else ALL_SETUP.replace("_all", "_detail").replace("GetAllCombatInfo", "GetDetailCombatInfo")
    runner.lua.execute(setup)
    runner.lua.execute("ClientProxy._" + component + "CombatSlices[123]={time=90,receivedCount=1,[1]=string.char(0,255,65),[3]=false,zero=0};__complete(123)")
    result = exporter.capture_report(runner, 123, tmp_path / "runtime", expected_identity=IDENTITY)
    assert result["complete"] and runner.restored()
    documents = assert_archive_preserves_all(tmp_path, clock, result)
    carriers = [payload for _path, payload in documents if payload.get("captureKind") == "cache_at_dispatch"
                and component in payload.get("componentFragments", {})]
    assert carriers
    payload = carriers[0]
    fragments = payload["componentFragments"][component]
    assert fragments["1"] == {"$luaBytesHex": "00ff41"} and fragments["3"] is False and fragments["zero"] == 0
    assert payload["luaTableKeyTypes"]["/componentFragments/" + component]["1"] == "number"
    assert payload["luaTableKeyTypes"]["/componentFragments/" + component]["3"] == "number"
