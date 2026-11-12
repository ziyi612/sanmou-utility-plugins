"""Network-overlap tests with sequential game-thread Lua and a virtual clock."""
import json
import threading
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from lupa.lua51 import LuaRuntime

from capture.game_assets import export_battle_report_runtime as exporter


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class DelayedLuaRunner:
    def __init__(self, clock, latency=3, detail_cached=False, partial_snapshot=False):
        self.clock = clock
        self.owner_thread = threading.get_ident()
        self.lua_calls = 0
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().sim_latency = latency
        self.lua.globals().sim_partial_snapshot = partial_snapshot
        self.lua.execute("""
ClientConst={ReqBattleDiffType={Normal=1}}
G_SCENARIO_ID=14
ClientProxy={avatarId=42,GetServerId=function() return 1 end,
 all={},detail={},reportStaticsInfo={},allBattleRecordInfo={}}
__calls={}
__pending={}
local function battle(id)
 return {battleID=id,eventDataList={{eventId=1,intList={id},tab=0}},marker='battle-'..id}
end
ClientProxy.GetCombatInfo=function(self,id) return {battleID=id,basicMarker='basic-'..id} end
ClientProxy.GetAllCombatInfo=function(self,id) return self.all[id] end
ClientProxy.GetDetailCombatInfo=function(self,id) return self.detail[id] end
local function request(id,component)
 __calls[#__calls+1]={id=id,component=component,at=sim_now}
 __pending[id]=__pending[id] or {}
 __pending[id][component]=sim_now+sim_latency
end
ClientProxy.RPCGetAllCombatInfo=function(self,id) request(id,'all') end
ClientProxy.RPCGetDetailCombatInfo=function(self,id) request(id,'detail') end
ClientProxy.RPCGetStaticsCombatInfo=function(self,id) request(id,'statics') end
ClientProxy.RPCGetHeroBattleSnapRecords=function(self,id,tp) request(id,'snapshots') end
function __deliver()
 for id,pending in pairs(__pending) do
  for component,readyAt in pairs(pending) do
   if sim_now>=readyAt then
    if component=='all' then
     ClientProxy.all[id]=battle(id)
     ClientProxy.detail[id]=ClientProxy.all[id]
    elseif component=='detail' then ClientProxy.detail[id]=battle(id)
    elseif component=='statics' then ClientProxy.reportStaticsInfo[id]={damage=id*10}
    elseif component=='snapshots' then
     ClientProxy.allBattleRecordInfo[id]={isComplete=not sim_partial_snapshot,
      receivedCount=1,pages={{marker=id,page=1}}}
     if not sim_partial_snapshot then ClientProxy.allBattleRecordInfo[id].battleSnapData={maxIDX=id} end
    end
    pending[component]=nil
   end
  end
 end
end
function __cache_detail(id) ClientProxy.detail[id]=battle(id) end
""")
        if detail_cached:
            for battle_id in (123, 124):
                self.lua.globals()["__cache_detail"](battle_id)

    def execute_on_update(self, code, timeout=10):
        assert threading.get_ident() == self.owner_thread, "Lua calls must stay on one thread"
        self.lua_calls += 1
        self.lua.globals().sim_now = self.clock.now
        self.lua.globals()["__deliver"]()
        try:
            return {"ok": True, "result": self.lua.execute(code)}
        except Exception as exc:
            return {"ok": False, "stage": "lua", "error": str(exc)}

    execute = execute_on_update

    def requests(self):
        return [(int(row["id"]), row["component"]) for _, row in self.lua.globals()["__calls"].items()]

    def request_times(self):
        return [(int(row["id"]), row["component"], float(row["at"])) for _, row in self.lua.globals()["__calls"].items()]


def virtual_time(clock):
    stack = ExitStack()
    stack.enter_context(patch.object(exporter.time, "monotonic", side_effect=clock.monotonic))
    stack.enter_context(patch.object(exporter.time, "sleep", side_effect=clock.sleep))
    return stack


def read_capture(record):
    return json.loads(Path(record["path"]).read_text(encoding="utf-8"))


def test_two_ids_overlap_network_without_duplicate_or_cross_battle_data(tmp_path):
    clock = Clock()
    runner = DelayedLuaRunner(clock)
    with virtual_time(clock):
        prefetched = exporter.prefetch_reports(runner, [123, 124, 123], min_request_interval=0, prefetch_snapshots=True)
        assert clock.now == 100
        assert set(prefetched) == {"123", "124"}
        assert len(runner.requests()) == 6
        assert all(meta["deadline"] == 120 for group in prefetched.values() for meta in group.values())
        records = [exporter.capture_report(runner, bid, tmp_path, prefetched_requests=prefetched[str(bid)]) for bid in (123, 124)]
    assert all(record["complete"] for record in records)
    assert len(runner.requests()) == 6
    assert not any(component == "detail" for _, component in runner.requests())
    assert clock.now == 103
    saved = [read_capture(record) for record in records]
    for battle_id, payload in zip((123, 124), saved):
        assert payload["data"]["battleID"] == battle_id
        assert payload["components"]["all"]["marker"] == f"battle-{battle_id}"
        assert payload["components"]["all"]["eventDataList"][0]["intList"] == [battle_id]
        assert payload["components"]["statics"]["damage"] == battle_id * 10
        assert payload["components"]["snapshots"]["battleSnapData"]["maxIDX"] == battle_id

    # Same simulated delays and outputs, with the unchanged serial API.
    serial_clock = Clock()
    serial = DelayedLuaRunner(serial_clock)
    with virtual_time(serial_clock):
        serial_records = [exporter.capture_report(serial, bid, tmp_path / "serial") for bid in (123, 124)]
    assert serial_clock.now == 118
    assert [read_capture(r)["components"] for r in serial_records] == [p["components"] for p in saved]


def test_expired_prefetch_deadline_does_not_start_another_twenty_second_wait(tmp_path):
    clock = Clock()
    runner = DelayedLuaRunner(clock, latency=100, detail_cached=True)
    with virtual_time(clock):
        prefetched = exporter.prefetch_reports(runner, [123], min_request_interval=0)
        clock.now = 121
        record = exporter.capture_report(runner, 123, tmp_path, prefetched_requests=prefetched["123"])
    assert record["complete"] is False
    assert clock.sleeps == []
    assert clock.now == 121
    assert len(runner.requests()) == 3
    saved = read_capture(record)
    assert saved["componentStatus"]["all"]["error"] == "timeout"
    assert saved["prefetchRequests"]["all"]["deadline"] == 120


def test_only_remaining_original_budget_is_waited(tmp_path):
    clock = Clock()
    runner = DelayedLuaRunner(clock, latency=100, detail_cached=True)
    with virtual_time(clock):
        prefetched = exporter.prefetch_reports(runner, [123], min_request_interval=0)
        clock.now = 119
        record = exporter.capture_report(runner, 123, tmp_path, prefetched_requests=prefetched["123"])
    assert record["complete"] is False
    assert sum(clock.sleeps) == pytest.approx(1)
    assert clock.now == 120
    assert len(runner.requests()) == 3


def test_missing_snapshot_pages_still_partial_and_received_pages_are_saved(tmp_path):
    clock = Clock()
    runner = DelayedLuaRunner(clock, partial_snapshot=True)
    with virtual_time(clock):
        prefetched = exporter.prefetch_reports(runner, [123], min_request_interval=0)
        record = exporter.capture_report(runner, 123, tmp_path, prefetched_requests=prefetched["123"])
    saved = read_capture(record)
    assert record["complete"] is False
    assert clock.now == 120
    assert len(runner.requests()) == 3
    assert saved["components"]["snapshots"]["isComplete"] is False
    assert saved["components"]["snapshots"]["pages"][0]["marker"] == 123
    assert saved["componentStatus"]["snapshots"]["error"] == "timeout"


def test_export_only_never_requests_or_waits_and_can_preserve_inflight_cache(tmp_path):
    clock = Clock()
    runner = DelayedLuaRunner(clock)
    with virtual_time(clock):
        prefetched = exporter.prefetch_reports(runner, [123, 124], min_request_interval=0)
        with patch.object(exporter, "wait_for", side_effect=AssertionError("must not wait")):
            partial = exporter.capture_report(runner, 123, tmp_path, prefetched_requests=prefetched["123"], request_missing=False)
            assert partial["complete"] is False
            assert len(runner.requests()) == 6
            clock.now = 103
            complete = exporter.capture_report(runner, 124, tmp_path, prefetched_requests=prefetched["124"], request_missing=False)
    assert complete["complete"] is True
    assert len(runner.requests()) == 6
    assert clock.sleeps == []
    assert read_capture(partial)["captureMode"] == "export_only"
    assert read_capture(complete)["components"]["all"]["battleID"] == 124


def test_prefetch_request_error_is_retained_without_resending(tmp_path):
    clock = Clock()
    runner = DelayedLuaRunner(clock)
    runner.lua.execute("""ClientProxy.RPCGetStaticsCombatInfo=function(self,id)
 __calls[#__calls+1]={id=id,component='statics'}; error('server rejected statics') end""")
    with virtual_time(clock):
        prefetched = exporter.prefetch_reports(runner, [123], min_request_interval=0)
        assert "server rejected statics" in prefetched["123"]["statics"]["error"]
        record = exporter.capture_report(runner, 123, tmp_path, prefetched_requests=prefetched["123"])
    saved = read_capture(record)
    assert record["complete"] is False
    assert len(runner.requests()) == 3
    assert "server rejected statics" in saved["componentStatus"]["statics"]["error"]
    assert saved["prefetchRequests"]["statics"]["error"] == saved["componentStatus"]["statics"]["error"]


def test_wrong_battle_or_invalid_deadline_is_rejected_before_lua(tmp_path):
    clock = Clock()
    runner = DelayedLuaRunner(clock)
    with virtual_time(clock):
        prefetched = exporter.prefetch_reports(runner, [123], min_request_interval=0)
        before = runner.lua_calls
        with pytest.raises(ValueError, match="another battle"):
            exporter.capture_report(runner, 124, tmp_path, prefetched_requests=prefetched["123"])
        invalid = {k: dict(v) for k, v in prefetched["123"].items()}
        invalid["all"]["deadline"] = float("nan")
        with pytest.raises(ValueError, match="deadline"):
            exporter.capture_report(runner, 123, tmp_path, prefetched_requests=invalid)
        invalid = {k: dict(v) for k, v in prefetched["123"].items()}
        invalid["snapshots"]["complete"] = True
        with pytest.raises(ValueError, match="override"):
            exporter.capture_report(runner, 123, tmp_path, prefetched_requests=invalid)
    assert runner.lua_calls == before
    assert not list(tmp_path.glob("battle_*"))


def test_identity_guard_rejects_changed_account_on_every_lua_call(tmp_path):
    clock = Clock()
    runner = DelayedLuaRunner(clock)
    identity = {"avatarId": "42", "serverId": "1", "scenarioId": "14"}
    with virtual_time(clock):
        prefetched = exporter.prefetch_reports(runner, [123], expected_identity=identity, min_request_interval=0)
        runner.lua.execute("ClientProxy.avatarId=99")
        with pytest.raises(RuntimeError, match="changed"):
            exporter.capture_report(runner, 123, tmp_path, prefetched_requests=prefetched["123"], expected_identity=identity)
    assert len(runner.requests()) == 3
    assert not list(tmp_path.glob("battle_*"))


def test_default_spacing_is_per_method_and_does_not_reduce_each_network_budget():
    clock = Clock()
    runner = DelayedLuaRunner(clock, latency=100)
    with virtual_time(clock):
        prefetched = exporter.prefetch_reports(runner, [123, 124], wait_seconds=20)
    times = runner.request_times()
    assert times[:3] == [(123, "all", 100), (123, "statics", 100), (123, "snapshots", 100)]
    assert times[3:] == [(124, "all", 101), (124, "statics", 101), (124, "snapshots", 101)]
    assert clock.sleeps == [1]
    assert all(meta["deadline"] == 120 for meta in prefetched["123"].values())
    assert all(meta["deadline"] == 121 for meta in prefetched["124"].values())


def test_same_runner_preserves_method_spacing_across_batches_with_identity_wrapper():
    clock = Clock()
    runner = DelayedLuaRunner(clock, latency=100)
    identity = {"avatarId": "42", "serverId": "1", "scenarioId": "14"}
    with virtual_time(clock):
        exporter.prefetch_reports(runner, [123], expected_identity=identity)
        exporter.prefetch_reports(runner, [124], expected_identity=identity)
    times = runner.request_times()
    for component in ("all", "statics", "snapshots"):
        requests = [at for _, method, at in times if method == component]
        assert requests == [100, 101]
    assert clock.sleeps == [1]


def test_new_runner_has_an_independent_dispatch_ledger():
    clock = Clock()
    first = DelayedLuaRunner(clock, latency=100)
    second = DelayedLuaRunner(clock, latency=100)
    with virtual_time(clock):
        exporter.prefetch_reports(first, [123])
        exporter.prefetch_reports(second, [124])
    assert clock.sleeps == []
    assert [at for _, _, at in first.request_times()] == [100, 100, 100]
    assert [at for _, _, at in second.request_times()] == [100, 100, 100]


@pytest.mark.parametrize("failure", ["exception", "error_reply"])
def test_failed_dispatch_reserves_method_and_global_gaps_from_completion(failure):
    clock = Clock()
    owner = SimpleNamespace()
    attempts = []

    def send(*args, **kwargs):
        attempts.append(clock.now)
        clock.now += 0.25  # Time in the client/IPC before success or failure.
        if len(attempts) == 1:
            if failure == "exception":
                raise RuntimeError("IPC result lost after RPC")
            return "client failure after RPC"
        return "requested component"

    with virtual_time(clock), patch.object(exporter, "run_checked", side_effect=send):
        with pytest.raises(RuntimeError):
            exporter._dispatch_component(object(), 123, "all", rate_owner=owner,
                                         min_request_interval=2, min_dispatch_interval=0.5)
        assert owner._battle_prefetch_dispatch_times == {"all": 100.25, "__all_methods__": 100.25}
        second = exporter._dispatch_component(object(), 124, "statics", rate_owner=owner,
                                              min_request_interval=2, min_dispatch_interval=0.5)
        third = exporter._dispatch_component(object(), 125, "all", rate_owner=owner,
                                             min_request_interval=2, min_dispatch_interval=0.5)
    assert attempts == [100, 100.75, 102.25]
    assert second == 101 and third == 102.5
    assert clock.sleeps == [0.5, 1.25]
    assert owner._battle_prefetch_dispatch_times["all"] == 102.5
    assert owner._battle_prefetch_dispatch_times["__all_methods__"] == 102.5


@pytest.mark.parametrize("battle_id,component", [(0, "all"), (123, "unknown_component")])
def test_dispatch_validation_does_not_wait_or_consume_a_slot(battle_id, component):
    clock = Clock()
    ledger = {"all": 99.5, "__all_methods__": 99.5}
    owner = SimpleNamespace(_battle_prefetch_dispatch_times=ledger.copy())
    with virtual_time(clock), patch.object(exporter, "run_checked") as send:
        with pytest.raises((ValueError, KeyError)):
            exporter._dispatch_component(object(), battle_id, component, rate_owner=owner,
                                         min_request_interval=2, min_dispatch_interval=1)
    send.assert_not_called()
    assert owner._battle_prefetch_dispatch_times == ledger
    assert clock.sleeps == [] and clock.now == 100


def test_cached_skips_do_not_move_the_last_real_dispatch_time():
    clock = Clock()
    runner = DelayedLuaRunner(clock, latency=100)
    with virtual_time(clock):
        exporter.prefetch_reports(runner, [123])
        clock.now = 100.4
        runner.lua.execute("__pending[124]={all=0,statics=0,snapshots=0}")
        cached = exporter.prefetch_reports(runner, [124])
        assert all(not meta["requested"] and meta["cached"] for meta in cached["124"].values())
        exporter.prefetch_reports(runner, [125])
    assert len(runner.requests()) == 6
    assert all(at == 101 for bid, _, at in runner.request_times() if bid == 125)
    assert sum(clock.sleeps) == pytest.approx(0.6)


def test_global_spacing_limits_different_methods_without_spending_network_budget():
    clock = Clock()
    runner = DelayedLuaRunner(clock, latency=100)
    with virtual_time(clock):
        first = exporter.prefetch_reports(runner, [123], min_request_interval=1, min_dispatch_interval=0.5)
        second = exporter.prefetch_reports(runner, [124], min_request_interval=1, min_dispatch_interval=0.5)
    times = runner.request_times()
    assert [at for _, _, at in times] == [100, 100.5, 101, 101.5, 102, 102.5]
    for index, (battle_id, method, at) in enumerate(times):
        meta = (first if battle_id == 123 else second)[str(battle_id)][method]
        assert meta["deadline"] == at + 20
        if index:
            assert at - times[index - 1][2] >= 0.5
    for method in ("all", "statics", "snapshots"):
        per_method = [at for _, component, at in times if component == method]
        assert per_method[1] - per_method[0] >= 1


def test_snapshot_prefetch_can_be_deferred_and_each_capture_still_gets_all_content(tmp_path):
    clock = Clock()
    runner = DelayedLuaRunner(clock)
    with virtual_time(clock):
        prefetched = exporter.prefetch_reports(runner, [123, 124], min_request_interval=1,
                                              min_dispatch_interval=1, prefetch_snapshots=False)
        assert runner.requests() == [(123, "all"), (123, "statics"), (124, "all"), (124, "statics")]
        assert all(group["snapshots"]["requested"] is False and group["snapshots"]["deferred"] is True
                   for group in prefetched.values())
        assert all("deadline" not in group["snapshots"] for group in prefetched.values())
        first = exporter.capture_report(runner, 123, tmp_path, prefetched_requests=prefetched["123"])
        first_finished_at = clock.now
        second = exporter.capture_report(runner, 124, tmp_path, prefetched_requests=prefetched["124"])
    assert first["complete"] is True
    assert second["complete"] is True
    snapshots = [(bid, at) for bid, method, at in runner.request_times() if method == "snapshots"]
    assert [bid for bid, _ in snapshots] == [123, 124]
    assert snapshots[1][1] >= first_finished_at
    assert len(runner.requests()) == 6
    assert read_capture(first)["components"]["snapshots"]["battleSnapData"]["maxIDX"] == 123
    assert read_capture(second)["components"]["snapshots"]["battleSnapData"]["maxIDX"] == 124
    assert all(entry["complete"] for entry in read_capture(first)["componentStatus"].values())


def test_deferred_snapshot_uses_shared_global_gap_and_full_twenty_second_budget(tmp_path):
    clock = Clock()
    runner = DelayedLuaRunner(clock, latency=100)
    # The target already has all/detail/statics; only its snapshot is missing.
    runner.lua.execute("__pending[123]={all=0,statics=0}")
    with virtual_time(clock):
        target = exporter.prefetch_reports(runner, [123], min_request_interval=1,
                                          min_dispatch_interval=5, prefetch_snapshots=False)
        assert runner.requests() == []
        exporter.prefetch_reports(runner, [124], min_request_interval=1,
                                  min_dispatch_interval=5, prefetch_snapshots=False)
        assert clock.now == 105
        record = exporter.capture_report(runner, 123, tmp_path, prefetched_requests=target["123"])
    assert record["complete"] is False
    sent = next(at for bid, method, at in runner.request_times() if bid == 123 and method == "snapshots")
    assert sent == 110
    assert clock.now - sent == 20
    assert clock.now == 130
    assert read_capture(record)["componentStatus"]["snapshots"]["error"] == "timeout"


def test_export_only_with_deferred_snapshot_does_not_start_it(tmp_path):
    clock = Clock()
    runner = DelayedLuaRunner(clock)
    with virtual_time(clock):
        prefetched = exporter.prefetch_reports(runner, [123], prefetch_snapshots=False)
        before = list(runner.requests())
        before_time = clock.now
        record = exporter.capture_report(runner, 123, tmp_path, prefetched_requests=prefetched["123"], request_missing=False)
    assert record["complete"] is False
    assert runner.requests() == before
    assert clock.now == before_time
    assert all(component != "snapshots" for _, component in runner.requests())
    assert read_capture(record)["prefetchRequests"]["snapshots"]["deferred"] is True


def test_prefetch_rate_metadata_must_be_valid_and_consistent_before_any_capture_lua(tmp_path):
    clock = Clock()
    runner = DelayedLuaRunner(clock)
    with virtual_time(clock):
        prefetched = exporter.prefetch_reports(runner, [123], min_request_interval=1,
                                              min_dispatch_interval=1, prefetch_snapshots=False)
        before = runner.lua_calls
        invalid = {k: dict(v) for k, v in prefetched["123"].items()}
        invalid["snapshots"]["min_dispatch_interval"] = 0.5
        with pytest.raises(ValueError, match="conflicting"):
            exporter.capture_report(runner, 123, tmp_path, prefetched_requests=invalid)
        invalid["snapshots"]["min_dispatch_interval"] = float("inf")
        with pytest.raises(ValueError, match="min_dispatch_interval"):
            exporter.capture_report(runner, 123, tmp_path, prefetched_requests=invalid)
        assert runner.lua_calls == before
    with pytest.raises(ValueError, match="prefetch_snapshots"):
        exporter.prefetch_reports(runner, [123], prefetch_snapshots="false")


@pytest.mark.parametrize("interval", [float("nan"), float("inf"), -1])
def test_invalid_spacing_is_rejected_before_any_lua(interval):
    clock = Clock()
    runner = DelayedLuaRunner(clock)
    with pytest.raises(ValueError, match="min_request_interval"):
        exporter.prefetch_reports(runner, [123], min_request_interval=interval)
    with pytest.raises(ValueError, match="min_dispatch_interval"):
        exporter.prefetch_reports(runner, [123], min_dispatch_interval=interval)
    assert runner.lua_calls == 0
