"""Lifecycle and timeout regression tests; live RPC compatibility is separate."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import os
import shutil
import subprocess
import time

import frida
import pytest

from capture.game_assets import run_lua_in_game as runtime
from capture.game_assets.run_lua_in_game import GameLuaRunner, build_agent_source
from capture.game_assets.runtime_errors import RuntimeBridgeUnavailable


def runner_with_api(api):
    runner = GameLuaRunner(123)
    runner.script = SimpleNamespace(exports_sync=api)
    return runner


def test_timeout_cancels_pending_execution():
    api = Mock()
    api.schedule.return_value = 8
    api.poll.return_value = {"done": False}
    api.cancel.return_value = {"id": 8, "done": True, "ok": False, "stage": "timeout", "cancelled": True}
    with pytest.raises(RuntimeBridgeUnavailable, match='PID 123') as error:
        runner_with_api(api).execute_on_update("return 1", timeout=.002, poll=.001)
    assert error.value.result["cancelled"]
    assert error.value.result["id"] == 8 and error.value.result["pid"] == 123
    api.cancel.assert_called_once_with(8)


def test_cancelled_session_fails_fast_without_rescheduling_and_info_remains_readable():
    api = Mock()
    api.schedule.return_value = 8
    api.poll.return_value = {"done": False}
    api.cancel.return_value = {"id": 8, "done": True, "ok": False, "stage": "timeout", "cancelled": True}
    api.info.return_value = {"queuedJobs": 0, "selectedThreadId": 7}
    runner = runner_with_api(api)
    with pytest.raises(RuntimeBridgeUnavailable):
        runner.execute_on_update('return 1', timeout=.002, poll=.001)
    for method in (runner.execute, runner.execute_on_update):
        with patch.object(runtime.time, 'sleep') as sleep:
            with pytest.raises(RuntimeBridgeUnavailable, match='must be reconnected'):
                method('return "cleanup"')
            sleep.assert_not_called()
    api.schedule.assert_called_once_with('return 1')
    api.cancel.assert_called_once_with(8)
    info = runner.info()
    assert info['sessionAvailable'] is False and info['selectedThreadId'] == 7
    assert info['cancelledJob']['id'] == 8 and info['cancelledJob']['pid'] == 123
    assert '0.00s' in info['sessionUnavailableReason']


def test_invalid_session_still_shuts_down_and_detaches():
    api = Mock()
    api.schedule.return_value = 8
    api.poll.return_value = {"done": False}
    api.cancel.return_value = {"id": 8, "done": True, "ok": False, "cancelled": True}
    runner = runner_with_api(api)
    runner._loaded, runner.session = True, Mock()
    session = runner.session
    with pytest.raises(RuntimeBridgeUnavailable):
        runner.execute_on_update('return 1', timeout=.002, poll=.001)
    runner.close()
    api.shutdown.assert_called_once()
    session.detach.assert_called_once()
    assert runner.script is None and runner.session is None


@pytest.mark.parametrize('failure_stage', ['schedule', 'poll', 'cancel'])
@pytest.mark.parametrize('exception_type', [frida.InvalidOperationError, frida.TransportError,
                                           frida.ProcessNotFoundError, frida.ServerNotRunningError])
def test_frida_channel_failure_invalidates_session(failure_stage, exception_type):
    api = Mock()
    api.schedule.return_value = 8
    api.poll.return_value = {"done": False}
    getattr(api, failure_stage).side_effect = exception_type('session is gone')
    runner = runner_with_api(api)
    with pytest.raises(RuntimeBridgeUnavailable, match='PID 123') as first:
        runner.execute_on_update('return 1', timeout=.002, poll=.001)
    assert first.value.result['error_type'] == exception_type.__name__
    first_count = api.schedule.call_count
    with pytest.raises(RuntimeBridgeUnavailable):
        runner.execute('return 2')
    assert api.schedule.call_count == first_count


def test_destroyed_script_rpc_is_typed_but_lua_rpc_error_is_not():
    from frida.core import RPCException
    api = Mock()
    api.schedule.side_effect = RPCException('script has been destroyed')
    runner = runner_with_api(api)
    with pytest.raises(RuntimeBridgeUnavailable, match='RPCException'):
        runner.execute('return 1')
    api.schedule.side_effect = RPCException('Lua business function failed')
    other = runner_with_api(api)
    with pytest.raises(RPCException, match='business function'):
        other.execute('return 1')
    assert other._unavailable_reason is None


def test_real_lua_error_keeps_session_usable():
    api = Mock()
    api.schedule.return_value = 8
    api.poll.return_value = {"id": 8, "done": True, "ok": False, "stage": "lua", "error": "no record"}
    runner = runner_with_api(api)
    assert runner.execute('error("no record")')['stage'] == 'lua'
    api.poll.return_value = {"id": 8, "done": True, "ok": True, "result": "healthy"}
    assert runner.execute('return "healthy"')['result'] == 'healthy'
    assert runner._unavailable_reason is None


def test_info_transport_failure_returns_local_first_failure_diagnostics():
    api = Mock()
    api.schedule.return_value = 8
    api.poll.return_value = {"done": False}
    api.cancel.return_value = {"id": 8, "done": True, "ok": False, "cancelled": True}
    api.info.side_effect = frida.InvalidOperationError('script has been destroyed')
    runner = runner_with_api(api)
    with pytest.raises(RuntimeBridgeUnavailable):
        runner.execute_on_update('return 1', timeout=.002, poll=.001)
    info = runner.info()
    assert info['sessionAvailable'] is False and info['pid'] == 123
    assert info['cancelledJob']['id'] == 8  # info failure must preserve the original timeout.
    assert 'destroyed' in info['infoError']


def test_fresh_attach_restores_execution_after_invalid_session_was_closed():
    api = Mock()
    api.schedule.return_value = 8
    api.poll.return_value = {"done": False}
    api.cancel.return_value = {"id": 8, "done": True, "ok": False, "cancelled": True}
    runner = runner_with_api(api)
    with pytest.raises(RuntimeBridgeUnavailable):
        runner.execute_on_update('return 1', timeout=.002, poll=.001)
    runner.close()
    session = Mock()
    fresh_api = session.create_script.return_value.exports_sync
    fresh_api.schedule.return_value = 1
    fresh_api.poll.return_value = {"id": 1, "done": True, "ok": True, "result": "reconnected"}
    with patch.object(runtime.frida, 'attach', return_value=session):
        with runner:
            assert runner.execute('return "reconnected"')['result'] == 'reconnected'
            assert runner._unavailable_reason is None


def processes(*entries):
    return [SimpleNamespace(info={'pid': pid, 'name': name,
        'memory_info': SimpleNamespace(rss=rss), 'create_time': 1.0}) for pid, name, rss in entries]


def test_windows_process_selection_prefers_visible_game_over_larger_background_process():
    entries = processes((10804, 'com.bilibili.nslg.exe', 100),
                        (18664, 'com.bilibili.nslg.exe', 1000),
                        (19999, 'PCGamePlatform.exe', 2000))
    windows = {10804: [{'hwnd': 71, 'title': '三国：谋定天下', 'class_name': 'UnityWndClass'}]}
    with patch.object(runtime.psutil, 'process_iter', return_value=entries), \
         patch.object(runtime.sys, 'platform', 'win32'), \
         patch.object(runtime, '_visible_game_windows', return_value=windows) as visible:
        selected = runtime.find_game_process()
    assert selected['pid'] == 10804 and selected['selection'] == 'visible_main_window'
    assert selected['windows'] == windows[10804]
    visible.assert_called_once_with({10804, 18664})


@pytest.mark.parametrize('windows', [{}, {10804: [{'title': 'A'}], 18664: [{'title': 'B'}]}])
def test_multiple_windows_processes_without_unique_main_window_require_explicit_pid(windows):
    entries = processes((10804, 'com.bilibili.nslg.exe', 100), (18664, 'com.bilibili.nslg.exe', 1000))
    with patch.object(runtime.psutil, 'process_iter', return_value=entries), \
         patch.object(runtime.sys, 'platform', 'win32'), \
         patch.object(runtime, '_visible_game_windows', return_value=windows):
        with pytest.raises(RuntimeError, match='--pid') as error:
            runtime.find_game_process()
    assert '10804' in str(error.value) and '18664' in str(error.value)


def test_windows_window_inspection_failure_does_not_guess_by_memory():
    entries = processes((10804, 'com.bilibili.nslg.exe', 100), (18664, 'com.bilibili.nslg.exe', 1000))
    with patch.object(runtime.psutil, 'process_iter', return_value=entries), \
         patch.object(runtime.sys, 'platform', 'win32'), \
         patch.object(runtime, '_visible_game_windows', side_effect=OSError('enumeration failed')):
        with pytest.raises(RuntimeError, match='--pid'):
            runtime.find_game_process()


def test_single_candidate_and_no_candidate_do_not_need_window_enumeration():
    for entries, expected in [(processes((10804, 'com.bilibili.nslg.exe', 100),
                                        (19999, 'PCGamePlatform.exe', 2000)), 10804), ([], None)]:
        with patch.object(runtime.psutil, 'process_iter', return_value=entries), \
             patch.object(runtime, '_visible_game_windows') as visible:
            selected = runtime.find_game_process()
        assert (selected['pid'] if selected else None) == expected
        visible.assert_not_called()


def test_non_windows_multiple_candidates_preserve_memory_fallback():
    entries = processes((10804, 'com.bilibili.nslg.exe', 100), (18664, 'com.bilibili.nslg.exe', 1000))
    with patch.object(runtime.psutil, 'process_iter', return_value=entries), \
         patch.object(runtime.sys, 'platform', 'linux'), \
         patch.object(runtime, '_visible_game_windows') as visible:
        assert runtime.find_game_process()['pid'] == 18664
    visible.assert_not_called()


def test_explicit_pid_skips_automatic_selection():
    with patch.object(runtime, 'find_game_process') as find:
        assert GameLuaRunner(10804).pid == 10804
    find.assert_not_called()


def test_read_only_window_enumeration_filters_hidden_owned_tool_and_untitled_windows(monkeypatch):
    import ctypes
    window_data = {
        0x100000001: (10804, True, 0, 0, '三国：谋定天下'),
        2: (18664, False, 0, 0, 'hidden game'),
        3: (18664, True, 99, 0, 'owned dialog'),
        4: (18664, True, 0, 0x80, 'tool window'),
        5: (18664, True, 0, 0, ''),
        6: (999, True, 0, 0, 'unrelated'),
    }
    api = SimpleNamespace()
    def enumerate_windows(callback, _):
        for hwnd in window_data:
            assert callback(hwnd, 0)
        return True
    def set_pid(hwnd, pointer):
        pointer._obj.value = window_data[hwnd][0]
        return 1
    def get_text(hwnd, buffer, _):
        buffer.value = window_data[hwnd][4]
        return len(buffer.value)
    def get_class(hwnd, buffer, _):
        buffer.value = 'UnityWndClass'
        return len(buffer.value)
    functions = {'EnumWindows': enumerate_windows,
        'IsWindowVisible': lambda hwnd: window_data[hwnd][1],
        'GetWindow': lambda hwnd, _: window_data[hwnd][2],
        'GetWindowLongW': lambda hwnd, _: window_data[hwnd][3],
        'GetWindowThreadProcessId': set_pid,
        'GetWindowTextLengthW': lambda hwnd: len(window_data[hwnd][4]),
        'GetWindowTextW': get_text, 'GetClassNameW': get_class}
    for name, method in functions.items():
        setattr(api, name, Mock(side_effect=method))
    monkeypatch.setattr(ctypes, 'WinDLL', Mock(return_value=api), raising=False)
    monkeypatch.setattr(ctypes, 'WINFUNCTYPE', lambda *_: lambda method: method, raising=False)
    result = runtime._visible_game_windows({10804, 18664})
    assert result == {10804: [{'hwnd': 0x100000001, 'title': '三国：谋定天下', 'class_name': 'UnityWndClass'}]}
    api.EnumWindows.assert_called_once()


@pytest.mark.parametrize("value", [float('nan'),float('inf'),-float('inf')])
@pytest.mark.parametrize("parameter", ["timeout","poll"])
def test_nonfinite_timers_are_rejected_without_scheduling(value,parameter):
    api=Mock()
    with pytest.raises(ValueError):
        runner_with_api(api).execute_on_update("return 1",**{parameter:value})
    api.schedule.assert_not_called()


def test_completion_racing_deadline_is_returned():
    api = Mock()
    api.schedule.return_value = 8
    api.poll.return_value = {"done": False}
    api.cancel.return_value = {"id": 8, "done": True, "ok": True, "result": "completed"}
    result = runner_with_api(api).execute_on_update("return 1", timeout=.002, poll=.001)
    assert result["ok"] and result["result"] == "completed"


def test_attach_load_failure_detaches():
    session = Mock()
    session.create_script.return_value.load.side_effect = RuntimeError("load failed")
    with patch("capture.game_assets.run_lua_in_game.frida.attach", return_value=session):
        with pytest.raises(RuntimeError, match="load failed"):
            GameLuaRunner(123).__enter__()
    session.detach.assert_called_once()


def wait_for_frida_cancellation(*args, **kwargs):
    """Exercise the real thread-local cancellation API without a game connection."""
    current = frida.Cancellable.get_current()
    while not current.is_cancelled():
        time.sleep(.001)
    current.raise_if_cancelled()
    raise AssertionError('Cancelled Frida operation did not raise')


@pytest.mark.parametrize('stage', ['attach', 'script creation', 'script load'])
def test_startup_deadline_cancels_blocked_native_stage_and_cleans_session(stage):
    session = Mock()
    attach = Mock(return_value=session)
    if stage == 'attach':
        attach.side_effect = wait_for_frida_cancellation
    elif stage == 'script creation':
        session.create_script.side_effect = wait_for_frida_cancellation
    else:
        session.create_script.return_value.load.side_effect = wait_for_frida_cancellation
    runner = GameLuaRunner(123, startup_timeout=.025, cleanup_timeout=.025)
    started = time.monotonic()
    with patch('capture.game_assets.run_lua_in_game.frida.attach', attach):
        with pytest.raises(TimeoutError, match=stage):
            runner.__enter__()
    assert time.monotonic() - started < 1
    assert runner.session is None and runner.script is None
    if stage == 'attach':
        session.detach.assert_not_called()
    else:
        session.detach.assert_called_once()
    session.create_script.return_value.exports_sync.shutdown.assert_not_called()


@pytest.mark.parametrize('stage', ['shutdown', 'detach'])
def test_cleanup_is_bounded_and_releases_local_handles(stage):
    runner = GameLuaRunner(123, cleanup_timeout=.025)
    runner.session, runner.script, runner._loaded = Mock(), Mock(), True
    session, script = runner.session, runner.script
    target = script.exports_sync.shutdown if stage == 'shutdown' else session.detach
    target.side_effect = wait_for_frida_cancellation
    started = time.monotonic()
    if stage == 'detach':
        with pytest.raises(TimeoutError, match='session detach'):
            runner.close()
    else:
        runner.close()
    assert time.monotonic() - started < 1
    session.detach.assert_called_once()
    assert runner.session is None and runner.script is None
    runner.close()  # Repeated cleanup must be harmless.


@pytest.mark.parametrize('parameter', ['startup_timeout', 'cleanup_timeout'])
@pytest.mark.parametrize('value', [0, -1, float('nan'), float('inf')])
def test_invalid_connection_deadlines_are_rejected_before_attach(parameter, value):
    with patch('capture.game_assets.run_lua_in_game.frida.attach') as attach:
        with pytest.raises(ValueError, match='finite and positive'):
            GameLuaRunner(123, **{parameter: value})
    attach.assert_not_called()


def test_unicode_source_and_direct_alias_use_queue():
    api = Mock()
    api.schedule.return_value = 1
    api.poll.return_value = {"done": True, "ok": True, "result": "战报😀"}
    result = runner_with_api(api).execute("return '战报😀'")
    assert result["result"] == "战报😀"
    api.schedule.assert_called_once_with("return '战报😀'")


def test_source_uses_no_build_specific_pointers():
    source = build_agent_source()
    assert "GameAssembly" not in source
    assert "utf8Length(wrapped)" in source
    assert "settop(L, before)" in source


def test_nul_is_rejected_before_scheduling():
    api = Mock()
    with pytest.raises(ValueError, match="NUL"):
        runner_with_api(api).execute("return '\0'")
    api.schedule.assert_not_called()


def test_poll_failure_cancels_job():
    api = Mock()
    api.schedule.return_value = 9
    api.poll.side_effect = RuntimeError("disconnected")
    with pytest.raises(RuntimeError, match="disconnected"):
        runner_with_api(api).execute("return 1")
    api.cancel.assert_called_once_with(9)


def test_hook_keeps_jobs_on_selected_state_and_thread_and_cancels_before_call():
    """Exercise the actual JS queue against a minimal Lua C API double."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable for the isolated hook test")
    harness = r'''
const vm = require('vm'), fs = require('fs'), assert = require('assert');
let hooks, tid = 1, executed = [], apiCalls = 0;
const mem = text => ({text, isNull:()=>false, readUtf8String:()=>text});
global.Memory = {
 allocUtf8String:mem,
 alloc:()=>({n:0,readU32(){return this.n;},readU64(){return {toNumber:()=>this.n};}})
};
global.Process = {pointerSize:8,getCurrentThreadId:()=>tid,
 getModuleByName:()=>({base:{toString:()=> 'base'},getExportByName:n=>n})};
global.Interceptor = {attach:(name,h)=>{hooks=h;return {detach(){}};}};
global.rpc = {};
const natives = {
 lua_gettop:L=>L.stack.length,
 lua_settop:(L,n)=>{L.stack.length=n;},
 lua_type:()=>4,
 lua_tolstring:(L,index,len)=>{const text=L.stack[L.stack.length+index];len.n=Buffer.byteLength(text);return mem(text);},
 luaL_loadbuffer:(L,chunk,length)=>{assert.equal(Buffer.byteLength(chunk.text),length);L.stack.push(chunk.text);return 0;},
 lua_pcall:L=>{
   const c={};hooks.onEnter.call(c,[L]);
   const code=L.stack.pop();
   if(code.includes("type(ClientProxy)")) L.stack.push(L.eligible?'client':'other');
   else {executed.push({state:L.name,tid,code});L.stack.push('战报😀');}
   hooks.onLeave.call(c);return 0;
 }
};
global.NativeFunction=function(name){return (...args)=>{apiCalls++;return natives[name](...args);};};
vm.runInThisContext(fs.readFileSync(0,'utf8'));
const state=(name,eligible)=>({name,eligible,stack:['original'],toString(){return this.name;}});
function tick(L,thread){tid=thread;const c={};hooks.onEnter.call(c,[L]);hooks.onLeave.call(c);assert.deepEqual(L.stack,['original']);}
const unrelated=state('other',false), first=state('client-one',true), second=state('client-two',true);
let id=rpc.exports.schedule("return '战报😀'");
tick(unrelated,1);assert.equal(executed.length,0);
tick(first,7);assert.equal(rpc.exports.poll(id).result,'战报😀');assert.equal(executed.length,1);
id=rpc.exports.schedule('return 2');
tick(second,7);tick(first,9);assert.equal(executed.length,1);
tick(first,7);assert.equal(executed.length,2);assert.equal(rpc.exports.poll(id).ok,true);
id=rpc.exports.schedule('return 3');assert.equal(rpc.exports.cancel(id).cancelled,true);
tick(first,7);assert.equal(executed.length,2);
const callsBefore=apiCalls;rpc.exports.info();assert.equal(apiCalls,callsBefore);
console.log(JSON.stringify({executed:executed.length,thread:rpc.exports.info().selectedThreadId}));
'''
    env = dict(os.environ)
    env.pop("NODE_COMPILE_CACHE", None)
    result = subprocess.run([node, "-e", harness], input=build_agent_source(), text=True,
                            encoding="utf-8", capture_output=True, env=env, check=True)
    assert json.loads(result.stdout) == {"executed": 2, "thread": 7}
