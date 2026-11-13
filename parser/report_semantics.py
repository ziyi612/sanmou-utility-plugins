"""Rebuildable Chinese meanings alongside an immutable battle archive.

This module owns only ``report_semantic_*``, ``report_id_meanings`` and
``report_event_meanings``. It never rewrites exported JSON, original bytes,
completeness or any of the archive's reproducible raw projections.

Lean ID rows share complete source/mapping JSON in an auxiliary cache. Their
two JSON columns contain {"storage":"shared_id_mapping_v1","mapping_ref":...};
``resolve_id_meaning`` and ``report_resolved_id_meanings`` expose the original
complete texts. Explicit relational links distinguish references from literals.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Callable, Mapping


SEMANTICS_SCHEMA_VERSION = "1"
# Include this in the composed mapper version so changed field-selection rules
# rebuild prior ready rows even when the client dictionaries are unchanged.
SEMANTICS_INDEX_VERSION = "report-semantics-2"
SHARED_ID_MAPPING_STORAGE = "shared_id_mapping_v1"

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
    "CREATE TABLE IF NOT EXISTS report_semantic_event_modes("
    "snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id),version TEXT NOT NULL "
    "REFERENCES report_semantic_versions(version),"
    "event_mode TEXT NOT NULL CHECK(event_mode IN ('stored','on_demand')),"
    "PRIMARY KEY(snapshot_id,version))",
    "CREATE TABLE IF NOT EXISTS report_id_meanings("
    "snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id),pointer TEXT NOT NULL,"
    "version TEXT NOT NULL REFERENCES report_semantic_versions(version),kind TEXT NOT NULL,"
    "raw_value_json TEXT NOT NULL,name_zh TEXT,mapped INTEGER NOT NULL,status TEXT NOT NULL,"
    "source_json TEXT NOT NULL,mapping_json TEXT NOT NULL,"
    "PRIMARY KEY(snapshot_id,pointer,version))",
    "CREATE INDEX IF NOT EXISTS report_id_meanings_lookup ON report_id_meanings(kind,raw_value_json,version)",
    "CREATE TABLE IF NOT EXISTS report_semantic_mapping_cache("
    "mapping_ref TEXT PRIMARY KEY,source_json TEXT NOT NULL,mapping_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS report_semantic_id_links("
    "snapshot_id INTEGER NOT NULL,pointer TEXT NOT NULL,version TEXT NOT NULL,"
    "mapping_ref TEXT NOT NULL REFERENCES report_semantic_mapping_cache(mapping_ref),"
    "PRIMARY KEY(snapshot_id,pointer,version),"
    "FOREIGN KEY(snapshot_id,pointer,version) REFERENCES report_id_meanings(snapshot_id,pointer,version) ON DELETE CASCADE)",
    "CREATE VIEW IF NOT EXISTS report_resolved_id_meanings AS "
    "SELECT m.snapshot_id,m.pointer,m.version,m.kind,m.raw_value_json,m.name_zh,m.mapped,m.status,"
    "COALESCE(c.source_json,m.source_json) source_json,COALESCE(c.mapping_json,m.mapping_json) mapping_json "
    "FROM report_id_meanings m LEFT JOIN report_semantic_id_links l "
    "ON l.snapshot_id=m.snapshot_id AND l.pointer=m.pointer AND l.version=m.version "
    "LEFT JOIN report_semantic_mapping_cache c ON c.mapping_ref=l.mapping_ref",
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


def _mapping_ref(source_json: str, mapping_json: str) -> str:
    # A JSON pair supplies unambiguous boundaries without depending on byte
    # separators absent from either source. The full texts remain in the cache.
    return hashlib.sha256(_json([source_json, mapping_json]).encode("utf-8")).hexdigest()


def resolve_id_meaning(connection: sqlite3.Connection, row: Mapping[str, Any]) -> dict:
    """Return full provenance for a stored ID row, without writing anything.

    Lean rows keep small JSON references in source_json/mapping_json. An
    explicit auxiliary link, rather than the contents of user JSON, determines
    whether a row uses the shared cache. Thus old literal mappings stay literal.
    SQL clients can instead query report_resolved_id_meanings for the same
    original column layout with both JSON values already expanded.
    """
    result = dict(row)

    def require_legacy_row():
        # Every lean ID row written by this version has an explicit cache link.
        # Check the snapshot policy only on a missing-link path so ordinary
        # successful reads need no extra schema or snapshot query.
        columns = {item[1] for item in connection.execute("PRAGMA table_info(report_snapshots)")}
        if "storage_mode" in columns:
            mode = connection.execute("SELECT storage_mode FROM report_snapshots WHERE id=?",
                                      (result["snapshot_id"],)).fetchone()
            if mode is not None and mode[0] == "lean":
                raise ValueError("Shared ID mapping link is missing; full provenance cannot be verified")
        return result

    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='report_semantic_id_links'").fetchone():
        return require_legacy_row()
    link = connection.execute(
        "SELECT l.mapping_ref,c.source_json,c.mapping_json FROM report_semantic_id_links l "
        "LEFT JOIN report_semantic_mapping_cache c ON c.mapping_ref=l.mapping_ref "
        "WHERE l.snapshot_id=? AND l.pointer=? AND l.version=?",
        (result["snapshot_id"], result["pointer"], result["version"])).fetchone()
    if link is None:
        return require_legacy_row()
    ref, source_json, mapping_json = link
    if source_json is None or mapping_json is None or _mapping_ref(source_json, mapping_json) != ref:
        raise ValueError("Shared ID mapping cache failed its content hash validation")
    expected = _json({"storage": SHARED_ID_MAPPING_STORAGE, "mapping_ref": ref})
    if result.get("source_json") != expected or result.get("mapping_json") != expected:
        raise ValueError("Shared ID mapping row no longer matches its cache reference")
    result.update(source_json=source_json, mapping_json=mapping_json)
    return result


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


def decode_events_on_demand(connection: sqlite3.Connection, snapshot_id: int,
                            events, mapper: SemanticMapper | None = None) -> dict:
    """Explain only the supplied page without creating or updating database rows.

    The current event decoder needs the armies, heroes and scenario, not the
    complete event history. Read those small projections to avoid materializing
    a full snapshot merely to display a page. Callers retain the raw events.
    """
    event_items = [dict(event) for event in events]
    if mapper is None:
        from .semantic_catalog import build_default_mapper
        mapper = build_default_mapper()
    if mapper is None:
        return {"version": None, "events": [], "storage": "on_demand",
                "available": False, "error": "未启用中文解释字典，原始事件仍可查看。"}
    row = connection.execute(
        "SELECT r.battle_uid FROM report_snapshots s JOIN report_records r ON r.id=s.report_id WHERE s.id=?",
        (snapshot_id,)).fetchone()
    if row is None:
        raise KeyError(f"Snapshot does not exist: {snapshot_id}")
    settlement = {side: json.loads(raw) for side, raw in connection.execute(
        "SELECT side,raw_json FROM report_sides WHERE snapshot_id=? ORDER BY id", (snapshot_id,))}
    scenario = None
    for pointer in ("/runtimeIdentity/scenarioId", "/data/scenarioId",
                    "/components/all/scenarioId", "/components/detail/scenarioId", "/components/basic/scenarioId"):
        value = connection.execute(
            "SELECT value_json FROM report_fields WHERE snapshot_id=? AND pointer=?", (snapshot_id, pointer)).fetchone()
        if value is not None:
            scenario = json.loads(value[0])
            if scenario is not None:
                break
    payload = {"data": {"battleSettlement": settlement}, "runtimeIdentity": {"scenarioId": scenario}}
    context = {"payload": payload, "battle_uid": row[0], "scenario_id": scenario}
    if mapper.context_id_lookup is not None:
        def lookup(kind, value):
            return mapper.context_id_lookup(kind, value, context)
    else:
        lookup = mapper.id_lookup
    context.update(build_context(payload, row[0], lookup))
    decoded = []
    for event in event_items:
        item = {"sequence": event["sequence"], "event_id": event.get("event_id")}
        try:
            raw_event = event.get("raw")
            if raw_event is None:
                raw_event = json.loads(event["raw_json"])
            if not isinstance(raw_event, dict) or raw_event.get("preview_truncated") or raw_event.get("preview_invalid_json"):
                raise ValueError("事件原文预览不完整，无法生成可靠解释。")
            result = dict(mapper.event_decode(raw_event, context))
            item.update(event_name_zh=_text(result.get("event_name_zh")),
                        summary_zh=_text(result.get("summary_zh")),
                        parameters=result.get("parameters", []), status=_text(result.get("status")) or "unmapped")
        except Exception as exc:
            item.update(event_name_zh=None, summary_zh=None, parameters=[], status="failed",
                        error=_text(f"{type(exc).__name__}: {exc}"))
        decoded.append(item)
    return {"version": mapper.version, "events": decoded, "storage": "on_demand", "available": True}


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

    def _ready_for(self, snapshot_id: int, include_events: bool) -> bool:
        previous = self.connection.execute(
            "SELECT status FROM report_semantic_runs WHERE snapshot_id=? AND version=?",
            (snapshot_id, self.mapper.version)).fetchone()
        if previous is None or previous[0] != "ready":
            return False
        if not include_events:
            return True
        mode = self.connection.execute(
            "SELECT event_mode FROM report_semantic_event_modes WHERE snapshot_id=? AND version=?",
            (snapshot_id, self.mapper.version)).fetchone()
        # Existing databases predate the mode table and already stored events.
        return mode is None or mode[0] == "stored"

    def enrich_snapshot(self, snapshot_id: int, *, force: bool = False, include_events: bool = True) -> dict:
        """Return failed on semantic errors; committed raw archive data is untouched."""
        connection, version = self.connection, self.mapper.version
        if not force and self._ready_for(snapshot_id, include_events):
            return {"status": "duplicate", "snapshot_id": snapshot_id, "version": version}
        snapshot_columns = {item[1] for item in connection.execute("PRAGMA table_info(report_snapshots)")}
        mode_sql = "s.storage_mode" if "storage_mode" in snapshot_columns else "'full'"
        row = connection.execute(f"SELECT r.battle_uid,s.payload_json,{mode_sql} FROM report_snapshots s "
                                 "JOIN report_records r ON r.id=s.report_id WHERE s.id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(f"Snapshot does not exist: {snapshot_id}")
        try:
            # Raw archive projections are immutable after import. Materialize
            # each SELECT and close its cursor before running user decoders;
            # no new read transaction or live cursor spans the long CPU phase.
            keys = tuple(dict.fromkeys(tuple(self.mapper.id_fields) + ("group", "skinId")))
            placeholders = ",".join("?" for _ in keys)
            payload = json.loads(row[1])
            shared_mappings = row[2] == "lean"
            if shared_mappings:
                # Lean omits the large snapshots/statics projections, but the
                # immutable payload still contains their complete ID values.
                # Match the previous compact ID-selection policy, including
                # IDs found only in those omitted branches.
                from .report_archive import iter_fields
                raw_ids = []
                for pointer, _, key, _, kind, value_json, _ in iter_fields(payload):
                    parts = pointer.split("/", 4)
                    if (pointer.startswith("/data/eventDataList/") or
                            (len(parts) == 5 and parts[1] == "components" and parts[3] == "eventDataList")):
                        continue
                    if key in keys or "/warbooks/idList/" in pointer or "/armsAdvancedSkillIdList/" in pointer:
                        raw_ids.append((pointer, key, value_json, kind))
                raw_ids.sort(key=lambda item: item[0])
            else:
                raw_ids = connection.execute(
                    f"SELECT pointer,field_key,value_json,node_type FROM report_fields WHERE snapshot_id=? "
                    f"AND (field_key IN ({placeholders}) OR pointer LIKE '%/warbooks/idList/%' "
                    f"OR pointer LIKE '%/armsAdvancedSkillIdList/%') ORDER BY pointer", (snapshot_id, *keys)).fetchall()
            raw_events = connection.execute(
                "SELECT sequence,event_id,raw_json FROM report_events WHERE snapshot_id=? ORDER BY sequence",
                (snapshot_id,)).fetchall() if include_events else []
            context = {"payload": payload, "battle_uid": row[0], "scenario_id": _scenario_id(payload)}
            if self.mapper.context_id_lookup is not None:
                def lookup(kind, value):
                    return self.mapper.context_id_lookup(kind, value, context)
            else:
                lookup = self.mapper.id_lookup
            context.update(build_context(payload, row[0], lookup))
            id_rows = []
            cache_rows = {}
            cache_links = []
            for pointer, key, value_json, node_type in raw_ids:
                kind = self._kind(key, pointer)
                if kind is None or node_type not in ("integer", "string"):
                    continue
                raw_value = json.loads(value_json)
                result = dict(lookup(kind, raw_value))
                mapped = result.get("mapped") is True
                status = _text(result.get("status")) or ("mapped" if mapped else "unmapped")
                source_json, mapping_json = _json(_source(result)), _json(result)
                if shared_mappings:
                    ref = _mapping_ref(source_json, mapping_json)
                    cache_rows[ref] = (ref, source_json, mapping_json)
                    cache_links.append((snapshot_id, pointer, version, ref))
                    source_json = mapping_json = _json({"storage": SHARED_ID_MAPPING_STORAGE, "mapping_ref": ref})
                id_rows.append((snapshot_id, pointer, version, kind, value_json, _text(result.get("name_zh")),
                                int(mapped), status, source_json, mapping_json))
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
                if not force and self._ready_for(snapshot_id, include_events):
                    return {"status": "duplicate", "snapshot_id": snapshot_id, "version": version}
                connection.execute("DELETE FROM report_semantic_id_links WHERE snapshot_id=? AND version=?", (snapshot_id, version))
                connection.execute("DELETE FROM report_id_meanings WHERE snapshot_id=? AND version=?", (snapshot_id, version))
                connection.execute("DELETE FROM report_event_meanings WHERE snapshot_id=? AND version=?", (snapshot_id, version))
                connection.executemany("INSERT OR IGNORE INTO report_semantic_mapping_cache VALUES(?,?,?)", cache_rows.values())
                for ref, source_json, mapping_json in cache_rows.values():
                    saved = connection.execute("SELECT source_json,mapping_json FROM report_semantic_mapping_cache WHERE mapping_ref=?", (ref,)).fetchone()
                    if tuple(saved) != (source_json, mapping_json):
                        raise ValueError("Shared ID mapping cache conflicts with its content hash")
                connection.executemany("INSERT INTO report_id_meanings VALUES(?,?,?,?,?,?,?,?,?,?)", id_rows)
                connection.executemany("INSERT INTO report_semantic_id_links VALUES(?,?,?,?)", cache_links)
                connection.executemany("INSERT INTO report_event_meanings VALUES(?,?,?,?,?,?,?,?,?,?)", event_rows)
                connection.execute("INSERT INTO report_semantic_event_modes VALUES(?,?,?) "
                                   "ON CONFLICT(snapshot_id,version) DO UPDATE SET event_mode=excluded.event_mode",
                                   (snapshot_id, version, "stored" if include_events else "on_demand"))
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
                    if not force and self._ready_for(snapshot_id, include_events):
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
            query += "AND x.version=? AND x.status='ready') OR EXISTS(SELECT 1 FROM report_semantic_event_modes m "
            query += "WHERE m.snapshot_id=s.id AND m.version=? AND m.event_mode='on_demand')"
            params = (self.mapper.version, self.mapper.version)
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


def enrich_snapshot(connection: sqlite3.Connection, snapshot_id: int, mapper: SemanticMapper, *,
                    force: bool = False, include_events: bool = True) -> dict:
    return SemanticArchive(connection, mapper).enrich_snapshot(snapshot_id, force=force, include_events=include_events)


def backfill(connection: sqlite3.Connection, mapper: SemanticMapper, *, only_current: bool = False,
             limit: int | None = None, force: bool = False, on_progress=None) -> dict:
    return SemanticArchive(connection, mapper).backfill(only_current=only_current, limit=limit,
                                                       force=force, on_progress=on_progress)
