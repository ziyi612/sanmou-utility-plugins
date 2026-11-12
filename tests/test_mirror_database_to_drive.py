"""Small fake-HTTP tests; no external requests or real database payloads."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest
import requests


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mirror_database", ROOT / "tools/mirror_database_to_drive.py")
mirror = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mirror)
SECRET_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=PRIVATE_SESSION_TOKEN"
Q = 256 * 1024


def hashes(data):
    return {"size": len(data), "md5": hashlib.md5(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest()}


def fixture_manifest(chunks=None):
    chunks = chunks or [b"a" * Q, b"b" * 37]
    parts, bodies = [], {}
    for index, data in enumerate(chunks, 1):
        name = f"battle.tar.zst.part{index:03d}"
        url = f"https://github.com/ziyi612/sanmou-utility-plugins/releases/download/v0.1.0/{name}"
        parts.append({"index": index, "name": name, "download_url": url, **hashes(data)})
        bodies[url] = data
    joined = b"".join(chunks)
    return {"archive": {"name": "battle.tar.zst", **hashes(joined)}, "parts": parts}, bodies, joined


def metadata(manifest):
    archive = manifest["archive"]
    return {"id": "file_id", "name": archive["name"], "size": str(archive["size"]),
            "md5Checksum": archive["md5"], "sha256Checksum": archive["sha256"],
            "webViewLink": "https://drive.google.com/file/d/file_id/view"}


class Response:
    def __init__(self, status, body=b"", headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


class Session:
    def __init__(self, bodies, responses):
        self.bodies, self.responses = bodies, list(responses)
        self.calls = []

    def put(self, url, data, headers, **kwargs):
        if hasattr(data, "read"):
            pieces = []
            while True:
                value = data.read()
                if not value:
                    break
                pieces.append(value)
            body = b"".join(pieces)
        else:
            body = data
        self.calls.append(("put", body, headers["Content-Range"]))
        assert not kwargs["allow_redirects"]
        assert len(body) == int(headers["Content-Length"])
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        self.calls.append(("get", url))
        return Response(200, self.bodies[url])

    def close(self):
        pass


def instance(tmp_path, manifest, session, events=None):
    return mirror.Mirror(manifest, SECRET_URL, tmp_path, session=session,
                         sleep=lambda _: None, emit=(events or []).append, chunk_size=Q)


def test_full_transfer_probes_first_and_verifies_parts_and_final_metadata(tmp_path):
    manifest, bodies, joined = fixture_manifest()
    session = Session(bodies, [Response(308), Response(308, headers={"Range": f"bytes=0-{Q-1}"}),
                               Response(200, json.dumps(metadata(manifest)).encode())])
    result = instance(tmp_path, manifest, session).run()
    assert session.calls[0] == ("put", b"", f"bytes */{len(joined)}")
    assert [call[1] for call in session.calls if call[0] == "put" and call[1]] == [joined[:Q], joined[Q:]]
    assert result["verified"] and result["md5_verified"] and result["sha256_verified"]
    assert len(list(tmp_path.iterdir())) == 1  # At most one active part is retained.


def test_bad_part_sha_never_sends_its_payload(tmp_path):
    manifest, bodies, _ = fixture_manifest()
    manifest["parts"][0]["sha256"] = "0" * 64
    session = Session(bodies, [Response(308)])
    with pytest.raises(mirror.MirrorStopped, match="checksum"):
        instance(tmp_path, manifest, session).run()
    assert not any(call[0] == "put" and call[1] for call in session.calls)


def test_uncertain_put_probes_then_resumes_at_server_offset_across_parts(tmp_path):
    manifest, bodies, joined = fixture_manifest()
    session = Session(bodies, [Response(308), requests.Timeout(SECRET_URL),
                               Response(308, headers={"Range": f"bytes=0-{Q//2-1}"}),
                               Response(200, json.dumps(metadata(manifest)).encode())])
    result = instance(tmp_path, manifest, session).run()
    puts = [call for call in session.calls if call[0] == "put"]
    assert puts[2] == ("put", b"", f"bytes */{len(joined)}")
    assert puts[3][1] == joined[Q//2:]
    assert puts[3][2] == f"bytes {Q//2}-{len(joined)-1}/{len(joined)}"
    assert result["verified"]


def test_failed_probes_never_trigger_another_payload_send(tmp_path):
    manifest, bodies, _ = fixture_manifest()
    session = Session(bodies, [Response(308)] + [requests.Timeout(SECRET_URL)] * 8)
    with pytest.raises(mirror.MirrorStopped, match="budget"):
        instance(tmp_path, manifest, session).run()
    assert len([call for call in session.calls if call[0] == "put" and call[1]]) == 1


def test_regressing_probe_stops_without_resending(tmp_path):
    manifest, bodies, _ = fixture_manifest([b"a" * (2 * Q), b"c" * 37])
    session = Session(bodies, [Response(308), Response(308, headers={"Range": f"bytes=0-{Q-1}"}),
                               requests.Timeout(SECRET_URL), Response(308, headers={"Range": "bytes=0-1"})])
    with pytest.raises(mirror.MirrorStopped, match="regressed"):
        instance(tmp_path, manifest, session).run()
    assert len([call for call in session.calls if call[0] == "put" and call[1]]) == 2


def test_range_outside_current_chunk_is_rejected(tmp_path):
    manifest, bodies, _ = fixture_manifest()
    session = Session(bodies, [Response(308), Response(308, headers={"Range": f"bytes=0-{Q}"})])
    with pytest.raises(mirror.MirrorStopped, match="outside"):
        instance(tmp_path, manifest, session).run()


@pytest.mark.parametrize("field,value", [("size", "1"), ("md5Checksum", "0" * 32),
                                         ("sha256Checksum", "0" * 64), ("sha256Checksum", None)])
def test_all_three_final_metadata_values_are_required(tmp_path, field, value):
    manifest, bodies, _ = fixture_manifest()
    final = metadata(manifest)
    final[field] = value
    session = Session(bodies, [Response(200, json.dumps(final).encode())])
    with pytest.raises(mirror.MirrorStopped, match="final size, MD5 or SHA256"):
        instance(tmp_path, manifest, session).run()
    assert len(session.calls) == 1


def test_already_complete_session_downloads_nothing(tmp_path):
    manifest, bodies, _ = fixture_manifest()
    session = Session(bodies, [Response(200, json.dumps(metadata(manifest)).encode())])
    result = instance(tmp_path, manifest, session).run()
    assert len(session.calls) == 1
    assert result["resumed_from_bytes"] == manifest["archive"]["size"]


@pytest.mark.parametrize("url", ["http://github.com/ziyi612/sanmou-utility-plugins/releases/download/v0.1.0/x",
                                 "https://evil.example/x",
                                 "https://github.com/other/repo/releases/download/v0.1.0/x"])
def test_manifest_rejects_unexpected_release_sources(url):
    manifest, _, _ = fixture_manifest()
    manifest["parts"][0]["download_url"] = url
    with pytest.raises(mirror.MirrorStopped, match="expected public repository"):
        mirror.validate_manifest(manifest)


def test_requests_file_body_has_exact_content_length_not_chunked_encoding():
    body = mirror.UploadBody(b"x" * Q)
    prepared = requests.Request("PUT", SECRET_URL, data=body,
                                headers={"Content-Length": str(Q)}).prepare()
    assert prepared.headers["Content-Length"] == str(Q)
    assert "Transfer-Encoding" not in prepared.headers
    body.close()


def test_cli_does_not_render_transport_secrets(monkeypatch, capsys):
    def fail(_):
        raise requests.Timeout("Private transport details: " + SECRET_URL)
    monkeypatch.setattr(mirror, "main", fail)
    assert mirror.entrypoint([]) == 2
    captured = capsys.readouterr()
    assert SECRET_URL not in captured.err + captured.out
    assert "PRIVATE_SESSION_TOKEN" not in captured.err + captured.out
    assert "Timeout" in captured.err
