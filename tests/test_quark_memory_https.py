"""Offline-only contracts for the optional private-pipe HTTPS transport."""
from __future__ import annotations

import ast
import base64
from email.message import Message
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import ssl
import subprocess
import sys
from types import SimpleNamespace
import urllib.error

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON_BRIDGE = ROOT / "tools/quark_memory_https.py"
NODE_BRIDGE = ROOT / "tools/quark_memory_https.cjs"


def namespace():
    return runpy.run_path(str(PYTHON_BRIDGE))


@pytest.mark.parametrize("url", ["http://open-api-drive.quark.cn/a", "https://example.invalid/a",
    "https://open-api-drive.quark.cn.attacker.invalid/a", "https://open-api-drive.quark.cn:80/a",
    "https://open-api-drive.quark.cn:8443/a", "https://user:password@open-api-drive.quark.cn/a",
    "https://open-drive.quark.cn/a"])
def test_python_transport_rejects_nonallowlisted_targets_without_opening_network(monkeypatch, url):
    scope = namespace()
    monkeypatch.setattr(scope["urllib"].request, "build_opener", lambda *_a: pytest.fail("Rejected target opened the network"))
    assert scope["exchange"]({"url": url}) == {"transport_error": "ENDPOINT_NOT_ALLOWED"}


class Response:
    code = 206
    reason = "Partial Content"
    def __init__(self, payload):
        self.payload = payload
        self.headers = Message()
        self.headers.add_header("Content-Length", str(len(payload)))
        self.headers.add_header("X-Original-Response", "kept")
        self.headers.add_header("Set-Cookie", "synthetic=first")
        self.headers.add_header("Set-Cookie", "synthetic=second")
        self.closed = False
    def read(self, maximum): return self.payload[:maximum]
    def close(self): self.closed = True


def test_python_preserves_payload_headers_url_and_tls_checks_without_redirects(monkeypatch):
    scope = namespace();calls = []
    body = b"\x00\xff" + "原字节".encode()
    returned = Response(b"\x00\xfeoriginal response")
    headers = {"x-Fixture-Signature": "unchanged", "Range": "bytes=0-123", "Connection": "keep-alive"}
    target = "https://dl-sz.open-drive.quark.cn/original%2fpath?literal=%2B&token=fixture"
    def open_request(request, timeout):
        calls.append((request, timeout))
        return returned
    def opener(*handlers):
        secure = next(item for item in handlers if isinstance(item, scope["VerifiedHTTPS"]))
        assert secure.context.check_hostname is True and secure.context.verify_mode == ssl.CERT_REQUIRED
        assert any(isinstance(item, scope["NoRedirect"]) for item in handlers)
        assert any(isinstance(item, scope["urllib"].request.ProxyHandler) and item.proxies == {} for item in handlers)
        return SimpleNamespace(open=open_request)
    monkeypatch.setattr(scope["urllib"].request, "build_opener", opener)
    result = scope["exchange"]({"url": target, "method": "POST", "headers": headers,
        "body_base64": base64.b64encode(body).decode(), "has_body": True, "timeout_seconds": 12})
    request, timeout = calls[0]
    assert request.full_url == target and request.data == body and request.get_method() == "POST"
    assert request._quark_headers == list(headers.items()) and timeout == 12
    assert result["status"] == 206 and base64.b64decode(result["body_base64"]) == returned.payload
    assert result["headers"] == list(returned.headers.raw_items()) and returned.closed
    assert scope["NoRedirect"]().redirect_request(None, None, 302, "redirect", {}, "http://attacker.invalid") is None


def test_python_https_sender_keeps_explicit_header_casing_and_connection(monkeypatch):
    scope = namespace();operations = []
    response = Response(b"payload")
    class Connection:
        debuglevel = 0
        def __init__(self, host, timeout, context):
            assert host == "open-api-drive.quark.cn" and context.check_hostname
        def putrequest(self, *args, **kwargs): operations.append(("request", args, kwargs))
        def putheader(self, *args): operations.append(("header", args))
        def endheaders(self, body, **kwargs): operations.append(("body", body, kwargs))
        def getresponse(self): return response
        def close(self): operations.append(("close",))
    monkeypatch.setattr(scope["http"].client, "HTTPSConnection", Connection)
    context = ssl.create_default_context()
    request = scope["urllib"].request.Request("https://open-api-drive.quark.cn/path?literal=%2f", data=b"exact", method="POST")
    request._quark_headers = [("X-Signature-Original", "kept"), ("Connection", "keep-alive")]
    request.timeout = 8
    assert scope["VerifiedHTTPS"](context).https_open(request) is response
    assert ("header", ("X-Signature-Original", "kept")) in operations
    assert ("header", ("Connection", "keep-alive")) in operations
    assert ("body", b"exact", {"encode_chunked": False}) in operations
    assert operations[0] == ("request", ("POST", "/path?literal=%2f"), {"skip_host": False, "skip_accept_encoding": True})


def test_python_returns_http_failures_unchanged_and_limits_response_bytes(monkeypatch):
    scope = namespace()
    response = Response(b"failure-body")
    response.code = 403
    def rejected(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://open-api-drive.quark.cn/a", 403, "Forbidden", response.headers, response)
    monkeypatch.setattr(scope["urllib"].request, "build_opener", lambda *_a: SimpleNamespace(open=rejected))
    result = scope["exchange"]({"url": "https://open-api-drive.quark.cn/a"})
    assert result["status"] == 403 and base64.b64decode(result["body_base64"]) == b"failure-body"
    large = Response(b"x" * 200)
    monkeypatch.setitem(scope["exchange"].__globals__, "MAX_BYTES", 128)
    monkeypatch.setattr(scope["urllib"].request, "build_opener", lambda *_a: SimpleNamespace(open=lambda *_a, **_k: large))
    assert scope["exchange"]({"url": "https://open-api-drive.quark.cn/a"}) == {"transport_error": "MEMORY_LIMIT"}
    assert large.closed


def test_python_errors_never_include_urls_headers_or_exception_text(monkeypatch):
    scope = namespace()
    secret = "fixture-private-credential"
    def broken(*_a, **_k): raise ssl.SSLCertVerificationError("sensitive " + secret)
    monkeypatch.setattr(scope["urllib"].request, "build_opener", lambda *_a: SimpleNamespace(open=broken))
    result = scope["exchange"]({"url": "https://open-api-drive.quark.cn/a?token=" + secret})
    assert result == {"transport_error": "TLS_VERIFICATION_FAILED"}
    assert secret not in json.dumps(result)


@pytest.mark.parametrize("function,operation", [("do_handshake", "handshake"), ("read", "read"), ("write", "write")])
def test_tls_error_metadata_is_structured_and_classification_is_unchanged(monkeypatch, capsys, function, operation):
    scope = namespace()
    secret = "fixture-private-credential://sensitive/path"
    error = ssl.SSLError(1, secret)
    error.library = "SSL"
    error.reason = "WRONG_VERSION_NUMBER"
    error.verify_code = 18
    frame_scope = {"__name__": "ssl", "error": error}
    exec(compile(f"def {function}():\n raise error\n", secret, "exec"), frame_scope)
    def broken(*_a, **_k):
        try:
            frame_scope[function]()
        except ssl.SSLError as error:
            raise urllib.error.URLError(error)
    monkeypatch.setattr(scope["urllib"].request, "build_opener", lambda *_a: SimpleNamespace(open=broken))
    assert scope["exchange"]({"url": "https://open-api-drive.quark.cn/a?token=" + secret}) == {"transport_error": "EPROTO"}
    output = capsys.readouterr().err
    metadata = next(json.loads(line) for line in output.splitlines() if json.loads(line)["phase"] == "transport-error")
    assert metadata == {"kind": "quark-https-phase", "phase": "transport-error", "error_code": "EPROTO",
        "ssl_error_class": "SSLError", "ssl_library": "SSL", "ssl_reason": "WRONG_VERSION_NUMBER",
        "ssl_errno": 1, "verify_code": 18, "ssl_operation": operation}
    assert secret not in output


def test_tls_metadata_rejects_free_text_nonstandard_classes_and_invalid_integer_values(capsys):
    scope = namespace()
    secret = "fixture-private-credential://sensitive/path"
    scope["phase"]("transport-error", error_code="EPROTO", ssl_error_class=secret, ssl_library=secret,
        ssl_reason=secret, ssl_errno=True, verify_code=2**40, ssl_operation=secret, path=secret, body=secret,
        traceback=secret, verify_message=secret)
    assert json.loads(capsys.readouterr().err) == {
        "kind": "quark-https-phase", "phase": "transport-error", "error_code": "EPROTO"}
    scope["phase"]("transport-error", ssl_error_class=[], ssl_operation=[])
    assert json.loads(capsys.readouterr().err) == {"kind": "quark-https-phase", "phase": "transport-error"}


def test_real_in_memory_tls_handshake_reports_only_ssl_identifiers(capsys):
    scope = namespace()
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = ssl.create_default_context().wrap_bio(incoming, outgoing, server_side=False, server_hostname="synthetic.invalid")
    incoming.write(b"HTTP/1.1 502 Synthetic\r\nContent-Length: 0\r\n\r\n")
    with pytest.raises(ssl.SSLError) as failure:
        client.do_handshake()
    assert scope["category"](failure.value) == "EPROTO"
    scope["phase"]("transport-error", error_code="EPROTO", **scope["tls_metadata"](failure.value))
    metadata = json.loads(capsys.readouterr().err)
    assert metadata["ssl_error_class"] == "SSLError" and metadata["ssl_operation"] == "handshake"
    assert metadata["ssl_library"] == "SSL" and metadata["ssl_reason"] == "WRONG_VERSION_NUMBER"
    assert metadata["ssl_errno"] == 1
    assert "synthetic.invalid" not in json.dumps(metadata)


def test_node_bridge_is_opt_in_allowlisted_and_preserves_request_response_pipes(tmp_path):
    node = shutil.which("node")
    if node is None: pytest.skip("Node is optional")
    script = r'''
const assert=require('node:assert/strict');
const https=require('node:https');
const child=require('node:child_process');
const {EventEmitter}=require('node:events');
let nativeCalls=0,spawnCalls=0,submitted;
const native=function(...args){ nativeCalls++; return {original:args}; };
https.request=native;
child.spawn=function(python,args,options){
  spawnCalls++; assert.equal(python,process.argv[2]); assert.equal(args[0],'-B');
  assert(options.windowsHide); assert.deepEqual(options.stdio,['pipe','pipe','pipe']);
  const worker=new EventEmitter();worker.stdout=new EventEmitter();worker.stderr=new EventEmitter();
  worker.stdin=new EventEmitter();worker.kill=()=>true;
  worker.stdin.end=function(payload){ submitted=JSON.parse(payload);queueMicrotask(()=>{
    worker.stderr.emit('data',Buffer.from(JSON.stringify({kind:'quark-https-phase',phase:'response-headers',status:206,
     content_length:3,path:'secret/path',headers:'private-headers',body:'private-body',url:'private-url'})+'\n'));
    worker.stderr.emit('data',Buffer.from(JSON.stringify({kind:'quark-https-phase',phase:'arbitrary-secret-phase'})+'\n'));
    worker.stderr.emit('data',Buffer.from(JSON.stringify({kind:'quark-https-phase',phase:'transport-error',
     error_code:'EPROTO',ssl_error_class:'SSLEOFError',ssl_library:'SSL',ssl_reason:'UNEXPECTED_EOF_WHILE_READING',
     ssl_errno:8,verify_code:18,ssl_operation:'read',strerror:'private-text',traceback:'secret-path'})+'\n'));
    worker.stderr.emit('data',Buffer.from(JSON.stringify({kind:'quark-https-phase',phase:'transport-error',
     ssl_error_class:'secret-class',ssl_library:'private-url',ssl_reason:'private-url',ssl_errno:true,
     verify_code:1099511627776,ssl_operation:'private-phase'})+'\n'));
    worker.stdout.emit('data',Buffer.from(JSON.stringify({status:206,status_message:'Partial Content',headers:[['X-Original','kept'],['set-cookie','one'],['set-cookie','two']],body_base64:Buffer.from([0,255,9]).toString('base64')})));
    worker.emit('close',0);
  }); };
  return worker;
};
const bridge=require(process.argv[1]);
assert.equal(bridge.installMemoryHttpsBridge({mode:'native'}),false); assert.equal(https.request,native);
assert.equal(bridge.installMemoryHttpsBridge({mode:'urllib',python:process.argv[2]}),true);
const headers={'x-Fixture-Signature':'same','Range':'bytes=0-3'};
const options={hostname:'open-api-drive.quark.cn',port:443,path:'/open/v1/file/get_download_url?token=fixture%2Bvalue',method:'POST',headers};
const original=JSON.stringify(options);
const phases=[];
const promise=new Promise((resolve,reject)=>{
  const request=https.request(options,response=>{
    assert.equal(response.statusCode,206);assert.equal(response.headers['x-original'],'kept');assert.deepEqual(response.headers['set-cookie'],['one','two']);
    const chunks=[];response.on('data',value=>chunks.push(value));response.on('end',()=>{assert.deepEqual(Buffer.concat(chunks),Buffer.from([0,255,9]));resolve();});
  });
  request.on('quark-memory-https-phase',value=>phases.push(value));
  request.on('error',reject);request.setTimeout(5000);request.write(Buffer.from([0,254]));request.end(Buffer.from([7,8]));
});
promise.then(()=>{
 assert.equal(submitted.url,'https://open-api-drive.quark.cn/open/v1/file/get_download_url?token=fixture%2Bvalue');
 assert.equal(submitted.method,'POST');assert.deepEqual(submitted.headers,headers);assert.deepEqual(Buffer.from(submitted.body_base64,'base64'),Buffer.from([0,254,7,8]));
 assert.equal(JSON.stringify(options),original);assert.equal(spawnCalls,1);
 assert.deepEqual(phases.find(value=>value.phase==='response-headers'),{phase:'response-headers',status:206,content_length:3});
 assert.deepEqual(phases.filter(value=>value.phase==='transport-error'),[
  {phase:'transport-error',error_code:'EPROTO',ssl_error_class:'SSLEOFError',ssl_library:'SSL',
   ssl_reason:'UNEXPECTED_EOF_WHILE_READING',ssl_errno:8,verify_code:18,ssl_operation:'read'},
  {phase:'transport-error'}]);
 assert(!JSON.stringify(phases).includes('private'));assert(!JSON.stringify(phases).includes('secret'));
 for(const options of [{hostname:'example.invalid',port:443,path:'/a'}, {hostname:'open-api-drive.quark.cn',port:80,path:'/a'}, {hostname:'open-api-drive.quark.cn',protocol:'http:',path:'/a'}, {hostname:'open-api-drive.quark.cn.evil.invalid',path:'/a'}]) https.request(options);
 assert.equal(nativeCalls,4);assert.equal(spawnCalls,1);console.log('RAM_HTTPS_CONTRACT_OK');
}).catch(error=>{console.error(error);process.exitCode=1;});
'''
    environment = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path), NODE_DISABLE_COMPILE_CACHE="1")
    result = subprocess.run([node, "-e", script, str(NODE_BRIDGE), sys.executable], cwd=ROOT, env=environment,
        capture_output=True, text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RAM_HTTPS_CONTRACT_OK" in result.stdout


def test_bridge_sources_parse_without_creating_bytecode(tmp_path):
    ast.parse(PYTHON_BRIDGE.read_text(encoding="utf-8"))
    node = shutil.which("node")
    if node is None: pytest.skip("Node is optional")
    completed = subprocess.run([node, "--check", str(NODE_BRIDGE)], cwd=ROOT,
        capture_output=True, text=True, timeout=10,
        env=dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path), NODE_DISABLE_COMPILE_CACHE="1"),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert completed.returncode == 0, completed.stderr
