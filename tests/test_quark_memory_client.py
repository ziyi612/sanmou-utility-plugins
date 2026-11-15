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
function result(data) {
  data.ramFixturePeakKiB=process.resourceUsage().maxRSS;
  data.ramFixtureRssBytes=process.memoryUsage().rss;
  console.log(JSON.stringify({type:'result',code:0,data})); process.exit(0);
}
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
    const bytes = option('--fid') === 'fixture-large-63m'
      ? Buffer.alloc(63*1024*1024,Buffer.from(Array.from({length:256},(_,index)=>index)))
      : Buffer.from('zero disk original\0中文','utf8');
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


@pytest.mark.parametrize("limit", [True, False, None, 0, -1, 64 * 1024 * 1024 + 1, 1.5, "67108864"])
def test_upload_limit_configuration_rejects_noninteger_or_out_of_range_values(limit):
    with pytest.raises(ValueError, match="max_upload_bytes"):
        MemoryQuarkClient(config={}, max_upload_bytes=limit)


def test_default_upload_boundary_and_explicit_runner_limit_remain_sixteen_mib(memory_client, monkeypatch):
    calls = []
    def call(action, **kwargs):
        calls.append((action, kwargs))
        return {"result": {"data": {"fids": ["fixture"]}}}
    monkeypatch.setattr(memory_client, "_call", call)
    assert memory_client.max_upload_bytes == MAX_OBJECT_BYTES
    raw = b"x" * MAX_OBJECT_BYTES
    assert memory_client.upload_bytes(raw, "boundary.gz") == {"fid": "fixture", "size": len(raw)}
    assert calls[0][0] == "upload" and calls[0][1]["max_bytes"] == MAX_OBJECT_BYTES
    assert len(base64.b64decode(calls[0][1]["body_base64"])) == MAX_OBJECT_BYTES


@pytest.mark.parametrize("limit", [1, 8])
def test_explicit_small_upload_limit_rejects_before_starting_sdk(memory_client, monkeypatch, limit):
    client = MemoryQuarkClient(config=memory_client.config, max_upload_bytes=limit)
    monkeypatch.setattr(client, "_call", lambda *_a, **_k: pytest.fail("Oversized upload started SDK"))
    with pytest.raises(CloudError, match=f"{limit}字节"):
        client.upload_bytes(b"x" * (limit + 1), "fixture.gz")


def test_opt_in_large_upload_and_download_cross_real_production_runner_only_in_ram(memory_client, monkeypatch):
    """Sequential 63 MiB pipes/memfs regression, with bounded synthetic data."""
    import gc
    client = MemoryQuarkClient(config=memory_client.config, max_upload_bytes=64 * 1024 * 1024)
    snapshots, limits = [], []
    original = client._call
    def call(action, **kwargs):
        limits.append((action, kwargs["max_bytes"]))
        response = original(action, **kwargs)
        values = response["result"]["data"]
        snapshots.append({"action": action, "peak_kib": values["ramFixturePeakKiB"], "rss_bytes": values["ramFixtureRssBytes"]})
        assert response["audit"]["report_disk_writes"] == 0
        assert response["audit"]["physical_writes"] == []
        return response
    monkeypatch.setattr(client, "_call", call)
    size = 63 * 1024 * 1024
    pattern = bytes(range(256))
    raw = pattern * (size // len(pattern))
    identity = hashlib.sha256(raw).hexdigest()
    assert client.upload_bytes(raw, "synthetic-large.bin") == {"fid": "fixture-" + identity, "size": size}
    del raw
    gc.collect()
    received = client.download_bytes("fixture-large-63m", max_bytes=size)
    assert len(received) == size and hashlib.sha256(received).hexdigest() == identity
    assert received[:256] == received[-256:] == pattern
    assert limits == [("upload", 64 * 1024 * 1024), ("download", size)]
    assert client.last_audit["report_disk_writes"] == 0 and client.last_audit["physical_writes"] == []
    assert not (ROOT / ".ram-quark").exists()
    print("RAM_LARGE_OBJECT_OBSERVATIONS=" + json.dumps(snapshots))


def fake_envelope(*, ok, message=None, code=0, audit_bytes=0, data=None):
    result = {"code": code, "type": "result", "data": data or {}}
    if message is not None:
        result["msg"] = message
    response = {"ok": ok, "result": result,
        "audit": {"report_disk_writes": audit_bytes, "physical_writes": []}}
    if ok:
        response["body_base64"] = base64.b64encode(b"synthetic RAM response").decode()
    return SimpleNamespace(returncode=0 if ok else 1, stdout=json.dumps(response), stderr="")


@pytest.mark.parametrize("action", ["download", "list"])
def test_read_deadline_retries_identical_request_and_accepts_only_the_next_valid_audit(memory_client, monkeypatch, capsys, action):
    calls, delays = [], []
    memory_client.last_audit = {"stale": "must-be-discarded"}
    def run(command, **kwargs):
        calls.append((command, json.loads(kwargs["input"])))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, 180, output="sensitive-partial-base64", stderr="sensitive-private-value")
        assert memory_client.last_audit is None
        response = fake_envelope(ok=True)
        if action == "list":
            value = json.loads(response.stdout)
            value["items"] = [{"fid": "fixture", "file_name": "cloud.bin", "size": 20}]
            response.stdout = json.dumps(value)
        return response
    monkeypatch.setattr(memory_module.subprocess, "run", run)
    monkeypatch.setattr(memory_module.time, "sleep", delays.append)
    if action == "download":
        assert memory_client.download_bytes("same-opaque-fid") == b"synthetic RAM response"
        assert calls[0][1]["fid"] == "same-opaque-fid"
    else:
        assert memory_client.list_files("same-parent") == [{"fid": "fixture", "file_name": "cloud.bin", "size": 20, "is_dir": False}]
        assert calls[0][1]["parent_fid"] == "same-parent"
    assert len(calls) == 2 and calls[0] == calls[1] and delays == [1]
    assert memory_client.last_audit["report_disk_writes"] == 0
    assert memory_client.last_audit["physical_writes"] == []
    assert memory_client.last_audit["transport_attempts"] == 2
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("action", ["download", "list", "upload", "mkdir"])
def test_read_deadlines_stop_after_three_but_uncertain_writes_are_never_reissued(memory_client, monkeypatch, capsys, action):
    calls, delays = [], []
    memory_client.last_audit = {"report_disk_writes": 0, "physical_writes": []}
    def run(command, **kwargs):
        calls.append((command, json.loads(kwargs["input"])))
        raise subprocess.TimeoutExpired(command, 180, output="sensitive-partial-base64", stderr="sensitive-private-value")
    monkeypatch.setattr(memory_module.subprocess, "run", run)
    monkeypatch.setattr(memory_module.time, "sleep", delays.append)
    payload = {"fid": "same-opaque-fid"} if action == "download" else {"parent_fid": "same-parent"}
    with pytest.raises(subprocess.TimeoutExpired):
        memory_client._call(action, **payload)
    maximum = 3 if action in {"download", "list"} else 1
    assert len(calls) == maximum and all(item == calls[0] for item in calls)
    assert delays == ([1, 2] if maximum == 3 else [])
    assert memory_client.last_audit is None
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("invalid", ["no-audit", "physical-write", "certificate"])
def test_a_read_deadline_does_not_relax_audit_or_certificate_validation_on_retry(memory_client, monkeypatch, invalid):
    calls, delays = [], []
    def run(command, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, 180)
        response = fake_envelope(ok=True)
        value = json.loads(response.stdout)
        if invalid == "no-audit":
            value.pop("audit")
        elif invalid == "physical-write":
            value["audit"]["physical_writes"] = [{"account_configuration_only": False}]
        else:
            response.returncode = 1
            value["ok"] = False
            value["result"]["msg"] = "TLS_VERIFICATION_FAILED"
        response.stdout = json.dumps(value)
        return response
    monkeypatch.setattr(memory_module.subprocess, "run", run)
    monkeypatch.setattr(memory_module.time, "sleep", delays.append)
    with pytest.raises(CloudError):
        memory_client.download_bytes("same-opaque-fid")
    assert calls == [1, 1] and delays == [1]


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


def audited_bridge_timeout():
    response = audited_timeout(message="Request timeout after 60000ms: POST https://open-api-drive.quark.cn/open/v1/file/upload_pre?access_token=[redacted]")
    value = json.loads(response.stdout)
    record = value["audit"]["network"][0]
    record.pop("error_code")
    record["endpoint"] = "upload-pre"
    record["lifecycle"] = [{"phase": name} for name in ["helper-spawned", "opening", "request-started", "timeout", "destroyed", "helper-exit"]]
    response.stdout = json.dumps(value)
    return response


@pytest.mark.parametrize("succeed_on_third", [True, False])
@pytest.mark.parametrize("host", ["open-api-drive.quark.cn", "dl-sz.open-drive.quark.cn"])
def test_audited_bridge_timeout_phase_retries_the_identical_upload_at_most_three(memory_client, monkeypatch, succeed_on_third, host):
    requests, delays = [], []
    def run(command, **kwargs):
        requests.append((command, json.loads(kwargs["input"])))
        if succeed_on_third and len(requests) == 3:
            return fake_envelope(ok=True, data={"fids": ["fixture"], "successCount": 1})
        response = audited_bridge_timeout()
        value = json.loads(response.stdout)
        value["audit"]["network"][0]["hostname"] = host
        response.stdout = json.dumps(value)
        return response
    monkeypatch.setattr(memory_module.subprocess, "run", run)
    monkeypatch.setattr(memory_module.time, "sleep", delays.append)
    raw = b"immutable synthetic report\0" * 8
    if succeed_on_third:
        assert memory_client.upload_bytes(raw, "same.gz") == {"fid": "fixture", "size": len(raw)}
    else:
        with pytest.raises(CloudError, match="Request timeout"):
            memory_client.upload_bytes(raw, "same.gz")
    assert len(requests) == 3 and requests[0] == requests[1] == requests[2]
    assert delays == [1, 2] and memory_client.last_audit["transport_attempts"] == 3
    assert base64.b64decode(requests[0][1]["body_base64"]) == raw
    assert memory_client.last_audit["physical_writes"] == []


@pytest.mark.parametrize("change", ["unknown-phase", "fake-host", "missing-audit", "bad-physical-audit", "permission",
    "certificate-code", "certificate-metadata", "certificate-message", "unknown-message"])
def test_bridge_timeout_phase_never_overrides_hard_refusals_or_untrusted_evidence(memory_client, monkeypatch, change):
    response = audited_bridge_timeout()
    value = json.loads(response.stdout)
    record = value["audit"]["network"][0]
    if change == "unknown-phase":
        next(item for item in record["lifecycle"] if item["phase"] == "timeout")["phase"] = "timed-out"
    elif change == "fake-host":
        record["hostname"] = "open-api-drive.quark.cn.attacker.invalid"
    elif change == "missing-audit":
        value["audit"].pop("report_disk_writes")
    elif change == "bad-physical-audit":
        value["audit"]["physical_writes"] = [{"account_configuration_only": False}]
    elif change == "permission":
        value["result"]["code"] = 403
    elif change == "certificate-code":
        record["error_code"] = "TLS_VERIFICATION_FAILED"
    elif change == "certificate-metadata":
        record["lifecycle"].insert(2, {"phase": "transport-error", "ssl_error_class": "SSLCertVerificationError"})
    elif change == "certificate-message":
        value["result"]["msg"] += " certificate verification failed"
    elif change == "unknown-message":
        value["result"]["msg"] = "Unknown failure"
    response.stdout = json.dumps(value)
    calls, delays = [], []
    def run(*_args, **_kwargs):
        calls.append(1)
        return response
    monkeypatch.setattr(memory_module.subprocess, "run", run)
    monkeypatch.setattr(memory_module.time, "sleep", delays.append)
    with pytest.raises(CloudError):
        memory_client.upload_bytes(b"fixture", "same.gz")
    assert calls == [1] and delays == []


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


def test_network_audit_is_bounded_and_counts_omissions_while_respecting_agent_default_port(memory_client):
    specifications = [{"transport": "https:", "options": {"hostname": "cdn.example.invalid",
        "method": "GET", "agent": {"defaultPort": 80, "protocol": "https:"}, "path": "/not-retained"}} for _ in range(135)]
    response = memory_client._call("contract", network_contract=specifications)
    assert len(response["audit"]["network"]) == 128
    assert response["audit"]["network_omitted_count"] == 7
    assert all(item["port"] == 80 and item["transport_protocol"] == "https:" for item in response["audit"]["network"])
    assert response["contract"]["network_arguments_preserved"] is True
    assert "not-retained" not in json.dumps(response)


def test_late_multipart_finish_timeout_crosses_real_runner_bridge_and_enters_bounded_retry(memory_client, tmp_path, monkeypatch):
    """A real Node bridge timeout after 24 earlier requests must remain visible."""
    runtime = Path(memory_client.config["cli_command"][-1])
    preload = tmp_path / "synthetic-network-preload.cjs"
    preload.write_text(r'''
const https = require('node:https');
const child = require('node:child_process');
const {EventEmitter} = require('node:events');
// No socket is opened. Nonallowlisted telemetry uses this original delegate;
// the production bridge handles the allowlisted finish request below it.
https.request = function () {
  const request = new EventEmitter();
  request.setTimeout = () => request;
  request.write = () => true;
  request.end = () => request;
  request.destroy = () => request;
  return request;
};
child.spawn = function () {
  const worker = new EventEmitter();
  worker.stdout = new EventEmitter();
  worker.stderr = new EventEmitter();
  worker.stdin = new EventEmitter();
  worker.killed = false;
  worker.kill = () => { worker.killed = true; return true; };
  worker.stdin.end = function (_payload, done) {
    if (typeof done === 'function') done();
    queueMicrotask(() => worker.stderr.emit('data', Buffer.from(JSON.stringify({
      kind:'quark-https-phase', phase:'request-started'
    }) + '\n')));
    // The helper deliberately never returns a response. The actual production
    // bridge timer emits the audited timeout and destroys this synthetic child.
  };
  return worker;
};
''', encoding="utf-8")
    runtime.write_text(r'''
const https = require('node:https');
const secret = 'fixture-private-network-value-do-not-record';
for (let index = 0; index < 24; index++) {
  https.request({hostname:'px.wpk.quark.cn',port:443,method:'POST',
    path:'/synthetic-telemetry?token='+secret,headers:{Authorization:'Bearer '+secret}}).end(secret);
}
const request = https.request({hostname:'open-api-drive.quark.cn',port:443,method:'POST',
  path:'/open/v1/file/upload_finish?access_token='+secret,
  headers:{Authorization:'Bearer '+secret}});
request.on('error', () => {});
request.setTimeout(20);
request.on('timeout', () => {
  console.log(JSON.stringify({type:'result',code:1007,msg:
    'Request timeout after 20ms: POST https://open-api-drive.quark.cn/open/v1/file/upload_finish?access_token='+secret}));
  process.exit(1);
});
request.end(JSON.stringify({synthetic_body:secret}));
''', encoding="utf-8")
    memory_client.config["cli_command"] = [memory_client.config["cli_command"][0], "--require", str(preload), str(runtime)]
    memory_client.config["cli_env"] = {"SANMOU_QUARK_MEMORY_HTTPS": "urllib", "SANMOU_QUARK_PYTHON": sys.executable}
    delays = []
    monkeypatch.setattr(memory_module.time, "sleep", delays.append)
    with pytest.raises(memory_module._TransientTransportError, match="Request timeout after 20ms") as failure:
        memory_client.upload_bytes(b"synthetic multipart payload", "same-content-addressed.gz")
    audit = memory_client.last_audit
    assert audit["https_adapter"] == "urllib-allowlist"
    assert audit["transport_attempts"] == 3 and delays == [1, 2]
    assert audit["network_omitted_count"] == 0 and len(audit["network"]) == 25
    assert all(item["endpoint"] == "telemetry" for item in audit["network"][:24])
    finish = audit["network"][-1]
    assert finish["endpoint"] == "upload-finish" and finish["hostname"] == "open-api-drive.quark.cn"
    assert {item["phase"] for item in finish["lifecycle"]} >= {"helper-spawned", "request-started", "timeout"}
    assert audit["report_disk_writes"] == 0 and audit["physical_writes"] == []
    assert "fixture-private-network-value-do-not-record" not in json.dumps(audit) + str(failure.value)
