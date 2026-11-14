"""Bounded pre-HTTP CDN DNS fallback; no external resolver or cloud requests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", ["success", "candidate-cap", "certificate", "already-sent", "read-failure",
    "api", "missing-class", "dns-empty", "dns-stall", "total-budget"])
def test_cdn_fallback_is_bounded_and_never_replays_an_already_sent_request(tmp_path, scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is optional")
    script = r'''
const assert=require('node:assert/strict'),https=require('node:https'),child=require('node:child_process');
const {EventEmitter}=require('node:events');
const scenario=process.argv[3],payloads=[],phases=[];
let dnsCalls=0,cancelled=0;
child.spawn=function(python,args,options){
 const worker=new EventEmitter();worker.stdout=new EventEmitter();worker.stderr=new EventEmitter();worker.stdin=new EventEmitter();
 worker.kill=()=>{worker.killed=true;return true;};
 worker.stdin.end=function(value,done){
  const source=JSON.parse(value);payloads.push(source);if(done)done();
  if(scenario==='total-budget'&&payloads.length===2)return;
  setImmediate(()=>{
   const phase=value=>worker.stderr.emit('data',Buffer.from(JSON.stringify({kind:'quark-https-phase',...value})+'\n'));
   if(scenario==='already-sent')phase({phase:'request-sent',bytes:7});
   const diagnostic={phase:'transport-error',error_code:'EPROTO',ssl_error_class:'SSLError',ssl_library:'SSL',
    ssl_reason:'WRONG_VERSION_NUMBER',ssl_errno:1,ssl_operation:'handshake'};
   if(scenario==='certificate'){diagnostic.error_code='TLS_VERIFICATION_FAILED';diagnostic.ssl_error_class='SSLCertVerificationError';diagnostic.ssl_reason='CERTIFICATE_VERIFY_FAILED';}
   if(scenario==='read-failure')diagnostic.ssl_operation='read';
   if(scenario==='missing-class')delete diagnostic.ssl_error_class;
   const success=payloads.length===2&&scenario==='success';
   if(!success)phase(diagnostic);
   const result=success?{status:200,headers:[],body_base64:Buffer.from('exact').toString('base64')}:{transport_error:diagnostic.error_code};
   worker.stdout.emit('data',Buffer.from(JSON.stringify(result)));worker.emit('close',0);
  });
 };
 return worker;
};
const bridge=require(process.argv[1]);
for(const address of ['127.0.0.1','10.0.0.1','100.64.0.1','169.254.1.1','192.168.1.1','192.0.2.1','198.18.1.1',
 '198.51.100.1','203.0.113.1','224.0.0.1','255.255.255.255','::1','8.8.8.8@secret','8.8.8.8/path','8.8.8.8:443'])assert(!bridge.isGlobalIPv4(address));
assert(bridge.isGlobalIPv4('8.8.8.8'));assert(bridge.isGlobalIPv4('1.1.1.1'));
bridge.installMemoryHttpsBridge({mode:'urllib',python:process.argv[2],resolverFactory(options){
 assert(options.timeout>0&&options.timeout<=1500);assert.equal(options.tries,1);
 return {setServers(servers){assert.deepEqual(servers,['223.5.5.5']);},cancel(){cancelled++;},
  resolve4(host,callback){dnsCalls++;assert.equal(host,'dl-sz.open-drive.quark.cn');
   if(scenario==='dns-stall')return;
   const addresses=scenario==='dns-empty'?['127.0.0.1','192.0.2.1','secret.invalid']:['127.0.0.1','8.8.8.8','8.8.8.8','1.1.1.1','9.9.9.9'];
   setImmediate(()=>callback(null,addresses));}};
}});
const start=Date.now();
const finished=new Promise((resolve,reject)=>{
 const request=https.request({hostname:scenario==='api'?'open-api-drive.quark.cn':'dl-sz.open-drive.quark.cn',port:443,
  path:'/opaque?token=private-fixture',method:'POST',headers:{'X-Signature':'same'}},response=>{
  response.resume();response.on('end',()=>resolve({status:response.statusCode}));
 });
 request.on('quark-memory-https-phase',phase=>phases.push(phase));
 request.on('error',error=>resolve({error:error.code}));
 request.setTimeout(['total-budget','dns-stall'].includes(scenario)?80:3000);
 request.end('exact-body');
});
finished.then(result=>{
 const prohibited=['certificate','already-sent','read-failure','api','missing-class'].includes(scenario);
 if(scenario==='success')assert.equal(result.status,200);else assert(result.error);
 if(prohibited){assert.equal(dnsCalls,0);assert.equal(payloads.length,1);}
 else {assert.equal(dnsCalls,1);assert(payloads.length<=3);}
 if(scenario==='candidate-cap')assert.equal(payloads.length,3);
 if(scenario==='dns-empty'||scenario==='dns-stall')assert.equal(payloads.length,1);
 if(scenario==='dns-stall'||scenario==='total-budget'){assert.equal(result.error,'ETIMEDOUT');assert(Date.now()-start<1000);}
 for(const value of payloads.slice(1)){
  assert(['8.8.8.8','1.1.1.1'].includes(value.connect_address));
  assert.equal(value.url,payloads[0].url);assert.equal(value.method,payloads[0].method);
  assert.deepEqual(value.headers,payloads[0].headers);assert.equal(value.body_base64,payloads[0].body_base64);
  assert(value.timeout_seconds<=payloads[0].timeout_seconds);
 }
 assert(phases.length<=48);const rendered=JSON.stringify(phases);
 for(const secret of ['private-fixture','8.8.8.8','1.1.1.1','X-Signature','exact-body'])assert(!rendered.includes(secret));
 console.log('BOUNDED_CDN_DNS_OK');
}).catch(error=>{console.error(error);process.exitCode=1;});
'''
    completed = subprocess.run([node, "-e", script, str(ROOT / "tools/quark_memory_https.cjs"), sys.executable, scenario],
        cwd=ROOT, env=dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path), NODE_DISABLE_COMPILE_CACHE="1"),
        capture_output=True, text=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "BOUNDED_CDN_DNS_OK" in completed.stdout


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "100.64.0.1", "192.0.2.1", "224.0.0.1", "::1",
    "8.8.8.8:443", "8.8.8.8/path", {"address": "8.8.8.8"}])
def test_python_connect_address_rejects_nonglobal_or_injected_values_before_network(monkeypatch, address):
    import runpy
    scope = runpy.run_path(str(ROOT / "tools/quark_memory_https.py"))
    monkeypatch.setattr(scope["urllib"].request, "build_opener", lambda *_a: pytest.fail("Rejected destination opened network"))
    assert scope["exchange"]({"url": "https://dl-sz.open-drive.quark.cn/a", "connect_address": address}) == {
        "transport_error": "CONNECT_ADDRESS_NOT_ALLOWED"}


def test_python_connect_address_cannot_override_the_api_endpoint(monkeypatch):
    import runpy
    scope = runpy.run_path(str(ROOT / "tools/quark_memory_https.py"))
    monkeypatch.setattr(scope["urllib"].request, "build_opener", lambda *_a: pytest.fail("API destination override opened network"))
    assert scope["exchange"]({"url": "https://open-api-drive.quark.cn/a", "connect_address": "8.8.8.8"}) == {
        "transport_error": "CONNECT_ADDRESS_NOT_ALLOWED"}
