#!/usr/bin/env python3
"""Verify or restore a streamed SQLite DB+WAL backup. Requires zstandard."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import time

import zstandard

FORMAT = "codex-sqlite-cold-backup-v1"
DATA_NAMES = ("battle_reports.db", "battle_reports.db-wal")
MANIFEST_NAME = "manifest.json"
CHUNK = 1024 * 1024
MAX_MANIFEST = 1024 * 1024
RESTORE_RESERVE = 256 * 1024 * 1024


def _is_reparse(path: Path) -> bool:
    value = path.lstat()
    return path.is_symlink() or bool(getattr(value, "st_file_attributes", 0) & 0x400)


def _validate_manifest(manifest: dict, observed: dict) -> None:
    if manifest.get("format") != FORMAT or manifest.get("source_shm_excluded") is not True:
        raise ValueError("Unsupported backup format or inconsistent SHM policy")
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) != 2:
        raise ValueError("Manifest must describe exactly the DB and its WAL")
    expected = {}
    for rec in records:
        if not isinstance(rec, dict) or rec.get("name") not in DATA_NAMES:
            raise ValueError("Unsafe or unexpected manifest file name")
        name = rec["name"]
        if name in expected:
            raise ValueError("Duplicate manifest file")
        if type(rec.get("size")) is not int or rec["size"] < 0:
            raise ValueError("Invalid manifest size")
        if type(rec.get("mtime_ns")) is not int or rec["mtime_ns"] < 0:
            raise ValueError("Invalid manifest timestamp")
        if not isinstance(rec.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", rec["sha256"]):
            raise ValueError("Invalid manifest SHA256")
        expected[name] = rec
    if set(expected) != set(DATA_NAMES) or set(observed) != set(DATA_NAMES):
        raise ValueError("DB+WAL member set differs from the manifest")
    for name, rec in expected.items():
        if observed[name]["size"] != rec["size"] or observed[name]["sha256"] != rec["sha256"]:
            raise ValueError(f"SHA256 or size mismatch: {name}")


def _scan_archive(archive_path: Path, destination: Path | None = None, expected_manifest: dict | None = None):
    started = time.monotonic()
    before = archive_path.stat()
    observed = {}
    manifest = None
    seen = set()
    first_expected = {r["name"]: r for r in expected_manifest["files"]} if expected_manifest else None
    with archive_path.open("rb") as compressed:
        with zstandard.ZstdDecompressor().stream_reader(compressed, read_across_frames=True) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                for member in archive:
                    name = member.name
                    if name not in (*DATA_NAMES, MANIFEST_NAME) or not member.isfile() or member.issparse():
                        raise ValueError(f"Unsafe or unexpected tar member: {name!r}")
                    if name in seen or manifest is not None:
                        raise ValueError("Duplicate member or member after the final manifest")
                    seen.add(name)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError("Unable to read tar member")
                    if name == MANIFEST_NAME:
                        if set(observed) != set(DATA_NAMES) or member.size > MAX_MANIFEST:
                            raise ValueError("Manifest must follow both data files and be at most 1 MiB")
                        manifest = json.loads(source.read())
                        _validate_manifest(manifest, observed)
                        if expected_manifest is not None and manifest != expected_manifest:
                            raise ValueError("Archive manifest changed between verification and restore")
                        continue
                    if first_expected and member.size != first_expected[name]["size"]:
                        raise ValueError("Archive size changed between verification and restore")
                    digest = hashlib.sha256()
                    count = 0
                    sink = None
                    try:
                        if destination is not None:
                            if _is_reparse(destination):
                                raise ValueError("Restore directory became a reparse point")
                            sink = (destination / name).open("xb")
                        while True:
                            chunk = source.read(CHUNK)
                            if not chunk:
                                break
                            if sink is not None:
                                if shutil.disk_usage(destination).free < RESTORE_RESERVE + len(chunk):
                                    raise OSError("Restore stopped before using its 256 MiB disk reserve")
                                sink.write(chunk)
                            digest.update(chunk)
                            count += len(chunk)
                    finally:
                        if sink is not None:
                            sink.close()
                    if count != member.size:
                        raise ValueError("Truncated tar member")
                    observed[name] = {"size": count, "sha256": digest.hexdigest()}
                    if first_expected and observed[name]["sha256"] != first_expected[name]["sha256"]:
                        raise ValueError("Restored file differs from the previously checked payload")
            # tarfile stops at its end marker. Drain padding and the zstd frame so
            # zstandard also validates the frame checksum and truncated trailers.
            for padding in iter(lambda: reader.read(CHUNK), b""):
                if any(padding):
                    raise ValueError("Unexpected nonzero content after the tar end marker")
    if manifest is None or seen != {*DATA_NAMES, MANIFEST_NAME}:
        raise ValueError("Incomplete archive: DB, WAL and final manifest are required")
    after = archive_path.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise ValueError("Archive changed while it was being read")
    summary = {
        "format": FORMAT,
        "archive": str(archive_path),
        "archive_bytes": after.st_size,
        "file_count": 2,
        "logical_bytes": sum(r["size"] for r in manifest["files"]),
        "files": manifest["files"],
        "member_set_exact": True,
        "all_file_sha256_and_size_match": True,
        "source_shm_excluded": True,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    return manifest, summary


def check_archive(archive_path):
    return _scan_archive(Path(archive_path))[1]


def _new_target(target) -> Path:
    target = Path(os.path.abspath(target))
    if target == target.parent or target.exists() or target.is_symlink():
        raise FileExistsError("Restore destination must be a new directory; overwrite is refused")
    current = target.parent
    while True:
        if (current.exists() or current.is_symlink()) and _is_reparse(current):
            raise ValueError("Restore destination has a symbolic-link or reparse-point ancestor")
        if current == current.parent:
            break
        current = current.parent
    return target


def restore_archive(archive_path, destination):
    archive_path = Path(archive_path)
    target = _new_target(destination)
    identity_before = archive_path.stat()
    manifest, checked = _scan_archive(archive_path)
    probe = target.parent
    while not probe.exists():
        probe = probe.parent
    required = checked["logical_bytes"] + RESTORE_RESERVE
    if shutil.disk_usage(probe).free < required:
        raise OSError(f"Insufficient destination space: need at least {required} bytes including reserve")
    target = _new_target(target)
    identity_after = archive_path.stat()
    if identity_before.st_size != identity_after.st_size or identity_before.st_mtime_ns != identity_after.st_mtime_ns:
        raise ValueError("Archive changed after initial verification")
    target.mkdir(parents=True, exist_ok=False)
    _, restored = _scan_archive(archive_path, destination=target, expected_manifest=manifest)
    for rec in manifest["files"]:
        output = target / rec["name"]
        if _is_reparse(output):
            raise ValueError("Restored file became a symbolic link or reparse point")
        os.utime(output, ns=(rec["mtime_ns"], rec["mtime_ns"]))
        if output.stat().st_mtime_ns != rec["mtime_ns"]:
            raise OSError("Destination filesystem could not preserve the exact file timestamp")
    return {**restored, "destination": str(target), "restored_files": 2, "overwritten_files": 0, "restored_mtime_ns_match": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "check", "restore"))
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", nargs="?", type=Path)
    args = parser.parse_args()
    if args.command == "restore":
        if args.destination is None:
            parser.error("restore requires a new destination directory")
        result = restore_archive(args.archive, args.destination)
    else:
        result = check_archive(args.archive)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
