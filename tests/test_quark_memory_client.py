"""Official-CLI RAM filesystem contracts; synthetic fixtures never use a cloud account."""
from __future__ import annotations

import hashlib
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from parser.quark_cloud import CloudError, ROOT
from parser.quark_memory_client import MAX_OBJECT_BYTES, MemoryQuarkClient, _account_mutex
from parser import quark_memory_client as memory_module


@pytest.fixture
def memory_client(tmp_path):
    node = shutil.which("node")
    if node is None or not (ROOT / ".runtime/quark-memory/node_modules/memfs/package.json").exists():
        pytest.skip("Optional Quark memory runtime dependencies are not installed")
    scripts = tmp_path / "official-fixture/scripts"
    scripts.mkdir(parents=True)
    sdk = scripts / "quark-drive.cjs"
    sdk.write_text(r'''
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const parameters = process.argv.slice(2);
function result(data) { console.log(JSON.stringify({type:'result',code:0,data})); process.exit(0); }
function option(name) { return parameters[parameters.indexOf(name)+1]; }
async function main() {
  const marker = path.join(process.cwd(), '.ram-quark', 'outside-download-report.json');
  fs.writeFileSync(marker, 'synthetic SDK checkpoint');
  await fsp.writeFile(marker+'.promise', 'synthetic promise checkpoint');
  if (parameters[0] === 'upload') {
    const bytes = fs.readFileSync(parameters[1]);
    const hash = crypto.createHash('sha256').update(bytes).digest('hex');
    result({fids:['fixture-'+hash],successCount:1});
  } else if (parameters[0] === 'download') {
    const directory = option('--output-dir');
    fs.mkdirSync(directory,{recursive:true});
    const output = path.join(directory,'fixture.bin');
    const fd = fs.openSync(output,'w');
    const bytes = Buffer.from('zero disk original\0中文','utf8');
    fs.writeSync(fd,bytes,0,bytes.length,0);
    fs.closeSync(fd);
    result({filePath:output});
  } else if (parameters[0] === 'browse') {
    const directory = option('--task-dir');
    fs.mkdirSync(directory,{recursive:true});
    const output = path.join(directory,'full.jsonl');
    const values = Array.from({length:7},(_,index)=>({fid:String(index),file_name:'file-'+index,size:index,file_type:1}));
    fs.writeFileSync(output,values.map(value=>JSON.stringify(value)).join('\n'));
    result({file_path:output,file_list:values.slice(0,5),count:7,hasMore:false});
  } else if (parameters[0] === 'create-folder') {
    if (option('--dir-path') === 'refresh-contract') {
      const account = path.join(path.dirname(path.dirname(__filename)), 'codex', 'config.json');
      const staging = account+'.'+process.pid+'.tmp';
      const fd=fs.openSync(staging,'w');
      fs.writeFileSync(fd,JSON.stringify({synthetic_refresh:true}));fs.fsyncSync(fd);fs.closeSync(fd);
      fs.renameSync(staging,account);
    }
    result({fid:option('--parent-fid')+'/'+option('--dir-path')});
  }
}
main().catch(error=>{console.log(JSON.stringify({type:'result',code:9,msg:error.message}));process.exit(1)});
''', encoding="utf-8")
    client = MemoryQuarkClient(config={"cli_command": [node, str(sdk)], "folder_id": "fixture-parent",
        "temp_dir": str(tmp_path), "cli_env": {}})
    yield client
    assert not (ROOT / ".ram-quark").exists(), "RAM SDK data escaped to the real filesystem"


def test_ram_filesystem_handles_sync_async_fd_rename_and_streams_without_disk(memory_client):
    response = memory_client._call("contract")
    assert response["contract"]["exact"] and response["contract"]["hash_worker_hidden"]
    assert response["audit"]["report_disk_writes"] == 0
    assert response["audit"]["physical_writes"] == []
    assert response["audit"]["ram_write_calls"] >= 3
    assert not Path(response["contract"]["marker"]).exists()


def test_upload_input_crosses_only_pipe_and_ram(memory_client):
    data = b"\x00\xff" + "战报原文".encode() * 128
    result = memory_client.upload_bytes(data, "synthetic.json.gz")
    assert result == {"fid": "fixture-" + hashlib.sha256(data).hexdigest(), "size": len(data)}
    assert memory_client.last_audit["report_disk_writes"] == 0
    assert memory_client.last_audit["physical_writes"] == []


def test_download_returns_exact_bytes_without_a_cache_file(memory_client):
    assert memory_client.download_bytes("fixture-object") == b"zero disk original\0" + "中文".encode()
    assert memory_client.last_audit["physical_writes"] == []


def test_download_limit_rejects_before_writing_large_ram_file(memory_client):
    with pytest.raises(CloudError, match="in-memory limit"):
        memory_client.download_bytes("fixture-object", max_bytes=5)
    assert memory_client.last_audit["physical_writes"] == []


def test_browse_uses_complete_ram_artifact_instead_of_five_row_preview(memory_client):
    values = memory_client.list_files("fixture-parent")
    assert len(values) == 7
    assert values[-1] == {"fid": "6", "file_name": "file-6", "size": 6, "is_dir": False}
    assert memory_client.last_audit["physical_writes"] == []


def test_create_folder_keeps_the_authorized_parent(memory_client):
    assert memory_client.create_folder("archive") == "fixture-parent/archive"
    assert memory_client.create_folder("index", parent_fid="chosen") == "chosen/index"


def test_only_official_account_configuration_can_persist(memory_client):
    runtime = Path(memory_client.config["cli_command"][-1])
    account = runtime.parent.parent / "codex/config.json"
    account.parent.mkdir(parents=True)
    account.write_text('{"synthetic_initial":true}', encoding="utf-8")
    memory_client.create_folder("refresh-contract")
    assert json.loads(account.read_text(encoding="utf-8")) == {"synthetic_refresh": True}
    assert memory_client.last_audit["report_disk_writes"] == 0
    assert memory_client.last_audit["physical_writes"]
    assert all(item["account_configuration_only"] for item in memory_client.last_audit["physical_writes"])
    assert not list(account.parent.glob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex contract")
def test_account_mutex_serializes_other_processes_without_lock_file(memory_client):
    runtime = memory_client.config["cli_command"][-1]
    script = """
import sys
from parser.quark_memory_client import _account_mutex
from parser.quark_cloud import CloudError
try:
    with _account_mutex(sys.argv[1], 0.1):
        print('UNEXPECTED_ACQUIRE')
except CloudError:
    print('LOCK_BUSY')
"""
    with _account_mutex(runtime, 2):
        completed = subprocess.run([sys.executable, "-B", "-c", script, runtime], cwd=ROOT,
            capture_output=True, text=True, timeout=5,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), creationflags=subprocess.CREATE_NO_WINDOW)
    assert completed.returncode == 0
    assert completed.stdout.strip() == "LOCK_BUSY"


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex contract")
def test_cloud_folder_mutex_is_independent_of_installation_and_working_directory(memory_client, tmp_path):
    folder = "folder:SameCaseSensitiveCloudFolder"
    script = """
import sys
sys.path.insert(0, sys.argv[2])
from parser.quark_memory_client import _account_mutex
from parser.quark_cloud import CloudError
try:
    with _account_mutex(sys.argv[1], 0):
        print('UNEXPECTED_ACQUIRE')
except CloudError:
    print('LOCK_BUSY')
"""
    with _account_mutex(folder, 2):
        completed = subprocess.run([sys.executable, "-B", "-c", script, folder, str(ROOT)], cwd=tmp_path,
            capture_output=True, text=True, timeout=5,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), creationflags=subprocess.CREATE_NO_WINDOW)
    assert completed.returncode == 0
    assert completed.stdout.strip() == "LOCK_BUSY"


@pytest.mark.parametrize("name", ["../report", "nested/report", "nested\\report", "C:report", "", ".", ".."])
def test_invalid_upload_name_is_rejected_without_running_sdk(memory_client, monkeypatch, name):
    monkeypatch.setattr(memory_client, "_call", lambda *_a, **_k: pytest.fail("SDK should not start"))
    with pytest.raises(ValueError):
        memory_client.upload_bytes(b"fixture", name)


def test_oversized_upload_is_rejected_without_running_sdk(memory_client, monkeypatch):
    monkeypatch.setattr(memory_client, "_call", lambda *_a, **_k: pytest.fail("SDK should not start"))
    with pytest.raises(CloudError, match="16 MiB"):
        memory_client.upload_bytes(b"x" * (MAX_OBJECT_BYTES + 1), "fixture.gz")


def fake_envelope(*, ok, message=None, code=0, audit_bytes=0, data=None):
    result = {"code": code, "type": "result", "data": data or {}}
    if message is not None:
        result["msg"] = message
    response = {"ok": ok, "result": result,
        "audit": {"report_disk_writes": audit_bytes, "physical_writes": []}}
    if ok:
        response["body_base64"] = base64.b64encode(b"synthetic RAM response").decode()
    return SimpleNamespace(returncode=0 if ok else 1, stdout=json.dumps(response), stderr="")


@pytest.mark.parametrize("message", [
    "write EPROTO: SSL routines:tls_get_more_records:packet length too long",
    "read ECONNRESET", "request ETIMEDOUT", "socket hang up",
])
def test_explicit_transport_error_retries_exact_request_then_succeeds(memory_client, monkeypatch, message):
    requests, delays = [], []
    def run(command, **kwargs):
        requests.append((command, json.loads(kwargs["input"])))
        if len(requests) == 1:
            return fake_envelope(ok=False, message=message, code=-1704)
        return fake_envelope(ok=True)
    monkeypatch.setattr(memory_module.subprocess, "run", run)
    monkeypatch.setattr(memory_module.time, "sleep", delays.append)
    assert memory_client.download_bytes("same-opaque-fid", max_bytes=100) == b"synthetic RAM response"
    assert len(requests) == 2 and requests[0] == requests[1]
    assert requests[0][1] == {"action": "download", "max_bytes": 100, "fid": "same-opaque-fid"}
    assert delays == [1]
    assert memory_client.last_audit["transport_attempts"] == 2
    assert memory_client.last_audit["physical_writes"] == []


def test_transport_retries_stop_after_three_identical_uploads(memory_client, monkeypatch):
    requests, delays = [], []
    def run(command, **kwargs):
        requests.append((command, json.loads(kwargs["input"])))
        return fake_envelope(ok=False, message="write EPROTO temporary TLS transport", code=-1704)
    monkeypatch.setattr(memory_module.subprocess, "run", run)
    monkeypatch.setattr(memory_module.time, "sleep", delays.append)
    raw = b"immutable synthetic bytes\0" * 20
    with pytest.raises(CloudError, match="EPROTO"):
        memory_client.upload_bytes(raw, "same-content-addressed-name.gz")
    assert len(requests) == 3 and requests[0] == requests[1] == requests[2]
    assert base64.b64decode(requests[0][1]["body_base64"]) == raw
    assert requests[0][1]["name"] == "same-content-addressed-name.gz"
    assert delays == [1, 2]
    assert memory_client.last_audit["transport_attempts"] == 3
    assert memory_client.last_audit["physical_writes"] == []


def audited_timeout(*, network=None, message=None, code=-1704):
    response = fake_envelope(ok=False, message=message or
        "Request timeout after 60000ms: POST https://open-api-drive.quark.cn/open/v1/file/get_download_url?access_token=[redacted]", code=code)
    value = json.loads(response.stdout)
    value["audit"]["network"] = network if network is not None else [{
        "transport_protocol": "https:", "protocol": "https:", "hostname": "open-api-drive.quark.cn",
        "port": 443, "method": "POST", "endpoint": "download-url", "error_code": "ECONNRESET"}]
    response.stdout = json.dumps(value)
    return response


@pytest.mark.parametrize("succeed_on_third", [True, False])
@pytest.mark.parametrize("host", ["open-api-drive.quark.cn", "dl-sz.open-drive.quark.cn"])
def test_audited_quark_reset_with_sdk_timeout_wrapper_retries_at_most_three(memory_client, monkeypatch, succeed_on_third, host):
    requests, delays = [], []
    def run(command, **kwargs):
        requests.append((command, json.loads(kwargs["input"])))
        if succeed_on_third and len(requests) == 3:
            return fake_envelope(ok=True)
        response = audited_timeout()
        value = json.loads(response.stdout)
        value["audit"]["network"][0]["hostname"] = host
        response.stdout = json.dumps(value)
        return response
    monkeypatch.setattr(memory_module.subprocess, "run", run)
    monkeypatch.setattr(memory_module.time, "sleep", delays.append)
    if succeed_on_third:
        assert memory_client.download_bytes("same-opaque-fid", max_bytes=100) == b"synthetic RAM response"
    else:
        with pytest.raises(CloudError, match="Request timeout"):
            memory_client.download_bytes("same-opaque-fid", max_bytes=100)
    assert len(requests) == 3 and requests[0] == requests[1] == requests[2]
    assert delays == [1, 2] and memory_client.last_audit["transport_attempts"] == 3
    assert memory_client.last_audit["report_disk_writes"] == 0
    assert memory_client.last_audit["physical_writes"] == []


@pytest.mark.parametrize("change", ["permission", "authorization-code", "configuration", "size", "missing-audit", "bad-physical-audit",
    "unknown-error", "malformed-error", "missing-network", "telemetry", "lookalike-host", "unknown-message"])
def test_timeout_network_evidence_does_not_override_hard_refusals_or_unknown_audit(memory_client, monkeypatch, change):
    response = audited_timeout()
    value = json.loads(response.stdout)
    if change in {"permission", "configuration", "size"}:
        suffix = {"permission": "permission denied", "configuration": "invalid config", "size": "size limit"}[change]
        value["result"]["msg"] += " " + suffix
        if change == "permission":
            value["result"]["code"] = 403
    elif change == "authorization-code":
        value["result"]["code"] = 401
    elif change == "missing-audit":
        del value["audit"]["report_disk_writes"]
    elif change == "bad-physical-audit":
        value["audit"]["physical_writes"] = [{"account_configuration_only": False}]
    elif change == "unknown-error":
        value["audit"]["network"][0]["error_code"] = "UNKNOWN"
    elif change == "malformed-error":
        value["audit"]["network"][0]["error_code"] = {"unexpected": "ECONNRESET"}
    elif change == "missing-network":
        value["audit"].pop("network")
    elif change == "telemetry":
        value["audit"]["network"][0]["hostname"] = "px.wpk.quark.cn"
    elif change == "lookalike-host":
        value["audit"]["network"][0]["hostname"] = "open-api-drive.quark.cn.attacker.invalid"
    elif change == "unknown-message":
        value["result"]["msg"] = "Unclassified failure"
    response.stdout = json.dumps(value)
    calls, delays = [], []
    def run(*_args, **_kwargs):
        calls.append(1)
        return response
    monkeypatch.setattr(memory_module.subprocess, "run", run)
    monkeypatch.setattr(memory_module.time, "sleep", delays.append)
    with pytest.raises(CloudError):
        memory_client.download_bytes("fixture")
    assert calls == [1] and delays == []


@pytest.mark.parametrize("message,code,audit_bytes", [
    ("permission denied with unrelated EPROTO text", 403, 0),
    ("EPROTO but account is unauthorized", -1704, 0),
    ("invalid config EPROTO", -1704, 0),
    ("download file size limit ETIMEDOUT", 23018, 0),
    ("socket hang up", -1704, 1),
    ("unclassified failure without transport evidence", -1704, 0),
])
def test_permission_configuration_audit_size_and_unknown_errors_never_retry(memory_client, monkeypatch, message, code, audit_bytes):
    calls, delays = [], []
    def run(*args, **kwargs):
        calls.append(1)
        return fake_envelope(ok=False, message=message, code=code, audit_bytes=audit_bytes)
    monkeypatch.setattr(memory_module.subprocess, "run", run)
    monkeypatch.setattr(memory_module.time, "sleep", delays.append)
    with pytest.raises(CloudError):
        memory_client.download_bytes("fixture")
    assert calls == [1] and delays == []


def test_local_configuration_error_cannot_start_or_retry_sdk(memory_client, monkeypatch):
    memory_client.config["cli_command"] = []
    monkeypatch.setattr(memory_module.subprocess, "run", lambda *_a, **_k: pytest.fail("Invalid config started SDK"))
    monkeypatch.setattr(memory_module.time, "sleep", lambda *_a: pytest.fail("Invalid config entered retry"))
    with pytest.raises(CloudError, match="Node.js"):
        memory_client.download_bytes("fixture")


def test_nonaccount_physical_write_audit_failure_never_enters_transport_retry(memory_client, monkeypatch):
    calls, delays = [], []
    response = fake_envelope(ok=False, message="write EPROTO", code=-1704)
    value = json.loads(response.stdout)
    value["audit"]["physical_writes"] = [{"method": "writeFileSync", "account_configuration_only": False}]
    response.stdout = json.dumps(value)
    def run(*_args, **_kwargs):
        calls.append(1)
        return response
    monkeypatch.setattr(memory_module.subprocess, "run", run)
    monkeypatch.setattr(memory_module.time, "sleep", delays.append)
    with pytest.raises(CloudError, match="审计未通过"):
        memory_client.download_bytes("fixture")
    assert calls == [1] and delays == []


def test_network_audit_records_binding_without_urls_headers_bodies_or_error_messages(memory_client):
    secret = "fixture-private-network-value-do-not-record"
    specifications = [
        {"transport": "https:", "url": "http://fixture-user:" + secret + "@cdn.example.invalid:80/content?token=" + secret,
         "options": {"method": "GET", "headers": {"Cookie": secret, "Authorization": "Bearer " + secret}, "rejectUnauthorized": True},
         "error_code": "EPROTO", "error_message": "sensitive URL " + secret},
        {"transport": "https:", "options": {"hostname": "open-api-drive.quark.cn", "port": 443, "method": "POST",
         "path": "/open/v1/file/get_download_url?access_token=" + secret, "headers": {"x-pan-token": secret}, "body": secret}},
    ]
    response = memory_client._call("contract", network_contract=specifications)
    network = response["audit"]["network"]
    assert response["contract"]["network_arguments_preserved"] is True
    assert network[0] == {"transport_protocol": "https:", "protocol": "http:", "hostname": "cdn.example.invalid",
        "port": 80, "method": "GET", "endpoint": "other-host", "error_code": "EPROTO"}
    assert network[1] == {"transport_protocol": "https:", "protocol": "https:", "hostname": "open-api-drive.quark.cn",
        "port": 443, "method": "POST", "endpoint": "download-url"}
    rendered = json.dumps(response)
    assert secret not in rendered and "fixture-user" not in rendered and "access_token=" not in rendered


def test_network_audit_is_limited_to_twenty_entries_and_respects_agent_default_port(memory_client):
    specifications = [{"transport": "https:", "options": {"hostname": "cdn.example.invalid",
        "method": "GET", "agent": {"defaultPort": 80, "protocol": "https:"}, "path": "/not-retained"}} for _ in range(25)]
    response = memory_client._call("contract", network_contract=specifications)
    assert len(response["audit"]["network"]) == 20
    assert all(item["port"] == 80 and item["transport_protocol"] == "https:" for item in response["audit"]["network"])
    assert response["contract"]["network_arguments_preserved"] is True
    assert "not-retained" not in json.dumps(response)
