"""Lossless SQLite archive for runtime battle reports (Python standard library only).

The original bytes are authoritative. Query tables are reproducible projections,
and ``report_fields`` indexes JSON nodes using RFC 6901 JSON Pointers. The
compact policy omits redundant event-list descendants from that projection;
lean also omits recursive projections of statics and snapshots components.
complete event objects and authoritative original bytes remain available.
``complete`` describes the five exported components, never account-wide coverage.
Old exports have status ``legacy`` even when their source is ``all``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


COMPONENTS = ("basic", "detail", "all", "statics", "snapshots")
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = ("1", "2")
STORAGE_MODES = ("full", "compact", "lean")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    # ASCII escaping also preserves unpaired JSON Unicode surrogates in SQLite.
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON object key: {key!r}; original bytes retained")
        value[key] = item
    return value


def _invalid_number(value: str) -> None:
    raise ValueError(f"Non-finite JSON number: {value}")


def _decode(raw: bytes | str) -> Any:
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_number)


def _token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def iter_fields(value: Any, pointer: str = "", parent: str | None = None,
                key: str | None = None, ordinal: int = 0) -> Iterator[tuple]:
    """Yield all nodes, including null/empty containers; numeric text is exact."""
    if isinstance(value, dict):
        kind, scalar = "object", None
        node_json = "{}"
    elif isinstance(value, list):
        kind, scalar = "array", None
        node_json = "[]"
    elif value is None:
        kind, scalar, node_json = "null", None, "null"
    elif isinstance(value, bool):
        kind = "boolean"
        scalar = node_json = "true" if value else "false"
    elif isinstance(value, int):
        kind, scalar = "integer", str(value)
        # JSON's native integer representation is exactly its decimal text.
        # Keep the original encoder path for subclasses with custom methods.
        node_json = scalar if type(value) is int else _json(value)
    elif isinstance(value, float):
        kind = "number"
        scalar = node_json = _json(value)
    elif isinstance(value, str):
        kind, scalar = "string", value
        node_json = json.encoder.encode_basestring_ascii(value) if type(value) is str else _json(value)
        # Store unusual surrogate-bearing strings safely; value_json is canonical.
        if type(value) is not str or not value.isascii():
            scalar = scalar.encode("utf-8", "backslashreplace").decode("utf-8")
    else:
        raise TypeError(f"Unsupported JSON value at {pointer}: {type(value).__name__}")
    yield pointer, parent, key, ordinal, kind, node_json, scalar
    children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for index, (child_key, child) in enumerate(children):
        text_key = str(child_key)
        yield from iter_fields(child, pointer + "/" + _token(text_key), pointer, text_key, index)


def _indexed_fields(fields: list[tuple], storage_mode: str) -> list[tuple]:
    """Select query nodes without altering original data or quality checks.

    Event bodies already have ordered rows in report_events and occur again in
    the payload/components. Keep their array roots, but avoid a second large
    per-scalar index. Unknown branches and all non-event metadata stay indexed.
    """
    if storage_mode == "full":
        return fields
    if storage_mode not in ("compact", "lean"):
        raise ValueError(f"Unsupported report storage mode: {storage_mode!r}")
    selected = []
    for field in fields:
        pointer = field[0]
        if storage_mode == "lean" and pointer.startswith(("/components/snapshots/", "/components/statics/")):
            continue
        if pointer.startswith("/data/eventDataList/"):
            continue
        parts = pointer.split("/", 4)
        if len(parts) == 5 and parts[1] == "components" and parts[3] == "eventDataList":
            continue
        selected.append(field)
    return selected


def _uid(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    return text if text.strip() else None


def _int(value: Any) -> int | None:
    # Missing and unrecognized values stay NULL, never silently become zero.
    if isinstance(value, int) and not isinstance(value, bool) and -(2**63) <= value < 2**63:
        return value
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value).encode("utf-8", "backslashreplace").decode("utf-8")
    return None


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _write_path(path: str | Path) -> Path:
    result = Path(path).expanduser().resolve()
    return result


_DDL = """
CREATE TABLE IF NOT EXISTS report_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS report_files(
 id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, sha256 TEXT NOT NULL,
 raw_bytes BLOB NOT NULL, byte_count INTEGER NOT NULL, source_path TEXT NOT NULL,
 parse_status TEXT NOT NULL DEFAULT 'pending', error TEXT,
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
 UNIQUE(namespace,sha256));
CREATE TABLE IF NOT EXISTS report_imports(
 id INTEGER PRIMARY KEY, file_id INTEGER REFERENCES report_files(id),
 namespace TEXT NOT NULL, source_path TEXT NOT NULL, status TEXT NOT NULL,
 error TEXT, imported_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS report_records(
 id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, battle_uid TEXT NOT NULL,
 current_snapshot_id INTEGER REFERENCES report_snapshots(id),
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
 UNIQUE(namespace,battle_uid));
CREATE TABLE IF NOT EXISTS report_snapshots(
 id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES report_records(id),
 file_id INTEGER NOT NULL REFERENCES report_files(id), document_index INTEGER NOT NULL,
 payload_json TEXT NOT NULL, source TEXT, capture_version INTEGER,
 completeness TEXT NOT NULL, quality_json TEXT NOT NULL, issues_json TEXT NOT NULL,
 event_count INTEGER NOT NULL, field_count INTEGER NOT NULL, imported_at TEXT NOT NULL,
 storage_mode TEXT NOT NULL DEFAULT 'full', indexed_field_count INTEGER,
 UNIQUE(file_id,document_index));
CREATE TABLE IF NOT EXISTS report_fields(
 snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id), pointer TEXT NOT NULL,
 parent_pointer TEXT, field_key TEXT, ordinal INTEGER NOT NULL,
 node_type TEXT NOT NULL, value_json TEXT NOT NULL, scalar_text TEXT,
 PRIMARY KEY(snapshot_id,pointer));
CREATE INDEX IF NOT EXISTS report_fields_key ON report_fields(field_key,node_type);
CREATE TABLE IF NOT EXISTS report_components(
 snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id), name TEXT NOT NULL,
 payload_json TEXT, requested INTEGER, available INTEGER NOT NULL, complete INTEGER,
 status_json TEXT NOT NULL, PRIMARY KEY(snapshot_id,name));
CREATE TABLE IF NOT EXISTS report_events(
 snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id), sequence INTEGER NOT NULL,
 source_pointer TEXT NOT NULL, event_id TEXT, tab TEXT, raw_json TEXT NOT NULL,
 PRIMARY KEY(snapshot_id,sequence));
CREATE INDEX IF NOT EXISTS report_events_id ON report_events(event_id);
CREATE TABLE IF NOT EXISTS report_sides(
 id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id),
 side TEXT NOT NULL, army_uid TEXT, player_uid TEXT, player_name TEXT, formation_id TEXT,
 morale INTEGER, raw_json TEXT NOT NULL, UNIQUE(snapshot_id,side));
CREATE TABLE IF NOT EXISTS report_heroes(
 id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id),
 side_id INTEGER NOT NULL REFERENCES report_sides(id), ordinal INTEGER NOT NULL,
 hero_uid TEXT, position INTEGER, hero_name TEXT, level INTEGER,
 troops_initial INTEGER, troops_remaining INTEGER, troops_dead INTEGER, troops_wounded INTEGER,
 raw_json TEXT NOT NULL, UNIQUE(side_id,ordinal));
CREATE TABLE IF NOT EXISTS report_skills(
 id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id),
 hero_id INTEGER NOT NULL REFERENCES report_heroes(id), ordinal INTEGER NOT NULL,
 skill_uid TEXT, level INTEGER, order_level INTEGER, raw_json TEXT NOT NULL,
 UNIQUE(hero_id,ordinal));
CREATE VIEW IF NOT EXISTS report_current_snapshots AS
 SELECT r.namespace,r.battle_uid,s.* FROM report_records r
 JOIN report_snapshots s ON s.id=r.current_snapshot_id;
CREATE VIEW IF NOT EXISTS report_current_events AS
 SELECT r.namespace,r.battle_uid,e.* FROM report_records r
 JOIN report_events e ON e.snapshot_id=r.current_snapshot_id;
CREATE VIEW IF NOT EXISTS report_current_heroes AS
 SELECT r.namespace,r.battle_uid,h.* FROM report_records r
 JOIN report_heroes h ON h.snapshot_id=r.current_snapshot_id;
CREATE VIEW IF NOT EXISTS report_current_fields AS
 SELECT r.namespace,r.battle_uid,f.* FROM report_records r
 JOIN report_fields f ON f.snapshot_id=r.current_snapshot_id;
CREATE VIEW IF NOT EXISTS report_current_sides AS
 SELECT r.namespace,r.battle_uid,s.* FROM report_records r
 JOIN report_sides s ON s.snapshot_id=r.current_snapshot_id;
CREATE VIEW IF NOT EXISTS report_current_skills AS
 SELECT r.namespace,r.battle_uid,s.* FROM report_records r
 JOIN report_skills s ON s.snapshot_id=r.current_snapshot_id;
"""


def _main_data(payload: dict) -> tuple[dict, str]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data, "/data"
    components = _mapping(payload.get("components"))
    for name in ("all", "detail", "basic"):
        if isinstance(components.get(name), dict):
            return components[name], "/components/" + name
    raise ValueError("Report has no data object or usable basic/detail/all component")


def _settlement_source(payload: dict) -> tuple[dict, str | None]:
    """Use an actually captured settlement without changing the event source.

    Detail responses can contain only eventDataList. Their basic component can
    already contain the original sides/heroes. Keep that component's real JSON
    pointer rather than constructing a synthetic merged /data document.
    """
    data, pointer = _main_data(payload)
    components = _mapping(payload.get("components"))
    candidates = [(data, pointer)] + [(components.get(name), "/components/" + name)
                                      for name in ("all", "detail", "basic")]
    uid = _uid(payload.get("battleID")) or _uid(data.get("battleID"))
    for value, prefix in candidates:
        if not isinstance(value, dict):
            continue
        settlement = _mapping(value.get("battleSettlement"))
        if not any(isinstance(settlement.get(side), dict) for side in ("attacker", "defensive", "defender")):
            continue
        candidate_uid = _uid(value.get("battleID"))
        if uid is not None and candidate_uid is not None and candidate_uid != uid:
            raise ValueError(f"Conflicting settlement battleID: {uid} versus {candidate_uid}")
        return settlement, prefix + "/battleSettlement"
    return {}, None


def _settlement_values(settlement: dict) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """Pure expected relational values; IDs are assigned only while writing."""
    sides, heroes, skills = [], [], []
    for side in ("attacker", "defensive", "defender"):
        army = settlement.get(side)
        if not isinstance(army, dict):
            continue
        avatar = _mapping(army.get("avatar_data"))
        sides.append((side, _text(army.get("army_id")), _text(avatar.get("avatar_id")),
                      _text(avatar.get("avatar_name")), _text(army.get("formationId")),
                      _int(army.get("morale")), _json(army)))
        members = army.get("army_member")
        for ordinal, hero in enumerate(members if isinstance(members, list) else []):
            h = _mapping(hero)
            heroes.append((side, ordinal, _text(h.get("heros_id")), _int(h.get("index")),
                           _text(h.get("name")), _int(h.get("hero_lv")), _int(h.get("origin_troop_num")),
                           _int(h.get("troop_num")), _int(h.get("dead_num")), _int(h.get("wound_num")), _json(h)))
            source_skills = h.get("skills")
            for skill_index, skill in enumerate(source_skills if isinstance(source_skills, list) else []):
                s = _mapping(skill)
                skills.append((side, ordinal, skill_index, _text(s.get("skillId")),
                               _int(s.get("level")), _int(s.get("orderLevel")), _json(s)))
    return sides, heroes, skills


def _quality(payload: dict, fields: list[tuple], data: dict) -> tuple[str, list, list]:
    components = _mapping(payload.get("components"))
    statuses = _mapping(payload.get("componentStatus"))
    issues = list(payload.get("serializationIssues") or []) if isinstance(payload.get("serializationIssues"), list) else []
    for pointer, _, _, _, kind, _, value in fields:
        if kind == "string" and (value in ("<cycle>", "<max_depth>") or
                                  (value and value.startswith(("<function:", "<userdata:", "<thread:")))):
            issues.append({"path": pointer, "reason": "serialization_placeholder"})
    events = data.get("eventDataList")
    event_count = len(events) if isinstance(events, list) else 0
    if not isinstance(events, list):
        issues.append({"path": "/data/eventDataList", "reason": "event_list_missing_or_not_array"})
    if "eventCount" in payload and payload["eventCount"] != event_count:
        issues.append({"path": "/eventCount", "reason": "event_count_mismatch", "actual": event_count})
    if payload.get("captureComplete") is False:
        issues.append({"path": "/captureComplete", "reason": "collector_reported_incomplete"})
    if payload.get("captureWarnings"):
        issues.append({"path": "/captureWarnings", "reason": "collector_warnings", "warnings": payload["captureWarnings"]})
    available = [name for name in COMPONENTS if name in components and components[name] is not None]
    completed = [name for name in available if _mapping(statuses.get(name)).get("complete") is True
                 and _mapping(statuses.get(name)).get("available") is True
                 and not _mapping(statuses.get(name)).get("error")]
    modern = (_int(payload.get("captureVersion")) or 0) >= 2
    completeness = "complete" if modern and len(completed) == len(COMPONENTS) and not issues else "partial" if modern or issues else "legacy"
    # Lexicographic preference; more complete data cannot be replaced by a later
    # small/basic export. Every version remains accessible in report_snapshots.
    rank = [int(completeness == "complete"), int(not issues), len(completed),
            len(available), {"all": 3, "detail": 2, "basic": 1}.get(payload.get("source"), 0),
            event_count, len(fields)]
    return completeness, rank, issues


class ReportArchive:
    """Atomic, append-only raw archive with best-snapshot query projections."""

    def __init__(self, db_path: str | Path, read_only: bool = False, semantic_mapper=None,
                 storage_mode: str = "full"):
        if storage_mode not in STORAGE_MODES:
            raise ValueError(f"storage_mode must be one of {STORAGE_MODES}")
        self.storage_mode = storage_mode
        self.semantic_mapper = semantic_mapper
        self._semantic_archive = None
        self.read_only = read_only
        self.db_path = Path(db_path).expanduser().resolve() if read_only else _write_path(db_path)
        if read_only:
            self.connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=30)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(self.db_path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        try:
            # Check before changing journal mode or creating tables. An old
            # launcher must never silently modify a newer database layout.
            version = None
            if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_meta'").fetchone():
                version = self.connection.execute("SELECT value FROM report_meta WHERE key='schema_version'").fetchone()
                if version is not None and version[0] not in SUPPORTED_SCHEMA_VERSIONS:
                    raise ValueError(f"Unsupported archive schema version {version[0]!r}; this program supports {SUPPORTED_SCHEMA_VERSIONS}")
            if read_only:
                required = {"report_" + name for name in ("meta", "files", "imports", "records", "snapshots", "fields", "components", "events", "sides", "heroes", "skills")}
                required.update("report_current_" + name for name in ("snapshots", "events", "heroes", "fields", "sides", "skills"))
                actual = {r[0] for r in self.connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
                if version is None or required - actual:
                    raise ValueError("Not an initialized battle archive database; use import/collect to create one")
                # This changes only connection-local temporary storage. No
                # persistent journal/schema setting is modified in read mode.
                self.connection.execute("PRAGMA temp_store=MEMORY")
                self._snapshot_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(report_snapshots)")}
                return
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=30000")
            self.connection.execute("PRAGMA temp_store=MEMORY")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.executescript(_DDL)
            with self.connection:
                # Additive metadata lets old full-index snapshots coexist with
                # new compact ones. A read-only open never changes old schemas.
                self.connection.execute("BEGIN IMMEDIATE")
                columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(report_snapshots)")}
                if "storage_mode" not in columns:
                    self.connection.execute("ALTER TABLE report_snapshots ADD COLUMN storage_mode TEXT NOT NULL DEFAULT 'full'")
                if "indexed_field_count" not in columns:
                    self.connection.execute("ALTER TABLE report_snapshots ADD COLUMN indexed_field_count INTEGER")
                self.connection.execute("INSERT OR IGNORE INTO report_meta VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            self._snapshot_columns = columns | {"storage_mode", "indexed_field_count"}
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> "ReportArchive":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _attempt(self, file_id: int | None, namespace: str, path: str,
                 status: str, error: str | None = None) -> None:
        self.connection.execute(
            "INSERT INTO report_imports(file_id,namespace,source_path,status,error,imported_at) VALUES(?,?,?,?,?,?)",
            (file_id, namespace, path, status, error, _now()))

    def import_file(self, path: str | Path, namespace: str = "default") -> dict:
        """Import one file. Malformed/unreadable input returns status='failed'."""
        if self.read_only:
            raise PermissionError("Archive was opened read-only")
        source = str(Path(path).resolve())
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            with self.connection:
                self._attempt(None, namespace, source, "failed", str(exc))
            return {"status": "failed", "path": source, "error": str(exc), "snapshot_ids": []}
        return self.import_bytes(raw, source_path=source, namespace=namespace)

    def import_bytes(self, raw: bytes, source_path: str = "<memory>", namespace: str = "default") -> dict:
        result = self._import_original_bytes(raw, source_path, namespace)
        if self.semantic_mapper is not None and result.get("status") in {"imported", "duplicate"}:
            try:
                semantics = self.semantic_archive()
                items = []
                for sid in result.get("snapshot_ids", []):
                    # Duplicates retain their original storage policy even if
                    # imported through a differently configured connection.
                    stored_mode = self.connection.execute(
                        "SELECT storage_mode FROM report_snapshots WHERE id=?", (sid,)).fetchone()[0]
                    items.append(semantics.enrich_snapshot(sid, include_events=stored_mode == "full"))
                result["semantics"] = {"version": self.semantic_mapper.version, "results": items,
                                       "failed": sum(item.get("status") == "failed" for item in items)}
            except Exception as exc:
                # Derived explanations must never invalidate committed originals.
                result["semantics"] = {"failed": 1, "error": f"{type(exc).__name__}: {exc}"}
        return result

    def semantic_archive(self):
        if self.read_only:
            raise PermissionError("Archive was opened read-only")
        if self.semantic_mapper is None:
            raise ValueError("No semantic dictionary was configured")
        if self._semantic_archive is None:
            from .report_semantics import SemanticArchive
            self._semantic_archive = SemanticArchive(self.connection, self.semantic_mapper)
        return self._semantic_archive

    def _import_original_bytes(self, raw: bytes, source_path: str = "<memory>", namespace: str = "default") -> dict:
        if self.read_only:
            raise PermissionError("Archive was opened read-only")
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be a nonempty account/server identifier")
        if not isinstance(raw, bytes):
            raise TypeError("raw must contain the original bytes")
        digest = hashlib.sha256(raw).hexdigest()
        now = _now()
        # Raw preservation commits first; even a parsing/indexing failure cannot
        # destroy the input. All projections for one file share one transaction.
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO report_files(namespace,sha256,raw_bytes,byte_count,source_path,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)",
                (namespace, digest, raw, len(raw), source_path, now, now))
            row = self.connection.execute("SELECT id,parse_status FROM report_files WHERE namespace=? AND sha256=?", (namespace, digest)).fetchone()
            file_id = row["id"]
            self.connection.execute("UPDATE report_files SET last_seen=? WHERE id=?", (now, file_id))
            if row["parse_status"] == "parsed":
                self._attempt(file_id, namespace, source_path, "duplicate")
                ids = [r[0] for r in self.connection.execute("SELECT id FROM report_snapshots WHERE file_id=? ORDER BY document_index", (file_id,))]
                return {"status": "duplicate", "file_id": file_id, "sha256": digest, "path": source_path, "snapshot_ids": ids}
        try:
            document = _decode(raw)
            reports = document if isinstance(document, list) else [document]
            if not reports:
                raise ValueError("Empty report batch")
            snapshots = []
            with self.connection:
                # A second importer may have committed this same file while we
                # decoded it. Serialize the indexing decision, then recheck it.
                self.connection.execute("BEGIN IMMEDIATE")
                status = self.connection.execute("SELECT parse_status FROM report_files WHERE id=?", (file_id,)).fetchone()[0]
                if status == "parsed":
                    self._attempt(file_id, namespace, source_path, "duplicate")
                    ids = [r[0] for r in self.connection.execute("SELECT id FROM report_snapshots WHERE file_id=? ORDER BY document_index", (file_id,))]
                    return {"status": "duplicate", "file_id": file_id, "sha256": digest, "path": source_path, "snapshot_ids": ids}
                for index, payload in enumerate(reports):
                    if not isinstance(payload, dict):
                        raise ValueError(f"Report {index} must be an object")
                    snapshots.append(self._index_report(payload, namespace, file_id, index))
                self.connection.execute("UPDATE report_files SET parse_status='parsed',error=NULL WHERE id=?", (file_id,))
                self._attempt(file_id, namespace, source_path, "imported")
            return {"status": "imported", "file_id": file_id, "sha256": digest, "path": source_path,
                    "snapshot_ids": [s["id"] for s in snapshots], "reports": snapshots}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            with self.connection:
                self.connection.execute("UPDATE report_files SET parse_status='failed',error=? WHERE id=? AND parse_status<>'parsed'", (error, file_id))
                self._attempt(file_id, namespace, source_path, "failed", error)
            return {"status": "failed", "file_id": file_id, "sha256": digest, "path": source_path,
                    "error": error, "snapshot_ids": []}

    def _store_settlement_projection(self, snapshot_id: int, settlement: dict, *,
                                     allow_existing: bool = False) -> dict[str, int]:
        """Append missing relation rows, never replace an existing projection."""
        sides, heroes, skills = _settlement_values(settlement)
        if allow_existing:
            existing_sides = [tuple(row) for row in self.connection.execute(
                "SELECT side,army_uid,player_uid,player_name,formation_id,morale,raw_json "
                "FROM report_sides WHERE snapshot_id=?", (snapshot_id,))]
            existing_heroes = [tuple(row) for row in self.connection.execute(
                "SELECT s.side,h.ordinal,h.hero_uid,h.position,h.hero_name,h.level,h.troops_initial,"
                "h.troops_remaining,h.troops_dead,h.troops_wounded,h.raw_json FROM report_heroes h "
                "JOIN report_sides s ON s.id=h.side_id WHERE h.snapshot_id=?", (snapshot_id,))]
            existing_skills = [tuple(row) for row in self.connection.execute(
                "SELECT s.side,h.ordinal,k.ordinal,k.skill_uid,k.level,k.order_level,k.raw_json "
                "FROM report_skills k JOIN report_heroes h ON h.id=k.hero_id "
                "JOIN report_sides s ON s.id=h.side_id WHERE k.snapshot_id=?", (snapshot_id,))]
            wrong_owner = self.connection.execute(
                "SELECT 1 FROM report_heroes h JOIN report_sides s ON s.id=h.side_id "
                "WHERE h.snapshot_id=? AND s.snapshot_id<>h.snapshot_id "
                "UNION ALL SELECT 1 FROM report_skills k JOIN report_heroes h ON h.id=k.hero_id "
                "JOIN report_sides s ON s.id=h.side_id WHERE k.snapshot_id=? "
                "AND (k.snapshot_id<>h.snapshot_id OR k.snapshot_id<>s.snapshot_id) LIMIT 1",
                (snapshot_id, snapshot_id)).fetchone()
            if wrong_owner or any(not set(actual).issubset(expected) for actual, expected in (
                    (existing_sides, set(sides)), (existing_heroes, set(heroes)), (existing_skills, set(skills)))):
                raise ValueError("Existing settlement projection conflicts with the captured source; no rows replaced")
        existing_side_ids = {row["side"]: row["id"] for row in self.connection.execute(
            "SELECT id,side FROM report_sides WHERE snapshot_id=?", (snapshot_id,))}
        existing_hero_ids = {(row["side"], row["ordinal"]): row["id"] for row in self.connection.execute(
            "SELECT h.id,s.side,h.ordinal FROM report_heroes h JOIN report_sides s ON s.id=h.side_id "
            "WHERE h.snapshot_id=?", (snapshot_id,))}
        existing_skill_keys = {(row["side"], row["hero_ordinal"], row["ordinal"]) for row in self.connection.execute(
            "SELECT s.side,h.ordinal hero_ordinal,k.ordinal FROM report_skills k "
            "JOIN report_heroes h ON h.id=k.hero_id JOIN report_sides s ON s.id=h.side_id "
            "WHERE k.snapshot_id=?", (snapshot_id,))}
        inserted = {"sides": 0, "heroes": 0, "skills": 0}
        for row in sides:
            if row[0] in existing_side_ids:
                continue
            cursor = self.connection.execute(
                "INSERT INTO report_sides(snapshot_id,side,army_uid,player_uid,player_name,formation_id,morale,raw_json) "
                "VALUES(?,?,?,?,?,?,?,?)", (snapshot_id, *row))
            existing_side_ids[row[0]] = cursor.lastrowid
            inserted["sides"] += 1
        for row in heroes:
            key = row[:2]
            if key in existing_hero_ids:
                continue
            cursor = self.connection.execute(
                "INSERT INTO report_heroes(snapshot_id,side_id,ordinal,hero_uid,position,hero_name,level,"
                "troops_initial,troops_remaining,troops_dead,troops_wounded,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (snapshot_id, existing_side_ids[row[0]], *row[1:]))
            existing_hero_ids[key] = cursor.lastrowid
            inserted["heroes"] += 1
        for row in skills:
            if row[:3] in existing_skill_keys:
                continue
            self.connection.execute(
                "INSERT INTO report_skills(snapshot_id,hero_id,ordinal,skill_uid,level,order_level,raw_json) "
                "VALUES(?,?,?,?,?,?,?)", (snapshot_id, existing_hero_ids[row[:2]], *row[2:]))
            inserted["skills"] += 1
        return inserted

    def _index_report(self, payload: dict, namespace: str, file_id: int, index: int) -> dict:
        data, data_pointer = _main_data(payload)
        battle_uid = _uid(payload.get("battleID")) or _uid(data.get("battleID"))
        if battle_uid is None:
            raise ValueError("Missing or invalid battleID; IDs must be strings or integers")
        for candidate in [data] + list(_mapping(payload.get("components")).values()):
            candidate_id = _uid(_mapping(candidate).get("battleID"))
            if candidate_id is not None and candidate_id != battle_uid:
                raise ValueError(f"Conflicting battleID: {battle_uid} versus {candidate_id}")
        fields = list(iter_fields(payload))
        indexed_fields = _indexed_fields(fields, self.storage_mode)
        completeness, quality, issues = _quality(payload, fields, data)
        events = data.get("eventDataList") if isinstance(data.get("eventDataList"), list) else []
        now = _now()
        self.connection.execute("INSERT OR IGNORE INTO report_records(namespace,battle_uid,first_seen,last_seen) VALUES(?,?,?,?)", (namespace, battle_uid, now, now))
        record = self.connection.execute("SELECT id,current_snapshot_id FROM report_records WHERE namespace=? AND battle_uid=?", (namespace, battle_uid)).fetchone()
        cursor = self.connection.execute(
            "INSERT INTO report_snapshots(report_id,file_id,document_index,payload_json,source,capture_version,completeness,quality_json,issues_json,event_count,field_count,imported_at,storage_mode,indexed_field_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record["id"], file_id, index, _json(payload), _text(payload.get("source")), _int(payload.get("captureVersion")),
             completeness, _json(quality), _json(issues), len(events), len(fields), now,
             self.storage_mode, len(indexed_fields)))
        snapshot_id = cursor.lastrowid
        self.connection.executemany("INSERT INTO report_fields VALUES(?,?,?,?,?,?,?,?)", ((snapshot_id, *field) for field in indexed_fields))
        components = _mapping(payload.get("components"))
        statuses = _mapping(payload.get("componentStatus"))
        for name in sorted(set(COMPONENTS) | set(components) | set(statuses)):
            status = _mapping(statuses.get(name))
            available = name in components and components[name] is not None
            requested = int(status["requested"]) if isinstance(status.get("requested"), bool) else None
            complete = int(status["complete"] is True and available and status.get("available") is True and not status.get("error")) if "complete" in status else None
            self.connection.execute("INSERT INTO report_components VALUES(?,?,?,?,?,?,?)", (
                snapshot_id, name, _json(components[name]) if name in components else None,
                requested, int(available), complete, _json(status)))
        self.connection.executemany("INSERT INTO report_events VALUES(?,?,?,?,?,?)", (
            (snapshot_id, seq, data_pointer + "/eventDataList/" + str(seq),
             _text(_mapping(event).get("eventId")), _text(_mapping(event).get("tab")), _json(event))
            for seq, event in enumerate(events)))
        settlement, settlement_pointer = _settlement_source(payload)
        self._store_settlement_projection(snapshot_id, settlement)
        current_id = record["current_snapshot_id"]
        current_quality = None if current_id is None else json.loads(self.connection.execute("SELECT quality_json FROM report_snapshots WHERE id=?", (current_id,)).fetchone()[0])
        # A larger wrapper or extra incomplete component cannot justify losing
        # an already captured full event list or downgrading all -> basic.
        selected = current_quality is None or (
            quality >= current_quality and quality[4] >= current_quality[4]
            and quality[5] >= current_quality[5])
        self.connection.execute("UPDATE report_records SET last_seen=?,current_snapshot_id=? WHERE id=?", (now, snapshot_id if selected else current_id, record["id"]))
        return {"id": snapshot_id, "battle_uid": battle_uid, "completeness": completeness,
                "event_count": len(events), "field_count": len(fields), "selected_current": selected, "issues": issues,
                "storage_mode": self.storage_mode, "indexed_field_count": len(indexed_fields),
                "capture_kind": payload.get("captureKind"), "component_status": statuses,
                "settlement_source_pointer": settlement_pointer}

    @staticmethod
    def _explicit_snapshot_ids(snapshot_ids) -> list[int]:
        if isinstance(snapshot_ids, (str, bytes, int)):
            raise ValueError("Pass an explicit sequence of positive snapshot IDs")
        try:
            values = list(snapshot_ids)
        except TypeError as exc:
            raise ValueError("Pass an explicit sequence of positive snapshot IDs") from exc
        if any(isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63 for value in values):
            raise ValueError("Snapshot IDs must be positive SQLite integers")
        return list(dict.fromkeys(values))

    def repair_settlement_projections(self, snapshot_ids) -> dict:
        """Add omitted sides/heroes/skills for explicit snapshots, one short TX each.

        Original files, snapshots, recursive fields, events, completeness and
        current selection are immutable here. Existing rows must be exact
        subsets of the captured source; conflicts are reported without removal.
        """
        if self.read_only:
            raise PermissionError("Archive was opened read-only")
        ids = self._explicit_snapshot_ids(snapshot_ids)
        if self.connection.in_transaction:
            raise RuntimeError("Finish the caller transaction before repairing settlement projections")
        result = {"snapshots": [], "repaired": 0, "unchanged": 0, "failed": 0}
        for snapshot_id in ids:
            try:
                row = self.connection.execute(
                    "SELECT s.payload_json,s.file_id,s.document_index,s.report_id,r.battle_uid,r.namespace,"
                    "r.current_snapshot_id,f.sha256,f.byte_count FROM report_snapshots s "
                    "JOIN report_records r ON r.id=s.report_id JOIN report_files f ON f.id=s.file_id "
                    "WHERE s.id=?", (snapshot_id,)).fetchone()
                if row is None:
                    raise KeyError(f"Unknown snapshot ID {snapshot_id}")
                raw = self.original_file_bytes(row["file_id"])
                if hashlib.sha256(raw).hexdigest() != row["sha256"] or len(raw) != row["byte_count"]:
                    raise ValueError("Original file hash does not match; no projection repaired")
                document = _decode(raw)
                original = document[row["document_index"]] if isinstance(document, list) else document
                payload = _decode(row["payload_json"])
                if not isinstance(original, dict) or _json(payload) != _json(original):
                    raise ValueError("Snapshot no longer matches its original file; no projection repaired")
                data, _ = _main_data(payload)
                if (_uid(payload.get("battleID")) or _uid(data.get("battleID"))) != row["battle_uid"]:
                    raise ValueError("Snapshot battle ID does not match its report record")
                settlement, pointer = _settlement_source(payload)
                # All decoding and hashing above occurs before the writer lock.
                with self.connection:
                    self.connection.execute("BEGIN IMMEDIATE")
                    current = self.connection.execute(
                        "SELECT payload_json,file_id,document_index,report_id FROM report_snapshots WHERE id=?",
                        (snapshot_id,)).fetchone()
                    if current is None or tuple(current) != tuple(row[key] for key in (
                            "payload_json", "file_id", "document_index", "report_id")):
                        raise ValueError("Snapshot changed during repair; no projection repaired")
                    if pointer is not None:
                        source_field = self.connection.execute(
                            "SELECT node_type FROM report_fields WHERE snapshot_id=? AND pointer=?",
                            (snapshot_id, pointer)).fetchone()
                        if source_field is None or source_field[0] != "object":
                            raise ValueError("Captured settlement JSON pointer is not indexed; no projection repaired")
                    inserted = self._store_settlement_projection(snapshot_id, settlement, allow_existing=True)
                status = "repaired" if any(inserted.values()) else "unchanged"
                result[status] += 1
                result["snapshots"].append({"snapshot_id": snapshot_id, "battle_uid": row["battle_uid"],
                    "namespace": row["namespace"], "status": status, "inserted": inserted,
                    "settlement_source_pointer": pointer, "raw_sha256": row["sha256"],
                    "was_current_when_read": row["current_snapshot_id"] == snapshot_id})
            except Exception as exc:
                result["failed"] += 1
                result["snapshots"].append({"snapshot_id": snapshot_id, "status": "failed",
                                             "error": f"{type(exc).__name__}: {exc}"})
        return result

    def list_reports(self, limit: int = 100, namespace: str | None = None) -> list[dict]:
        where, params = (" WHERE r.namespace=?", [namespace]) if namespace is not None else ("", [])
        sql = """SELECT r.namespace,r.battle_uid,r.current_snapshot_id,s.completeness,s.source,
                        s.event_count,s.field_count,r.first_seen,r.last_seen,
                        (SELECT count(*) FROM report_snapshots v WHERE v.report_id=r.id) AS versions
                 FROM report_records r JOIN report_snapshots s ON s.id=r.current_snapshot_id"""
        return [dict(row) for row in self.connection.execute(sql + where + " ORDER BY r.id DESC LIMIT ?", [*params, max(0, int(limit))])]

    def get_payload(self, snapshot_id: int) -> dict:
        row = self.connection.execute("SELECT payload_json FROM report_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return _decode(row[0])

    def original_file_bytes(self, file_id: int) -> bytes:
        """Read a validated inline original or its verified cloud counterpart.

        Remote resolution may fill an F-drive cache, but never changes archive
        rows. Empty offloaded placeholders are not original bytes. Verification
        can require a cache-only lookup while holding its SQLite read snapshot.
        """
        row = self.connection.execute(
            "SELECT raw_bytes,sha256,byte_count FROM report_files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise KeyError(file_id)
        raw = bytes(row["raw_bytes"])
        if len(raw) == row["byte_count"] and hashlib.sha256(raw).hexdigest() == row["sha256"]:
            return raw
        has_cloud_table = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_cloud_files'").fetchone()
        verified = has_cloud_table and self.connection.execute(
            "SELECT 1 FROM report_cloud_files WHERE file_id=? AND status='verified'", (file_id,)).fetchone()
        if not verified:
            raise ValueError(f"Original file {file_id} failed byte/hash validation and has no verified cloud reference")
        from .quark_cloud import resolve_original
        if getattr(self, "_original_cache_only", False):
            raw = resolve_original(self.connection, file_id, self.db_path, allow_network=False)
        else:
            raw = resolve_original(self.connection, file_id, self.db_path)
        if not isinstance(raw, bytes) or len(raw) != row["byte_count"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError(f"Cloud original file {file_id} failed archive byte/hash validation")
        return raw

    def original_bytes(self, snapshot_id: int) -> bytes:
        row = self.connection.execute("SELECT file_id FROM report_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return self.original_file_bytes(row[0])

    def protect_output_path(self, path: str | Path) -> Path:
        """Reject output paths that alias the database or SQLite sidecars."""
        target = _write_path(path)
        protected = [self.db_path] + [Path(str(self.db_path) + suffix) for suffix in ("-wal", "-shm", "-journal")]
        for item in protected:
            if target == item or (target.exists() and item.exists() and target.samefile(item)):
                raise ValueError("Cannot export over the archive database or its SQLite sidecar files")
        return target

    def export_original(self, snapshot_id: int, path: str | Path) -> dict:
        target = self.protect_output_path(path)
        raw = self.original_bytes(snapshot_id)
        if target.exists():
            if not target.is_file() or target.read_bytes() != raw:
                raise FileExistsError(f"Export destination already contains different data: {target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Publish atomically without overwriting a file created in the
            # meantime. Hard-link publication also cannot follow target links.
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(prefix=".battle-export-", suffix=".tmp", dir=target.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    if not target.is_file() or target.read_bytes() != raw:
                        raise FileExistsError(f"Export destination was created by another writer: {target}")
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return {"path": str(target), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def collection_stats(self) -> dict:
        """Small collection-status counters, without historical payload scans.

        Event and field totals sum the stored projection counts for each current
        snapshot. They are not counts of historical projection rows.
        Files/snapshots use their small covering indexes for CLI compatibility.
        Full historical statistics remain available through stats().
        """
        self.connection.execute("SAVEPOINT archive_collection_stats")
        try:
            field_count = ("COALESCE(s.indexed_field_count,s.field_count)"
                           if "indexed_field_count" in self._snapshot_columns else "s.field_count")
            rows = list(self.connection.execute(
                "SELECT s.completeness,COUNT(*) records,COUNT(s.id) current_reports,"
                "COALESCE(SUM(s.event_count),0) current_events,"
                f"COALESCE(SUM({field_count}),0) current_fields "
                "FROM report_records r LEFT JOIN report_snapshots s "
                "ON s.id=r.current_snapshot_id AND s.report_id=r.id GROUP BY s.completeness"))
            result = {
                "statistics_mode": "current_reports",
                "records": sum(row["records"] for row in rows),
                "current_reports": sum(row["current_reports"] for row in rows),
                "current_events": sum(row["current_events"] for row in rows),
                "current_fields": sum(row["current_fields"] for row in rows),
                "current_completeness": {row["completeness"]: row["current_reports"] for row in rows
                                         if row["completeness"] is not None and row["current_reports"]},
                "files": self.connection.execute("SELECT COUNT(*) FROM report_files").fetchone()[0],
                "snapshots": self.connection.execute("SELECT COUNT(*) FROM report_snapshots").fetchone()[0],
                "collection_coverage": "unknown",
            }
            result["missing_current_snapshots"] = result["records"] - result["current_reports"]
            return result
        finally:
            self.connection.execute("RELEASE SAVEPOINT archive_collection_stats")

    def stats(self) -> dict:
        counts = {}
        for suffix in ("files", "records", "snapshots", "fields", "events", "sides", "heroes", "skills", "imports"):
            counts[suffix] = self.connection.execute(f"SELECT count(*) FROM report_{suffix}").fetchone()[0]
        counts["failed_files"] = self.connection.execute("SELECT count(*) FROM report_files WHERE parse_status='failed'").fetchone()[0]
        counts["current_events"] = self.connection.execute("SELECT count(*) FROM report_current_events").fetchone()[0]
        counts["current_fields"] = self.connection.execute("SELECT count(*) FROM report_current_fields").fetchone()[0]
        counts["current_completeness"] = dict(self.connection.execute("SELECT completeness,count(*) FROM report_current_snapshots GROUP BY completeness"))
        counts["collection_coverage"] = "unknown"
        return counts

    def verify(self) -> dict:
        prewarm_errors, cloud_originals = [], {}
        # A SELECT is fully materialized before any download. Do not hold a
        # SQLite read snapshot across network waits, which would retain WAL.
        # A caller-owned transaction cannot be released here; in that case
        # verification uses only originals already available in the cache.
        if self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_cloud_files'").fetchone():
            files = list(self.connection.execute(
                "SELECT f.id,f.sha256,f.byte_count,f.parse_status FROM report_files f "
                "JOIN report_cloud_files c ON c.file_id=f.id "
                "WHERE c.status='verified' AND length(f.raw_bytes)=0 AND f.byte_count>0 ORDER BY f.id"))
            previous_cache_only = getattr(self, "_original_cache_only", False)
            self._original_cache_only = previous_cache_only or self.connection.in_transaction
            try:
                for file in files:
                    file_id = file["id"]
                    try:
                        raw = self.original_file_bytes(file_id)
                        if len(raw) != file["byte_count"] or hashlib.sha256(raw).hexdigest() != file["sha256"]:
                            raise ValueError("Original file metadata changed while preparing verification")
                        summary = dict(file)
                        summary.update(document_hashes=[], decode_error=None)
                        if file["parse_status"] == "parsed":
                            try:
                                document = _decode(raw)
                                documents = document if isinstance(document, list) else [document]
                                summary["document_hashes"] = [hashlib.sha256(_json(value).encode("ascii")).hexdigest()
                                                              for value in documents]
                            except Exception as exc:
                                summary["decode_error"] = f"{type(exc).__name__}: {exc}"
                        cloud_originals[file_id] = summary
                    except Exception as exc:
                        prewarm_errors.append({"file_id": file_id, "check": "cloud_original_prewarm",
                                               "error": f"{type(exc).__name__}: {exc}"})
            finally:
                self._original_cache_only = previous_cache_only
            # Only hashes/counts survive preparation; neither original bytes nor
            # the whole cloud corpus has to remain in memory or the disk cache.
            if files:
                raw = document = documents = None
        # Pin a single read snapshot so a live importer cannot appear halfway
        # through a committed batch during the multi-query verification.
        previous_cache_only = getattr(self, "_original_cache_only", False)
        self.connection.execute("SAVEPOINT archive_verify")
        self._original_cache_only = True
        try:
            result = self._verify_snapshot(cloud_originals)
            if prewarm_errors:
                result["errors"][:0] = prewarm_errors
                result["ok"] = False
            return result
        finally:
            try:
                self.connection.execute("RELEASE SAVEPOINT archive_verify")
            finally:
                self._original_cache_only = previous_cache_only

    def _verify_snapshot(self, cloud_originals: dict | None = None) -> dict:
        """Recompute bytes, quality, policy-selected nodes, events and relations."""
        cloud_originals = cloud_originals or {}
        errors: list[dict] = []
        integrity = [r[0] for r in self.connection.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in self.connection.execute("PRAGMA foreign_key_check")]
        if integrity != ["ok"] or foreign_keys:
            errors.append({"check": "sqlite", "integrity": integrity, "foreign_keys": foreign_keys})
        wrong_current = [dict(row) for row in self.connection.execute(
            "SELECT r.id,r.current_snapshot_id FROM report_records r LEFT JOIN report_snapshots s "
            "ON s.id=r.current_snapshot_id WHERE s.id IS NULL OR s.report_id<>r.id")]
        if wrong_current:
            errors.append({"check": "current_snapshot_ownership", "records": wrong_current})
        wrong_relations = [dict(row) for row in self.connection.execute(
            "SELECT 'hero' AS kind,h.id FROM report_heroes h JOIN report_sides s ON s.id=h.side_id WHERE h.snapshot_id<>s.snapshot_id "
            "UNION ALL SELECT 'skill',k.id FROM report_skills k JOIN report_heroes h ON h.id=k.hero_id WHERE k.snapshot_id<>h.snapshot_id")]
        if wrong_relations:
            errors.append({"check": "relation_snapshot_ownership", "records": wrong_relations})
        files_checked = 0
        offloaded_files, verified_cloud_originals = set(), {}
        for row in self.connection.execute("SELECT id,raw_bytes,sha256,byte_count,parse_status FROM report_files"):
            files_checked += 1
            raw = bytes(row["raw_bytes"])
            summary = None
            if not raw and row["byte_count"] > 0:
                offloaded_files.add(row["id"])
                candidate = cloud_originals.get(row["id"])
                if candidate is None:
                    errors.append({"file_id": row["id"], "check": "raw_bytes_sha256",
                                   "error": "Cloud original was not validated before the verification snapshot"})
                elif any(candidate[key] != row[key] for key in ("sha256", "byte_count", "parse_status")):
                    errors.append({"file_id": row["id"], "check": "cloud_original_metadata",
                                   "error": "Original file metadata changed after cloud validation"})
                elif not self.connection.execute(
                        "SELECT 1 FROM report_cloud_files WHERE file_id=? AND status='verified'", (row["id"],)).fetchone():
                    errors.append({"file_id": row["id"], "check": "cloud_original_metadata",
                                   "error": "Cloud original no longer has a verified reference"})
                else:
                    summary = candidate
                    verified_cloud_originals[row["id"]] = summary
            elif hashlib.sha256(raw).hexdigest() != row["sha256"] or len(raw) != row["byte_count"]:
                errors.append({"file_id": row["id"], "check": "raw_bytes_sha256"})
            indexed = [r[0] for r in self.connection.execute("SELECT document_index FROM report_snapshots WHERE file_id=? ORDER BY document_index", (row["id"],))]
            if row["parse_status"] == "parsed":
                try:
                    if row["id"] in offloaded_files:
                        if summary is None:
                            raise ValueError("Cloud original was not validated before the verification snapshot")
                        if summary["decode_error"]:
                            raise ValueError(summary["decode_error"])
                        expected = list(range(len(summary["document_hashes"])))
                    else:
                        value = _decode(raw)
                        expected = list(range(len(value))) if isinstance(value, list) else [0]
                    if not expected or indexed != expected:
                        errors.append({"file_id": row["id"], "check": "file_snapshot_coverage", "expected": expected, "actual": indexed})
                except Exception as exc:
                    errors.append({"file_id": row["id"], "check": "parsed_file_decode", "error": str(exc)})
            elif indexed:
                errors.append({"file_id": row["id"], "check": "unparsed_file_has_snapshots"})
        snapshots_checked = fields_checked = indexed_fields_checked = events_checked = 0
        original_file_id, original_document = None, None
        for row in self.connection.execute("SELECT * FROM report_snapshots ORDER BY file_id,document_index"):
            sid = row["id"]
            snapshots_checked += 1
            try:
                payload = _decode(row["payload_json"])
                if row["file_id"] in offloaded_files:
                    summary = verified_cloud_originals.get(row["file_id"])
                    if summary is None or summary["decode_error"]:
                        raise ValueError("Cloud original document was not validated before the verification snapshot")
                    index = row["document_index"]
                    actual_hash = hashlib.sha256(_json(payload).encode("ascii")).hexdigest()
                    if not 0 <= index < len(summary["document_hashes"]) or actual_hash != summary["document_hashes"][index]:
                        errors.append({"snapshot_id": sid, "check": "payload_matches_original"})
                        # Never use an unproven local payload as the authority
                        # for checking its field/component/event projections.
                        continue
                else:
                    if original_file_id != row["file_id"]:
                        original_raw = self.connection.execute(
                            "SELECT raw_bytes FROM report_files WHERE id=?", (row["file_id"],)).fetchone()[0]
                        original_document = _decode(bytes(original_raw))
                        original_file_id = row["file_id"]
                    original = original_document[row["document_index"]] if isinstance(original_document, list) else original_document
                    if _json(payload) != _json(original):
                        errors.append({"snapshot_id": sid, "check": "payload_matches_original"})
                    payload = original
                identity = self.connection.execute(
                    "SELECT r.namespace,r.battle_uid,f.namespace AS file_namespace FROM report_records r "
                    "JOIN report_files f ON f.id=? WHERE r.id=?", (row["file_id"], row["report_id"])).fetchone()
                data, _ = _main_data(payload)
                uid = _uid(payload.get("battleID")) or _uid(data.get("battleID"))
                if identity is None or identity["namespace"] != identity["file_namespace"] or identity["battle_uid"] != uid:
                    errors.append({"snapshot_id": sid, "check": "report_identity"})
                if row["source"] != _text(payload.get("source")) or row["capture_version"] != _int(payload.get("captureVersion")):
                    errors.append({"snapshot_id": sid, "check": "snapshot_metadata"})
                expected_fields = list(iter_fields(payload))
                storage_mode = row["storage_mode"] if "storage_mode" in row.keys() else "full"
                expected_indexed_fields = _indexed_fields(expected_fields, storage_mode)
                actual_fields = [tuple(field) for field in self.connection.execute("SELECT pointer,parent_pointer,field_key,ordinal,node_type,value_json,scalar_text FROM report_fields WHERE snapshot_id=?", (sid,))]
                fields_checked += len(expected_fields)
                indexed_fields_checked += len(expected_indexed_fields)
                if sorted(expected_indexed_fields) != sorted(actual_fields) or row["field_count"] != len(expected_fields):
                    errors.append({"snapshot_id": sid, "check": "all_fields"})
                if ("indexed_field_count" in row.keys() and row["indexed_field_count"] is not None
                        and row["indexed_field_count"] != len(expected_indexed_fields)):
                    errors.append({"snapshot_id": sid, "check": "indexed_field_count"})
                source_components = _mapping(payload.get("components"))
                source_statuses = _mapping(payload.get("componentStatus"))
                expected_components = []
                for name in sorted(set(COMPONENTS) | set(source_components) | set(source_statuses)):
                    state = _mapping(source_statuses.get(name))
                    available = name in source_components and source_components[name] is not None
                    requested = int(state["requested"]) if isinstance(state.get("requested"), bool) else None
                    complete = int(state["complete"] is True and available and state.get("available") is True and not state.get("error")) if "complete" in state else None
                    expected_components.append((name, _json(source_components[name]) if name in source_components else None,
                                                requested, int(available), complete, _json(state)))
                actual_components = [tuple(component) for component in self.connection.execute(
                    "SELECT name,payload_json,requested,available,complete,status_json FROM report_components WHERE snapshot_id=? ORDER BY name", (sid,))]
                if expected_components != actual_components:
                    errors.append({"snapshot_id": sid, "check": "components_and_statuses"})
                data, data_pointer = _main_data(payload)
                completeness, quality, issues = _quality(payload, expected_fields, data)
                if completeness != row["completeness"] or quality != _decode(row["quality_json"]) or issues != _decode(row["issues_json"]):
                    errors.append({"snapshot_id": sid, "check": "completeness_classification"})
                source_events = data.get("eventDataList") if isinstance(data.get("eventDataList"), list) else []
                expected_events = [(seq, data_pointer + "/eventDataList/" + str(seq), _text(_mapping(e).get("eventId")), _text(_mapping(e).get("tab")), _json(e)) for seq, e in enumerate(source_events)]
                actual_events = [tuple(event) for event in self.connection.execute("SELECT sequence,source_pointer,event_id,tab,raw_json FROM report_events WHERE snapshot_id=? ORDER BY sequence", (sid,))]
                events_checked += len(expected_events)
                if expected_events != actual_events or row["event_count"] != len(expected_events):
                    errors.append({"snapshot_id": sid, "check": "ordered_events"})
                expected_sides, expected_heroes, expected_skills = [], [], []
                settlement, _settlement_pointer = _settlement_source(payload)
                for side in ("attacker", "defensive", "defender"):
                    army = settlement.get(side)
                    if not isinstance(army, dict):
                        continue
                    avatar = _mapping(army.get("avatar_data"))
                    expected_sides.append((side, _text(army.get("army_id")), _text(avatar.get("avatar_id")),
                                           _text(avatar.get("avatar_name")), _text(army.get("formationId")), _int(army.get("morale")), _json(army)))
                    members = army.get("army_member")
                    for ordinal, hero in enumerate(members if isinstance(members, list) else []):
                        h = _mapping(hero)
                        expected_heroes.append((side, ordinal, _text(h.get("heros_id")), _int(h.get("index")),
                                                _text(h.get("name")), _int(h.get("hero_lv")), _int(h.get("origin_troop_num")),
                                                _int(h.get("troop_num")), _int(h.get("dead_num")), _int(h.get("wound_num")), _json(hero)))
                        skills = h.get("skills")
                        for skill_index, skill in enumerate(skills if isinstance(skills, list) else []):
                            skill_data = _mapping(skill)
                            expected_skills.append((side, ordinal, skill_index, _text(skill_data.get("skillId")),
                                                    _int(skill_data.get("level")), _int(skill_data.get("orderLevel")), _json(skill)))
                actual_sides = [tuple(r) for r in self.connection.execute("SELECT side,army_uid,player_uid,player_name,formation_id,morale,raw_json FROM report_sides WHERE snapshot_id=?", (sid,))]
                actual_heroes = [tuple(r) for r in self.connection.execute("SELECT s.side,h.ordinal,h.hero_uid,h.position,h.hero_name,h.level,h.troops_initial,h.troops_remaining,h.troops_dead,h.troops_wounded,h.raw_json FROM report_heroes h JOIN report_sides s ON s.id=h.side_id WHERE h.snapshot_id=?", (sid,))]
                actual_skills = [tuple(r) for r in self.connection.execute("SELECT s.side,h.ordinal,k.ordinal,k.skill_uid,k.level,k.order_level,k.raw_json FROM report_skills k JOIN report_heroes h ON h.id=k.hero_id JOIN report_sides s ON s.id=h.side_id WHERE k.snapshot_id=?", (sid,))]
                if sorted(expected_sides) != sorted(actual_sides) or sorted(expected_heroes) != sorted(actual_heroes) or sorted(expected_skills) != sorted(actual_skills):
                    errors.append({"snapshot_id": sid, "check": "sides_heroes_skills"})
            except Exception as exc:
                errors.append({"snapshot_id": sid, "check": "verification_exception", "error": str(exc)})
        return {"ok": not errors, "files_checked": files_checked, "snapshots_checked": snapshots_checked,
                "fields_checked": fields_checked, "events_checked": events_checked, "errors": errors,
                "indexed_fields_checked": indexed_fields_checked,
                "scope": "Stored original bytes and their indexed projections; does not prove account-wide collection or client completeness."}
