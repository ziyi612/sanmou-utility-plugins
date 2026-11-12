"""Rebuildable Chinese meanings alongside an immutable battle archive.

This module owns only ``report_semantic_*``, ``report_id_meanings`` and
``report_event_meanings``. It never rewrites exported JSON, original bytes,
completeness or any of the archive's reproducible raw projections.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Callable, Mapping


SEMANTICS_SCHEMA_VERSION = "1"
# Include this in the composed mapper version so changed field-selection rules
# rebuild prior ready rows even when the client dictionaries are unchanged.
SEMANTICS_INDEX_VERSION = "report-semantics-2"

# Deliberately exclude generic id/teamId/heroId: those may describe battle
# objects or players rather than design-table identifiers.
DEFAULT_ID_FIELDS = {
    "heros_id": "hero", "skillId": "skill", "skill_id": "skill",
    "formationId": "formation", "basicArmsId": "arms",
    "armsAdvancedId": "advanced_arms", "equipId": "equipment",
    "horseId": "horse", "buffId": "buff", "stateId": "state",
}


@dataclass(frozen=True)
class SemanticMapper:
    version: str
    sources: Any
    id_lookup: Callable[[str, Any], Mapping[str, Any]]
    event_decode: Callable[[dict, dict], Mapping[str, Any]]
    id_fields: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_ID_FIELDS))
    context_id_lookup: Callable[[str, Any, dict], Mapping[str, Any]] | None = None

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("Semantic mapper version must be a nonempty string")


_DDL = (
    "CREATE TABLE IF NOT EXISTS report_semantic_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS report_semantic_versions("
    "version TEXT PRIMARY KEY,sources_json TEXT NOT NULL,created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS report_semantic_settings("
    "singleton INTEGER PRIMARY KEY CHECK(singleton=1),active_version TEXT NOT NULL "
    "REFERENCES report_semantic_versions(version))",
    "CREATE TABLE IF NOT EXISTS report_semantic_runs("
    "snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id),"
    "version TEXT NOT NULL REFERENCES report_semantic_versions(version),"
    "status TEXT NOT NULL,error TEXT,id_count INTEGER NOT NULL DEFAULT 0,"
    "mapped_id_count INTEGER NOT NULL DEFAULT 0,event_count INTEGER NOT NULL DEFAULT 0,"
    "mapped_event_count INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,"
    "PRIMARY KEY(snapshot_id,version))",
    "CREATE TABLE IF NOT EXISTS report_id_meanings("
    "snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id),pointer TEXT NOT NULL,"
    "version TEXT NOT NULL REFERENCES report_semantic_versions(version),kind TEXT NOT NULL,"
    "raw_value_json TEXT NOT NULL,name_zh TEXT,mapped INTEGER NOT NULL,status TEXT NOT NULL,"
    "source_json TEXT NOT NULL,mapping_json TEXT NOT NULL,"
    "PRIMARY KEY(snapshot_id,pointer,version))",
    "CREATE INDEX IF NOT EXISTS report_id_meanings_lookup ON report_id_meanings(kind,raw_value_json,version)",
    "CREATE TABLE IF NOT EXISTS report_event_meanings("
    "snapshot_id INTEGER NOT NULL,sequence INTEGER NOT NULL,"
    "version TEXT NOT NULL REFERENCES report_semantic_versions(version),event_id TEXT,"
    "event_name_zh TEXT,summary_zh TEXT,parameters_json TEXT NOT NULL,status TEXT NOT NULL,"
    "source_json TEXT NOT NULL,mapping_json TEXT NOT NULL,"
    "PRIMARY KEY(snapshot_id,sequence,version),"
    "FOREIGN KEY(snapshot_id,sequence) REFERENCES report_events(snapshot_id,sequence))",
    "CREATE VIEW IF NOT EXISTS report_current_id_meanings AS "
    "SELECT r.namespace,r.battle_uid,m.* FROM report_records r "
    "JOIN report_id_meanings m ON m.snapshot_id=r.current_snapshot_id "
    "JOIN report_semantic_settings a ON a.singleton=1 AND a.active_version=m.version "
    "JOIN report_semantic_runs x ON x.snapshot_id=m.snapshot_id AND x.version=m.version AND x.status='ready'",
    "CREATE VIEW IF NOT EXISTS report_current_event_meanings AS "
    "SELECT r.namespace,r.battle_uid,m.* FROM report_records r "
    "JOIN report_event_meanings m ON m.snapshot_id=r.current_snapshot_id "
    "JOIN report_semantic_settings a ON a.singleton=1 AND a.active_version=m.version "
    "JOIN report_semantic_runs x ON x.snapshot_id=m.snapshot_id AND x.version=m.version AND x.status='ready'",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).encode("utf-8", "backslashreplace").decode("utf-8")


def _id(value: Any) -> str | None:
    return str(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else None


def _source(result: Mapping[str, Any]) -> dict:
    return {key: result[key] for key in ("source", "sources", "verification", "reason") if key in result}


@contextmanager
def _write_scope(connection: sqlite3.Connection, name: str):
    """Start a short writer transaction, or isolate work inside a caller's one.

    A top-level SAVEPOINT would establish a read snapshot before its first
    write. In WAL mode another writer can invalidate that snapshot, and SQLite
    cannot upgrade it even with busy_timeout. Acquire the writer slot first.
    """
    outer = connection.in_transaction
    connection.execute(f"SAVEPOINT {name}" if outer else "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if outer:
            connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
            connection.execute(f"RELEASE SAVEPOINT {name}")
        else:
            connection.rollback()
        raise
    else:
        if outer:
            connection.execute(f"RELEASE SAVEPOINT {name}")
        else:
            connection.commit()


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create auxiliary objects without implicitly committing a caller transaction."""
    with _write_scope(connection, "report_semantic_schema"):
        existing = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_semantic_meta'").fetchone()
        if existing:
            row = connection.execute("SELECT value FROM report_semantic_meta WHERE key='schema_version'").fetchone()
            if row is not None and row[0] != SEMANTICS_SCHEMA_VERSION:
                raise ValueError(f"Unsupported semantic schema version {row[0]!r}")
        for statement in _DDL:
            connection.execute(statement)
        connection.execute("INSERT OR IGNORE INTO report_semantic_meta VALUES('schema_version',?)",
                           (SEMANTICS_SCHEMA_VERSION,))


def _data(payload: dict) -> dict:
    if isinstance(payload.get("data"), dict):
        return payload["data"]
    components = payload.get("components")
    if isinstance(components, dict):
        for name in ("all", "detail", "basic"):
            if isinstance(components.get(name), dict):
                return components[name]
    return {}


def _scenario_id(payload: dict) -> Any:
    identity = payload.get("runtimeIdentity")
    if isinstance(identity, dict) and identity.get("scenarioId") is not None:
        return identity["scenarioId"]
    return _data(payload).get("scenarioId")


def build_context(payload: dict, battle_uid: str, id_lookup: Callable[[str, Any], Mapping[str, Any]]) -> dict:
    """Resolve army IDs within this report, without treating them as side numbers."""
    data = _data(payload)
    settlement = data.get("battleSettlement")
    settlement = settlement if isinstance(settlement, dict) else {}
    teams: dict[str, dict] = {}
    by_side: dict[str, dict] = {}
    heroes = []
    labels = {"attacker": "进攻方", "defensive": "防守方", "defender": "防守方"}
    for side, label in labels.items():
        army = settlement.get(side)
        if not isinstance(army, dict):
            continue
        team = {"side": side, "name_zh": label, "label_zh": label, "army": army, "heroes": {}}
        positions = by_side.setdefault(side, {})
        members = army.get("army_member")
        for ordinal, raw in enumerate(members if isinstance(members, list) else []):
            if not isinstance(raw, dict):
                continue
            hid = _id(raw.get("heros_id"))
            lookup = dict(id_lookup("hero", raw["heros_id"])) if hid is not None else {}
            hero = {"id": raw.get("heros_id"), "heros_id": raw.get("heros_id"),
                    "name_zh": lookup.get("name_zh"), "name_mapping": lookup,
                    "position": raw.get("index"), "ordinal": ordinal, "side": side, "raw": raw}
            heroes.append(hero)
            position = _id(raw.get("index"))
            if position is not None:
                positions[position] = hero
            if hid is not None:
                # Keep ambiguous duplicate configurations explicit rather than
                # silently using the last member for an event reference.
                if hid in team["heroes"]:
                    old = team["heroes"].pop(hid)
                    team.setdefault("hero_candidates", {}).setdefault(hid, [old]).append(hero)
                elif hid in team.get("hero_candidates", {}):
                    team["hero_candidates"][hid].append(hero)
                else:
                    team["heroes"][hid] = hero
        army_id = _id(army.get("army_id"))
        if army_id is not None:
            if army_id in teams:
                # The raw settlement remains available; do not pick a side if
                # a malformed report reuses an army identifier.
                teams[army_id] = {"ambiguous": True, "army_id": army.get("army_id")}
            else:
                teams[army_id] = team
    return {"payload": payload, "battle_uid": battle_uid, "settlement": settlement,
            "scenario_id": _scenario_id(payload), "teams": teams, "teams_by_id": teams,
            "heroes": heroes, "heroes_by_side_position": by_side, "id_lookup": id_lookup}


class SemanticArchive:
    """Apply one sourced dictionary version, with failures isolated from raw data."""
    def __init__(self, connection: sqlite3.Connection, mapper: SemanticMapper, *, activate: bool = True):
        self.connection, self.mapper = connection, mapper
        ensure_schema(connection)
        sources = _json(mapper.sources)
        with _write_scope(connection, "report_semantic_register"):
            previous = connection.execute("SELECT sources_json FROM report_semantic_versions WHERE version=?",
                                          (mapper.version,)).fetchone()
            if previous is not None and previous[0] != sources:
                raise ValueError("Semantic version is already registered with different sources")
            connection.execute("INSERT OR IGNORE INTO report_semantic_versions VALUES(?,?,?)",
                               (mapper.version, sources, _now()))
            if activate:
                connection.execute("INSERT INTO report_semantic_settings VALUES(1,?) "
                                   "ON CONFLICT(singleton) DO UPDATE SET active_version=excluded.active_version",
                                   (mapper.version,))

    def _kind(self, key: str, pointer: str) -> str | None:
        if key in self.mapper.id_fields:
            return self.mapper.id_fields[key]
        if key == "group" and "/army_member/" in pointer:
            return "group"
        parts = pointer.rsplit("/", 3)
        if key.isascii() and key.isdigit():
            # BattleDef uses warbookCfg[warbookId] for precisely this list;
            # unrelated idList arrays are deliberately not treated as books.
            if parts[-3:-1] == ["warbooks", "idList"]:
                return "warbook"
            # ArmsAdvancedHelper looks these IDs up in the advanced-arms
            # skill table, not the ordinary skills table.
            if parts[-2] == "armsAdvancedSkillIdList":
                return "advanced_arms_skill"
        if key == "skinId" and parts[-2] in ("heroSkinInfo", "skillSkinInfo"):
            # AppearanceHelper resolves these skin IDs via appearance_appearance.
            return "skin"
        return None

    def enrich_snapshot(self, snapshot_id: int, *, force: bool = False) -> dict:
        """Return failed on semantic errors; committed raw archive data is untouched."""
        connection, version = self.connection, self.mapper.version
        if not force:
            previous = connection.execute("SELECT status FROM report_semantic_runs WHERE snapshot_id=? AND version=?",
                                          (snapshot_id, version)).fetchone()
            if previous is not None and previous[0] == "ready":
                return {"status": "duplicate", "snapshot_id": snapshot_id, "version": version}
        row = connection.execute("SELECT r.battle_uid,s.payload_json FROM report_snapshots s "
                                 "JOIN report_records r ON r.id=s.report_id WHERE s.id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(f"Snapshot does not exist: {snapshot_id}")
        try:
            # Raw archive projections are immutable after import. Materialize
            # each SELECT and close its cursor before running user decoders;
            # no new read transaction or live cursor spans the long CPU phase.
            keys = tuple(dict.fromkeys(tuple(self.mapper.id_fields) + ("group", "skinId")))
            placeholders = ",".join("?" for _ in keys)
            raw_ids = connection.execute(
                f"SELECT pointer,field_key,value_json,node_type FROM report_fields WHERE snapshot_id=? "
                f"AND (field_key IN ({placeholders}) OR pointer LIKE '%/warbooks/idList/%' "
                f"OR pointer LIKE '%/armsAdvancedSkillIdList/%') ORDER BY pointer", (snapshot_id, *keys)).fetchall()
            raw_events = connection.execute(
                "SELECT sequence,event_id,raw_json FROM report_events WHERE snapshot_id=? ORDER BY sequence",
                (snapshot_id,)).fetchall()
            payload = json.loads(row[1])
            context = {"payload": payload, "battle_uid": row[0], "scenario_id": _scenario_id(payload)}
            if self.mapper.context_id_lookup is not None:
                def lookup(kind, value):
                    return self.mapper.context_id_lookup(kind, value, context)
            else:
                lookup = self.mapper.id_lookup
            context.update(build_context(payload, row[0], lookup))
            id_rows = []
            for pointer, key, value_json, node_type in raw_ids:
                kind = self._kind(key, pointer)
                if kind is None or node_type not in ("integer", "string"):
                    continue
                raw_value = json.loads(value_json)
                result = dict(lookup(kind, raw_value))
                mapped = result.get("mapped") is True
                status = _text(result.get("status")) or ("mapped" if mapped else "unmapped")
                id_rows.append((snapshot_id, pointer, version, kind, value_json, _text(result.get("name_zh")),
                                int(mapped), status, _json(_source(result)), _json(result)))
            event_rows = []
            for sequence, event_id, raw_json in raw_events:
                raw_event = json.loads(raw_json)
                result = dict(self.mapper.event_decode(raw_event, context))
                status = _text(result.get("status")) or "unmapped"
                event_rows.append((snapshot_id, sequence, version, event_id,
                                   _text(result.get("event_name_zh")), _text(result.get("summary_zh")),
                                   _json(result.get("parameters", [])), status, _json(_source(result)), _json(result)))
            counts = {"id_count": len(id_rows), "mapped_id_count": sum(r[6] for r in id_rows),
                      "event_count": len(event_rows), "mapped_event_count": sum(r[7] == "mapped" for r in event_rows)}
            with _write_scope(connection, "report_semantic_index"):
                # Another semantic worker may have finished while we decoded.
                if not force:
                    previous = connection.execute(
                        "SELECT status FROM report_semantic_runs WHERE snapshot_id=? AND version=?",
                        (snapshot_id, version)).fetchone()
                    if previous is not None and previous[0] == "ready":
                        return {"status": "duplicate", "snapshot_id": snapshot_id, "version": version}
                connection.execute("DELETE FROM report_id_meanings WHERE snapshot_id=? AND version=?", (snapshot_id, version))
                connection.execute("DELETE FROM report_event_meanings WHERE snapshot_id=? AND version=?", (snapshot_id, version))
                connection.executemany("INSERT INTO report_id_meanings VALUES(?,?,?,?,?,?,?,?,?,?)", id_rows)
                connection.executemany("INSERT INTO report_event_meanings VALUES(?,?,?,?,?,?,?,?,?,?)", event_rows)
                connection.execute("INSERT INTO report_semantic_runs VALUES(?,?,?,NULL,?,?,?,?,?) "
                                   "ON CONFLICT(snapshot_id,version) DO UPDATE SET status=excluded.status,error=NULL,"
                                   "id_count=excluded.id_count,mapped_id_count=excluded.mapped_id_count,"
                                   "event_count=excluded.event_count,mapped_event_count=excluded.mapped_event_count,"
                                   "updated_at=excluded.updated_at",
                                   (snapshot_id, version, "ready", *counts.values(), _now()))
        except Exception as exc:
            error = _text(f"{type(exc).__name__}: {exc}")
            result = {"status": "failed", "snapshot_id": snapshot_id, "version": version, "error": error}
            # This acquires a fresh writer slot rather than upgrading the old
            # computation's read snapshot. Caller-owned transactions remain
            # caller-owned, including a stale outer read transaction we cannot
            # legitimately commit or roll back on its behalf.
            try:
                with _write_scope(connection, "report_semantic_failure"):
                    previous = connection.execute(
                        "SELECT status FROM report_semantic_runs WHERE snapshot_id=? AND version=?",
                        (snapshot_id, version)).fetchone()
                    if not force and previous is not None and previous[0] == "ready":
                        return {"status": "duplicate", "snapshot_id": snapshot_id, "version": version}
                    connection.execute("INSERT INTO report_semantic_runs(snapshot_id,version,status,error,updated_at) "
                                       "VALUES(?,?,?,?,?) ON CONFLICT(snapshot_id,version) DO UPDATE SET "
                                       "status=excluded.status,error=excluded.error,updated_at=excluded.updated_at",
                                       (snapshot_id, version, "failed", error, _now()))
            except sqlite3.Error as record_error:
                result["failure_record_error"] = _text(f"{type(record_error).__name__}: {record_error}")
            return result
        return {"status": "ready", "snapshot_id": snapshot_id, "version": version, **counts}

    def backfill(self, *, only_current: bool = False, limit: int | None = None,
                 force: bool = False, on_progress: Callable[[dict], None] | None = None) -> dict:
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ValueError("Backfill limit must be a positive integer")
        table = "report_current_snapshots" if only_current else "report_snapshots"
        query = f"SELECT s.id FROM {table} s"
        params: tuple = ()
        if not force:
            query += " WHERE NOT EXISTS(SELECT 1 FROM report_semantic_runs x WHERE x.snapshot_id=s.id "
            query += "AND x.version=? AND x.status='ready')"
            params = (self.mapper.version,)
        query += " ORDER BY s.id"
        if limit is not None:
            query += " LIMIT ?"
            params += (limit,)
        ids = [row[0] for row in self.connection.execute(query, params)]
        result = {"version": self.mapper.version, "snapshots": len(ids), "ready": 0, "duplicate": 0,
                  "failed": 0, "errors": []}
        for snapshot_id in ids:
            item = self.enrich_snapshot(snapshot_id, force=force)
            result[item["status"]] += 1
            if item["status"] == "failed":
                result["errors"].append(item)
            if on_progress is not None:
                on_progress(item)
        return result


def semantic_stats(connection: sqlite3.Connection) -> dict:
    """Inspect auxiliary mappings without creating objects on a read-only path."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_semantic_settings'").fetchone():
        return {"available": False}
    active = connection.execute("SELECT active_version FROM report_semantic_settings WHERE singleton=1").fetchone()
    if active is None:
        return {"available": True, "active_version": None}
    version = active[0]
    runs = dict(connection.execute("SELECT status,count(*) FROM report_semantic_runs WHERE version=? GROUP BY status", (version,)))
    ids = connection.execute("SELECT count(*),coalesce(sum(mapped),0) FROM report_current_id_meanings").fetchone()
    events = dict(connection.execute("SELECT status,count(*) FROM report_current_event_meanings GROUP BY status"))
    return {"available": True, "active_version": version, "snapshot_runs": runs,
            "current_id_fields": ids[0], "current_mapped_id_fields": ids[1], "current_event_statuses": events}


def enrich_snapshot(connection: sqlite3.Connection, snapshot_id: int, mapper: SemanticMapper, *, force: bool = False) -> dict:
    return SemanticArchive(connection, mapper).enrich_snapshot(snapshot_id, force=force)


def backfill(connection: sqlite3.Connection, mapper: SemanticMapper, *, only_current: bool = False,
             limit: int | None = None, force: bool = False, on_progress=None) -> dict:
    return SemanticArchive(connection, mapper).backfill(only_current=only_current, limit=limit,
                                                       force=force, on_progress=on_progress)
