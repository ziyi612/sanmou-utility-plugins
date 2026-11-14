"""Read preexisting sources in place; delete only after independent cloud proof."""
from __future__ import annotations
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import zlib
from .cloud_store import CloudStore, MAX_RAW_CHUNK, sha, unpack
from .quark_cloud import CloudError

PROJECT_ROOT = Path(__file__).absolute().parents[1]
VERIFIER_COMPRESSED_CACHE_BYTES = 128 * 1024 * 1024


def _absolute(path):
    value = Path(path).expanduser()
    if ".." in value.parts:
        raise ValueError("Migration paths cannot contain parent traversal")
    return Path(os.path.abspath(value))


def _reject_links(path, *, allow_missing=False):
    # Inspect the caller's path before resolving any link or junction.
    for part in [path, *path.parents]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Migration does not follow symbolic links or reparse points")
    if path.exists() and path.is_file() and path.stat().st_nlink > 1:
        raise ValueError("Migration does not clean hard-linked source files")


def _workspace(root):
    root = _absolute(root)
    _reject_links(root)
    if not root.is_dir():
        raise ValueError("Migration requires an existing workspace")
    return root


def signature(path):
    info = path.stat()
    return info.st_size, info.st_mtime_ns


def file_sha(path):
    before = signature(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if signature(path) != before:
        raise CloudError("源文件在校验时发生变化，未授权清理该文件。")
    return digest.hexdigest()


def _safe(path, root, *, allow_missing=False):
    path, root = _absolute(path), _absolute(root)
    if root not in path.parents:
        raise ValueError("Migration source must stay inside its explicit root")
    _reject_links(path, allow_missing=allow_missing)
    return path


def report_roots(root):
    root = _workspace(root)
    return [root / name for name in (
        "data/incoming", "data/archive", "data/database", "data/cloud/cache",
        "capture/output/processed/battle_reports", "output/exports", "logs/capture", "logs/gui")]


def _root_identity(root):
    return sha(str(root).replace("\\", "/").casefold().encode())[:20]


def _history_prefix(root):
    return "history/" if root == PROJECT_ROOT else "history/" + _root_identity(root) + "/"


def _scope_id(root, roots):
    names = sorted(str(path).replace("\\", "/").casefold() for path in roots)
    return "project-history-v1-" + sha(json.dumps([str(root).casefold(), names]).encode())[:20]


def plan_sources(root, roots=None):
    root = _workspace(root)
    result = []
    selected = report_roots(root) if roots is None else list(roots)

    def add(path):
        if path.name == ".gitkeep":
            return
        path = _safe(path, root)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Migration only accepts regular source files")
        result.append({"path": path, "key": _history_prefix(root) + path.relative_to(root).as_posix(),
                       "signature": signature(path)})

    def walk_error(error):
        raise error

    for value in selected:
        base = _safe(value, root, allow_missing=True)
        if not base.exists():
            continue
        if base.is_file():
            add(base)
            continue
        if not base.is_dir():
            raise ValueError("Unsupported migration source")
        for parent, directories, files in os.walk(base, followlinks=False, onerror=walk_error):
            for name in directories:
                _safe(Path(parent) / name, root)
            for name in files:
                add(Path(parent) / name)
    unique = {item["path"]: item for item in result}
    return sorted(unique.values(), key=lambda item: item["key"])


def namespace_for(path, root):
    for parent in path.parents:
        if parent != root and root not in parent.parents:
            break
        binding = _safe(parent / "account.json", root, allow_missing=True)
        if binding.is_file():
            try:
                value = json.loads(binding.read_bytes())
                if isinstance(value.get("namespace"), str):
                    return value["namespace"]
            except (ValueError, OSError):
                pass
    return "legacy"


def _db_connection(path):
    wal, shm = Path(str(path) + "-wal"), Path(str(path) + "-shm")
    active_wal = wal.exists() and wal.stat().st_size > 0
    if active_wal and not shm.exists():
        raise CloudError("数据库含非空 WAL 但缺少共享辅助文件；已保留原件，不能以 immutable 忽略 WAL。")
    uri = path.as_uri() + ("?mode=ro" if active_wal else "?mode=ro&immutable=1")
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA temp_store=MEMORY")
    return connection


def _local_file(value, root):
    """Return only an existing regular, unlinked file inside the explicit root."""
    if not isinstance(value, (str, os.PathLike)) or not value:
        return None
    try:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = _safe(candidate, root)
        return candidate if candidate.is_file() else None
    except (OSError, ValueError):
        return None


def _existing_original(row, root):
    candidate = _local_file(row["source_path"], root)
    if candidate is None or candidate.stat().st_size != row["byte_count"]:
        return None
    before = signature(candidate)
    try:
        raw = candidate.read_bytes()
        if signature(candidate) == before and len(raw) == row["byte_count"] and sha(raw) == row["sha256"]:
            return raw
    except OSError:
        pass
    return None


def _old_original(stored, row, ref):
    if sha(stored) != ref["stored_sha256"] or len(stored) != ref["stored_byte_count"]:
        raise CloudError("旧云端原文压缩对象不匹配。")
    raw = unpack(stored, 128 * 1024 * 1024)
    if len(raw) != row["byte_count"] or sha(raw) != row["sha256"]:
        raise CloudError("旧数据库原始字节 SHA-256 不匹配。")
    if (("raw_byte_count" in ref.keys() and len(raw) != ref["raw_byte_count"])
            or ("raw_sha256" in ref.keys() and sha(raw) != ref["raw_sha256"])):
        raise CloudError("旧云端原文身份与归档记录不匹配。")
    return raw


def _existing_cached_original(row, ref, store, root):
    configurations = [store.config]
    config_path = _local_file(ref["config_path"] if "config_path" in ref.keys() else None, root)
    if config_path is not None and config_path.parent == root / "config" and config_path.stat().st_size <= 1024 * 1024:
        try:
            previous = json.loads(config_path.read_bytes())
            if isinstance(previous, dict):
                configurations.append(previous)
        except (OSError, ValueError):
            pass
    names = []
    digest = str(ref["stored_sha256"])
    if re.fullmatch(r"[0-9a-f]{64}", digest):
        names.append(digest + ".json.gz")
    object_name = str(ref["object_name"])
    if re.fullmatch(r"[0-9a-f]{64}\.json\.gz", object_name) and object_name not in names:
        names.append(object_name)
    for configuration in configurations:
        directory = configuration.get("cache_dir")
        if not isinstance(directory, str) or not directory:
            continue
        base = Path(directory)
        if not base.is_absolute():
            base = root / base
        for name in names:
            candidate = _local_file(base / name, root)
            if candidate is None or candidate.stat().st_size != ref["stored_byte_count"]:
                continue
            before = signature(candidate)
            try:
                stored = candidate.read_bytes()
                if signature(candidate) != before:
                    continue
                return _old_original(stored, row, ref)
            except (CloudError, OSError, ValueError, TypeError, zlib.error):
                # A bad or stale cache is never authoritative. The unchanged
                # legacy remote fallback must still supply the matching bytes.
                continue
    return None


def _db_rows(path, store, *, root):
    """Extract old inline/offloaded bytes under one consistent read snapshot."""
    root = _workspace(root)
    path = _safe(path, root)
    connection = _db_connection(path)
    try:
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "report_files" not in tables:
            return
        for row in connection.execute("SELECT id,namespace,sha256,byte_count,source_path,raw_bytes FROM report_files ORDER BY id"):
            raw = bytes(row["raw_bytes"])
            if not raw and row["byte_count"]:
                raw = _existing_original(row, root)
            if raw is None:
                if "report_cloud_files" not in tables:
                    raise CloudError("旧数据库缺少原文的已验证云端位置。")
                ref = connection.execute("SELECT * FROM report_cloud_files WHERE file_id=?", (row["id"],)).fetchone()
                if ref is None or ref["status"] != "verified":
                    raise CloudError("旧数据库原文尚未通过云端校验。")
                if "provider" in ref.keys() and ref["provider"] != "quark":
                    raise CloudError("无法用夸克读取其他提供方的旧原文。")
                if "codec" in ref.keys() and ref["codec"] != "gzip-v1":
                    raise CloudError("旧原文压缩格式不兼容。")
                raw = _existing_cached_original(row, ref, store, root)
                if raw is None:
                    stored = store.client.download_bytes(ref["remote_file_id"], max_bytes=16 * 1024 * 1024)
                    raw = _old_original(stored, row, ref)
            if sha(raw) != row["sha256"] or len(raw) != row["byte_count"]:
                raise CloudError("旧数据库原始字节 SHA-256 不匹配。")
            yield row["namespace"], row["sha256"], raw
    finally:
        connection.close()


def _snapshot_bytes(path):
    source, memory = _db_connection(path), sqlite3.connect(":memory:")
    try:
        memory.execute("PRAGMA temp_store=MEMORY")
        source.backup(memory)
        memory.execute("PRAGMA journal_mode=MEMORY")
        if memory.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise CloudError("当前数据库的一致性备份核验失败。")
        serialized = memory.serialize()
        if not serialized.startswith(b"SQLite format 3\x00") or serialized[18:20] not in (b"\x01\x01", b"\x02\x02"):
            raise CloudError("不兼容的 SQLite 内存快照格式。")
        # backup() has already merged committed WAL pages into this independent
        # image. The documented header bytes 18/19 select WAL (2) or a standalone
        # rollback-journal image (1); no source database bytes are modified.
        # https://www.sqlite.org/fileformat.html#file_format_version_numbers
        if serialized[18:20] == b"\x02\x02":
            serialized = serialized[:18] + b"\x01\x01" + serialized[20:]
        _validate_snapshot(serialized)
        return serialized
    finally:
        memory.close()
        source.close()


def _validate_snapshot(raw):
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("PRAGMA temp_store=MEMORY")
        probe.deserialize(raw)
        if probe.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise CloudError("独立 SQLite 内存快照核验失败。")
    except sqlite3.DatabaseError as error:
        raise CloudError("SQLite 内存快照无法独立恢复，保留本地数据库。") from error
    finally:
        probe.close()


def _assert_inventory(root, roots, sources):
    expected = {item["path"]: item["signature"] for item in sources}
    actual = {item["path"]: item["signature"] for item in plan_sources(root, roots)}
    if actual != expected:
        raise CloudError("源目录出现新增、缺失或变化的文件，采集/后台写入可能仍在运行；未授权本轮清理。")


def _verify_object(verifier, key, digest, size):
    info = verifier.catalog["files"].get(key)
    if not info or info["sha256"] != digest or info["byte_count"] != size:
        raise CloudError("独立云端清单缺少原件或身份不匹配；保留本地原件。")
    actual, length = hashlib.sha256(), 0
    for part in verifier.iter_file(key):
        actual.update(part)
        length += len(part)
    if length != size or actual.hexdigest() != digest:
        raise CloudError("独立整件云端 SHA-256 或长度不匹配；保留本地原件。")


def _verification_key(verifier, key):
    """Group verification reads; never alter source inventory/deletion order."""
    info = verifier.catalog["files"].get(key, {})
    blob = verifier.catalog["blobs"].get(info.get("sha256"), {})
    parts = blob.get("parts", [])
    first = parts[0].get("chunk", {}).get("stored_sha256", "") if parts else ""
    return first, key


def _migration_checkpoint(store, identity, value):
    previous = copy.deepcopy(store.catalog["migrations"].get(identity))
    existed = identity in store.catalog["migrations"]
    store.catalog["migrations"][identity] = value
    try:
        return store.checkpoint()
    except BaseException:
        if existed:
            store.catalog["migrations"][identity] = previous
        else:
            store.catalog["migrations"].pop(identity, None)
        raise


def migrate_local(store, root, *, roots=None, delete_after_verify=False, progress=None):
    with store.writer_lease():
        return _migrate_local(store, root, roots=roots, delete_after_verify=delete_after_verify, progress=progress)


def _migrate_local(store, root, *, roots=None, delete_after_verify=False, progress=None):
    root = _workspace(root)
    selected = report_roots(root) if roots is None else [_safe(value, root, allow_missing=True) for value in roots]
    migration_id = _scope_id(root, selected)
    progress = progress or (lambda value: None)
    store.refresh()
    initial = plan_sources(root, selected)
    databases = [item for item in initial if item["path"].suffix.lower() in (".db", ".sqlite", ".sqlite3")]
    expected_extra, batch, batch_size = {}, [], 0

    def flush():
        nonlocal batch, batch_size
        if batch:
            store.put_files(batch)
            batch, batch_size = [], 0

    # Finish SQLite reads before freezing physical sources: existing SHM
    # reader marks may change even on an otherwise read-only connection.
    for item in databases:
        path = item["path"]
        wal = Path(str(path) + "-wal")
        before = {p: signature(p) for p in (path, wal) if p.exists()}
        for namespace, digest, raw in _db_rows(path, store, root=root):
            key = "database-originals/" + sha(item["key"].encode())[:20] + "/" + sha(namespace.encode())[:20] + "/" + digest + ".json"
            expected_extra[key] = digest, len(raw)
            known = store.catalog["files"].get(key)
            if known and known["sha256"] == digest and known["byte_count"] == len(raw):
                continue
            if batch_size + len(raw) > MAX_RAW_CHUNK:
                flush()
            if len(raw) > MAX_RAW_CHUNK:
                store.put_file(key, raw, namespace=namespace)
            else:
                batch.append((key, raw, namespace, True))
                batch_size += len(raw)
        flush()
        if path == root / "data/database/battle_reports.db":
            serialized = _snapshot_bytes(path)
            key = "database-snapshots/" + _root_identity(root) + "/current-consistent.db"
            expected_extra[key] = sha(serialized), len(serialized)
            known = store.catalog["files"].get(key)
            if not known or known["sha256"] != sha(serialized) or known["byte_count"] != len(serialized):
                store.put_stream(key, io.BytesIO(serialized), len(serialized))
            del serialized
        if {p: signature(p) for p in (path, wal) if p.exists()} != before:
            raise CloudError("数据库或 WAL 在读取期间变化，保留本地数据库及辅助文件。")
        progress({"phase": "indexed_database", "cloud_reports": len(store.catalog["reports"])})

    sources = plan_sources(root, selected)
    old = {item["path"]: item["signature"] for item in initial if not item["path"].name.endswith("-shm")}
    new = {item["path"]: item["signature"] for item in sources if not item["path"].name.endswith("-shm")}
    if old != new:
        raise CloudError("准备迁移时源文件变化；未清理源文件。")
    total_bytes = sum(item["signature"][0] for item in sources)
    processed = 0

    def flush_sources():
        nonlocal processed
        count = len(batch)
        flush()
        processed += count
        progress({"phase": "uploaded", "processed_files": processed, "source_files": len(sources),
                  "source_bytes": total_bytes, "catalog_files": len(store.catalog["files"])})

    for item in sources:
        path, key = item["path"], item["key"]
        if signature(path) != item["signature"]:
            raise CloudError("本地源文件仍在变化；未清理源文件。")
        size = item["signature"][0]
        known = store.catalog["files"].get(key)
        if known and known["byte_count"] == size and known["sha256"] == file_sha(path):
            processed += 1
            continue
        if size > MAX_RAW_CHUNK:
            flush_sources()
            with path.open("rb") as stream:
                store.put_stream(key, stream, size)
            if signature(path) != item["signature"]:
                raise CloudError("上传期间源文件变化；未清理源文件。")
            processed += 1
            progress({"phase": "uploaded_large_file", "processed_files": processed,
                      "source_files": len(sources), "source_bytes": total_bytes})
        else:
            if batch_size + size > MAX_RAW_CHUNK:
                flush_sources()
            raw = path.read_bytes()
            if len(raw) != size or signature(path) != item["signature"]:
                raise CloudError("读取时源文件变化；未清理源文件。")
            batch.append((key, raw, namespace_for(path, root), path.suffix.lower() == ".json"))
            batch_size += size
    flush_sources()
    _assert_inventory(root, selected, sources)
    _migration_checkpoint(store, migration_id, {"source_files": len(sources), "source_bytes": total_bytes,
        "phase": "uploaded", "cloud_reports": len(store.catalog["reports"])})

    verifier = CloudStore(config_path=store.config.get("config_path"), client=store.client,
                          compressed_cache_bytes=VERIFIER_COMPRESSED_CACHE_BYTES)
    verifier.refresh()
    for key in sorted(expected_extra, key=lambda value: _verification_key(verifier, value)):
        digest, size = expected_extra[key]
        _verify_object(verifier, key, digest, size)
        if key.startswith("database-snapshots/"):
            _validate_snapshot(verifier.read_file(key, maximum=size))
    verification_sources = sorted(sources, key=lambda value: _verification_key(verifier, value["key"]))
    for number, item in enumerate(verification_sources, 1):
        _safe(item["path"], root)
        if signature(item["path"]) != item["signature"]:
            raise CloudError("清理前源文件发生变化；保留本地原件。")
        digest = file_sha(item["path"])
        _verify_object(verifier, item["key"], digest, item["signature"][0])
        if number % 250 == 0:
            progress({"phase": "verifying", "verified_files": number, "source_files": len(sources)})
    progress({"phase": "verification_complete", "verified_files": len(sources), "source_files": len(sources)})
    _assert_inventory(root, selected, sources)
    for item in sources:
        if file_sha(item["path"]) != verifier.catalog["files"][item["key"]]["sha256"]:
            raise CloudError("清理前原件内容变化，即使长度或时间戳相同也保留本地文件。")

    removed, released = 0, 0
    if delete_after_verify:
        for item in sources:
            path = _safe(item["path"], root)
            info = verifier.catalog["files"][item["key"]]
            if signature(path) != item["signature"] or file_sha(path) != info["sha256"]:
                raise CloudError("删除前源文件发生变化，后续清理已停止。")
            path.unlink()
            removed += 1
            released += item["signature"][0]
        if plan_sources(root, selected):
            raise CloudError("清理时产生了新的本地文件；尚未完成，请停止后台写入后重试。")
        directories = sorted({parent for item in sources for parent in item["path"].parents
                              if root in parent.parents}, key=lambda path: len(path.parts), reverse=True)
        for directory in directories:
            try:
                _safe(directory, root)
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    result = {"source_files": len(sources), "source_bytes": total_bytes, "verified_files": len(sources),
              "removed_files": removed, "released_bytes": released,
              "cloud_reports": len(store.catalog["reports"]), "catalog_fid": store.catalog_fid,
              "migration_id": migration_id,
              "phase": "complete" if delete_after_verify else "verified_local_retained"}
    _migration_checkpoint(store, migration_id, result)
    progress(result)
    return result
