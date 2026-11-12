#!/usr/bin/env python3
"""Mirror verified public release parts into one existing Drive upload session.

The short-lived upload capability is read only from BATTLE_DRIVE_UPLOAD_URL.
This program does not create sessions or change file sharing permissions.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit

import requests
from requests.adapters import HTTPAdapter


REPOSITORY_PREFIX = "/ziyi612/sanmou-utility-plugins/releases/download/"
QUANTUM = 256 * 1024
CHUNK = 32 * 1024 * 1024
MAX_PART = 1024 ** 3
MAX_RESPONSE = 1024 * 1024


class MirrorStopped(RuntimeError):
    """Only messages created by this module are safe to print."""


def _integer(value, label):
    if type(value) is not int or value <= 0:
        raise MirrorStopped(f"Invalid positive integer: {label}")
    return value


def _checksum(value, length, label):
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", value):
        raise MirrorStopped(f"Invalid checksum: {label}")
    return value.lower()


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise MirrorStopped("Invalid archive or part file name")
    return value


def validate_manifest(document):
    if not isinstance(document, dict) or not isinstance(document.get("archive"), dict):
        raise MirrorStopped("Manifest must contain archive and parts objects")
    source = document["archive"]
    archive = {"name": _name(source.get("name")),
               "size": _integer(source.get("size"), "archive.size"),
               "md5": _checksum(source.get("md5"), 32, "archive.md5"),
               "sha256": _checksum(source.get("sha256"), 64, "archive.sha256")}
    items = document.get("parts")
    if not isinstance(items, list) or not items:
        raise MirrorStopped("Manifest parts must be a nonempty ordered list")
    parts, names = [], set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict) or type(item.get("index")) is not int or item["index"] != index:
            raise MirrorStopped("Part indices must be consecutive and ordered from one")
        name = _name(item.get("name"))
        if name in names:
            raise MirrorStopped("Duplicate part name")
        names.add(name)
        size = _integer(item.get("size"), "part.size")
        if size > MAX_PART or (index < len(items) and size % QUANTUM):
            raise MirrorStopped("Parts must be at most 1 GiB; non-final sizes must be 256 KiB aligned")
        url = item.get("download_url")
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == "https" and parsed.hostname == "github.com"
                     and parsed.port in (None, 443) and parsed.username is None
                     and parsed.password is None and not parsed.query and not parsed.fragment
                     and parsed.path.startswith(REPOSITORY_PREFIX)
                     and len(parsed.path[len(REPOSITORY_PREFIX):].split("/")) == 2
                     and ".." not in unquote(parsed.path).split("/")
                     and unquote(parsed.path.rsplit("/", 1)[-1]) == name)
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise MirrorStopped("Part URL must be an HTTPS release asset in the expected public repository")
        parts.append({"index": index, "name": name, "size": size,
                      "md5": _checksum(item.get("md5"), 32, "part.md5"),
                      "sha256": _checksum(item.get("sha256"), 64, "part.sha256"),
                      "download_url": url})
    if sum(part["size"] for part in parts) != archive["size"]:
        raise MirrorStopped("Part sizes do not add up to archive.size")
    return {"archive": archive, "parts": parts}


def validate_session_url(value):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname == "www.googleapis.com"
                 and parsed.port in (None, 443) and parsed.username is None
                 and parsed.password is None and not parsed.fragment
                 and parsed.path == "/upload/drive/v3/files"
                 and "upload_id=" in parsed.query)
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise MirrorStopped("BATTLE_DRIVE_UPLOAD_URL must be a Google Drive HTTPS resumable session")
    return value


def confirmed_offset(headers, total):
    value = next((value for key, value in headers.items() if key.lower() == "range"), None)
    if value is None:
        return 0
    match = re.fullmatch(r"bytes=0-(\d+)", value.strip(), re.I)
    if not match or int(match.group(1)) + 1 > total:
        raise MirrorStopped("Drive returned an invalid confirmed Range")
    return int(match.group(1)) + 1


def transient(status, body):
    if status in {408, 429, 500, 502, 503, 504}:
        return True
    if status == 403:
        try:
            return any(error.get("reason") in {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}
                       for error in json.loads(body).get("error", {}).get("errors", [])
                       if isinstance(error, dict))
        except (ValueError, TypeError, AttributeError):
            return False
    return False


def make_session():
    session = requests.Session()
    session.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=2,
                                         max_retries=0, pool_block=True))
    session.headers.update({"Connection": "keep-alive", "User-Agent": "sanmou-database-mirror/1"})
    return session


class UploadBody(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, QUANTUM) if size >= 0 else QUANTUM)


class Mirror:
    def __init__(self, manifest, session_url, temporary_directory, session=None,
                 sleep=time.sleep, emit=None, chunk_size=CHUNK):
        self.manifest = validate_manifest(manifest)
        self.archive, self.parts = self.manifest["archive"], self.manifest["parts"]
        self.url = validate_session_url(session_url)
        self.temporary = Path(temporary_directory)
        self.session = session or make_session()
        self.sleep = sleep
        self.emit = emit or (lambda row: print(json.dumps(row, ensure_ascii=False), flush=True))
        if type(chunk_size) is not int or chunk_size < QUANTUM or chunk_size % QUANTUM:
            raise MirrorStopped("Upload chunks must be positive multiples of 256 KiB")
        self.chunk_size = min(chunk_size, CHUNK)
        self.starts = [0]
        for part in self.parts:
            self.starts.append(self.starts[-1] + part["size"])
        self.offset = self.resumed_from = self.failures = 0
        self.sent_payload = False
        self.started = time.monotonic()
        self.active_index, self.active_path = None, None
        self.result = None

    def retry(self, reason):
        self.failures += 1
        if self.failures > 5:
            raise MirrorStopped("Transfer failure budget exhausted; confirmed remote data retained")
        self.emit({"status": "retrying", "reason": reason, "attempt": self.failures,
                   "uploaded_bytes": self.offset})
        self.sleep(min(2 ** self.failures, 16))

    def request(self, data, content_range):
        self.sent_payload = self.sent_payload or bool(data)
        body = UploadBody(data) if data else b""
        try:
            with self.session.put(self.url, data=body,
                                  headers={"Content-Type": "application/octet-stream",
                                           "Content-Length": str(len(data)),
                                           "Content-Range": content_range},
                                  timeout=(120, 300), allow_redirects=False, stream=True) as response:
                pieces, size = [], 0
                for piece in response.iter_content(64 * 1024):
                    size += len(piece)
                    if size > MAX_RESPONSE:
                        raise MirrorStopped("Drive response exceeded the metadata size limit")
                    pieces.append(piece)
                return response.status_code, dict(response.headers), b"".join(pieces)
        finally:
            if isinstance(body, UploadBody):
                body.close()

    def finish(self, body):
        try:
            metadata = json.loads(body)
        except (TypeError, ValueError):
            raise MirrorStopped("Drive completion did not contain valid JSON metadata") from None
        if not isinstance(metadata, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(metadata.get("id", ""))):
            raise MirrorStopped("Drive completion has no valid file identifier")
        if (str(metadata.get("size")) != str(self.archive["size"])
                or str(metadata.get("md5Checksum", "")).lower() != self.archive["md5"]
                or str(metadata.get("sha256Checksum", "")).lower() != self.archive["sha256"]):
            raise MirrorStopped("Drive final size, MD5 or SHA256 did not match the archive")
        allowed = {"id", "name", "size", "md5Checksum", "sha256Checksum"}
        receipt = {key: value for key, value in metadata.items() if key in allowed}
        link = metadata.get("webViewLink")
        if isinstance(link, str) and urlsplit(link).scheme == "https" and urlsplit(link).hostname == "drive.google.com":
            receipt["webViewLink"] = link
        receipt.update(status="complete", verified=True, size_verified=True, md5_verified=True,
                       sha256_verified=True,
                       resumed_from_bytes=(self.resumed_from if self.sent_payload else self.archive["size"]),
                       elapsed_seconds=round(time.monotonic() - self.started, 2))
        self.offset = self.archive["size"]
        self.result = receipt
        self.emit(receipt)
        return receipt

    def probe(self, maximum=None):
        while True:
            try:
                status, headers, body = self.request(b"", f"bytes */{self.archive['size']}")
            except requests.RequestException:
                self.retry("status_probe_transport")
                continue
            if status in {200, 201}:
                return self.finish(body)
            if status == 308:
                acknowledged = confirmed_offset(headers, self.archive["size"])
                if acknowledged < self.offset or (maximum is not None and acknowledged > maximum):
                    raise MirrorStopped("Drive acknowledgement regressed or exceeded this writer's sent range")
                self.offset = acknowledged
                return None
            if not transient(status, body):
                raise MirrorStopped(f"Drive status probe returned HTTP {status}")
            self.retry("status_probe_http")

    def download_part(self, index):
        if self.active_index == index:
            return self.active_path
        if self.active_path is not None:
            self.active_path.unlink()
            self.active_index, self.active_path = None, None
        part = self.parts[index]
        path = self.temporary / f"part-{index + 1:06d}"
        self.emit({"status": "downloading_part", "part": index + 1,
                   "parts": len(self.parts), "part_bytes": part["size"]})
        while True:
            if shutil.disk_usage(self.temporary).free < part["size"] + 64 * 1024 * 1024:
                raise MirrorStopped("Insufficient temporary space for one verified part")
            try:
                with self.session.get(part["download_url"], stream=True, timeout=(30, 180),
                                      allow_redirects=True) as response:
                    if response.status_code != 200:
                        if response.status_code in {408, 429, 500, 502, 503, 504}:
                            self.retry("download_http")
                            continue
                        raise MirrorStopped(f"Release download returned HTTP {response.status_code}")
                    md5, sha, count = hashlib.md5(), hashlib.sha256(), 0
                    with path.open("xb") as output:
                        for block in response.iter_content(1024 * 1024):
                            count += len(block)
                            if count > part["size"]:
                                raise MirrorStopped("Release part exceeded its declared size")
                            output.write(block)
                            md5.update(block)
                            sha.update(block)
                if (count != part["size"] or md5.hexdigest() != part["md5"]
                        or sha.hexdigest() != part["sha256"]):
                    raise MirrorStopped("Release part size or checksum verification failed; no data from this download sent")
                self.active_index, self.active_path = index, path
                return path
            except requests.RequestException:
                path.unlink(missing_ok=True)
                self.retry("download_transport")

    def read_at(self, offset, size):
        pieces, remaining = [], size
        while remaining:
            index = bisect.bisect_right(self.starts, offset) - 1
            if index >= len(self.parts):
                raise MirrorStopped("Unexpected end of the archive parts")
            path = self.download_part(index)
            count = min(remaining, self.starts[index + 1] - offset)
            with path.open("rb") as source:
                source.seek(offset - self.starts[index])
                data = source.read(count)
            if len(data) != count:
                raise MirrorStopped("Verified local part changed or was truncated")
            pieces.append(data)
            offset += count
            remaining -= count
        return b"".join(pieces)

    def run(self):
        # A zero-byte probe is the first network operation, even on a new session.
        if self.probe() is not None:
            return self.result
        self.resumed_from = self.offset
        while self.offset < self.archive["size"]:
            start = self.offset
            data = self.read_at(start, min(self.chunk_size, self.archive["size"] - start))
            end = start + len(data)
            uncertain = False
            try:
                status, headers, body = self.request(data, f"bytes {start}-{end - 1}/{self.archive['size']}")
            except requests.RequestException:
                uncertain = True
            else:
                if status in {200, 201}:
                    return self.finish(body)
                if status == 308:
                    acknowledged = confirmed_offset(headers, self.archive["size"])
                    if not start <= acknowledged <= end:
                        raise MirrorStopped("Drive acknowledgement is outside the current chunk")
                    self.offset = acknowledged
                    uncertain = self.offset == start
                elif transient(status, body):
                    uncertain = True
                else:
                    raise MirrorStopped(f"Drive payload upload returned HTTP {status}")
            if uncertain:
                self.retry("payload_uncertain")
                if self.probe(maximum=end) is not None:
                    return self.result
            self.emit({"status": "uploading", "uploaded_bytes": self.offset,
                       "total_bytes": self.archive["size"],
                       "percent": round(self.offset / self.archive["size"] * 100, 2)})
        if self.probe() is not None:
            return self.result
        raise MirrorStopped("All bytes acknowledged but final metadata is not yet verified; resume later")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("data/releases/2026-11-13.json"))
    parser.add_argument("--temp-dir", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    manifest_path = args.manifest.resolve()
    try:
        manifest_path.relative_to((root / "data/releases").resolve())
    except ValueError:
        raise MirrorStopped("Manifest must be inside this repository's data/releases directory") from None
    document = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    temporary_root = args.temp_dir or Path(os.environ.get("RUNNER_TEMP", root / ".runtime/mirror"))
    temporary_root.mkdir(parents=True, exist_ok=True)
    result_path = args.result or temporary_root / "drive-mirror-result.json"
    session_url = os.environ.get("BATTLE_DRIVE_UPLOAD_URL", "")
    if not session_url:
        raise MirrorStopped("BATTLE_DRIVE_UPLOAD_URL is not configured")
    with tempfile.TemporaryDirectory(prefix="sanmou-drive-mirror-", dir=temporary_root) as temporary:
        mirror = Mirror(document, session_url, temporary)
        try:
            result = mirror.run()
        finally:
            mirror.session.close()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write("### Database mirror verified\n\n```json\n")
            output.write(json.dumps(result, ensure_ascii=False, indent=2))
            output.write("\n```\n")
    return 0


def entrypoint(argv=None):
    try:
        return main(argv)
    except KeyboardInterrupt:
        print(json.dumps({"status": "stopped", "error": "Interrupted; remote confirmed data retained"}), file=sys.stderr)
        return 2
    except Exception as error:
        # Never render Requests exceptions, tracebacks or the capability URL.
        message = str(error) if isinstance(error, MirrorStopped) else type(error).__name__
        print(json.dumps({"status": "stopped", "error": message}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(entrypoint())
