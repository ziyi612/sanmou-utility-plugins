"""Conservative offline recovery from one explicitly chosen snapshot pair.

No game connection, database writes or automatic donor search occur here. The
result is a derived document with per-component provenance, never a claim that
all components arrived in one network capture.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .report_archive import COMPONENTS, ReportArchive


class ComponentRecoveryError(ValueError):
    """Captured sources are insufficient or inconsistent for safe recovery."""


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _same(left: Any, right: Any) -> bool:
    return _json(left) == _json(right)


def _uid(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ComponentRecoveryError("A captured battle ID is missing or invalid")
    return str(value)


def _identity(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ComponentRecoveryError("A source lacks runtime account identity")
    result = {}
    for key in ("avatarId", "serverId", "scenarioId"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise ComponentRecoveryError("A source account identity is invalid")
        text = str(raw)
        if not text.isascii() or not text.isdigit() or int(text) < (0 if key == "scenarioId" else 1):
            raise ComponentRecoveryError("A source account identity is invalid")
        result[key] = str(int(text))
    return result


def _typed_key_types(payload: dict) -> dict:
    value = payload.get("luaTableKeyTypes", {})
    # The runtime serializer represents an empty Lua table as []. An empty
    # registry has no entries to lose; a nonempty array is not a valid map.
    if value == []:
        return {}
    if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(item, dict)
                                           for key, item in value.items()):
        raise ComponentRecoveryError("A source has invalid typed Lua-key metadata")
    return value


def _load_source(archive: ReportArchive, snapshot_id: int, namespace: str, battle_uid: str,
                 prepared_originals: dict[int, bytes] | None = None) -> dict:
    row = archive.connection.execute(
        "SELECT s.id,s.payload_json,s.file_id,s.document_index,r.namespace,r.battle_uid,f.namespace file_namespace,"
        "f.sha256,f.byte_count,f.parse_status FROM report_snapshots s "
        "JOIN report_records r ON r.id=s.report_id JOIN report_files f ON f.id=s.file_id WHERE s.id=?",
        (snapshot_id,)).fetchone()
    if row is None or row["namespace"] != namespace or row["file_namespace"] != namespace or row["battle_uid"] != battle_uid:
        raise ComponentRecoveryError("Source snapshot does not belong to the explicitly selected account and battle")
    try:
        raw = prepared_originals.get(row["file_id"]) if prepared_originals is not None else None
        if raw is None:
            raw = archive.original_file_bytes(row["file_id"])
    except (ValueError, OSError, RuntimeError, ImportError) as exc:
        raise ComponentRecoveryError("A source original file is unavailable or failed its byte/hash validation") from exc
    if row["parse_status"] != "parsed" or len(raw) != row["byte_count"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
        raise ComponentRecoveryError("A source original file failed its byte/hash validation")
    document = json.loads(raw)
    if isinstance(document, list):
        try:
            original = document[row["document_index"]]
        except IndexError as exc:
            raise ComponentRecoveryError("A source document index is invalid") from exc
    else:
        if row["document_index"] != 0:
            raise ComponentRecoveryError("A source document index is invalid")
        original = document
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict) or not _same(original, payload):
        raise ComponentRecoveryError("A source snapshot no longer matches its original bytes")
    if _uid(payload.get("battleID")) != battle_uid or payload.get("namespace", namespace) != namespace:
        raise ComponentRecoveryError("A source payload has a conflicting account or battle ID")
    version = payload.get("captureVersion")
    if isinstance(version, bool) or not isinstance(version, int) or version < 2:
        raise ComponentRecoveryError("Only modern runtime component captures can be recovered")
    if payload.get("captureKind") == "component_recovery" or "componentRecovery" in payload:
        raise ComponentRecoveryError("Use original captures, not an already derived recovery")
    if payload.get("serializationIssues") != [] or payload.get("captureWarnings", []) != []:
        raise ComponentRecoveryError("A source has unresolved serialization or capture warnings")
    components = payload.get("components")
    if not isinstance(components, dict):
        raise ComponentRecoveryError("A source lacks captured components")
    for value in [payload.get("data"), *components.values()]:
        if isinstance(value, dict) and "battleID" in value and _uid(value["battleID"]) != battle_uid:
            raise ComponentRecoveryError("A captured component has a conflicting battle ID")
    _typed_key_types(payload)
    return {"snapshot_id": snapshot_id, "raw_sha256": row["sha256"], "document_index": row["document_index"],
            "payload": payload, "identity": _identity(payload.get("runtimeIdentity"))}


def _complete_component(source: dict, name: str) -> tuple[dict, dict]:
    payload = source["payload"]
    component = payload["components"].get(name)
    source_statuses = payload.get("componentStatus")
    if not isinstance(source_statuses, dict):
        raise ComponentRecoveryError("A source lacks component status metadata")
    status = source_statuses.get(name)
    if (not isinstance(component, dict) or not isinstance(status, dict) or status.get("available") is not True
            or status.get("complete") is not True or status.get("error")):
        raise ComponentRecoveryError(f"The selected {name} component is not demonstrably complete")
    if name in ("all", "detail"):
        events = component.get("eventDataList")
        if not isinstance(events, list) or not events:
            raise ComponentRecoveryError("Zero or unavailable event history cannot be marked complete")
        if status.get("eventCount", len(events)) != len(events):
            raise ComponentRecoveryError("A source component event count is inconsistent")
    return component, status


def _snapshot_checks(component: dict, event_count: int) -> dict:
    data = component.get("battleSnapData")
    if component.get("isComplete") is not True or not isinstance(data, dict) or not data:
        raise ComponentRecoveryError("The donor has no complete deserialized snapshot data")
    pages = component.get("pages")
    if isinstance(pages, list):
        page_values = list(pages)
        keys = list(range(1, len(pages) + 1))
    elif isinstance(pages, dict):
        try:
            keys = sorted(int(key) for key in pages)
        except (ValueError, TypeError) as exc:
            raise ComponentRecoveryError("Snapshot page keys cannot be verified") from exc
        if any(str(key) not in pages for key in keys):
            raise ComponentRecoveryError("Snapshot page keys are ambiguous")
        page_values = [pages[str(key)] for key in keys]
    else:
        raise ComponentRecoveryError("The donor lacks its received snapshot pages")
    count = component.get("receivedCount")
    if (not keys or keys != list(range(1, len(keys) + 1)) or isinstance(count, bool)
            or not isinstance(count, int) or count != len(keys)):
        raise ComponentRecoveryError("Snapshot page count or continuity is inconsistent")
    max_page = component.get("maxPage")
    if max_page is not None and (isinstance(max_page, bool) or not isinstance(max_page, int) or max_page != len(keys)):
        raise ComponentRecoveryError("Retained maxPage does not match the complete received pages")
    maximum = data.get("maxIDX")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum != event_count - 1:
        raise ComponentRecoveryError("Snapshot event boundary does not match the full event history")
    if any(not isinstance(page, dict) or isinstance(page.get("maxIDX"), bool) or page.get("maxIDX") != maximum or
           not isinstance(page.get("snapDataList"), (list, dict)) for page in page_values):
        raise ComponentRecoveryError("Snapshot page event boundaries cannot be verified")
    if any(not isinstance(data.get(key), (dict, list)) for key in ("baseBattleInfoRecords", "diffHeroInfoRecords")):
        raise ComponentRecoveryError("Deserialized snapshot record groups are missing")
    return {"pages": keys, "receivedCount": count, "maxIDX": maximum,
            "client_complete_flag": True, "expected_maxPage_retained": component.get("maxPage")}


def _component_key_types(source: dict, name: str) -> dict:
    prefix = "/components/" + name
    maps = _typed_key_types(source["payload"])
    selected = {pointer[len(prefix):]: copy.deepcopy(value) for pointer, value in maps.items()
                if pointer == prefix or pointer.startswith(prefix + "/")}
    if name == "all":
        if not _same(source["payload"].get("data"), source["payload"]["components"]["all"]):
            raise ComponentRecoveryError("Primary data is not the selected full component")
        aliases = {pointer[len("/data"):]: copy.deepcopy(value) for pointer, value in maps.items()
                   if pointer == "/data" or pointer.startswith("/data/")}
        for suffix, value in aliases.items():
            if suffix in selected and not _same(selected[suffix], value):
                raise ComponentRecoveryError("Typed Lua keys conflict between full data aliases")
            selected[suffix] = value
    return {prefix + suffix: value for suffix, value in selected.items()}


@dataclass(frozen=True)
class RecoveryDocument:
    db_path: Path
    raw_bytes: bytes

    @property
    def payload(self) -> dict:
        return json.loads(self.raw_bytes)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw_bytes).hexdigest()

    @property
    def manifest(self) -> dict:
        payload = self.payload
        return {"namespace": payload["namespace"], "battle_uid": str(payload["battleID"]),
                "sha256": self.sha256, "bytes": len(self.raw_bytes), "capture_kind": "component_recovery",
                "recovery": payload["componentRecovery"]}

    def write(self, path: str | Path) -> dict:
        """Publish a new output file without replacing sources or DB sidecars."""
        with ReportArchive(self.db_path, read_only=True) as archive:
            target = archive.protect_output_path(path)
        if target.exists():
            if not target.is_file() or target.read_bytes() != self.raw_bytes:
                raise FileExistsError(f"Recovery output already contains different data: {target}")
            return {**self.manifest, "path": str(target), "existing_identical": True}
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".component-recovery-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(self.raw_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if not target.is_file() or target.read_bytes() != self.raw_bytes:
                    raise FileExistsError("A different recovery output appeared during publication")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {**self.manifest, "path": str(target), "existing_identical": False}


def build_component_recovery(db_path, *, namespace: str, battle_uid: str,
                             primary_snapshot_id: int, snapshot_source_id: int) -> RecoveryDocument:
    """Combine one explicit full-event capture and one explicit snapshot donor."""
    if not isinstance(namespace, str) or not namespace.strip():
        raise ComponentRecoveryError("An explicit namespace is required")
    battle_uid = _uid(battle_uid)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in (primary_snapshot_id, snapshot_source_id)) or primary_snapshot_id == snapshot_source_id:
        raise ComponentRecoveryError("Choose two distinct explicit source snapshot IDs")
    with ReportArchive(db_path, read_only=True) as archive:
        # Validate the explicitly selected ownership before resolving cloud
        # objects, then complete any network work before the read transaction.
        source_files = []
        for snapshot_id in (primary_snapshot_id, snapshot_source_id):
            source = archive.connection.execute(
                "SELECT s.file_id FROM report_snapshots s JOIN report_records r ON r.id=s.report_id "
                "JOIN report_files f ON f.id=s.file_id WHERE s.id=? AND r.namespace=? "
                "AND f.namespace=? AND r.battle_uid=?", (snapshot_id, namespace, namespace, battle_uid)).fetchone()
            if source is None:
                raise ComponentRecoveryError("Source snapshot does not belong to the explicitly selected account and battle")
            source_files.append(source[0])
        # Only these two explicit originals are retained during reconstruction;
        # a tiny shared cloud cache may evict either one during the next fetch.
        prepared_originals = {}
        for file_id in dict.fromkeys(source_files):
            try:
                prepared_originals[file_id] = archive.original_file_bytes(file_id)
            except (ValueError, OSError, RuntimeError, ImportError) as exc:
                raise ComponentRecoveryError("A source original file is unavailable or failed its byte/hash validation") from exc
        archive._original_cache_only = True
        archive.connection.execute("BEGIN")
        primary = _load_source(archive, primary_snapshot_id, namespace, battle_uid, prepared_originals)
        donor = _load_source(archive, snapshot_source_id, namespace, battle_uid, prepared_originals)
    if primary["identity"] != donor["identity"]:
        raise ComponentRecoveryError("The sources belong to different runtime accounts or scenarios")
    canonical = "server:{serverId}/scenario:{scenarioId}/avatar:{avatarId}".format(**primary["identity"])
    if namespace.startswith("server:") and namespace != canonical:
        raise ComponentRecoveryError("The namespace conflicts with the captured account identity")
    selected, statuses, sources = {}, {}, {}
    for name in COMPONENTS:
        source = donor if name == "snapshots" else primary
        selected[name], statuses[name] = _complete_component(source, name)
        sources[name] = source
    donor_detail, _ = _complete_component(donor, "detail")
    donor_basic, _ = _complete_component(donor, "basic")
    donor_statics, _ = _complete_component(donor, "statics")
    full = selected["all"]
    events = full["eventDataList"]
    if not _same(donor_detail["eventDataList"], events) or not _same(selected["detail"]["eventDataList"], events):
        raise ComponentRecoveryError("Detail event histories do not exactly match the full history")
    if not _same(donor_statics, selected["statics"]):
        raise ComponentRecoveryError("Statistics differ between the source captures")
    settlement = full.get("battleSettlement")
    if not isinstance(settlement, dict) or not settlement:
        raise ComponentRecoveryError("The captured settlement headers differ or are unavailable")
    required_headers = ("battleID", "battleJobId", "timeStamp", "endTimeStampTime", "combatType", "scenarioId")
    optional_headers = ("matchType", "locationOccur", "endRound", "drawNum", "sceneDamage", "siege", "siegeConfigId", "combatHashKey")
    for header in (donor_basic, selected["basic"]):
        if not _same(header.get("battleSettlement"), settlement):
            raise ComponentRecoveryError("The captured settlement headers differ or are unavailable")
        for name in (*required_headers, *optional_headers):
            if name in required_headers and (name not in full or name not in header):
                raise ComponentRecoveryError(f"A critical battle header is missing: {name}")
            if name in full or name in header:
                if name not in full or name not in header or not _same(full[name], header[name]):
                    raise ComponentRecoveryError(f"A critical battle header differs: {name}")
    if _uid(full["scenarioId"]) != primary["identity"]["scenarioId"]:
        raise ComponentRecoveryError("The battle header conflicts with the runtime scenario")
    page_checks = _snapshot_checks(selected["snapshots"], len(events))
    result = copy.deepcopy(primary["payload"])
    result.pop("exportedAt", None)
    result.update(captureKind="component_recovery", captureComplete=True, source="all", hasAll=True,
                  hasDetail=True, hasBasic=True, eventCount=len(events), captureWarnings=[], serializationIssues=[],
                  components=copy.deepcopy(selected), data=copy.deepcopy(full), namespace=namespace)
    result["componentStatus"] = {name: {"available": True, "complete": True, "requested": False,
        "recovered": True, "sourceSnapshotId": sources[name]["snapshot_id"],
        **({"eventCount": len(selected[name]["eventDataList"])} if name in ("all", "detail") else {})} for name in COMPONENTS}
    key_types = {pointer: copy.deepcopy(value) for pointer, value in _typed_key_types(primary["payload"]).items()
                 if not (pointer == "/data" or pointer.startswith("/data/") or pointer == "/components" or pointer.startswith("/components/"))}
    for name in COMPONENTS:
        copied_types = _component_key_types(sources[name], name)
        key_types.update(copied_types)
        if name == "all":
            for pointer, value in copied_types.items():
                key_types["/data" + pointer[len("/components/all"):]] = copy.deepcopy(value)
    result["luaTableKeyTypes"] = key_types if key_types else copy.deepcopy(primary["payload"].get("luaTableKeyTypes", {}))
    component_sources = {name: {"snapshot_id": source["snapshot_id"], "raw_sha256": source["raw_sha256"],
        "document_index": source["document_index"], "pointer": "/components/" + name,
        "original_status": copy.deepcopy(statuses[name])} for name, source in sources.items()}
    result["componentRecovery"] = {"version": 1, "method": "explicit_snapshot_pair", "new_network_capture": False,
        "identity": primary["identity"], "components": component_sources,
        "inputs": [{"snapshot_id": source["snapshot_id"], "raw_sha256": source["raw_sha256"],
                    "exportedAt": source["payload"].get("exportedAt"), "captureComplete": source["payload"].get("captureComplete")}
                   for source in sorted((primary, donor), key=lambda item: item["snapshot_id"])],
        "checks": {"identity_equal": True, "settlement_equal": True, "critical_headers_equal": True,
                   "detail_events_equal_full": True, "statics_equal": True, "snapshot": page_checks},
        "note": "Derived from existing captures; source originals and unselected component versions remain archived."}
    return RecoveryDocument(Path(db_path).resolve(), _json(result))
