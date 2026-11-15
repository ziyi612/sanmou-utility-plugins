"""Preserve existing large archives through bounded RAM and independent proof.

The archive format stays opaque. Source bytes are split into standard cloud
parts, without extracting archives or creating local manifests or caches.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import gzip
import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat

from .cloud_migration import _root_identity, _safe, _workspace, file_sha, signature
from .cloud_store import CloudStore, encode, sha
from .quark_cloud import CloudError, load_config

# The official download-url endpoint enforces a 50 MiB object ceiling for
# supported accounts. Leave room for gzip framing around opaque raw bytes.
COLD_RAW_BYTES = 48 * 1024 * 1024
COLD_STORED_BYTES = 50 * 1024 * 1024
VERIFIER_CACHE_BYTES = 128 * 1024 * 1024
DEFAULT_FOLDER_NAME = "旧完整战报归档"
COLD_NAMESPACE = "cold-archive"


def _emit(progress, phase, **counts):
    if progress is not None:
        progress({"phase": phase, **counts})


def _relative(value):
    text = str(value).replace("\\", "/")
    path = PurePosixPath(text)
    if (not text or path.is_absolute() or PureWindowsPath(text).drive
            or ":" in text or ".." in text.split("/") or path.as_posix() == "."):
        raise ValueError("Expected hashes require a relative path inside the source root")
    return path.as_posix()


def _preflight(root, sources, expected, progress):
    root = _workspace(root)
    values = list(sources)
    if not values:
        raise ValueError("At least one explicit source file is required")
    normalized = {}
    for name, digest in (expected or {}).items():
        name = _relative(name)
        if name in normalized and normalized[name] != digest:
            raise ValueError("Conflicting expected hashes identify the same source file")
        normalized[name] = digest
    expected = normalized
    if any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
           for digest in expected.values()):
        raise ValueError("Expected SHA-256 must contain exactly 64 hexadecimal digits")
    entries, seen = [], set()
    for value in values:
        value = Path(value)
        if ":" in str(value).replace(value.anchor, "", 1):
            raise ValueError("Archive source paths cannot contain alternate stream names")
        path = _safe(value if value.is_absolute() else root / value, root)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Cold archives require explicit regular source files")
        if path in seen:
            continue
        seen.add(path)
        relative = path.relative_to(root).as_posix()
        before = signature(path)
        digest = file_sha(path)
        if signature(path) != before:
            raise CloudError("归档源文件在预检时发生变化，尚未执行云端写入。")
        if relative in expected and digest != expected[relative].lower():
            raise CloudError("归档源文件 SHA-256 与预期不一致，尚未执行云端写入。")
        entries.append({"path": path, "relative": relative, "signature": before,
                        "sha256": digest, "byte_count": before[0],
                        "key": "cold-archives/" + _root_identity(root) + "/" + digest + "/" + relative})
        _emit(progress, "preflight_file", processed_files=len(entries),
              processed_bytes=sum(item["byte_count"] for item in entries))
    if set(expected) - {entry["relative"] for entry in entries}:
        raise ValueError("An expected hash does not identify one of the explicit source files")
    return root, entries


def _folder(config, client, name, *, verify_only):
    if (not isinstance(name, str) or not name.strip() or name in (".", "..")
            or any(char in name for char in ("/", "\\", "\0"))):
        raise ValueError("Cold archive folder name must be a single nonempty name")
    parent = config.get("folder_id")
    if isinstance(parent, bool) or not isinstance(parent, (str, int)) or not str(parent):
        raise CloudError("尚未配置已授权的夸克父目录。")
    from .quark_memory_client import _account_mutex
    # This short initialization mutex is distinct from both folder writer
    # leases. A cold run does not hold the active report folder's writer lease.
    identity = "folder:init:" + str(parent) + ":" + sha(name.encode("utf-8"))
    with _account_mutex(identity, 60):
        def matches():
            result = [item for item in client.list_files(str(parent))
                      if item.get("file_name", item.get("name")) == name]
            if len(result) > 1:
                raise CloudError("冷归档目录存在同名条目，无法确定唯一位置。")
            if result and (result[0].get("is_dir") not in (True, 1, "1")
                           or isinstance(result[0].get("fid"), bool)
                           or not isinstance(result[0].get("fid"), (str, int))
                           or not str(result[0]["fid"])):
                raise CloudError("冷归档同名条目不是有效的唯一文件夹。")
            return str(result[0]["fid"]) if result else None
        found = matches()
        if found is not None:
            return found
        if verify_only:
            raise CloudError("冷归档目录不存在，只读验证不会创建目录。")
        created = client.create_folder(name, parent_fid=str(parent))
        if isinstance(created, bool) or not isinstance(created, (str, int)) or not str(created):
            raise CloudError("新建冷归档目录未返回有效位置。")
        if str(created) in {str(parent), str(config.get("memory_folder_id"))}:
            raise CloudError("冷归档目录必须与已授权父目录及活动战报目录分别独立。")
        found = matches()
        # The official SDK can return a plain creation FID and an opaque
        # listing handle for the same directory. Keep both identifiers opaque;
        # the fresh unique parent listing supplies the handle for later calls.
        if found is None:
            raise CloudError("新建冷归档目录未得到唯一目录清单确认。")
        return found


class ColdArchiveStore(CloudStore):
    """Standard cloud catalog with larger level-zero archive parts.

    A verified-part registry exists only for this instance's lifetime. It can
    avoid repeating successful transfers during an in-process retry. It never
    supplies bytes to the final independently constructed reader.
    """
    def __init__(self, config_path=None, client=None, *, folder_id, progress=None):
        config = load_config(config_path)
        if client is None:
            from .quark_memory_client import MemoryQuarkClient
            client = MemoryQuarkClient(config=config, max_upload_bytes=COLD_STORED_BYTES)
        super().__init__(config_path, client)
        self.config = dict(self.config)
        self.config["memory_folder_id"] = str(folder_id)
        self.folder_id = str(folder_id)
        self._verified_parts = {}
        self._cold_archive_binding = None
        self.progress = progress
        self.uploaded_parts = self.uploaded_bytes = 0

    @contextmanager
    def writer_lease(self):
        binding = self._cold_archive_binding
        if binding is None:
            # An external bare-part probe has no catalog binding yet.
            with super().writer_lease():
                yield self
            return
        scope, folder_id, client = binding
        if (folder_id != str(self.folder_id) or client is not self.client
                or scope[3] != COLD_NAMESPACE):
            raise ValueError("Cold archive writer binding has changed")
        from .quark_memory_client import _account_mutex
        # Different parent listings may produce different opaque handles for
        # one physical directory. Serialize the whole batch by its attested
        # logical destination as well as retaining the original folder lease.
        # Active-report configuration is not part of the cold destination.
        identity = "folder:cold-logical:" + sha(encode([scope[0], scope[1], scope[3]]))
        with _account_mutex(identity, 0):
            with super().writer_lease():
                yield self

    def _archive_part(self, raw):
        if len(raw) > COLD_RAW_BYTES or len(raw) > 48 * 1024 * 1024:
            raise CloudError("冷归档原文分片超过48 MiB限制。")
        stored = gzip.compress(raw, compresslevel=0, mtime=0)
        if len(stored) > COLD_STORED_BYTES or len(stored) > 50 * 1024 * 1024:
            raise CloudError("冷归档压缩分片超过50 MiB限制。")
        digest = sha(stored)
        name = "memory-part-v1-" + digest + ".gz"
        identity = (self.folder_id, name, sha(raw), len(raw), len(stored))
        if identity in self._verified_parts:
            return dict(self._verified_parts[identity])
        fid = self._upload_verified(stored, name, maximum=COLD_STORED_BYTES)
        ref = {"fid": fid, "raw_sha256": identity[2], "raw_size": len(raw),
               "stored_sha256": digest, "stored_size": len(stored)}
        self._verified_parts[identity] = ref
        self.uploaded_parts += 1
        self.uploaded_bytes += len(raw)
        _emit(self.progress, "uploaded_part", uploaded_parts=self.uploaded_parts,
              uploaded_bytes=self.uploaded_bytes)
        return dict(ref)

    def put_stream(self, key, stream, size, namespace=COLD_NAMESPACE, *, expected_sha256=None):
        if type(size) is not int or size < 0:
            raise ValueError("Archive stream size must be a nonnegative integer")
        with self._lock:
            self._ensure_loaded()
            before = copy.deepcopy(self.catalog)
            digest, chunks, received = hashlib.sha256(), [], 0
            try:
                while True:
                    raw = stream.read(COLD_RAW_BYTES)
                    if not raw:
                        break
                    received += len(raw)
                    if received > size:
                        raise CloudError("归档源文件长度变化，未提交云端目录。")
                    digest.update(raw)
                    chunks.append(self._archive_part(raw))
                identity = digest.hexdigest()
                if received != size or expected_sha256 is not None and identity != expected_sha256:
                    raise CloudError("归档源文件 SHA-256 或长度变化，未提交云端目录。")
                self.catalog["blobs"][identity] = {"byte_count": received, "parts": [
                    {"chunk": chunk, "offset": 0, "length": chunk["raw_size"]} for chunk in chunks]}
                self.catalog["files"][str(key)] = {"sha256": identity, "byte_count": received,
                                                   "namespace": namespace}
                receipt = self._commit(before)
                if not isinstance(receipt, dict) or receipt.get("verified") is not True:
                    raise CloudError("归档目录写入尚未获得校验确认。")
                return receipt
            except BaseException:
                self.catalog = before
                raise


def cold_archive_reader(config_path=None, client=None, *, folder_id):
    """Return an empty-cache standard reader; iter_file restores any catalog key."""
    if client is None:
        from .quark_memory_client import MemoryQuarkClient
        client = MemoryQuarkClient(config=load_config(config_path), max_upload_bytes=COLD_STORED_BYTES)
    reader = CloudStore(config_path, client, compressed_cache_bytes=VERIFIER_CACHE_BYTES)
    reader.config = dict(reader.config)
    reader.config["memory_folder_id"] = str(folder_id)
    reader.folder_id = str(folder_id)
    return reader


def _check_local(entry, root):
    path = _safe(entry["path"], root)
    if (not stat.S_ISREG(path.lstat().st_mode) or signature(path) != entry["signature"]
            or file_sha(path) != entry["sha256"] or signature(path) != entry["signature"]):
        raise CloudError("本地归档已变化，未授权清理该文件。")


def migrate_cold_archives(root, sources, *, config_path=None, folder_name=DEFAULT_FOLDER_NAME,
                          expected_sha256=None, delete_local=False, verify_only=False,
                          progress=None, client=None, archive_store=None):
    """Upload opaque archives and optionally unlink only after all-file proof.

    Retain ``error.cold_archive_store`` or pass ``archive_store`` to reuse
    successful part receipts in a retry within this process. No retry registry
    or progress state is ever saved locally.
    """
    if verify_only and delete_local:
        raise ValueError("Read-only verification cannot delete local files")
    _emit(progress, "preflight")
    root, entries = _preflight(root, sources, expected_sha256, progress)
    counts = {"source_files": len(entries), "source_bytes": sum(item["byte_count"] for item in entries),
              "source_parts": sum((item["byte_count"] + COLD_RAW_BYTES - 1) // COLD_RAW_BYTES for item in entries)}
    _emit(progress, "preflight_complete", **counts)
    config = load_config(config_path)
    if client is None and archive_store is not None:
        client = archive_store.client
    if client is None:
        from .quark_memory_client import MemoryQuarkClient
        client = MemoryQuarkClient(config=config, max_upload_bytes=COLD_STORED_BYTES)
    scope = (str(config.get("folder_id")), folder_name,
             str(config.get("memory_folder_id")), COLD_NAMESPACE)
    if archive_store is not None and (not isinstance(archive_store, ColdArchiveStore)
                                      or archive_store.client is not client):
        raise ValueError("Retry archive store must use the selected cold client and namespace")
    binding = archive_store._cold_archive_binding if archive_store is not None else None
    if binding is not None:
        if (binding[0] != scope or binding[1] != str(archive_store.folder_id)
                or binding[2] is not client):
            raise ValueError("Retry archive store logical folder binding has changed")
        # The original unique parent/name listing already attested this handle.
        # Opaque handles can differ in later listings; keep the original rather
        # than interpreting identifiers or redirecting this instance's writes.
        folder_id = archive_store.folder_id
    else:
        folder_id = _folder(config, client, folder_name, verify_only=verify_only)
    if str(folder_id) in {str(config.get("folder_id")), str(config.get("memory_folder_id"))}:
        raise CloudError("冷归档目录必须与已授权父目录及活动战报目录分别独立。")
    store = archive_store or ColdArchiveStore(config_path, client, folder_id=folder_id, progress=progress)
    if str(store.folder_id) != str(folder_id) or store.client is not client:
        raise ValueError("Retry archive store must use the selected cold folder and client")
    if binding is None:
        store._cold_archive_binding = (scope, str(folder_id), client)
    store.progress = progress
    try:
        with store.writer_lease():
            store.refresh()
            processed_bytes = 0
            if not verify_only:
                for number, entry in enumerate(entries, 1):
                    path = _safe(entry["path"], root)
                    if signature(path) != entry["signature"]:
                        raise CloudError("归档源文件在上传前发生变化。")
                    published = store.catalog["files"].get(entry["key"])
                    if (not published or published.get("sha256") != entry["sha256"]
                            or published.get("byte_count") != entry["byte_count"]
                            or published.get("namespace") != COLD_NAMESPACE):
                        with path.open("rb") as stream:
                            store.put_stream(entry["key"], stream, entry["byte_count"],
                                             namespace=COLD_NAMESPACE,
                                             expected_sha256=entry["sha256"])
                    if signature(path) != entry["signature"]:
                        raise CloudError("归档源文件在上传时发生变化。")
                    processed_bytes += entry["byte_count"]
                    _emit(progress, "uploaded_file", **counts, processed_files=number,
                          processed_bytes=processed_bytes, uploaded_parts=store.uploaded_parts)
            # No upload reader, decoded chunk, or same-run registry is shared.
            fresh = cold_archive_reader(config_path, client, folder_id=folder_id)
            fresh.refresh()
            verified_bytes = verified_parts = 0
            for number, entry in enumerate(entries, 1):
                info = fresh.catalog["files"].get(entry["key"])
                if (not info or info.get("sha256") != entry["sha256"]
                        or info.get("byte_count") != entry["byte_count"]
                        or info.get("namespace") != COLD_NAMESPACE):
                    raise CloudError("独立回读未找到完整归档目录记录，未清理本地。")
                digest, received = hashlib.sha256(), 0
                for block in fresh.iter_file(entry["key"]):
                    digest.update(block)
                    received += len(block)
                    verified_parts += 1
                    _emit(progress, "verified_part", **counts, verified_files=number - 1,
                          verified_parts=verified_parts, verified_bytes=verified_bytes + received)
                if received != entry["byte_count"] or digest.hexdigest() != entry["sha256"]:
                    raise CloudError("独立回读归档整文件 SHA-256 或长度不匹配，未清理本地。")
                verified_bytes += received
                _emit(progress, "verified_file", **counts, verified_files=number,
                      verified_bytes=verified_bytes, verified_parts=verified_parts)
            result = {**counts, "verified_files": len(entries), "verified_bytes": verified_bytes,
                      "verified_parts": verified_parts, "removed_files": 0, "released_bytes": 0,
                      "folder_id": folder_id, "catalog_fid": fresh.catalog_fid,
                      "keys": [entry["key"] for entry in entries], "ok": True}
            _emit(progress, "verification_complete", **counts, verified_files=len(entries),
                  verified_bytes=verified_bytes, verified_parts=verified_parts)
            # Every source passes a full new local SHA before the first unlink.
            for entry in entries:
                _check_local(entry, root)
            if not delete_local:
                return {**result, "phase": "verified_only" if verify_only else "verified_local_retained"}
            try:
                for entry in entries:
                    _check_local(entry, root)
                    entry["path"].unlink()
                    result["removed_files"] += 1
                    result["released_bytes"] += entry["byte_count"]
                return {**result, "phase": "complete"}
            except Exception as error:
                # Fresh remote proof still holds, but partial local cleanup must
                # never be reported as completion. Preserve untouched sources.
                return {**result, "phase": "partial_cleanup", "ok": False,
                        "failed_files": len(entries) - result["removed_files"],
                        "error_type": type(error).__name__}
    except BaseException as error:
        try:
            error.cold_archive_store = store
        except (AttributeError, TypeError):
            pass
        raise
