"""Native faults latch the real JS hook and Python session without a game."""
import json
import os
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from capture.game_assets.run_lua_in_game import GameLuaRunner, build_agent_source
from capture.game_assets.runtime_errors import RuntimeBridgeUnavailable


HARNESS = r'''
const vm=require('vm'),fs=require('fs'),assert=require('assert');
let hooks,armed=false,faulted=false,detached=false,topReads=0,mode='healthy',tid=7;
const calls=[];
const fault=process.argv[1];
function record(name) {
  calls.push(name);
  const target=fault==='probe-load'?'luaL_loadbuffer':fault==='gettop-diagnostic'?'lua_gettop':fault==='gettop-job'?'lua_gettop':fault;
  if(name==='lua_gettop' && armed) topReads++;
  if(armed && name===target && (fault!=='gettop-job' || topReads===2)) {
    faulted=true;throw new Error('access violation accessing 0x12340000');
  }
}
const mem=text=>({text,isNull:()=>false,readUtf8String:()=>{record('readUtf8String');return text;}});
global.Memory={allocUtf8String:mem,alloc:()=>({n:0,readU32(){return this.n;},readU64(){return {toNumber:()=>this.n};}})};
global.Process={pointerSize:8,getCurrentThreadId:()=>tid,
 getModuleByName:()=>({base:{toString:()=> 'base'},getExportByName:name=>name})};
global.Interceptor={attach:(name,value)=>{hooks=value;return {detach(){detached=true;}};}};
global.rpc={};
const implementations={
 lua_gettop:L=>L.stack.length,
 lua_settop:(L,n)=>{L.stack.length=n;},
 lua_type:()=>4,
 lua_tolstring:(L,index,len)=>{const text=L.stack[L.stack.length+index];len.n=Buffer.byteLength(text);return mem(text);},
 luaL_loadbuffer:(L,chunk)=>{L.stack.push(mode==='loadbuffer'?'syntax error':chunk.text);return mode==='loadbuffer'?3:0;},
 lua_pcall:L=>{
   const invocation={};hooks.onEnter.call(invocation,[L]);
   const code=L.stack.pop();
   if(code.includes('type(ClientProxy)')) L.stack.push('client');
   else L.stack.push(mode==='pcall'?'protected Lua failure':mode==='lua'?'__LUA_ERROR__business Lua failure':'healthy');
   hooks.onLeave.call(invocation);return mode==='pcall'?2:0;
 }
};
global.NativeFunction=function(name){return (...args)=>{record(name);return implementations[name](...args);};};
vm.runInThisContext(fs.readFileSync(0,'utf8'));
const L={stack:['original'],toString(){return '0x1234';}};
function tick(state=L){const invocation={};hooks.onEnter.call(invocation,[state]);hooks.onLeave.call(invocation);}
if(!['probe-load','gettop-diagnostic'].includes(fault)) {
  const warm=rpc.exports.schedule('return "warmup"');tick();assert.equal(rpc.exports.poll(warm).ok,true);
}
if(fault==='idle-unrelated') {
  const count=calls.length;tick();assert.equal(calls.length,count);
  const pending=rpc.exports.schedule('return "selected only"');
  tick({stack:['unrelated'],toString(){return '0xabcd';}});assert.equal(calls.length,count);
  tid=9;tick();assert.equal(calls.length,count);assert.equal(rpc.exports.poll(pending).done,false);
  tid=7;tick();assert.equal(rpc.exports.poll(pending).ok,true);
  console.log(JSON.stringify({idleNativeCalls:0,unrelatedNativeCalls:0,reusable:true}));
} else if(['loadbuffer','pcall','lua'].includes(fault)) {
  mode=fault;
  const id=rpc.exports.schedule('return "normal Lua failure"');tick();const result=rpc.exports.poll(id);
  assert.equal(result.stage,fault);assert.equal(result.ok,false);assert.deepEqual(L.stack,['original']);
  assert.equal(rpc.exports.info().runtimeUnavailable,false);
  mode='healthy';const next=rpc.exports.schedule('return "next healthy job"');tick();
  assert.equal(rpc.exports.poll(next).ok,true);assert.deepEqual(L.stack,['original']);
  console.log(JSON.stringify({stage:result.stage,stackRestored:true,reusable:true}));
} else {
  const first=rpc.exports.schedule('return "fault"'),queued=rpc.exports.schedule('return "queued"');
  armed=true;tick();
  assert.equal(faulted,true);
  const firstResult=rpc.exports.poll(first),queuedResult=rpc.exports.poll(queued),info=rpc.exports.info();
  assert.equal(firstResult.stage,'lua_hook');assert.equal(firstResult.runtime_unavailable,true);
  assert.equal(queuedResult.done,true);assert.equal(queuedResult.error,firstResult.error);
  assert.equal(info.runtimeUnavailable,true);assert.equal(info.queuedJobs,0);
  assert.equal(info.bridgeFailure.error,firstResult.error);
  const count=calls.length,future=rpc.exports.schedule('return "future"');tick();
  assert.equal(calls.length,count);assert.equal(rpc.exports.poll(future).error,firstResult.error);
  rpc.exports.shutdown();tick();assert.equal(detached,true);assert.equal(calls.length,count);
  const target=fault==='probe-load'?'luaL_loadbuffer':fault.startsWith('gettop-')?'lua_gettop':fault;
  assert.equal(calls[calls.length-1],target); // No stack restoration/API call after the fault.
  console.log(JSON.stringify({stage:firstResult.stage,operation:firstResult.nativeOperation,
    queuedFailed:true,futureFailed:true,noNativeAfterFault:true}));
}
'''


def run_hook(case):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable for the isolated hook test")
    env = dict(os.environ)
    env.pop("NODE_COMPILE_CACHE", None)
    completed = subprocess.run([node, "-e", HARNESS, case], input=build_agent_source(),
                               text=True, encoding="utf-8", capture_output=True, env=env, check=True)
    return json.loads(completed.stdout)


@pytest.mark.parametrize("operation", ["gettop-diagnostic", "gettop-job", "probe-load", "luaL_loadbuffer",
                                        "lua_pcall", "lua_type", "lua_tolstring", "readUtf8String", "lua_settop"])
def test_real_hook_native_fault_never_restores_bad_stack_or_runs_another_job(operation):
    result = run_hook(operation)
    assert result["stage"] == "lua_hook" and result["noNativeAfterFault"]
    assert result["queuedFailed"] and result["futureFailed"]


@pytest.mark.parametrize("stage", ["loadbuffer", "pcall", "lua"])
def test_real_hook_normal_lua_errors_restore_stack_and_keep_session(stage):
    assert run_hook(stage) == {"stage": stage, "stackRestored": True, "reusable": True}


def test_idle_queue_and_unselected_state_or_thread_never_call_lua_api():
    assert run_hook("idle-unrelated") == {"idleNativeCalls": 0, "unrelatedNativeCalls": 0, "reusable": True}


@pytest.mark.parametrize("error", ["Error: access violation accessing 0x1a500090b34", "Error: system error"])
@pytest.mark.parametrize("completion", ["poll", "cancel"])
def test_python_native_failure_poison_is_immediate_and_preserves_first_result(error, completion):
    api = Mock()
    api.schedule.return_value = 7
    result = {"id": 7, "done": True, "ok": False, "stage": "lua_hook", "error": error,
              "nativeOperation": "lua_pcall", "rawL": "0x1234", "threadId": 99}
    api.poll.return_value = result if completion == "poll" else {"done": False}
    api.cancel.return_value = result
    api.info.return_value = {"queuedJobs": 0, "lastError": error}
    runner = GameLuaRunner(123)
    runner.script = SimpleNamespace(exports_sync=api)
    with pytest.raises(RuntimeBridgeUnavailable, match="must be reconnected") as failure:
        runner.execute_on_update("return 'fault'", timeout=.002, poll=.001)
    assert failure.value.result["error"] == error
    assert failure.value.result["nativeOperation"] == "lua_pcall"
    assert failure.value.result["pid"] == 123
    with pytest.raises(RuntimeBridgeUnavailable):
        runner.execute("return 'must never reach native' ")
    api.schedule.assert_called_once()
    info = runner.info()
    assert info["sessionAvailable"] is False and info["unavailableResult"]["error"] == error
