"""Exercise both authorization transports offline; no Quark account or sockets."""
from __future__ import annotations

import ast
import base64
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "tools/quark_http_transport.py"
PRELOAD = ROOT / "tools/quark_http_transport.cjs"
ENDPOINT = "https://open-api-drive.quark.cn/agent/v1/get_authorize_page_url?fixture=1"


def node_script(script, tmp_path, mode):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Quark integration requires Node.js")
    env = os.environ.copy()
    for name in ("NODE_OPTIONS", "SANMOU_QUARK_HTTP_DIAGNOSTICS"):
        env.pop(name, None)
    env.update(TEMP=str(tmp_path), TMP=str(tmp_path), TMPDIR=str(tmp_path),
               NODE_DISABLE_COMPILE_CACHE="1", SANMOU_QUARK_HTTP_TRANSPORT=mode,
               SANMOU_QUARK_PYTHON=sys.executable)
    result = subprocess.run([node, "-e", script, str(PRELOAD), sys.executable],
                            cwd=ROOT, env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "OFFLINE_TRANSPORT_OK" in result.stdout


def test_native_transport_preserves_body_and_signatures_and_only_frames_authorization_post(tmp_path):
    script = r'''
const assert = require('node:assert/strict');
const https = require('node:https');
const { EventEmitter } = require('node:events');
const calls = [];
https.request = function(options, callback) {
  const request = new EventEmitter();
  request.headersSent = false;
  request.headers = { ...(options.headers || {}) };
  request.removeHeader = name => { delete request.headers[name]; };
  request.setHeader = (name, value) => { request.headers[name] = value; };
  request.end = (body, done) => {
    request.body = body;
    request.emit('finish');
    if (typeof done === 'function') done();
    return request;
  };
  calls.push({options, request});
  return request;
};
require(process.argv[1]);
const options = {hostname:'open-api-drive.quark.cn', method:'POST',
  path:'/agent/v1/get_authorize_page_url?fixture=1',
  headers:{'Content-Type':'application/json', 'X-Fixture-Signature':'unchanged-fixture'}};
const original = JSON.stringify(options);
const first = Buffer.from('first bytes ');
const second = Buffer.from([0, 1, 254, 255]);
const request = https.request(options, () => {});
let completed = 0;
request.write(first, () => { completed += 1; });
request.end(second);
assert.equal(JSON.stringify(options), original);
assert.deepEqual(request.body, Buffer.concat([first, second]));
assert.equal(request.headers['Content-Length'], String(first.length + second.length));
assert.equal(request.headers['X-Fixture-Signature'], 'unchanged-fixture');
assert.equal(completed, 1);
for (const change of [{method:'GET'}, {path:'/agent/v1/upload'},
                      {hostname:'example.invalid'}, {port:444}]) {
  const other = {...options, ...change};
  const plain = https.request(other, () => {});
  plain.end(Buffer.from('unframed'));
  assert.equal(plain.headers['Content-Length'], undefined);
  assert.equal(calls[calls.length - 1].options, other);
}
console.log('OFFLINE_TRANSPORT_OK');
'''
    node_script(script, tmp_path, "content-length")


def test_urllib_node_transport_keeps_request_in_pipes_and_reconstructs_response(tmp_path):
    script = r'''
const assert = require('node:assert/strict');
const child = require('node:child_process');
const https = require('node:https');
const { EventEmitter } = require('node:events');
let calls = 0;
let sent;
child.spawn = function(executable, arguments, options) {
  calls += 1;
  assert.equal(executable, process.argv[2]);
  assert.equal(arguments[0], '-B');
  assert.ok(arguments[1].endsWith('quark_http_transport.py'));
  assert.equal(options.windowsHide, true);
  assert.deepEqual(options.stdio, ['pipe','pipe','pipe']);
  const worker = new EventEmitter();
  worker.stdout = new EventEmitter();
  worker.stderr = {resume() {}};
  worker.stdin = {end(text) {
    sent = JSON.parse(text);
    setImmediate(() => {
      worker.stdout.emit('data', Buffer.from(JSON.stringify({status:200,
        headers:[['Content-Type','application/json'],['X-Fixture','a'],['X-Fixture','b']],
        body_base64:Buffer.from('fixture-response').toString('base64')})));
      worker.emit('close', 0);
    });
  }};
  worker.kill = () => {};
  return worker;
};
https.request = () => { throw new Error('Unexpected native network request'); };
require(process.argv[1]);
(async () => {
  const responseDone = new Promise((resolve, reject) => {
    const options = {hostname:'open-api-drive.quark.cn', method:'POST',
      path:'/agent/v1/get_authorize_page_url?fixture=1', headers:{'X-Fixture-Signature':'kept'}};
    const request = https.request(options, response => {
      assert.equal(response.statusCode, 200);
      assert.deepEqual(response.headers['x-fixture'], ['a','b']);
      const chunks = [];
      response.on('data', value => chunks.push(value));
      response.on('end', () => resolve(Buffer.concat(chunks).toString()));
    });
    request.on('error', reject);
    request.setTimeout(60000);
    request.write(Buffer.from([0, 254, 255]));
    request.end(Buffer.from('end'));
  });
  assert.equal(await responseDone, 'fixture-response');
  assert.equal(calls, 1);
  assert.equal(sent.url, 'https://open-api-drive.quark.cn/agent/v1/get_authorize_page_url?fixture=1');
  assert.equal(sent.method, 'POST');
  assert.deepEqual(Buffer.from(sent.body_base64, 'base64'), Buffer.concat([Buffer.from([0,254,255]),Buffer.from('end')]));
  assert.equal(sent.headers['X-Fixture-Signature'], 'kept');
  assert.equal(sent.timeout_seconds, 45);
  console.log('OFFLINE_TRANSPORT_OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    node_script(script, tmp_path, "urllib")


def bridge_function(monkeypatch, document):
    namespace = runpy.run_path(str(BRIDGE))
    monkeypatch.setattr(namespace["sys"], "stdin", io.StringIO(json.dumps(document)))
    return namespace, namespace["run"]


@pytest.mark.parametrize("url,method", [
    ("https://example.invalid/agent/v1/get_authorize_page_url", "POST"),
    ("http://open-api-drive.quark.cn/agent/v1/get_authorize_page_url", "POST"),
    ("https://open-api-drive.quark.cn:444/agent/v1/get_authorize_page_url", "POST"),
    ("https://fixture@open-api-drive.quark.cn/agent/v1/get_authorize_page_url", "POST"),
    ("https://open-api-drive.quark.cn/agent/v1/upload", "POST"),
    (ENDPOINT, "GET"),
])
def test_python_bridge_rejects_other_destinations_and_methods_without_network(monkeypatch, url, method):
    namespace, run = bridge_function(monkeypatch, {"url": url, "method": method})
    monkeypatch.setattr(namespace["urllib"].request, "build_opener",
                        lambda *_args: pytest.fail("Invalid destination reached the network client"))
    assert run() == {"transport_error": "ENDPOINT_NOT_ALLOWED"}


def test_python_bridge_preserves_exact_body_headers_and_http_response(monkeypatch):
    payload = b'\x00binary\xfe\xff JSON body unchanged'
    document = {"url": ENDPOINT, "method": "POST", "headers": {"X-Fixture-Signature": "kept"},
                "body_base64": base64.b64encode(payload).decode(), "timeout_seconds": 99}
    namespace, run = bridge_function(monkeypatch, document)
    calls = []

    class Response:
        code = 200
        headers = Message()
        headers.add_header("Content-Type", "application/json")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"fixture":"response"}'

    def open_request(request, timeout):
        calls.append((request, timeout))
        return Response()

    def build_opener(*handlers):
        assert any(isinstance(handler, namespace["NoRedirect"]) for handler in handlers)
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(namespace["urllib"].request, "build_opener", build_opener)
    result = run()
    request, timeout = calls[0]
    assert request.full_url == ENDPOINT and request.get_method() == "POST"
    assert request.data == payload and request.get_header("X-fixture-signature") == "kept"
    assert timeout == 45 and result["status"] == 200
    assert base64.b64decode(result["body_base64"]) == b'{"fixture":"response"}'
    assert namespace["NoRedirect"]().redirect_request(None, None, 302, "redirect", {}, "https://example.invalid") is None


def test_transport_sources_parse_without_creating_python_bytecode(tmp_path):
    ast.parse(BRIDGE.read_text(encoding="utf-8"))
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node syntax check requires optional Node.js")
    env = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path), NODE_DISABLE_COMPILE_CACHE="1")
    env.pop("NODE_OPTIONS", None)
    for script in (PRELOAD, ROOT / "tools/quark_runtime_guard.cjs"):
        result = subprocess.run([node, "--check", str(script)], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("codex_shell,expected", [(None, None), ("1", "1"), ("0", None), ("true", None)])
def test_setup_writes_token_free_dual_preload_config_from_verified_offline_fixture(tmp_path, monkeypatch, codex_shell, expected):
    if codex_shell is None:
        monkeypatch.delenv("CODEX_SHELL", raising=False)
    else:
        monkeypatch.setenv("CODEX_SHELL", codex_shell)
    namespace = runpy.run_path(str(ROOT / "tools/setup_quark.py"))
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as package:
        package.writestr("scripts/quark-drive.cjs", "// synthetic runtime, never executed\n")
    archive = tmp_path / "fixture-runtime.zip"
    archive.write_bytes(payload.getvalue())
    install = namespace["install"]
    monkeypatch.setitem(install.__globals__, "SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    node = tmp_path / "fixture-node.exe"
    node.write_bytes(b"never executed")
    monkeypatch.setattr(namespace["shutil"], "which", lambda _name: str(node))
    project = tmp_path / "project"
    project.mkdir()
    result = install(project / ".runtime/quark", root=project, archive=archive)
    config = json.loads(Path(result["config"]).read_text(encoding="utf-8"))
    assert config["enabled"] is False and config["folder_id"] is None
    assert config["cli_command"].count("--require") == 2
    assert str(project / "tools/quark_runtime_guard.cjs") in config["cli_command"]
    assert str(project / "tools/quark_http_transport.cjs") in config["cli_command"]
    assert config["cli_env"]["SANMOU_QUARK_HTTP_TRANSPORT"] == "urllib"
    assert config["cli_env"]["SANMOU_QUARK_PYTHON"] == sys.executable
    assert config["cli_env"].get("CODEX_SHELL") == expected
    assert "CODEX_HOME" not in config["cli_env"] and "CODEX_THREAD_ID" not in config["cli_env"]
    assert "access_token" not in json.dumps(config) and "refresh_token" not in json.dumps(config)
    config["cache_max_bytes"] = 123456
    Path(result["config"]).write_text(json.dumps(config), encoding="utf-8")
    install(project / ".runtime/quark", root=project, archive=archive)
    assert json.loads(Path(result["config"]).read_text(encoding="utf-8"))["cache_max_bytes"] == 123456


def test_setup_checksum_failure_does_not_publish_runtime_or_config(tmp_path):
    namespace = runpy.run_path(str(ROOT / "tools/setup_quark.py"))
    archive = tmp_path / "untrusted.zip"
    archive.write_bytes(b"unverified fixture bytes")
    with pytest.raises(ValueError, match="checksum"):
        namespace["install"](tmp_path / "runtime", root=tmp_path, archive=archive)
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "config/quark.local.json").exists()
