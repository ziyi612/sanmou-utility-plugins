"""Bounded, read-only queries for the desktop battle archive browser.

Connections are local to one method call, so the GUI can run every operation on
its worker thread.  In particular this module never scans the recursive field
projection or loads a full snapshot just to draw a list or a detail panel.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any


COMPONENT_NAMES = {
    "basic": "基础信息", "detail": "详情", "all": "完整事件",
    "statics": "统计", "snapshots": "武将快照",
}
SIDE_NAMES = {"attacker": "进攻方", "defensive": "防守方", "defender": "防守方"}
_BASE_TABLES = {
    "report_meta", "report_records", "report_snapshots", "report_components", "report_fields",
    "report_events", "report_sides", "report_heroes", "report_skills", "report_files",
}
_METADATA_FIELDS = (
    "timeStamp", "endTimeStampTime", "locName", "locationOccur", "result",
    "winner", "winType", "isWin", "drawNum", "endRound", "combatType", "matchType",
)
_METADATA_PREFIXES = ("/data/", "/components/all/", "/components/detail/", "/components/basic/")
_MAX_JSON_CHARS = 24000


class BattleDataError(RuntimeError):
    """An archive cannot currently be read without changing its contents."""


class BattleQueryTimeout(BattleDataError):
    """A large or blocked query exceeded the browser's bounded time budget."""


def _integer(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{label} must be an integer from {low} through {high}")
    return value


def _json_preview(value: str | None, maximum: int = _MAX_JSON_CHARS) -> Any:
    if value is None:
        return None
    if len(value) > maximum:
        return {"preview_truncated": True, "text": value[:maximum],
                "note": "仅缩短界面预览；数据库原文未截断，可导出完整原文。"}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {"preview_invalid_json": True, "text": value}


class BattleData:
    """Small current-report queries; no schema migrations and no write API."""

    def __init__(self, db_path: str | Path, timeout_seconds: float = 5.0):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (float, int)) \
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be finite and between 0 and 60")
        self.db_path = Path(db_path).expanduser().resolve()
        self.timeout_seconds = float(timeout_seconds)

    @contextmanager
    def _connection(self):
        if not self.db_path.is_file():
            raise BattleDataError(f"数据库尚未创建或文件不可用：{self.db_path}")
        connection = None
        deadline = time.monotonic() + self.timeout_seconds
        try:
            connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=.5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=500")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            actual = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if _BASE_TABLES - actual:
                raise BattleDataError("该文件不是已初始化的战报数据库；请先启动采集。")
            from parser.report_archive import SCHEMA_VERSION
            version = connection.execute("SELECT value FROM report_meta WHERE key='schema_version'").fetchone()
            if not version or version[0] != str(SCHEMA_VERSION):
                raise BattleDataError("数据库结构版本与当前程序不兼容，请使用匹配版本的程序。")
            # Keep each browser operation internally consistent while the
            # independent collector commits new snapshots in WAL mode.
            connection.execute("BEGIN")
            yield connection
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower() or time.monotonic() >= deadline:
                raise BattleQueryTimeout("数据库查询超过时间预算，请缩小筛选范围或稍后刷新。") from exc
            raise BattleDataError(f"数据库暂时无法读取：{exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise BattleDataError(f"数据库读取失败：{exc}") from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _semantics(connection: sqlite3.Connection, snapshot_id: int) -> dict:
        needed = {"report_semantic_settings", "report_semantic_runs", "report_event_meanings", "report_id_meanings"}
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'report_%'")}
        if needed - names:
            return {"available": False, "version": None, "status": "not_generated", "error": None}
        active = connection.execute(
            "SELECT active_version FROM report_semantic_settings WHERE singleton=1").fetchone()
        if not active:
            return {"available": False, "version": None, "status": "not_generated", "error": None}
        row = connection.execute(
            "SELECT status,error FROM report_semantic_runs WHERE snapshot_id=? AND version=?",
            (snapshot_id, active[0])).fetchone()
        return {"available": True, "version": active[0], "status": row[0] if row else "pending",
                "error": row[1] if row else None}

    @staticmethod
    def _metadata(connection: sqlite3.Connection, snapshot_id: int) -> dict:
        pointers = [prefix + key for prefix in _METADATA_PREFIXES for key in _METADATA_FIELDS]
        placeholders = ",".join("?" for _ in pointers)
        rows = connection.execute(
            f"SELECT pointer,substr(value_json,1,2401) value_json FROM report_fields "
            f"WHERE snapshot_id=? AND pointer IN ({placeholders})", (snapshot_id, *pointers))
        values = {row[0]: _json_preview(row[1], 2400) for row in rows}
        metadata = {}
        for key in _METADATA_FIELDS:
            for prefix in _METADATA_PREFIXES:
                if prefix + key in values:
                    metadata[key] = values[prefix + key]
                    break
        return metadata

    @staticmethod
    def _attach_display(report: dict, metadata: dict) -> None:
        report["battle_time"] = metadata.get("endTimeStampTime", metadata.get("timeStamp"))
        report["location"] = metadata.get("locName")
        report["result"] = next((metadata[key] for key in ("result", "winner", "winType", "isWin")
                                 if key in metadata), None)
        report["result_verified"] = False  # No undocumented enum-to-win/lose guess.

    def overview(self) -> dict:
        base = {"available": False, "database_path": str(self.db_path), "total": 0,
                "complete": 0, "partial": 0, "legacy": 0, "events": 0, "accounts": []}
        if not self.db_path.exists():
            base["note"] = "数据库尚未创建，启动采集后会自动显示已保存战报。"
            return base
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT r.namespace,s.completeness,count(*) reports,coalesce(sum(s.event_count),0) events "
                "FROM report_records r JOIN report_snapshots s ON s.id=r.current_snapshot_id "
                "GROUP BY r.namespace,s.completeness")
            accounts = {}
            for row in rows:
                account = accounts.setdefault(row["namespace"], {"namespace": row["namespace"], "total": 0,
                    "complete": 0, "partial": 0, "legacy": 0, "events": 0})
                status = row["completeness"]
                account[status] = account.get(status, 0) + row["reports"]
                account["total"] += row["reports"]
                account["events"] += row["events"]
                base[status] = base.get(status, 0) + row["reports"]
                base["total"] += row["reports"]
                base["events"] += row["events"]
            base.update(available=True, accounts=sorted(accounts.values(), key=lambda x: x["namespace"]),
                        note="按当前最佳版本统计，每个账号的每封战报只计一次。")
        return base

    def list_reports(self, namespace: str | None = None, status: str | None = None,
                     search: str = "", limit: int = 100, offset: int = 0) -> dict:
        limit = _integer(limit, "limit", 1, 200)
        offset = _integer(offset, "offset", 0, 1_000_000)
        if status not in (None, "", "all", "complete", "partial", "legacy"):
            raise ValueError("Invalid completeness filter")
        if namespace is not None and (not isinstance(namespace, str) or len(namespace) > 500):
            raise ValueError("Invalid account namespace")
        if not isinstance(search, str) or len(search) > 200:
            raise ValueError("Search must contain at most 200 characters")
        where, params = [], []
        if namespace:
            where.append("r.namespace=?")
            params.append(namespace)
        if status not in (None, "", "all"):
            where.append("s.completeness=?")
            params.append(status)
        search = search.strip()
        search_mode = None
        if search:
            if search.isascii() and search.isdigit():
                where.append("r.battle_uid=?")
                params.append(search)
                search_mode = "exact_id"
            else:
                escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                where.append("(r.battle_uid LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM report_sides q "
                             "WHERE q.snapshot_id=s.id AND q.player_name LIKE ? ESCAPE '\\'))")
                params.extend((f"%{escaped}%", f"%{escaped}%"))
                search_mode = "id_or_player_name"
        query = (
            "SELECT r.id record_id,r.namespace,r.battle_uid,r.current_snapshot_id snapshot_id,"
            "r.first_seen,r.last_seen,s.completeness,s.event_count,s.source,s.imported_at,"
            "(SELECT q.player_name FROM report_sides q WHERE q.snapshot_id=s.id AND q.side='attacker') attacker_name,"
            "(SELECT q.player_name FROM report_sides q WHERE q.snapshot_id=s.id AND q.side IN ('defensive','defender') "
            "ORDER BY q.side LIMIT 1) defender_name "
            "FROM report_records r JOIN report_snapshots s ON s.id=r.current_snapshot_id"
        )
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY r.id DESC LIMIT ? OFFSET ?"
        with self._connection() as connection:
            rows = list(connection.execute(query, (*params, limit + 1, offset)))
            items = []
            for row in rows[:limit]:
                report = dict(row)
                metadata = self._metadata(connection, report["snapshot_id"])
                self._attach_display(report, metadata)
                items.append(report)
        return {"items": items, "has_more": len(rows) > limit, "limit": limit, "offset": offset,
                "search_mode": search_mode}

    def detail(self, snapshot_id: int, event_limit: int = 100, event_offset: int = 0) -> dict:
        snapshot_id = _integer(snapshot_id, "snapshot_id", 1, 2**63 - 1)
        event_limit = _integer(event_limit, "event_limit", 1, 200)
        event_offset = _integer(event_offset, "event_offset", 0, 10_000_000)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT s.id snapshot_id,r.namespace,r.battle_uid,s.completeness,s.event_count,s.source,"
                "s.imported_at,s.file_id,r.current_snapshot_id,r.first_seen,r.last_seen,"
                "substr(s.issues_json,1,24001) issues_json "
                "FROM report_snapshots s JOIN report_records r ON r.id=s.report_id WHERE s.id=?",
                (snapshot_id,)).fetchone()
            if row is None:
                raise KeyError(f"未找到战报快照 {snapshot_id}")
            report = dict(row)
            report["is_current"] = report["current_snapshot_id"] == snapshot_id
            report["issues"] = _json_preview(report.pop("issues_json"))
            metadata = self._metadata(connection, snapshot_id)
            self._attach_display(report, metadata)
            components = {}
            for row in connection.execute(
                    "SELECT name,available,complete,requested,substr(status_json,1,24001) status_json "
                    "FROM report_components WHERE snapshot_id=?", (snapshot_id,)):
                status = _json_preview(row["status_json"])
                components[row["name"]] = {
                    "name": row["name"], "name_zh": COMPONENT_NAMES.get(row["name"], row["name"]),
                    "available": bool(row["available"]),
                    "complete": None if row["complete"] is None else bool(row["complete"]),
                    "requested": None if row["requested"] is None else bool(row["requested"]),
                    "error": status.get("error") if isinstance(status, dict) else None,
                    "status": status,
                }
            for name, title in COMPONENT_NAMES.items():
                components.setdefault(name, {"name": name, "name_zh": title, "available": False,
                                            "complete": None, "requested": None, "error": None, "status": {}})
            semantics = self._semantics(connection, snapshot_id)
            meanings = {}
            if semantics["status"] == "ready":
                meaning_rows = list(connection.execute(
                        "SELECT kind,raw_value_json,name_zh,mapped,status FROM report_id_meanings "
                        "WHERE snapshot_id=? AND version=? AND kind IN ('hero','skill','formation') LIMIT 5001",
                        (snapshot_id, semantics["version"])))
                semantics["id_preview_truncated"] = len(meaning_rows) > 5000
                for row in meaning_rows[:5000]:
                    raw = _json_preview(row["raw_value_json"])
                    if row["mapped"] and row["name_zh"] and not isinstance(raw, (dict, list)):
                        meanings.setdefault((row["kind"], str(raw)), row["name_zh"])
            sides = []
            for row in connection.execute(
                    "SELECT id,side,army_uid,player_uid,player_name,formation_id,morale "
                    "FROM report_sides WHERE snapshot_id=? ORDER BY id", (snapshot_id,)):
                side = dict(row)
                side["name_zh"] = SIDE_NAMES.get(side["side"], side["side"])
                side["formation_name_zh"] = meanings.get(("formation", str(side["formation_id"])))
                side["heroes"] = []
                # The (side_id,ordinal) and (hero_id,ordinal) unique indexes keep
                # these small lookups off the entire historical hero table.
                hero_rows = list(connection.execute(
                        "SELECT id,ordinal,hero_uid,position,hero_name,level,troops_initial,troops_remaining,"
                        "troops_dead,troops_wounded,substr(raw_json,1,24001) raw_json "
                        "FROM report_heroes WHERE side_id=? ORDER BY ordinal LIMIT 101", (side["id"],)))
                side["heroes_preview_truncated"] = len(hero_rows) > 100
                for hero_row in hero_rows[:100]:
                    hero = dict(hero_row)
                    hero["name_zh"] = meanings.get(("hero", str(hero["hero_uid"]))) or hero["hero_name"]
                    hero["raw"] = _json_preview(hero.pop("raw_json"))
                    hero["skills"] = []
                    skill_rows = list(connection.execute(
                            "SELECT skill_uid,level,order_level,substr(raw_json,1,24001) raw_json "
                            "FROM report_skills WHERE hero_id=? ORDER BY ordinal LIMIT 101", (hero["id"],)))
                    hero["skills_preview_truncated"] = len(skill_rows) > 100
                    for skill_row in skill_rows[:100]:
                        skill = dict(skill_row)
                        skill["name_zh"] = meanings.get(("skill", str(skill["skill_uid"])))
                        skill["raw"] = _json_preview(skill.pop("raw_json"))
                        hero["skills"].append(skill)
                    side["heroes"].append(hero)
                sides.append(side)
            if semantics["status"] == "ready":
                event_query = (
                    "SELECT e.sequence,e.event_id,e.tab,substr(e.raw_json,1,24001) raw_json,"
                    "m.event_name_zh,m.summary_zh,substr(m.parameters_json,1,24001) parameters_json,m.status "
                    "FROM report_events e LEFT JOIN report_event_meanings m ON m.snapshot_id=e.snapshot_id "
                    "AND m.sequence=e.sequence AND m.version=? WHERE e.snapshot_id=? AND e.sequence>=? "
                    "ORDER BY e.sequence LIMIT ?"
                )
                event_rows = list(connection.execute(event_query,
                    (semantics["version"], snapshot_id, event_offset, event_limit + 1)))
            else:
                event_rows = list(connection.execute(
                    "SELECT sequence,event_id,tab,substr(raw_json,1,24001) raw_json,NULL event_name_zh,"
                    "NULL summary_zh,NULL parameters_json,NULL status FROM report_events "
                    "WHERE snapshot_id=? AND sequence>=? ORDER BY sequence LIMIT ?",
                    (snapshot_id, event_offset, event_limit + 1)))
            events = []
            for row in event_rows[:event_limit]:
                event = dict(row)
                event["raw"] = _json_preview(event.pop("raw_json"))
                event["parameters"] = _json_preview(event.pop("parameters_json"))
                event["status"] = event["status"] or "not_generated"
                events.append(event)
            return {"report": report, "components": components,
                    "missing_components": [name for name in COMPONENT_NAMES if
                        not components[name]["available"] or components[name]["complete"] is not True
                        or components[name]["error"]],
                    "sides": sides, "events": events, "event_total": report["event_count"],
                    "event_limit": event_limit, "event_offset": event_offset,
                    "event_has_more": len(event_rows) > event_limit, "semantics": semantics,
                    "metadata": metadata,
                    "preview_note": "界面按页显示事件，大字段仅预览；完整原文与全部版本仍保存在数据库。"}

    def original_preview(self, snapshot_id: int, max_bytes: int = 24000) -> dict:
        snapshot_id = _integer(snapshot_id, "snapshot_id", 1, 2**63 - 1)
        max_bytes = _integer(max_bytes, "max_bytes", 100, 100000)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT f.byte_count,f.sha256,substr(f.raw_bytes,1,?) preview "
                "FROM report_snapshots s JOIN report_files f ON f.id=s.file_id WHERE s.id=?",
                (max_bytes + 1, snapshot_id)).fetchone()
            if row is None:
                raise KeyError(snapshot_id)
            raw = bytes(row["preview"])
            return {"text": raw[:max_bytes].decode("utf-8-sig", "replace"),
                    "bytes": row["byte_count"], "sha256": row["sha256"],
                    "truncated": row["byte_count"] > max_bytes,
                    "note": "仅缩短界面预览；导出原文可获得完整字节。"}

    def export_original(self, snapshot_id: int, path: str | Path) -> dict:
        snapshot_id = _integer(snapshot_id, "snapshot_id", 1, 2**63 - 1)
        from parser.report_archive import ReportArchive
        with ReportArchive(self.db_path, read_only=True) as archive:
            archive.connection.execute("PRAGMA busy_timeout=500")
            return archive.export_original(snapshot_id, path)


# A descriptive alias is convenient to applications that already use Data for
# another feature; both names expose precisely the same bounded read interface.
BattleArchiveData = BattleData
