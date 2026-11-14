"""Online battle browsing with cloud objects and short-lived RAM projections.

Only the program configuration and its static dictionaries are read from disk.
Report objects, catalog entries and SQLite projections never become local files.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import sqlite3
import threading
import time

from .data import BattleData, BattleDataError, _integer


class _MemoryView(BattleData):
    """Reuse the established detail renderer over an owned in-memory snapshot."""

    def __init__(self, connection):
        self.connection = connection
        self.timeout_seconds = 15.0

    @contextmanager
    def _connection(self):
        deadline = time.monotonic() + self.timeout_seconds
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            self.connection.execute("BEGIN")
            yield self.connection
        finally:
            self.connection.rollback()
            self.connection.set_progress_handler(None, 0)


class CloudBattleData:
    """The GUI's archive interface without a filesystem database or disk cache."""

    memory_only = True
    allow_export = False
    supported_pages = frozenset({"overview", "archive", "cloud", "logs"})

    def __init__(self, store=None, *, config_path=None, max_report_bytes=64 * 1024 * 1024):
        if store is None:
            from parser.cloud_store import CloudStore
            store = CloudStore(config_path=config_path)
        self.store = store
        self.max_report_bytes = _integer(max_report_bytes, "max_report_bytes", 1, 1024 ** 3)
        self._lock = threading.RLock()
        self._ids = {}
        self._keys = {}
        self._rows = {}

    @staticmethod
    def _rank(row):
        quality = row.get("quality")
        if isinstance(quality, list) and quality and all(isinstance(value, (int, float)) for value in quality):
            return quality
        return [int(row.get("completeness") == "complete"),
                {"all": 3, "detail": 2, "basic": 1}.get(row.get("source"), 0),
                row.get("event_count", 0)]

    def _refresh_rows(self):
        # No stale-catalog fallback: every new operation must confirm cloud access.
        self.store.refresh()
        values = self.store.list_reports()
        if not isinstance(values, list):
            raise BattleDataError("云端战报目录格式无效。")
        best = {}
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get("key"), str) or not value["key"]:
                raise BattleDataError("云端战报目录缺少稳定的对象位置。")
            row = dict(value)
            key = row["key"]
            if key not in self._ids:
                identifier = len(self._ids) + 1
                self._ids[key] = identifier
                self._keys[identifier] = key
            row.update(snapshot_id=self._ids[key], record_id=self._ids[key])
            row["namespace"] = str(row.get("namespace") or "default")
            row["battle_uid"] = str(row.get("battle_uid") or row.get("battleID") or "")
            if not row["battle_uid"]:
                raise BattleDataError("云端战报目录缺少战报编号。")
            if row.get("completeness") not in {"complete", "partial", "legacy"}:
                row["completeness"] = "partial"
            count = row.get("event_count", 0)
            row["event_count"] = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
            row.setdefault("source", None)
            row.setdefault("imported_at", row.get("created_at"))
            row.setdefault("first_seen", row.get("imported_at"))
            row.setdefault("last_seen", row.get("imported_at"))
            row.setdefault("battle_time", row.get("endTimeStampTime", row.get("timeStamp")))
            row.setdefault("location", row.get("locName"))
            row["result_verified"] = False
            identity = (row["namespace"], row["battle_uid"])
            previous = best.get(identity)
            source = {"all": 3, "detail": 2, "basic": 1}
            if previous is None or (self._rank(row) >= self._rank(previous)
                    and source.get(row.get("source"), 0) >= source.get(previous.get("source"), 0)
                    and row["event_count"] >= previous["event_count"]):
                best[identity] = row
        self._rows = {row["key"]: row for row in best.values()}
        return sorted(best.values(), key=lambda row: (str(row.get("last_seen") or ""), row["snapshot_id"]), reverse=True)

    def overview(self):
        with self._lock:
            rows = self._refresh_rows()
            result = {"available": True, "database_path": "夸克云端 · 仅运行内存", "total": 0,
                      "complete": 0, "partial": 0, "legacy": 0, "events": 0, "accounts": [],
                      "storage_backend": "quark-memory", "note": "联网读取云端目录；本机不保存战报数据库或缓存。"}
            accounts = {}
            for row in rows:
                item = accounts.setdefault(row["namespace"], {"namespace": row["namespace"], "total": 0,
                    "complete": 0, "partial": 0, "legacy": 0, "events": 0})
                for target in (result, item):
                    target["total"] += 1
                    target[row["completeness"]] += 1
                    target["events"] += row["event_count"]
            result["accounts"] = sorted(accounts.values(), key=lambda item: item["namespace"])
            return result

    def list_reports(self, namespace=None, status=None, search="", limit=100, offset=0):
        _integer(limit, "limit", 1, 200)
        _integer(offset, "offset", 0, 1_000_000)
        if status not in (None, "", "all", "complete", "partial", "legacy"):
            raise ValueError("Invalid completeness filter")
        if namespace is not None and (not isinstance(namespace, str) or len(namespace) > 500):
            raise ValueError("Invalid account namespace")
        if not isinstance(search, str) or len(search) > 200:
            raise ValueError("Search must contain at most 200 characters")
        with self._lock:
            rows = self._refresh_rows()
            if namespace:
                rows = [row for row in rows if row["namespace"] == namespace]
            if status not in (None, "", "all"):
                rows = [row for row in rows if row["completeness"] == status]
            search = search.strip()
            search_mode = None
            if search:
                search_mode = "exact_id" if search.isascii() and search.isdigit() else "id_or_player_name"
                if search_mode == "exact_id":
                    rows = [row for row in rows if row["battle_uid"] == search]
                else:
                    needle = search.casefold()
                    rows = [row for row in rows if any(needle in str(row.get(key) or "").casefold()
                            for key in ("battle_uid", "attacker_name", "defender_name"))]
            return {"items": [dict(row) for row in rows[offset:offset + limit]],
                    "has_more": len(rows) > offset + limit, "limit": limit, "offset": offset,
                    "search_mode": search_mode}

    def _raw(self, snapshot_id):
        _integer(snapshot_id, "snapshot_id", 1, 2**63 - 1)
        self._refresh_rows()
        key = self._keys.get(snapshot_id)
        row = self._rows.get(key)
        if row is None:
            raise KeyError(f"云端当前目录中没有快照 {snapshot_id}")
        declared = row.get("byte_count")
        if isinstance(declared, int) and not isinstance(declared, bool) and declared > self.max_report_bytes:
            raise BattleDataError("该份原文超过单次内存读取上限，未下载到本地文件。")
        raw = self.store.read_report(key)
        if not isinstance(raw, bytes):
            raise BattleDataError("云端读取结果不是原始字节。")
        if len(raw) > self.max_report_bytes:
            raise BattleDataError("该份原文超过单次内存读取上限，未下载到本地文件。")
        return dict(row), raw

    def detail(self, snapshot_id, event_limit=100, event_offset=0):
        _integer(event_limit, "event_limit", 1, 200)
        _integer(event_offset, "event_offset", 0, 10_000_000)
        with self._lock:
            row, raw = self._raw(snapshot_id)
            from parser.report_archive import ReportArchive
            connection = sqlite3.connect(":memory:")
            with ReportArchive(connection=connection, storage_mode="lean") as archive:
                imported = archive.import_bytes(raw, namespace=row["namespace"])
                if imported.get("status") != "imported":
                    raise BattleDataError("云端原文不能解析为战报：" + str(imported.get("error") or imported.get("status")))
                index = row.get("document_index", 0)
                if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                    raise BattleDataError("云端目录中的原文文档序号无效。")
                selected = connection.execute("SELECT s.id,r.battle_uid FROM report_snapshots s "
                    "JOIN report_records r ON r.id=s.report_id WHERE s.document_index=?", (index,)).fetchone()
                if selected is None or selected["battle_uid"] != row["battle_uid"]:
                    raise BattleDataError("云端目录与原文的战报编号或文档序号不匹配。")
                result = _MemoryView(connection).detail(selected["id"], event_limit=event_limit, event_offset=event_offset)
                result["report"]["snapshot_id"] = snapshot_id
                result["report"]["current_snapshot_id"] = snapshot_id
                result["report"]["imported_at"] = row.get("imported_at")
                result["preview_note"] = "本页原文及查询投影仅在运行内存中处理，关闭后不保留；联网重新读取。"
                result["storage_backend"] = "quark-memory"
                result["allow_export"] = False
                return result

    def original_preview(self, snapshot_id, max_bytes=24000):
        _integer(max_bytes, "max_bytes", 100, 100000)
        with self._lock:
            _, raw = self._raw(snapshot_id)
            return {"text": raw[:max_bytes].decode("utf-8-sig", "replace"), "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(), "truncated": len(raw) > max_bytes,
                    "note": "仅在内存中预览，不创建本地原文文件。"}

    def export_original(self, *_args, **_kwargs):
        raise PermissionError("严格云端模式禁止将战报导出到本地文件。")

    def cloud_status(self):
        with self._lock:
            self.store.refresh()
            result = dict(self.store.status())
            result.update(storage_backend="quark-memory", writes_local_report_files=False, disk_cache=False)
            return result
