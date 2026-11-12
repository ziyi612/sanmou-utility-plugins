"""Late cache responses can resolve a local wait without another RPC or wait."""
import json
from pathlib import Path
import runpy
from unittest.mock import patch

import pytest


BASE = runpy.run_path(str(Path(__file__).with_name("test_snapshot_partial_recovery.py")))
exporter, Runner, Clock = BASE["e"], BASE["Runner"], BASE["Clock"]


def capture_late_all(tmp_path, response="complete", zero_events=False):
    clock = Clock()
    runner = Runner(clock, replies=[{"after": 1, "page": 1, "maxPage": 1}])
    setup = BASE["ALL_SETUP"]
    if response == "missing_page":
        setup = setup.replace("code=0,sliceIndex=1,sliceCount=1", "code=0,sliceIndex=3,sliceCount=3")
    elif response == "rejected":
        setup = setup.replace("code=0,sliceIndex=1,sliceCount=1", "code=41,sliceIndex=1,sliceCount=1")
    elif response == "unobserved":
        setup = setup.replace("ClientProxy:RPCGetAllCombatInfoResponse(1,", "__bypassAll(ClientProxy,1,")
    if zero_events:
        setup = setup.replace("eventDataList={{eventId=1,intList={123},tab=0}}", "eventDataList={}")
    runner.lua.execute(setup)
    runner.lua.execute("""
__bypassAll=ClientProxy.RPCGetAllCombatInfoResponse
ClientProxy._allCombatSlices[123]={time=100,receivedCount=1,[1]='original partial'}
__pendingAll={at=102,battleID=123}
""")
    with patch.object(exporter.time, "monotonic", side_effect=clock.monotonic), \
         patch.object(exporter.time, "sleep", side_effect=clock.sleep):
        result = exporter.capture_report(runner, 123, tmp_path, wait_seconds=1, poll_seconds=.5)
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert clock.now == 102
    assert [row["method"] for row in runner.requests()] == ["RPCGetHeroBattleSnapRecords"]
    assert runner.restored()
    return result, payload


def test_late_complete_response_resolves_cache_wait_without_another_request(tmp_path):
    result, payload = capture_late_all(tmp_path)
    assert result["complete"] is True
    status = payload["componentStatus"]["all"]
    assert status["responseCoverage"]["complete"] is True
    assert status["resolvedAfterTimeout"] is True and "error" not in status
    history = payload["requestHistory"]
    assert len(history) == 1 and history[0]["component"] == "all"
    assert history[0]["error"] == "partial_cache_wait_timeout" and history[0]["resolved"] is True
    checkpoints = [json.loads(Path(path).read_text(encoding="utf-8"))
                   for path in result["cacheCheckpointPaths"]]
    assert any(value.get("componentFragments", {}).get("all", {}).get("1") == "original partial"
               for value in checkpoints)


@pytest.mark.parametrize("response", ["missing_page", "rejected", "unobserved"])
def test_late_client_cache_does_not_resolve_an_unproved_or_failed_response(tmp_path, response):
    result, payload = capture_late_all(tmp_path, response=response)
    assert result["complete"] is False
    assert payload["components"]["all"]["eventDataList"]
    assert payload["componentStatus"]["all"].get("error")
    assert "requestHistory" not in payload
    if response == "missing_page":
        assert payload["componentStatus"]["all"]["responseCoverage"]["missing"] == [2]
    elif response == "rejected":
        assert "server_rejected" in payload["componentStatus"]["all"]["error"]
    else:
        assert payload["componentStatus"]["all"]["error"] == "partial_cache_wait_timeout"


def test_late_zero_event_cache_cannot_resolve_local_wait(tmp_path):
    result, payload = capture_late_all(tmp_path, zero_events=True)
    assert result["complete"] is False and payload["eventCount"] == 0
    assert payload["componentStatus"]["all"]["error"] == "partial_cache_wait_timeout"
    assert "requestHistory" not in payload
