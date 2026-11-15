"""Cloud statistics projected into a bounded, process-local SQLite database.

The projection deliberately contains no original JSON or event bodies.  Quality
and serialization checks still examine the original document before those large
branches are omitted.  A successful catalog refresh gates every summary, even
when all of its projection rows can be reused from RAM.
"""
from __future__ import annotations

from collections import defaultdict
import copy
import hashlib
import json
import re
import sqlite3
import threading
import time

from .data import BattleData, BattleDataError, _integer


_NAMESPACE = re.compile(r"server:(\d+)/scenario:(\d+)/avatar:(\d+)\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_STATE_FIELDS = ("version", "namespace", "runtimeIdentity", "runtimeCapabilities",
                 "runtimeCapabilitiesObservedAt", "lastCycle", "updatedAt")


def _encoded(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"),
                      sort_keys=True, allow_nan=False).encode("ascii")


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _identity_namespace(identity):
    if not isinstance(identity, dict):
        return None
    values = []
    for name in ("serverId", "scenarioId", "avatarId"):
        value = identity.get(name)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        text = str(value)
        if not text.isascii() or not text.isdigit():
            return None
        values.append(str(int(text)))
    return "server:{}/scenario:{}/avatar:{}".format(*values)


class _QualityFields:
    """Only the original field count and strings needed by _quality are kept."""

    def __init__(self, payload):
        self.count = 0
        self.placeholders = []

        def visit(value, pointer=""):
            self.count += 1
            if isinstance(value, str) and (value in ("<cycle>", "<max_depth>") or
                    value.startswith(("<function:", "<userdata:", "<thread:"))):
                self.placeholders.append((pointer, None, None, 0, "string", None, value))
            children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
            for key, child in children:
                token = str(key).replace("~", "~0").replace("/", "~1")
                visit(child, pointer + "/" + token)

        visit(payload)

    def __iter__(self):
        return iter(self.placeholders)

    def __len__(self):
        return self.count


def _projection_fields(value, pointer="", parent=None, key=None, ordinal=0):
    """Keep non-event metadata without building a second full fields list."""
    from parser.report_archive import iter_fields
    yield next(iter_fields(value, pointer, parent, key, ordinal))
    if key == "eventDataList" or pointer in ("/components/statics", "/components/snapshots"):
        return
    children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for index, (child_key, child) in enumerate(children):
        text = str(child_key)
        token = text.replace("~", "~0").replace("/", "~1")
        yield from _projection_fields(child, pointer + "/" + token, pointer, text, index)


def _statistical_document(payload):
    """Project precisely the branches consumed by the four statistics services.

    Settlement troops/heroes/skills have their original relational rows instead
    of four duplicate recursive JSON indexes.  Arbitrary children of evidence
    subtrees remain intact: a future/unknown client evidence field is not cut
    out of the recorded basis of an IsLegend evaluation.
    """
    def other(value):
        return ({"continuousInfo": value["continuousInfo"]} if "continuousInfo" in value else {}) if isinstance(value, dict) else value

    def combat(value):
        if not isinstance(value, dict):
            return value
        result = {key: value[key] for key in ("battleID", "battleJobId", "timeStamp", "endTimeStampTime",
                  "childNum", "matchType", "blockHash", "scenarioId") if key in value}
        if "otherInfo" in value:
            result["otherInfo"] = other(value["otherInfo"])
        settlement = value.get("battleSettlement")
        if isinstance(settlement, dict):
            selected = {"winer": settlement["winer"]} if "winer" in settlement else {}
            for side in ("attacker", "defensive", "defender"):
                army = settlement.get(side)
                if isinstance(army, dict):
                    side_value = {"avatar_data": army["avatar_data"]} if "avatar_data" in army else {}
                    members = army.get("army_member")
                    if isinstance(members, list):
                        side_value["army_member"] = []
                        for hero in members:
                            if not isinstance(hero, dict):
                                side_value["army_member"].append(hero)
                                continue
                            hero_value = {key: hero[key] for key in ("heros_id", "index", "group", "skinId") if key in hero}
                            if isinstance(hero.get("skills"), list):
                                hero_value["skills"] = [{key: skill[key] for key in ("skillId", "skill_id") if key in skill}
                                    if isinstance(skill, dict) else skill for skill in hero["skills"]]
                            side_value["army_member"].append(hero_value)
                    selected[side] = side_value
            result["battleSettlement"] = selected
        elif "battleSettlement" in value:
            result["battleSettlement"] = settlement
        return result

    def observation(value):
        if not isinstance(value, dict):
            return value
        result = {key: value[key] for key in ("scope", "parent", "clientPeerless", "sourceFile") if key in value}
        if "row" in value:
            row = value["row"]
            selected = combat(row)
            if isinstance(row, dict) and "combatInfo" in row:
                selected["combatInfo"] = combat(row["combatInfo"])
            result["row"] = selected
        return result

    result = {}
    if "runtimeIdentity" in payload:
        result["runtimeIdentity"] = payload["runtimeIdentity"]
    if "data" in payload:
        result["data"] = combat(payload["data"])
    components = payload.get("components")
    if isinstance(components, dict):
        result["components"] = {name: combat(components[name]) for name in ("all", "detail", "basic") if name in components}
    discovery = payload.get("discovery")
    if isinstance(discovery, list):
        result["discovery"] = [observation(value) for value in discovery]
    elif isinstance(discovery, dict):
        result["discovery"] = {key: observation(value) for key, value in discovery.items()}
    elif "discovery" in payload:
        result["discovery"] = discovery
    return result


def _state_time(state):
    value = state.get("runtimeCapabilitiesObservedAt")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        last = state.get("lastCycle")
        value = last.get("startedAt") if isinstance(last, dict) else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else float("-inf")


class _Summary:
    def __init__(self, engine, page):
        self.engine, self.page = engine, page

    def summary(self, *args, **kwargs):
        return self.engine.summary(self.page, *args, **kwargs)


class CloudStatsEngine:
    """Incremental, disposable statistical projection of verified cloud objects."""

    def __init__(self, owner, *, max_projection_bytes=1024 * 1024 * 1024,
                 max_metadata_bytes=64 * 1024 * 1024, progress=None):
        self.owner = owner
        self.store = owner.store
        self.max_projection_bytes = _integer(max_projection_bytes, "max_projection_bytes", 64 * 1024, 2 ** 31)
        self.max_metadata_bytes = _integer(max_metadata_bytes, "max_metadata_bytes", 1024, 1024 ** 3)
        self.progress = progress
        if progress is not None and not callable(progress):
            raise ValueError("stats progress must be callable or None")
        self.connection = None
        self.archive = None
        self._semantics = None
        self._services = {}
        self._signatures = {}
        self._state_cache = {}
        self._runtime_states = {}
        self._progress = {"phase": "idle", "completed": 0, "total": 0}
        self.coverage = {}
        self._cancelled = threading.Event()
        self._last_progress_emitted = 0.0
        self._last_progress_phase = None
        # This cache contains compressed bytes in this process only.  Report
        # ordering below also makes the store's one decoded chunk reusable.
        if hasattr(self.store, "compressed_cache_bytes"):
            current = self.store.compressed_cache_bytes
            self.store.compressed_cache_bytes = min(current or 128 * 1024 * 1024, 128 * 1024 * 1024)

    def proxy(self, page):
        return _Summary(self, page)

    def status(self):
        # Snapshots are replaced atomically. A GUI progress poll must never wait
        # behind the long first projection while its worker owns the lock.
        return {**self._progress, "coverage": copy.deepcopy(self.coverage),
                "writes_local_report_files": False, "disk_cache": False}

    def _emit(self, phase, completed=0, total=0):
        self._progress = {"phase": phase, "completed": completed, "total": total}
        if phase not in {"error", "closed", "idle"}:
            self._check_cancelled()
        now = time.monotonic()
        force = phase != self._last_progress_phase or phase in {"ready", "error", "closed", "idle"}
        if self.progress is not None and (force or now - self._last_progress_emitted >= .1):
            self._last_progress_emitted = now
            self._last_progress_phase = phase
            self.progress({**self._progress, "coverage": copy.deepcopy(self.coverage)})

    def _check_cancelled(self):
        if self._cancelled.is_set():
            raise BattleDataError("云端统计已取消；没有写入本地文件。")

    def _budget_error(self):
        limit = self.max_projection_bytes
        if self.connection is not None:
            try:
                pages = self.connection.execute("PRAGMA max_page_count").fetchone()[0]
                size = self.connection.execute("PRAGMA page_size").fetchone()[0]
                limit = pages * size
            except sqlite3.Error:
                pass
        return BattleDataError(
            f"运行内存统计预算达到上限（{limit / (1024 * 1024):g} MiB）；"
            "此前已完成的内存投影仍保留，可增加统计内存预算后重试。没有写入本地文件，也没有返回抽样结果。")

    def cancel(self):
        # Do not take the projection lock: shutdown must be able to request
        # cancellation while a cloud read or another worker owns that lock.
        self._cancelled.set()

    def close(self):
        with self.owner._lock:
            if self.connection is not None:
                self.connection.close()
            self.connection = None
            self.archive = None
            self._semantics = None
            self._services.clear()
            self._signatures.clear()
            self._state_cache.clear()
            self._runtime_states.clear()
            self.coverage = {}
            self._cancelled.clear()
            self._last_progress_emitted = 0.0
            self._last_progress_phase = None
            self._emit("closed")

    def _open(self):
        if self.connection is not None:
            return
        from parser.report_archive import ReportArchive
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        try:
            connection.execute("PRAGMA page_size=4096")
            connection.execute("PRAGMA max_page_count=" + str(self.max_projection_bytes // 4096))
            self.archive = ReportArchive(connection=connection, storage_mode="lean")
            from parser.semantic_catalog import build_default_mapper
            mapper = build_default_mapper()
            if mapper is not None:
                from parser.report_semantics import SemanticArchive
                self._semantics = SemanticArchive(connection, mapper)
            self.connection = connection
        except BaseException:
            connection.close()
            raise

    def _service(self, page):
        if page not in self._services:
            data = BattleData(connection=self.connection, timeout_seconds=30)
            if page == "daily":
                from .daily import DailyStats
                service = DailyStats(data=data, own_union_only=True, runtime_state_provider=self._runtime_state)
            elif page == "team_winrates":
                from .team_winrates import TeamWinrateService
                service = TeamWinrateService(data=data, runtime_state_provider=self._runtime_state)
            elif page == "opponents":
                from .opponents import OpponentRoster
                service = OpponentRoster(data=data)
            elif page == "peerless":
                from .peerless import PeerlessRecords
                service = PeerlessRecords(data=data)
            else:
                raise ValueError("Unknown cloud statistics page")
            self._services[page] = service
        return self._services[page]

    def _runtime_state(self, namespace):
        state = self._runtime_states.get(namespace)
        return copy.deepcopy(state) if state is not None else None

    def _object_order(self, info):
        catalog = getattr(self.store, "catalog", {})
        blobs = catalog.get("blobs", {})
        blob = blobs.get(info.get("blob_sha256", info.get("sha256")), {}) if isinstance(blobs, dict) else {}
        parts = blob.get("parts", []) if isinstance(blob, dict) else []
        if parts and isinstance(parts[0], dict) and isinstance(parts[0].get("chunk"), dict):
            part = parts[0]
            chunk = part["chunk"]
            offset = part.get("offset", 0)
            return (str(chunk.get("stored_sha256") or chunk.get("fid") or ""),
                    offset if isinstance(offset, int) else 0)
        return ("", 0)

    @staticmethod
    def _checked_state(value, key=None):
        if not isinstance(value, dict) or value.get("version") != 1 or isinstance(value.get("version"), bool):
            return None
        namespace = value.get("namespace")
        if not isinstance(namespace, str) or _NAMESPACE.fullmatch(namespace) is None:
            return None
        if _identity_namespace(value.get("runtimeIdentity")) != namespace:
            return None
        if key is not None:
            parts = key.replace("\\", "/").split("/")
            folder = hashlib.sha256(namespace.encode()).hexdigest()[:20]
            if "runtime" in parts:
                index = max(i for i, part in enumerate(parts) if part == "runtime")
                if index + 1 >= len(parts) or parts[index + 1] != folder:
                    return None
            if parts and parts[0] == "capture" and (len(parts) < 3 or parts[1] != folder):
                return None
        state = {name: copy.deepcopy(value[name]) for name in _STATE_FIELDS if name in value}
        observations = {}
        reports = value.get("reports", {})
        if not isinstance(reports, dict):
            return None
        for uid, entry in reports.items():
            if not isinstance(entry, dict) or str(entry.get("battleID")) != str(uid):
                continue
            source = entry.get("observations", {})
            source = source.values() if isinstance(source, dict) else source if isinstance(source, list) else ()
            good = []
            for observation in source:
                if not isinstance(observation, dict) or not isinstance(observation.get("row"), dict):
                    continue
                row = observation["row"]
                combat = row if observation.get("parent") is not None else row.get("combatInfo")
                if not isinstance(combat, dict) or isinstance(combat.get("battleID"), bool) or str(combat.get("battleID")) != str(uid):
                    continue
                good.append(copy.deepcopy(observation))
            if good:
                observations[str(uid)] = good
        return namespace, state, observations

    def _read_file(self, key, info):
        from parser.report_archive import _decode
        size = info.get("byte_count")
        if isinstance(size, int) and not isinstance(size, bool) and size > self.max_metadata_bytes:
            raise BattleDataError("云端观察检查点超过内存读取上限，本次没有返回缺少证据的统计。")
        raw = self.store.read_file(key, maximum=self.max_metadata_bytes)
        self._check_cancelled()
        if not isinstance(raw, bytes) or len(raw) > self.max_metadata_bytes:
            raise BattleDataError("云端观察检查点超过内存读取上限。")
        expected = info.get("sha256")
        if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
            raise BattleDataError("云端观察检查点的内容哈希不匹配。")
        return _decode(raw)

    def _metadata(self, rows):
        catalog = getattr(self.store, "catalog", {})
        if not isinstance(catalog, dict):
            raise BattleDataError("云端目录格式无效。")
        namespaces = {row["namespace"] for row in rows}
        merged = defaultdict(list)
        seen = defaultdict(set)
        state_namespaces = set()
        runtimes = {}
        coverage = {"cloud_reports": len(rows), "checkpoint_sources": 0,
                    "invalid_checkpoints": 0, "checkpoint_observations": 0,
                    "capture_metadata_sources": 0, "metadata_scope": "best_reports_and_cloud_checkpoints"}

        def accept(checked):
            if checked is None:
                coverage["invalid_checkpoints"] += 1
                return
            namespace, state, observations = checked
            if namespace not in namespaces:
                return
            state_namespaces.add(namespace)
            coverage["checkpoint_sources"] += 1
            if namespace not in runtimes or _state_time(state) > _state_time(runtimes[namespace]):
                runtimes[namespace] = state
            for uid, entries in observations.items():
                identity = namespace, uid
                for entry in entries:
                    token = _hash(entry)
                    if token not in seen[identity]:
                        seen[identity].add(token)
                        merged[identity].append(entry)
                        coverage["checkpoint_observations"] += 1

        states = catalog.get("states", {})
        if isinstance(states, dict):
            for namespace in sorted(namespaces):
                if namespace in states:
                    checked = self._checked_state(states[namespace])
                    if checked is not None and checked[0] != namespace:
                        checked = None
                    accept(checked)
        files = catalog.get("files", {})
        if not isinstance(files, dict):
            raise BattleDataError("云端文件目录格式无效。")
        state_files = [(key, info) for key, info in files.items()
                       if isinstance(key, str) and key.replace("\\", "/").split("/")[-1] == "collector_state.json"]
        declared = {}
        for key, info in state_files:
            if isinstance(info, dict) and type(info.get("byte_count")) is int:
                declared[info.get("sha256") or key] = info["byte_count"]
        if sum(declared.values()) > 2 * self.max_metadata_bytes:
            raise BattleDataError("云端观察检查点总量超过运行内存预算，本次没有返回截断统计。")
        live_cache = set()
        for index, (key, info) in enumerate(sorted(state_files), 1):
            self._emit("metadata", index - 1, len(state_files))
            if not isinstance(info, dict):
                raise BattleDataError("云端检查点目录项格式无效。")
            digest = info.get("sha256")
            token = (key, digest) if isinstance(digest, str) and _SHA256.fullmatch(digest) else None
            if token is not None:
                live_cache.add(token)
            if token is not None and token in self._state_cache:
                checked = self._state_cache[token]
            else:
                value = self._read_file(key, info)
                checked = self._checked_state(value, key)
                if token is not None:
                    self._state_cache[token] = checked
            accept(checked)
        # Immutable discovery captures are the fallback when no usable account
        # checkpoint survived migration. Never scan unrelated logs/backups.
        needed = {(row["namespace"], row["battle_uid"]) for row in rows
                  if row["namespace"] not in state_namespaces}
        needed_uids = {uid for _namespace, uid in needed}
        fallback = []
        for key, info in files.items():
            if not isinstance(key, str) or not isinstance(info, dict):
                continue
            parts = key.replace("\\", "/").split("/")
            if len(parts) < 3 or parts[-3] != "captures" or not parts[-1].endswith(".json"):
                continue
            # Migrated file aliases can be labelled 'legacy'. Bind the decoded
            # runtime identity instead of borrowing the alias's account label.
            if parts[-2] in needed_uids:
                fallback.append((key, info, parts[-2]))
        for index, (key, info, uid) in enumerate(sorted(fallback), 1):
            self._emit("metadata_recovery", index - 1, len(fallback))
            digest = info.get("sha256")
            cache_token = ("capture", key, digest) if isinstance(digest, str) and _SHA256.fullmatch(digest) else None
            if cache_token is not None:
                live_cache.add(cache_token)
            if cache_token is not None and cache_token in self._state_cache:
                checked = self._state_cache[cache_token]
            else:
                value = self._read_file(key, info)
                checked = None
                if isinstance(value, dict) and str(value.get("battleID")) == uid:
                    namespace = _identity_namespace(value.get("runtimeIdentity"))
                    # Reuse the same account/report binding validation as states.
                    checked = self._checked_state({"version": 1, "namespace": namespace,
                        "runtimeIdentity": value.get("runtimeIdentity"), "reports": {uid: {"battleID": uid,
                        "observations": value.get("discovery", [])}}}, key)
                if cache_token is not None:
                    self._state_cache[cache_token] = checked
            if checked is None or (checked[0], uid) not in needed:
                continue
            namespace = checked[0]
            for observation in checked[2].get(uid, []):
                token = _hash(observation)
                if token not in seen[(namespace, uid)]:
                    seen[(namespace, uid)].add(token)
                    merged[(namespace, uid)].append(observation)
            coverage["capture_metadata_sources"] += 1
        self._state_cache = {key: value for key, value in self._state_cache.items() if key in live_cache}
        coverage["checkpoint_missing_namespaces"] = sorted(namespaces - state_namespaces)
        # A checkpoint can establish what was actually observed, never that the
        # game/account's entire history was collected.
        coverage["account_history_complete"] = False
        self._runtime_states = runtimes
        return merged, coverage

    def _delete(self, snapshot_id):
        connection = self.connection
        if self._semantics is not None:
            for table in ("report_semantic_id_links", "report_id_meanings", "report_event_meanings",
                          "report_semantic_event_modes", "report_semantic_runs"):
                connection.execute("DELETE FROM " + table + " WHERE snapshot_id=?", (snapshot_id,))
        for table in ("report_skills", "report_heroes", "report_sides", "report_events",
                      "report_components", "report_fields"):
            connection.execute("DELETE FROM " + table + " WHERE snapshot_id=?", (snapshot_id,))
        connection.execute("UPDATE report_records SET current_snapshot_id=NULL WHERE id=?", (snapshot_id,))
        connection.execute("DELETE FROM report_snapshots WHERE id=?", (snapshot_id,))
        connection.execute("DELETE FROM report_records WHERE id=?", (snapshot_id,))
        connection.execute("DELETE FROM report_files WHERE NOT EXISTS (SELECT 1 FROM report_snapshots s WHERE s.file_id=report_files.id)")

    def _project(self, row, observations):
        from parser.report_archive import (COMPONENTS, _decode, _int, _json, _main_data,
            _mapping, _quality, _settlement_source, _text, _uid)
        raw = self.store.read_report(row["key"])
        self._check_cancelled()
        if not isinstance(raw, bytes) or len(raw) > self.owner.max_report_bytes:
            raise BattleDataError("该份统计原文超过内存读取上限，没有转存本地文件。")
        digest = hashlib.sha256(raw).hexdigest()
        if row.get("blob_sha256") is not None and digest != row["blob_sha256"]:
            raise BattleDataError("云端统计原文与目录哈希不匹配。")
        document = _decode(raw)
        index = row.get("document_index", 0)
        _integer(index, "document_index", 0, 2 ** 31 - 1)
        documents = document if isinstance(document, list) else [document]
        if index >= len(documents) or not isinstance(documents[index], dict):
            raise BattleDataError("云端目录中的原文文档序号不匹配。")
        payload = documents[index]
        if "runtimeIdentity" in payload and _identity_namespace(payload["runtimeIdentity"]) != row["namespace"]:
            raise BattleDataError("云端目录与统计原文的运行账号身份不匹配，本次没有跨账号统计。")
        data, _ = _main_data(payload)
        uid = _uid(payload.get("battleID")) or _uid(data.get("battleID"))
        if uid != row["battle_uid"]:
            raise BattleDataError("云端目录与统计原文的战报编号不匹配。")
        for candidate in [data, *_mapping(payload.get("components")).values()]:
            candidate_uid = _uid(_mapping(candidate).get("battleID"))
            if candidate_uid is not None and candidate_uid != uid:
                raise BattleDataError("云端原文组件包含相互冲突的战报编号。")
        quality_fields = _QualityFields(payload)
        completeness, quality, issues = _quality(payload, quality_fields, data)
        event_count = len(data["eventDataList"]) if isinstance(data.get("eventDataList"), list) else 0
        merged = dict(payload)
        discovery = payload.get("discovery")
        existing = discovery.values() if isinstance(discovery, dict) else discovery if isinstance(discovery, list) else ()
        seen = {_hash(item) for item in existing}
        additions = []
        for observation in observations:
            token = _hash(observation)
            if token not in seen:
                additions.append((token, observation))
                seen.add(token)
        if additions:
            if isinstance(discovery, dict):
                replacement = dict(discovery)
                for token, value in additions:
                    key = "cloud_observation_" + token
                    while key in replacement:
                        key += "_"
                    replacement[key] = value
            else:
                replacement = list(discovery) if isinstance(discovery, list) else []
                replacement.extend(value for _, value in additions)
            merged["discovery"] = replacement
        if "runtimeIdentity" not in payload:
            state = self._runtime_states.get(row["namespace"])
            if state is not None:
                merged["runtimeIdentity"] = state["runtimeIdentity"]
        sid = row["snapshot_id"]
        now = row.get("imported_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        connection = self.connection
        statistical = _statistical_document(merged)
        with connection:
            self._delete(sid)
            connection.execute("INSERT OR IGNORE INTO report_files(namespace,sha256,raw_bytes,byte_count,source_path,parse_status,first_seen,last_seen) VALUES(?,?,?,?,?,'parsed',?,?)",
                (row["namespace"], digest, b"", len(raw), str(row.get("file_key") or row["key"]), now, now))
            file_id = connection.execute("SELECT id FROM report_files WHERE namespace=? AND sha256=?", (row["namespace"], digest)).fetchone()[0]
            connection.execute("INSERT INTO report_records(id,namespace,battle_uid,current_snapshot_id,first_seen,last_seen) VALUES(?,?,?,NULL,?,?)",
                (sid, row["namespace"], uid, row.get("first_seen") or now, row.get("last_seen") or now))
            connection.execute("INSERT INTO report_snapshots(id,report_id,file_id,document_index,payload_json,source,capture_version,completeness,quality_json,issues_json,event_count,field_count,imported_at,storage_mode,indexed_field_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'lean',0)",
                (sid, sid, file_id, index, "{}", _text(payload.get("source")), _int(payload.get("captureVersion")), completeness,
                 _json(quality), _json(issues), event_count, len(quality_fields), now))
            count = 0
            def fields():
                nonlocal count
                for field in _projection_fields(statistical):
                    count += 1
                    yield (sid, *field)
            connection.executemany("INSERT INTO report_fields VALUES(?,?,?,?,?,?,?,?)", fields())
            components = _mapping(payload.get("components"))
            statuses = _mapping(payload.get("componentStatus"))
            for name in sorted(set(COMPONENTS) | set(components) | set(statuses)):
                status = _mapping(statuses.get(name))
                available = name in components and components[name] is not None
                requested = int(status["requested"]) if isinstance(status.get("requested"), bool) else None
                complete = int(status["complete"] is True and available and status.get("available") is True and not status.get("error")) if "complete" in status else None
                connection.execute("INSERT INTO report_components VALUES(?,?,NULL,?,?,?,?)",
                    (sid, name, requested, int(available), complete, _json(status)))
            settlement, _ = _settlement_source(payload)
            self.archive._store_settlement_projection(sid, settlement)
            if self._semantics is not None:
                # Lean semantic indexing reads payload_json. Give it just the
                # small ID/identity projection, then erase that temporary copy.
                semantic_document = {key: value for key, value in statistical.items() if key != "discovery"}
                connection.execute("UPDATE report_snapshots SET payload_json=? WHERE id=?", (_json(semantic_document), sid))
                enriched = self._semantics.enrich_snapshot(sid, include_events=False)
                if enriched.get("status") not in {"ready", "duplicate"}:
                    if "database or disk is full" in str(enriched.get("error")).casefold():
                        raise self._budget_error()
                    raise BattleDataError("云端统计的中文名称映射失败：" + str(enriched.get("error")))
                connection.execute("UPDATE report_snapshots SET payload_json='{}' WHERE id=?", (sid,))
            connection.execute("UPDATE report_snapshots SET indexed_field_count=? WHERE id=?", (count, sid))
            connection.execute("UPDATE report_records SET current_snapshot_id=? WHERE id=?", (sid, sid))

    def summary(self, page, *args, **kwargs):
        with self.owner._lock:
            try:
                self._emit("refresh")
                rows = self.owner._refresh_rows()
                namespace = kwargs.get("namespace")
                position = 1 if page == "daily" else 0
                if namespace is None and len(args) > position:
                    namespace = args[position]
                if namespace is not None and (not isinstance(namespace, str) or len(namespace) > 500):
                    raise ValueError("Invalid account namespace")
                selected = [row for row in rows if namespace is None or row["namespace"] == namespace]
                observations, coverage = self._metadata(selected)
                self._open()
                self.connection.execute("PRAGMA query_only=OFF")
                live_ids = {row["snapshot_id"] for row in rows}
                for sid in set(self._signatures) - live_ids:
                    with self.connection:
                        self._delete(sid)
                    self._signatures.pop(sid)
                for index, row in enumerate(sorted(selected, key=self._object_order)):
                    self._emit("reports", index, len(selected))
                    size = row.get("byte_count")
                    if isinstance(size, int) and not isinstance(size, bool) and size > self.owner.max_report_bytes:
                        raise BattleDataError("该份统计原文超过内存读取上限，本次未返回不完整统计。")
                    identity = row["namespace"], row["battle_uid"]
                    evidence = observations.get(identity, [])
                    token = _hash([row, evidence, self._runtime_states.get(row["namespace"], {}).get("runtimeIdentity")])
                    # Without a content hash the catalog cannot prove that a
                    # cached document still denotes these bytes.
                    reusable = isinstance(row.get("blob_sha256"), str) and _SHA256.fullmatch(row["blob_sha256"])
                    if not reusable or self._signatures.get(row["snapshot_id"]) != token:
                        self._project(row, evidence)
                        self._signatures[row["snapshot_id"]] = token
                self.coverage = {**coverage, "projected_reports": len(selected),
                    "catalog_reports": len(rows), "namespace": namespace,
                    "sqlite_bytes": self.connection.execute("PRAGMA page_count").fetchone()[0] * 4096,
                    "originals_retained": False, "event_bodies_retained": False,
                    "semantic_names_available": self._semantics is not None}
                self._emit("query", len(selected), len(selected))
                result = self._service(page).summary(*args, **kwargs)
                result.update(storage_backend="quark-memory", memory_only=True,
                              cloud_projection=copy.deepcopy(self.coverage))
                self._emit("ready", len(selected), len(selected))
                return result
            except (MemoryError, RecursionError) as exc:
                self._emit("error")
                raise BattleDataError("云端统计超过运行内存或文档层级限制；没有写入本地文件，也没有返回抽样结果。") from exc
            except sqlite3.DatabaseError as exc:
                self._emit("error")
                if (getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL or
                        "database or disk is full" in str(exc).casefold()):
                    raise self._budget_error() from exc
                raise BattleDataError("云端统计内存投影无法完成，未写入本地文件：" + str(exc)) from exc
            except BaseException:
                self._emit("error")
                raise
