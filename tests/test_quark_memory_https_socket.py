"""Real local TLS and pipe contracts; no cloud account or global CA changes."""
from __future__ import annotations

import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _full_runner_probe(directory, node, wrapper, payload):
    """Exercise the production overlay, stdout interception and lifecycle audit."""
    from parser.quark_memory_client import MemoryQuarkClient

    sdk = directory / "official-fixture/scripts/quark-drive.cjs"
    sdk.parent.mkdir(parents=True)
    sdk.write_text(r'''
const fs=require('node:fs');const path=require('node:path');const https=require('node:https');
const args=process.argv.slice(2);const output=args[args.indexOf('--output-dir')+1];
const request=https.request({hostname:'open-api-drive.quark.cn',port:443,method:'POST',
 path:'/open/v1/file/get_download_url?access_token=local-synthetic',headers:{Connection:'keep-alive'}},response=>{
 const chunks=[];response.on('data',chunk=>chunks.push(chunk));response.on('end',()=>{
  const target=path.join(output,'socket-fixture.bin');fs.writeFileSync(target,Buffer.concat(chunks));
  console.log(JSON.stringify({type:'result',code:0,data:{filePath:target}}));process.exit(0);
 });
});
request.on('error',error=>{console.log(JSON.stringify({type:'result',code:9,msg:error.code}));process.exit(1);});
request.setTimeout(5000);request.end('{"fid":"synthetic TLS socket fixture"}');
''', encoding="utf-8")
    preload = directory / "local-helper-preload.cjs"
    preload.write_text("const child=require('node:child_process');const original=child.spawn;\n"
        "child.spawn=function(command,args,options){return original(command,args.map(value=>\n"
        f"String(value).endsWith('quark_memory_https.py')?{json.dumps(str(wrapper))}:value),options);}};\n",
        encoding="utf-8")
    client = MemoryQuarkClient(config={"cli_command": [node, "--require", str(preload), str(sdk)],
        "folder_id": "local-synthetic", "temp_dir": str(directory), "cli_env": {
            "SANMOU_QUARK_MEMORY_HTTPS": "urllib", "SANMOU_QUARK_PYTHON": sys.executable}})
    assert client.download_bytes("local-synthetic") == payload
    audit = client.last_audit
    assert audit["https_adapter"] == "urllib-allowlist"
    assert audit["report_disk_writes"] == 0 and audit["physical_writes"] == []
    assert audit["network"][0]["endpoint"] == "download-url"
    assert {item["phase"] for item in audit["network"][0]["lifecycle"]} >= {
        "stdin-complete", "request-sent", "response-headers", "response-body", "helper-exit", "sdk-response"}
    assert "local-synthetic" not in json.dumps(audit)
    assert not (ROOT / ".ram-quark").exists()


def _certificates(directory):
    crypto = pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic RAM transport test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                      data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                      crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "open-api-drive.quark.cn")])
    leaf = (x509.CertificateBuilder().subject_name(leaf_name).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("open-api-drive.quark.cn")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_path, cert_path, key_path = [directory / name for name in ("test-ca.pem", "test-server.pem", "test-key.pem")]
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    return ca_path, cert_path, key_path


@pytest.mark.parametrize("framing,ram_fs,fault", [
    ("content-length", False, None), ("chunked", False, None),
    ("content-length", True, None), ("chunked", True, None),
    ("content-length", False, "untrusted"), ("content-length", False, "hostname"),
    ("content-length", True, "timeout"), ("content-length", True, "redirect"),
])
def test_real_tls_private_pipes_complete_without_waiting_for_keepalive_eof(tmp_path, framing, ram_fs, fault):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is optional")
    memfs = ROOT / ".runtime/quark-memory/node_modules/memfs"
    if ram_fs and not memfs.is_dir():
        pytest.skip("RAM Node dependencies are optional")
    ca_path, cert_path, key_path = _certificates(tmp_path)
    received = []
    payload = b"real TLS bytes\0\xff" * 8192
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_args):
            pass
        def do_POST(self):
            self.connection.settimeout(8)
            received.append((self.path, self.headers, self.rfile.read(int(self.headers["Content-Length"]))))
            if fault == "timeout":
                release.wait(4)
                self.close_connection = True
                return
            self.send_response(302 if fault == "redirect" else 200)
            if fault == "redirect":
                self.send_header("Location", "https://must-not-follow.invalid/synthetic-redirect")
            self.send_header("Connection", "keep-alive")
            self.send_header("Content-Length" if framing == "content-length" else "Transfer-Encoding",
                             str(len(payload)) if framing == "content-length" else "chunked")
            self.end_headers()
            if framing == "content-length":
                self.wfile.write(payload)
            else:
                for start in range(0, len(payload), 4096):
                    part = payload[start:start + 4096]
                    self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            self.close_connection = False

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_path), str(key_path))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wrapper = tmp_path / "local_tls_helper.py"
    wrapper.write_text(
        "import http.client, runpy, socket, ssl\n"
        f"scope=runpy.run_path({str(ROOT / 'tools/quark_memory_https.py')!r})\n"
        "original_context=ssl.create_default_context\n" +
        ("" if fault == "untrusted" else
         f"ssl.create_default_context=lambda *a,**k:original_context(cafile={str(ca_path)!r})\n") +
        "def local_connect(self):\n"
        f" raw=socket.create_connection(('127.0.0.1',{server.server_port}),timeout=self.timeout)\n"
        f" self.sock=self._context.wrap_socket(raw,server_hostname={'wrong-host.invalid'!r} if {fault == 'hostname'!r} else self.host)\n"
        "http.client.HTTPSConnection.connect=local_connect\n"
        "scope['main']()\n", encoding="utf-8")
    script = r'''
const https=require('node:https');
const crypto=require('node:crypto');
const bridge=require(process.argv[1]);
bridge.installMemoryHttpsBridge({mode:'urllib',python:process.argv[2],helper:process.argv[3]});
if(process.argv[4]==='true'){
 const fs=require('node:fs');
 const memfs=require(process.argv[5]+'/memfs');
 const {patchFs}=require(process.argv[5]+'/fs-monkey');
 const ram=memfs.createFsFromVolume(new memfs.Volume());
 delete ram.constants;delete ram.F_OK;delete ram.R_OK;delete ram.W_OK;delete ram.X_OK;
 patchFs(ram);
}
const phases=[];
const request=https.request({hostname:'open-api-drive.quark.cn',port:443,path:'/local-only?opaque=fixture',method:'POST',
 headers:{Connection:'keep-alive','x-Signature-Original':'same','Content-Type':'application/json'}},response=>{
 const chunks=[];response.on('data',chunk=>chunks.push(chunk));
 response.on('end',()=>{const bytes=Buffer.concat(chunks);console.log(JSON.stringify({status:response.statusCode,
  bytes:bytes.length,hash:crypto.createHash('sha256').update(bytes).digest('hex'),phases}));});
});
request.on('quark-memory-https-phase',value=>phases.push(value));
request.on('error',error=>{console.log(JSON.stringify({error:error.code,phases}));process.exitCode=1;});
request.setTimeout(Number(process.argv[6]));request.write('{"fid":');request.end('"synthetic TLS socket fixture"}');
'''
    started = time.monotonic()
    try:
        completed = subprocess.run([node, "-e", script, str(ROOT / "tools/quark_memory_https.cjs"),
            sys.executable, str(wrapper), str(ram_fs).lower(), str(memfs.parent),
            "1500" if fault == "timeout" else "5000"], cwd=ROOT,
            env=dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path), NODE_DISABLE_COMPILE_CACHE="1"),
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        result = json.loads(completed.stdout)
        if fault in {"untrusted", "hostname", "timeout"}:
            assert completed.returncode == 1, completed.stdout + completed.stderr
            assert result["error"] == ("ETIMEDOUT" if fault == "timeout" else "TLS_VERIFICATION_FAILED")
            assert "sdk-response" not in {item["phase"] for item in result["phases"]}
            if fault == "timeout":
                assert {item["phase"] for item in result["phases"]} >= {"request-sent", "timeout"}
            return
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert result["status"] == (302 if fault == "redirect" else 200) and result["bytes"] == len(payload)
        assert result["hash"] == hashlib.sha256(payload).hexdigest()
        assert time.monotonic() - started < 5, "Response waited for keepalive socket EOF"
        assert received[0][0] == "/local-only?opaque=fixture"
        assert received[0][2] == b'{"fid":"synthetic TLS socket fixture"}'
        assert received[0][1]["Connection"] == "keep-alive"
        assert received[0][1]["x-Signature-Original"] == "same"
        assert received[0][1]["Host"] == "open-api-drive.quark.cn"
        assert {item["phase"] for item in result["phases"]} >= {
            "stdin-complete", "request-sent", "response-headers", "response-body", "sdk-response"}
        if ram_fs and framing == "content-length" and fault is None:
            _full_runner_probe(tmp_path, node, wrapper, payload)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
