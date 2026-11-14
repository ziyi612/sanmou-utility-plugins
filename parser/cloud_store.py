"""Immutable, verified Quark objects and a cloud-only report catalog.

No report bytes, catalog or restart state are written to the local filesystem.
Only the existing program configuration and official account settings are read.
"""
from __future__ import annotations

import copy
from collections import OrderedDict
from contextlib import contextmanager
import gzip
import hashlib
import json
from pathlib import Path
import re
import threading
import time

from .quark_cloud import CloudError, load_config

CATALOG_PREFIX = "memory-catalog-v1-"
CATALOG_NAME = re.compile(r"^memory-catalog-v1-(\d{20})-([0-9a-f]{64})\.json\.gz$")
MAX_RAW_CHUNK = 256 * 1024 * 1024
MAX_STORED_CHUNK = 8 * 1024 * 1024
MAX_CATALOG = 16 * 1024 * 1024


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def unpack(stored, maximum):
    """Bound decompression as well as network reads."""
    import zlib
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    raw = decoder.decompress(stored, maximum + 1)
    if len(raw) > maximum or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
        raise CloudError("云端对象超出内存限制或压缩格式无效。")
    return raw


def report_metadata(raw, namespace, file_key):
    """Use the archive's existing completeness and version preference rules."""
    from .report_archive import _decode, _main_data, _quality, _settlement_source, iter_fields
    try:
        document = _decode(raw)
    except (ValueError, UnicodeError):
        return []
    documents = document if isinstance(document, list) else [document]
    results = []
    for index, payload in enumerate(documents):
        if not isinstance(payload, dict):
            continue
        try:
            data, _ = _main_data(payload)
            uid = payload.get("battleID", data.get("battleID"))
            if isinstance(uid, bool) or not isinstance(uid, (str, int)):
                continue
            identity = payload.get("runtimeIdentity")
            report_namespace = namespace
            if isinstance(identity, dict) and all(identity.get(key) is not None for key in ("serverId", "scenarioId", "avatarId")):
                report_namespace = "server:{serverId}/scenario:{scenarioId}/avatar:{avatarId}".format(**identity)
            fields = list(iter_fields(payload))
            completeness, quality, _ = _quality(payload, fields, data)
            settlement, _ = _settlement_source(payload)
            def player(side):
                army = settlement.get(side, {})
                return army.get("avatar_data", {}).get("avatar_name") if isinstance(army, dict) else None
            stamp = data.get("timeStamp")
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            key = sha(encode([report_namespace, str(uid)]))
            results.append({"key": key, "namespace": report_namespace, "battle_uid": str(uid),
                "file_key": file_key, "document_index": index, "completeness": completeness,
                "blob_sha256": sha(raw), "byte_count": len(raw),
                "quality": quality, "event_count": len(data.get("eventDataList", [])) if isinstance(data.get("eventDataList"), list) else 0,
                "source": payload.get("source"), "timeStamp": stamp, "timestamp": stamp,
                "attacker_name": player("attacker"), "defender_name": player("defensive") or player("defender"),
                "matchType": data.get("matchType"), "result": data.get("result"), "locName": data.get("locName"),
                "imported_at": now, "first_seen": now, "last_seen": now})
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            # Backup/state/diagnostic JSON is preserved as a file, not mislabeled
            # as a browseable battle report.
            continue
    return results


class CloudStore:
    def __init__(self, config_path=None, client=None, *, journal_mode=False, compressed_cache_bytes=0):
        if isinstance(compressed_cache_bytes, bool) or not isinstance(compressed_cache_bytes, int) or compressed_cache_bytes < 0:
            raise ValueError("compressed_cache_bytes must be a nonnegative integer")
        self.config = load_config(config_path)
        if client is None:
            from .quark_memory_client import MemoryQuarkClient
            client = MemoryQuarkClient(config=self.config)
        self.client = client
        self.folder_id = self.config.get("memory_folder_id") or self.config.get("folder_id")
        if not self.folder_id:
            raise CloudError("请先连接夸克战报目录。")
        self.catalog = {"schema": 1, "generation": None, "files": {}, "blobs": {}, "reports": {}, "states": {}, "migrations": {}}
        self.catalog_fid = None
        self._objects = {}
        self._lock = threading.RLock()
        self._cached_chunk = None
        self.compressed_cache_bytes = compressed_cache_bytes
        self._compressed_cache = OrderedDict()
        self._compressed_cache_size = 0
        self._loaded = False
        self.journal_mode = journal_mode
        self._head_generation = 0

    @contextmanager
    def writer_lease(self):
        """Hold the folder's cross-process kernel mutex for a whole writer run."""
        from .quark_memory_client import _account_mutex
        with _account_mutex("folder:" + str(self.folder_id), 0):
            yield self

    def refresh(self):
        with self._lock:
            entries = self.client.list_files(self.folder_id)
            self._objects = {}
            candidates = []
            journals = []
            for item in entries:
                name = str(item.get("file_name", item.get("name", "")))
                fid = item.get("fid", item.get("file_id"))
                if not fid:
                    continue
                self._objects.setdefault(name, str(fid))
                match = CATALOG_NAME.fullmatch(name)
                if match:
                    candidates.append((name, str(fid), match.group(2)))
                journal = re.fullmatch(r"memory-change-v1-(\d{20})-([0-9a-f]{64})\.json\.gz", name)
                if journal:
                    journals.append((int(journal.group(1)), str(fid), journal.group(2)))
            if not candidates:
                if self.catalog_fid is not None:
                    raise CloudError("已使用的云端目录清单消失，已停止操作。")
                self._loaded = True
            else:
                _, fid, digest = max(candidates)
            if candidates and fid != self.catalog_fid:
                stored = self.client.download_bytes(fid, max_bytes=MAX_CATALOG)
                if sha(stored) != digest:
                    raise CloudError("最新云端目录清单的 SHA-256 校验失败。")
                catalog = json.loads(unpack(stored, 128 * 1024 * 1024))
                if catalog.get("schema") != 1 or any(not isinstance(catalog.get(key), dict) for key in ("files", "blobs", "reports", "states", "migrations")):
                    raise CloudError("云端目录清单格式不兼容。")
                self.catalog, self.catalog_fid = catalog, fid
                self._cached_chunk = None
            self._head_generation = int(self.catalog.get("generation") or 0)
            for generation, journal_fid, digest in sorted(journals):
                if generation <= self._head_generation:
                    continue
                stored = self.client.download_bytes(journal_fid, max_bytes=MAX_CATALOG)
                if sha(stored) != digest:
                    raise CloudError("云端增量目录 SHA-256 不匹配。")
                change = json.loads(unpack(stored, 128 * 1024 * 1024))
                if change.get("schema") != 1:
                    raise CloudError("不兼容的云端增量目录。")
                for key in ("files", "blobs", "reports", "states", "migrations"):
                    values = change.get(key, {})
                    if not isinstance(values, dict):
                        raise CloudError("云端增量目录字段无效。")
                    self.catalog[key].update(values)
                self._head_generation = generation
            self._loaded = True
            return self.catalog

    def _ensure_loaded(self):
        if not self._loaded:
            self.refresh()

    def _upload_verified(self, stored, name, maximum=MAX_STORED_CHUNK):
        if len(stored) > maximum:
            raise CloudError("云端对象超过单次内存传输限制。")
        fid = self._objects.get(name)
        if fid is None:
            uploaded = self.client.upload_bytes(stored, name, parent_fid=self.folder_id)
            fid = uploaded.get("fid")
            if not fid:
                raise CloudError("夸克未返回上传文件位置。")
        received = self.client.download_bytes(str(fid), max_bytes=maximum)
        if len(received) != len(stored) or sha(received) != sha(stored):
            raise CloudError("夸克新鲜回读与上传字节不一致；未提交云端目录或删除本地文件。")
        self._objects[name] = str(fid)
        return str(fid)

    def _publish(self):
        previous = self.catalog.get("generation")
        previous_fid, previous_head = self.catalog_fid, self._head_generation
        generation = max(time.time_ns(), int(previous or 0) + 1, self._head_generation + 1)
        self.catalog["generation"] = str(generation)
        try:
            stored = gzip.compress(encode(self.catalog), compresslevel=6, mtime=0)
            name = f"{CATALOG_PREFIX}{generation:020d}-{sha(stored)}.json.gz"
            fid = self._upload_verified(stored, name, MAX_CATALOG)
        except BaseException:
            self.catalog["generation"] = previous
            self.catalog_fid, self._head_generation = previous_fid, previous_head
            raise
        self.catalog_fid = fid
        self._head_generation = generation
        return {"verified": True, "catalog_fid": self.catalog_fid, "generation": str(generation)}

    def _commit(self, before):
        if not self.journal_mode:
            return self._publish()
        change = {"schema": 1}
        for key in ("files", "blobs", "reports", "states", "migrations"):
            change[key] = {name: value for name, value in self.catalog[key].items()
                           if before[key].get(name) != value}
        generation = max(time.time_ns(), self._head_generation + 1)
        stored = gzip.compress(encode(change), compresslevel=6, mtime=0)
        name = f"memory-change-v1-{generation:020d}-{sha(stored)}.json.gz"
        fid = self._upload_verified(stored, name, MAX_CATALOG)
        self._head_generation = generation
        return {"verified": True, "journal_fid": fid, "generation": str(generation)}

    def checkpoint(self):
        with self._lock:
            self._ensure_loaded()
            return self._publish()

    def _store_chunk(self, raw):
        if len(raw) > MAX_RAW_CHUNK:
            raise CloudError("原文分片超过内存限制。")
        stored = gzip.compress(raw, compresslevel=6, mtime=0)
        if len(stored) > MAX_STORED_CHUNK:
            midpoint = len(raw) // 2
            return self._store_chunk(raw[:midpoint]) + self._store_chunk(raw[midpoint:])
        name = "memory-part-v1-" + sha(stored) + ".gz"
        fid = self._upload_verified(stored, name)
        return [{"fid": fid, "raw_sha256": sha(raw), "raw_size": len(raw),
                 "stored_sha256": sha(stored), "stored_size": len(stored)}]

    def _chunks_for(self, raw):
        chunks = []
        for offset in range(0, len(raw), MAX_RAW_CHUNK):
            chunks.extend(self._store_chunk(raw[offset:offset + MAX_RAW_CHUNK]))
        return chunks

    def _add_reports(self, raw, namespace, file_key):
        for candidate in report_metadata(raw, namespace, file_key):
            existing = self.catalog["reports"].get(candidate["key"])
            quality = candidate["quality"]
            if existing:
                old = existing["quality"]
                if not (quality >= old and quality[4] >= old[4] and quality[5] >= old[5]):
                    continue
                candidate["first_seen"] = existing["first_seen"]
            self.catalog["reports"][candidate["key"]] = candidate

    def put_files(self, items):
        """Publish a bounded batch [(logical_key, bytes, namespace, index)]."""
        with self._lock:
            self._ensure_loaded()
            if sum(len(item[1]) for item in items) > MAX_RAW_CHUNK:
                raise CloudError("批次超过内存限制，请拆分迁移批次。")
            before = copy.deepcopy(self.catalog)
            try:
                unique = {}
                for key, raw, namespace, index in items:
                    digest = sha(raw)
                    if digest not in self.catalog["blobs"]:
                        unique.setdefault(digest, raw)
                packed = b"".join(unique.values())
                chunks = self._chunks_for(packed)
                positions = []
                cursor = 0
                for chunk in chunks:
                    positions.append((cursor, cursor + chunk["raw_size"], chunk))
                    cursor += chunk["raw_size"]
                start = 0
                for digest, raw in unique.items():
                    parts = []
                    for begin, end, chunk in positions:
                        lo, hi = max(start, begin), min(start + len(raw), end)
                        if lo < hi:
                            parts.append({"chunk": chunk, "offset": lo - begin, "length": hi - lo})
                    self.catalog["blobs"][digest] = {"byte_count": len(raw), "parts": parts}
                    start += len(raw)
                for key, raw, namespace, index in items:
                    self.catalog["files"][str(key)] = {"sha256": sha(raw), "byte_count": len(raw), "namespace": namespace}
                    if index:
                        self._add_reports(raw, namespace, str(key))
                return self._commit(before)
            except BaseException:
                self.catalog = before
                raise

    def put_file(self, key, raw, namespace="default", index=True):
        if not isinstance(raw, bytes):
            raise TypeError("Cloud originals must be bytes")
        if len(raw) <= MAX_RAW_CHUNK:
            return self.put_files([(str(key), raw, namespace, index)])
        with self._lock:
            self._ensure_loaded()
            before = copy.deepcopy(self.catalog)
            try:
                digest = sha(raw)
                if digest not in self.catalog["blobs"]:
                    chunks = self._chunks_for(raw)
                    self.catalog["blobs"][digest] = {"byte_count": len(raw), "parts": [
                        {"chunk": chunk, "offset": 0, "length": chunk["raw_size"]} for chunk in chunks]}
                self.catalog["files"][str(key)] = {"sha256": digest, "byte_count": len(raw), "namespace": namespace}
                if index:
                    self._add_reports(raw, namespace, str(key))
                return self._commit(before)
            except BaseException:
                self.catalog = before
                raise

    def put_stream(self, key, stream, size, namespace="backup"):
        """Read a preexisting large backup in bounded RAM; never create a copy."""
        with self._lock:
            self._ensure_loaded()
            before = copy.deepcopy(self.catalog)
            digest, chunks, received = hashlib.sha256(), [], 0
            try:
                while True:
                    raw = stream.read(MAX_RAW_CHUNK)
                    if not raw:
                        break
                    digest.update(raw)
                    received += len(raw)
                    chunks.extend(self._chunks_for(raw))
                if received != size:
                    raise CloudError("迁移时源文件长度变化，未提交完成记录。")
                identity = digest.hexdigest()
                self.catalog["blobs"][identity] = {"byte_count": received, "parts": [
                    {"chunk": chunk, "offset": 0, "length": chunk["raw_size"]} for chunk in chunks]}
                self.catalog["files"][str(key)] = {"sha256": identity, "byte_count": received, "namespace": namespace}
                return self._commit(before)
            except BaseException:
                self.catalog = before
                raise

    def _read_chunk(self, ref):
        identity = ref["stored_sha256"]
        if self._cached_chunk and self._cached_chunk[0] == identity:
            return self._cached_chunk[1]
        stored = self._compressed_cache.get(identity)
        if stored is None:
            stored = self.client.download_bytes(ref["fid"], max_bytes=MAX_STORED_CHUNK)
        if len(stored) != ref["stored_size"] or sha(stored) != identity:
            raise CloudError("云端分片 SHA-256 或长度不匹配。")
        raw = unpack(stored, MAX_RAW_CHUNK)
        if len(raw) != ref["raw_size"] or sha(raw) != ref["raw_sha256"]:
            raise CloudError("解压原文分片 SHA-256 或长度不匹配。")
        # This optional cache belongs to this store instance only. Admission
        # follows a fresh network read and both compressed/raw validations;
        # every later use still validates and decodes the compressed bytes.
        if self.compressed_cache_bytes and len(stored) <= self.compressed_cache_bytes:
            if identity not in self._compressed_cache:
                while self._compressed_cache_size + len(stored) > self.compressed_cache_bytes:
                    _, removed = self._compressed_cache.popitem(last=False)
                    self._compressed_cache_size -= len(removed)
                self._compressed_cache[identity] = stored
                self._compressed_cache_size += len(stored)
            self._compressed_cache.move_to_end(identity)
        self._cached_chunk = (identity, raw)
        return raw

    def iter_file(self, key):
        self._ensure_loaded()
        info = self.catalog["files"][str(key)]
        blob = self.catalog["blobs"][info["sha256"]]
        digest, length = hashlib.sha256(), 0
        for part in blob["parts"]:
            raw = self._read_chunk(part["chunk"])
            begin, size = part["offset"], part["length"]
            if begin < 0 or size < 0 or begin + size > len(raw):
                raise CloudError("云端文件分片范围无效。")
            value = raw[begin:begin + size]
            digest.update(value)
            length += len(value)
            yield value
        if length != info["byte_count"] or digest.hexdigest() != info["sha256"]:
            raise CloudError("整份云端文件 SHA-256 或长度不匹配。")

    def read_file(self, key, maximum=128 * 1024 * 1024):
        with self._lock:
            self._ensure_loaded()
            if self.catalog["files"][str(key)]["byte_count"] > maximum:
                raise CloudError("该备份超过单次预览内存限制，请使用分片恢复。")
            return b"".join(self.iter_file(key))

    def list_reports(self):
        with self._lock:
            self._ensure_loaded()
            return [dict(value) for value in self.catalog["reports"].values()]

    def read_report(self, key):
        with self._lock:
            self._ensure_loaded()
            info = self.catalog["reports"][str(key)]
            digest = info["blob_sha256"]
            blob = self.catalog["blobs"][digest]
            if blob["byte_count"] > 128 * 1024 * 1024:
                raise CloudError("该战报超过单次内存预览限制。")
            chunks = []
            for part in blob["parts"]:
                raw = self._read_chunk(part["chunk"])
                start, length = part["offset"], part["length"]
                if start < 0 or length < 0 or start + length > len(raw):
                    raise CloudError("战报分片范围无效。")
                chunks.append(raw[start:start + length])
            raw = b"".join(chunks)
            if len(raw) != blob["byte_count"] or sha(raw) != digest:
                raise CloudError("战报原文字节校验失败。")
            return raw

    def status(self):
        with self._lock:
            self._ensure_loaded()
            return {"available": True, "backend": "quark-memory", "reports": len(self.catalog["reports"]),
                    "files": len(self.catalog["files"]), "cloud_bytes": sum(item["byte_count"] for item in self.catalog["blobs"].values()),
                    "disk_cache_bytes": 0, "catalog_fid": self.catalog_fid, "folder_id": self.folder_id,
                    "note": "战报、目录和查询只在运行内存中；离线不可用。"}

    def save_state(self, namespace, value):
        with self._lock:
            self._ensure_loaded()
            before = copy.deepcopy(self.catalog)
            self.catalog["states"][namespace] = copy.deepcopy(value)
            try:
                return self._commit(before)
            except BaseException:
                self.catalog = before
                raise

    def load_state(self, namespace):
        with self._lock:
            self._ensure_loaded()
            return copy.deepcopy(self.catalog["states"].get(namespace))


class CloudArtifacts:
    """Collector storage contract: only verified remote commits acknowledge data."""
    def __init__(self, store, namespace, logical_root=None):
        self.store, self.namespace = store, namespace
        self.logical_root = Path(logical_root).resolve() if logical_root else None

    def key(self, path):
        value = Path(path)
        if self.logical_root:
            value = value.resolve().relative_to(self.logical_root)
        if value.is_absolute() or ".." in value.parts:
            raise CloudError("纯云端文件路径越出逻辑工作目录。")
        return "capture/" + sha(self.namespace.encode())[:20] + "/" + value.as_posix()

    def exists(self, path):
        self.store._ensure_loaded()
        return self.key(path) in self.store.catalog["files"]

    def read_bytes(self, path):
        key = self.key(path)
        if not self.exists(path):
            raise FileNotFoundError(str(path))
        return self.store.read_file(key)

    def write_bytes(self, path, raw):
        receipt = self.store.put_file(self.key(path), raw, namespace=self.namespace)
        if not isinstance(receipt, dict) or receipt.get("verified") is not True:
            raise CloudError("原文及其恢复位置尚未完成云端验证；不能确认采集成功。")
        if Path(path).name.startswith("battle_") and Path(path).name.endswith("_decrypted.json"):
            # Journals make every response durable; compact their metadata at
            # the end of a report so a new GUI need not replay every component.
            receipt = self.store.checkpoint()
            if not isinstance(receipt, dict) or receipt.get("verified") is not True:
                raise CloudError("云端目录检查点未完成验证；不能确认采集成功。")
        return receipt

    def write_json(self, path, value):
        return self.write_bytes(path, encode(value))

    def load_state(self, namespace):
        return self.store.load_state(namespace)

    def save_state(self, namespace, value):
        return self.store.save_state(namespace, value)
