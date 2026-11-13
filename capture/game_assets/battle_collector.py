"""Bounded, resumable personal/union/favourite battle discovery and collection.

Pagination follows the client's reqLen/SEG_LEN contract, including empty pages
that still consume a segment. Lists restart at page one because offsets move;
saved reports and pending database imports resume independently of list offsets.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from .export_battle_report_runtime import (
        LUA_JSON, DEFAULT_OUT_DIR, atomic_json, atomic_bytes, capture_report, decode_envelope,
        lua_string, run_checked, validate_battle_id, identity_guard_lua, safe_output_path, prefetch_reports,
        RuntimeBridgeUnavailable,
    )
    from .peerless_capture import page_evaluation_lua, evaluation_metadata, PeerlessPageResponse
except ImportError:
    from export_battle_report_runtime import (
        LUA_JSON, DEFAULT_OUT_DIR, atomic_json, atomic_bytes, capture_report, decode_envelope,
        lua_string, run_checked, validate_battle_id, identity_guard_lua, safe_output_path, prefetch_reports,
        RuntimeBridgeUnavailable,
    )
    from peerless_capture import page_evaluation_lua, evaluation_metadata, PeerlessPageResponse

SCOPES = ("personal", "union", "star")
ROUTES = {
    "personal": ("RPCGetBattleBlockList", "RPCGetChildCombatInfoList", "private"),
    "union": ("RPCGetUnionBattleBlockList", "RPCGetUnionChildCombatInfoList", "union"),
    "star": ("RPCStarBattleBlockList", "RPCStarBattleChildList", "star"),
}


class CollectorLeaseRunner:
    """Refresh ownership on every game-thread call, and reject a lost lease."""
    def __init__(self, runner: Any, owner: str):
        self.runner, self.owner = runner, owner

    def prefix(self) -> str:
        return f'''
local __lease = __codexBattleCollector
if not __lease or __lease.owner ~= {lua_string(self.owner)} then
  error('collector ownership changed; stop this collection cycle')
end
__lease.lastSeen = os.time()
'''

    def execute_on_update(self, code: str, timeout: float = 10.0) -> Any:
        return self.runner.execute_on_update(self.prefix() + code, timeout=timeout)

    def execute(self, code: str) -> Any:
        return self.runner.execute(self.prefix() + code)


def install_lua(owner: str, lease_seconds: int = 300) -> str:
    return LUA_JSON + f'''
if type(ClientProxy) ~= 'table' then return envelope({{error='ClientProxy is not a table'}}) end
if not (CommonConst and CommonConst.BattleBaseRelationType and CommonConst.BattleBaseRelationType.ALL and CommonConst.SEG_LEN) then
  return envelope({{error='Required current pagination constants are unavailable'}})
end
if type(ClientProxy.GetServerId) ~= 'function' or not ClientProxy.avatarId or not G_SCENARIO_ID then
  return envelope({{error='game not logged in or unavailable: account identity is unavailable'}})
end
local identity = {{avatarId=tostring(ClientProxy.avatarId),serverId=tostring(ClientProxy:GetServerId()),scenarioId=tostring(G_SCENARIO_ID)}}
if identity.avatarId == '0' or identity.serverId == 'nil' or identity.serverId == '0' then
  return envelope({{error='game not logged in or unavailable: account identity is invalid'}})
end
local now = os.time()
local previous = __codexBattleCollector
if previous then
  -- Old processes can die without restoring Lua callbacks. A fresh owner may
  -- reclaim only an expired lease; active owners continue refreshing lastSeen.
  if previous.owner ~= {lua_string(owner)} then
    if not previous.lastSeen then previous.lastSeen = now end
    local age = now - previous.lastSeen
    local duration = previous.leaseSeconds or {lease_seconds}
    if age <= duration and age >= -duration then
      return envelope({{error='Another collector is installed; active lease has not expired'}})
    end
  end
  -- Reinstalling the same owner also clears a failed cleanup, even if login
  -- replaced ClientProxy. Always restore the object that actually held hooks.
  for name,entry in pairs(previous.hooks or {{}}) do
    if previous.proxy and previous.proxy[name] == entry.wrapper then
      previous.proxy[name] = entry.original
    end
  end
  __codexBattleCollector = nil
end
local st = __codexBattleCollector or {{owner={lua_string(owner)},hooks={{}},responses={{}},proxy=ClientProxy,identity=identity}}
st.lastSeen, st.leaseSeconds = now, {lease_seconds}
__codexBattleCollector = st
local methods = {{
  'RPCGetBattleBlockList', 'RPCGetChildCombatInfoList',
  'RPCGetUnionBattleBlockList', 'RPCGetUnionChildCombatInfoList',
  'RPCStarBattleBlockList', 'RPCStarBattleChildList'
}}
local capabilities = {{}}
for _,method in ipairs(methods) do
  local response = method .. 'Response'
  capabilities[method] = type(ClientProxy[method]) == 'function' and type(ClientProxy[response]) == 'function'
  if capabilities[method] and not st.hooks[response] then
    local responseName = response
    local original = ClientProxy[responseName]
    local own = rawget(ClientProxy,responseName)
    local wrapper
    wrapper = function(self, msgId, proto, ...)
      local function pack(...) return {{n=select('#',...),...}} end
      local pending = st.pending
      local captured
      local rejected = type(proto) == 'table' and proto.code ~= nil
        and ErrCode and ErrCode.SUCCESS ~= nil and proto.code ~= ErrCode.SUCCESS
      if pending and pending.response == responseName and type(proto) == 'table'
        and (rejected or proto.sIndex == pending.index) then
        local parentOK = true
        if not rejected and pending.parent ~= nil then
          local actual = proto.parentHashKey or proto.battleID
          parentOK = tostring(actual) == tostring(pending.parent)
        end
        local typeOK = rejected or pending.relation == nil or proto.tp == nil or proto.tp == pending.relation
        if parentOK and typeOK then
          -- Retain the received table before client processing: the original
          -- handler may fail on an unexpected/empty server response. Keep the
          -- reference so successful client-derived fields remain available.
          captured = {{response=proto,method=pending.method,scope=pending.scope,index=pending.index,parent=pending.parent,
            clientProcessingComplete=false}}
          st.responses[pending.token] = captured
        end
      end
      local results = pack(original(self,msgId,proto,...))
      if captured then captured.clientProcessingComplete = true end
      return unpack(results,1,results.n)
    end
    st.hooks[responseName] = {{original=own,wrapper=wrapper}}
    ClientProxy[responseName] = wrapper
  end
end
local ready = false
for _,enabled in pairs(capabilities) do if enabled then ready = true end end
if not ready then return envelope({{error='game not logged in or unavailable: no current battle list interfaces'}}) end
return envelope({{capabilities=capabilities,segmentLength=CommonConst.SEG_LEN,
  relationAll=CommonConst.BattleBaseRelationType.ALL,runtimeIdentity=identity,successCode=ErrCode and ErrCode.SUCCESS,
  relationPvp=CommonConst.BattleBaseRelationType.PVP,
  battleMatchTypes=CommonConst.BATTLEOBJECTMATCHTYPE,
  unionId=type(ClientProxy.GetUnionId)=='function' and ClientProxy:GetUnionId() or nil}})
'''


def cleanup_lua(owner: str) -> str:
    return f'''
local st = __codexBattleCollector
if st and st.owner == {lua_string(owner)} then
  for name,entry in pairs(st.hooks) do
    if st.proxy[name] == entry.wrapper then st.proxy[name] = entry.original end
  end
  __codexBattleCollector = nil
end
return 'collector detached'
'''


def request_page_lua(owner: str, token: str, scope: str, index: int,
                     relation: int, parent: str | None = None) -> str:
    method = ROUTES[scope][1 if parent is not None else 0]
    if parent is None:
        args = str(index) if scope == "star" else f"{index},{relation}"
    elif scope == "star":
        args = f"{index},{lua_string(parent)}"
    else:
        args = f"{validate_battle_id(parent)},{index},{relation}"
    return f'''
local st = __codexBattleCollector
if not st or st.owner ~= {lua_string(owner)} then return 'collector not installed' end
if ClientProxy ~= st.proxy or tostring(ClientProxy.avatarId) ~= st.identity.avatarId
 or type(ClientProxy.GetServerId) ~= 'function' or tostring(ClientProxy:GetServerId()) ~= st.identity.serverId
 or tostring(G_SCENARIO_ID) ~= st.identity.scenarioId then return 'account/server/scenario changed' end
st.pending = {{token={lua_string(token)},method={lua_string(method)},response={lua_string(method+'Response')},
  scope={lua_string(scope)},index={index},relation={relation},parent={lua_string(parent) if parent is not None else 'nil'}}}
ClientProxy:{method}({args})
return 'requested page'
'''


def poll_page_lua(owner: str, token: str) -> str:
    return LUA_JSON + f'''
local st = __codexBattleCollector
if not st or st.owner ~= {lua_string(owner)} then return envelope({{error='collector not installed'}}) end
if ClientProxy ~= st.proxy or tostring(ClientProxy.avatarId) ~= st.identity.avatarId
 or type(ClientProxy.GetServerId) ~= 'function' or tostring(ClientProxy:GetServerId()) ~= st.identity.serverId
 or tostring(G_SCENARIO_ID) ~= st.identity.scenarioId then return envelope({{error='account/server/scenario changed'}}) end
local value = st.responses[{lua_string(token)}]
if not value then return envelope({{pending=true}}) end
st.responses[{lua_string(token)}] = nil
st.pending = nil
''' + page_evaluation_lua() + 'return envelope(value)\n'


def validate_page(value: dict[str, Any], index: int, segment: int,
                  children: bool = False, success_code: int | None = None) -> tuple[list[dict[str, Any]], int | None]:
    """Reject protocol drift instead of declaring an unknown response finished."""
    response = value.get("response")
    if not isinstance(response, dict):
        raise ValueError("Page response is not a record")
    if "code" in response and (success_code is None or response["code"] != success_code):
        raise ValueError(f"Server rejected list request with code={response['code']}")
    if value.get("clientProcessingComplete") is False:
        raise RuntimeError("Client response processing failed")
    if response.get("sIndex") != index:
        raise ValueError("Page response has no matching sIndex")
    req_len = response.get("reqLen")
    if isinstance(req_len, bool) or not isinstance(req_len, int) or not 0 <= req_len <= segment:
        raise ValueError("Page response reqLen is missing or invalid")
    key = "combatInfoList" if children else "blockInfoList"
    rows = response.get(key)
    # Lua's empty table is encoded as [] by the serializer.
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"Page response {key} is not a list of records")
    if req_len == 0 and rows:
        raise ValueError("Nonempty response with reqLen=0 cannot be paginated safely")
    next_index = None if req_len < segment else index + (segment if children else req_len)
    return rows, next_index


class BattleCollector:
    def __init__(self, runner: Any, out_dir: Path, state_file: Path | None = None,
                 scopes: tuple[str, ...] = SCOPES, wait_seconds: float = 20.0,
                 poll_seconds: float = 0.5, request_delay: float = 0.5,
                 max_pages: int = 10000, on_export: Callable[[Path], Any] | None = None,
                 namespace: str = "default", on_progress: Callable[[dict[str, Any]], Any] | None = None,
                 parallelism: int = 1, owner_id: str | None = None, pvp_only: bool = False):
        if any(scope not in SCOPES for scope in scopes) or not scopes:
            raise ValueError(f"Scopes must be chosen from {SCOPES}")
        if (not all(math.isfinite(v) for v in (wait_seconds, poll_seconds, request_delay))
                or wait_seconds < 0 or poll_seconds <= 0 or request_delay < 0
                or isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages <= 0):
            raise ValueError("Invalid collector wait/poll/delay/page limit")
        if isinstance(parallelism, bool) or not isinstance(parallelism, int) or not 1 <= parallelism <= 4:
            raise ValueError("parallelism must be an integer from 1 through 4")
        if owner_id is not None and (not isinstance(owner_id, str) or not owner_id.strip()):
            raise ValueError("owner_id must be a nonempty string")
        if not isinstance(pvp_only, bool):
            raise ValueError("pvp_only must be a boolean")
        self.owner = owner_id if owner_id is not None else uuid.uuid4().hex
        self.raw_runner = runner
        self.runner, self.out_dir = CollectorLeaseRunner(runner, self.owner), safe_output_path(out_dir)
        self.state_file = safe_output_path(state_file) if state_file else self.out_dir / "collector_state.json"
        self.scopes = tuple(dict.fromkeys(scopes))
        self.wait_seconds, self.poll_seconds = wait_seconds, poll_seconds
        self.request_delay, self.max_pages = request_delay, max_pages
        self.on_export, self.namespace = on_export, namespace
        self.on_progress = on_progress
        self.parallelism = parallelism
        self.pvp_only = pvp_only
        self.lease_seconds = max(300, math.ceil(wait_seconds * 4 + 90), math.ceil(request_delay + 90))
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if self.state_file.exists():
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
            if self.state.get("version") != 1 or self.state.get("namespace") != namespace:
                raise ValueError("Collector state version/namespace mismatch; select a separate state file")
        else:
            self.state = {"version": 1, "namespace": namespace, "reports": {}, "scopes": {}, "cycles": 0}
        self._stop_event: Any = None
        # Set only while run_cycle owns the runtime hooks. Direct discovery and
        # collect_pending callers keep their previous independent behavior.
        self._collect_pages = False
        self._cycle_attempted: set[str] | None = None
        self._cycle_collection: dict[str, int] | None = None
        self._cycle_deferred_retries: set[str] | None = None

    def save(self) -> None:
        self.state["updatedAt"] = time.time()
        atomic_json(self.state_file, self.state, compact=True)

    def stopped(self) -> bool:
        return bool(self._stop_event is not None and self._stop_event.is_set())

    def battle_class(self, entry: dict[str, Any]) -> str:
        """Apply the current client's PVP matchType rule, never a numeric guess.

        NPC/player names and IDs do not define the game mode. In particular,
        arena and NPC-containing continuous summaries cannot safely inherit a
        parent verdict; every child must carry its own recognized matchType.
        """
        enums = self.state.get("runtimeCapabilities", {}).get("battleMatchTypes")
        if not isinstance(enums, dict):
            return "unknown"
        pvp = enums.get("PVP")
        if isinstance(pvp, bool) or not isinstance(pvp, int):
            return "unknown"
        known = {value for value in enums.values() if isinstance(value, int) and not isinstance(value, bool)}
        verdicts = set()
        for observed in entry.get("observations", {}).values():
            if not isinstance(observed, dict):
                continue
            row = observed.get("row")
            if not isinstance(row, dict):
                continue
            combat = row if observed.get("parent") is not None else row.get("combatInfo")
            kind = combat.get("matchType") if isinstance(combat, dict) else None
            if isinstance(kind, bool) or not isinstance(kind, int) or kind not in known:
                continue
            verdicts.add("pvp" if kind == pvp else "pve")
        return next(iter(verdicts)) if len(verdicts) == 1 else "unknown"

    def _network_selected(self, entry: dict[str, Any]) -> bool:
        return not self.pvp_only or self.battle_class(entry) == "pvp"

    def filter_summary(self, entries) -> dict[str, Any]:
        classes = [self.battle_class(entry) for entry in entries]
        pvp, pve, unknown = (classes.count(kind) for kind in ("pvp", "pve", "unknown"))
        return {"mode": "pvp_only" if self.pvp_only else "all", "pvpReports": pvp,
                "pveReports": pve, "unknownReports": unknown,
                "excludedReports": pve + unknown if self.pvp_only else 0,
                "classification": "runtime_CommonConst.BATTLEOBJECTMATCHTYPE",
                "unknownPolicy": "skip_network_requests" if self.pvp_only else "collect"}

    def _notify_progress(self, value: dict[str, Any]) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress(value)
            except Exception:
                # A terminal renderer is optional; an output failure must not
                # invalidate a received page, archive commit or runtime lease.
                pass

    def _notify_capture_failed(self, entry: dict[str, Any], error: BaseException) -> None:
        self._notify_progress({"phase": "capture_failed", "battleID": entry.get("battleID"),
                               "error": str(error), "error_type": type(error).__name__,
                               "runtime_unavailable": isinstance(error, RuntimeBridgeUnavailable)})

    def retry_discovery_import(self, entry: dict[str, Any], *, defer_success_checkpoint: bool = False) -> dict[str, Any]:
        """Retry a durable list-derived capture without making a game request."""
        pending = entry.get("pendingDiscoveryImport")
        if not pending or not self.on_export:
            return {"status": "not_pending"}
        path = Path(pending["path"])
        try:
            result = self.on_export(path)
            if isinstance(result, dict) and (result.get("error") or result.get("status") in {"error", "failed", "invalid", "rejected"}):
                raise RuntimeError(f"Database rejected discovery capture: {result}")
        except RuntimeBridgeUnavailable:
            raise
        except Exception as exc:
            entry["discoveryImportError"] = str(exc)
            self.save()
            return {"status": "failed", "path": str(path), "error": str(exc)}
        entry["discoveryImportedPath"] = str(path)
        entry.pop("pendingDiscoveryImport", None)
        entry.pop("discoveryImportError", None)
        entry.pop("discoveryCheckpointError", None)
        # SQLite already committed. A later state-checkpoint failure must not
        # be persisted as an import failure or permanently poison coverage.
        if not defer_success_checkpoint:
            try:
                self.save()
            except Exception as exc:
                entry["discoveryCheckpointError"] = str(exc)
                return {"status": "imported", "path": str(path), "checkpoint_error": str(exc)}
        return {"status": "imported", "path": str(path)}

    def archive_discovery(self, entry: dict[str, Any], *, defer_success_checkpoint: bool = False) -> dict[str, Any]:
        """Persist known list data immediately, independently of detail collection.

        This is a list-derived partial capture, not a raw network packet. The
        method also supports replaying saved observations without changing
        lastSeenCycle or claiming that any detail component was recovered.
        """
        observations = list(entry.get("observations", {}).values())
        if not observations:
            raise ValueError("Cannot archive discovery without a saved observation")
        latest = observations[-1]
        row = latest["row"]
        combat = row if latest.get("parent") is not None else row.get("combatInfo")
        if not isinstance(combat, dict):
            raise ValueError("Saved discovery observation lacks combatInfo")
        battle_id = validate_battle_id(entry["battleID"])
        if validate_battle_id(combat.get("battleID")) != battle_id:
            raise ValueError("Saved discovery observation has a conflicting battle ID")
        names = ("basic", "detail", "all", "statics", "snapshots")
        payload = {
            "captureVersion": 2, "captureKind": "list_discovery", "battleID": battle_id,
            "source": "basic", "hasBasic": True, "hasDetail": False, "hasAll": False,
            "eventCount": -1, "data": combat, "components": {"basic": combat},
            "componentStatus": {name: {"available": name == "basic", "complete": name == "basic", "requested": False}
                                for name in names},
            "captureComplete": False,
            "captureWarnings": ["list_discovery_only_detail_components_not_collected"],
            "serializationIssues": [], "discovery": observations, "namespace": self.namespace,
        }
        if self.state.get("runtimeIdentity") is not None:
            payload["runtimeIdentity"] = self.state["runtimeIdentity"]
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        path = self.out_dir / "captures" / str(battle_id) / (hashlib.sha256(raw).hexdigest() + ".json")
        if not path.exists():
            atomic_bytes(path, raw)
        entry["discoveryArchivePath"] = str(path)
        if self.on_export and entry.get("discoveryImportedPath") != str(path):
            entry["pendingDiscoveryImport"] = {"path": str(path)}
        elif self.on_export and not entry.get("pendingDiscoveryImport"):
            # This exact immutable version is already acknowledged by SQLite.
            # Repair stale errors written by older post-commit checkpoints.
            entry.pop("discoveryImportError", None)
            entry.pop("discoveryCheckpointError", None)
        self.save()
        if entry.get("pendingDiscoveryImport"):
            return self.retry_discovery_import(entry, defer_success_checkpoint=defer_success_checkpoint)
        return {"status": "saved", "path": str(path)}

    def page(self, scope: str, index: int, relation: int, segment: int,
             parent: str | None = None) -> tuple[list[dict[str, Any]], int | None, dict[str, Any], Path]:
        token = uuid.uuid4().hex
        status = run_checked(self.runner, request_page_lua(self.owner, token, scope, index, relation, parent))
        if status != "requested page":
            raise RuntimeError(status)
        deadline = time.monotonic() + self.wait_seconds
        while True:
            envelope = decode_envelope(run_checked(self.runner, poll_page_lua(self.owner, token)))
            value = envelope["payload"]
            if value.get("error"):
                raise RuntimeError(value["error"])
            if not value.get("pending"):
                break
            if self.stopped():
                raise InterruptedError("Collection was stopped")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{scope} parent={parent} index={index}: response timeout")
            time.sleep(min(self.poll_seconds, max(0.01, deadline - time.monotonic())))
        # Unchanged pages across polling cycles contain no new data. Address
        # them by content so continual collection does not grow duplicate files.
        digest = hashlib.sha256(json.dumps(envelope, sort_keys=True, ensure_ascii=False,
                                           separators=(",", ":")).encode()).hexdigest()
        page_path = self.out_dir / "list_responses" / f"{digest}.json"
        if not page_path.exists():
            atomic_json(page_path, envelope)
        if envelope.get("serializationIssues"):
            raise ValueError(f"List response contains serialization issues; retained at {page_path}")
        try:
            rows, next_index = validate_page(value, index, segment, parent is not None,
                                             self.state.get("runtimeCapabilities", {}).get("successCode"))
        except (ValueError, RuntimeError) as exc:
            raise type(exc)(f"{exc}; received response retained at {page_path}") from exc
        if self.request_delay:
            time.sleep(self.request_delay)
        response = PeerlessPageResponse(value["response"], evaluation_metadata(value))
        return rows, next_index, response, page_path

    def remember(self, scope: str, row: dict[str, Any], response: dict[str, Any],
                 page_path: Path, parent: str | None = None, *, visible: bool = True) -> tuple[str, dict[str, Any]]:
        combat = row if parent is not None else row.get("combatInfo")
        if not isinstance(combat, dict):
            raise ValueError("List item lacks combatInfo")
        battle_id = str(validate_battle_id(combat.get("battleID")))
        entry = self.state["reports"].setdefault(battle_id, {
            "battleID": battle_id, "attempts": 0, "complete": False, "observations": {},
            "firstSeenAt": time.time(),
        })
        metadata = {key: value for key, value in response.items()
                    if key not in ("blockInfoList", "combatInfoList")}
        observed = {"scope": scope, "parent": parent, "row": row, "responseMetadata": metadata}
        evaluations = getattr(response, "client_peerless", None)
        if parent is None and isinstance(evaluations, list):
            other = row.get("otherInfo")
            continuous = other.get("continuousInfo") if isinstance(other, dict) else None
            matched = [item for item in evaluations if isinstance(item, dict)
                       and not isinstance(item.get("battleID"), bool)
                       and str(item.get("battleID")) == battle_id
                       and isinstance(continuous, dict)
                       and str(item.get("avatarId")) == str(continuous.get("avatarId"))
                       and item.get("isMuti") == continuous.get("isMuti")
                       and item.get("zgNum") == continuous.get("zgNum")
                       and item.get("winBattleNum") == continuous.get("winBattleNum")
                       and item.get("endTimeStamp") == combat.get("endTimeStampTime")]
            if matched:
                observed["clientPeerless"] = matched
        if not visible:
            observed["historicalRecovery"] = True
        digest = hashlib.sha256(json.dumps(observed, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        # Preserve changed list metadata, while identical rescans remain idempotent.
        changed = digest not in entry["observations"]
        if changed:
            entry["observations"][digest] = {**observed, "sourceFile": str(page_path)}
            if (entry.get("complete") or entry.get("lastResult", {}).get("complete")
                    or entry.get("pendingImport", {}).get("result", {}).get("complete")):
                entry["metadataDirty"] = True
                entry["complete"] = False
            # Changed list metadata must not erase a failed component's retry
            # backoff. An already complete capture can update its metadata from
            # disk without issuing another component request.
            if entry.get("metadataDirty") and entry.get("lastResult", {}).get("complete"):
                entry.pop("nextRetryAt", None)
        entry["lastSeenAt"] = time.time()
        if visible:
            entry["lastSeenCycle"] = self.state["cycles"]
        else:
            entry["lastRecoveredAt"] = time.time()
            entry["lastRecoveryCycle"] = self.state["cycles"]
        # The immutable capture is fsynced first, then archive_discovery saves a
        # durable pending checkpoint before database import. Do not serialize
        # this entire growing state again immediately before that checkpoint.
        try:
            if (changed or not entry.get("discoveryArchivePath")
                    or (entry.get("discoveryImportError") and not entry.get("pendingDiscoveryImport"))):
                self.archive_discovery(entry, defer_success_checkpoint=self._collect_pages)
            elif entry.get("pendingDiscoveryImport"):
                self.retry_discovery_import(entry, defer_success_checkpoint=self._collect_pages)
            elif not self._collect_pages:
                self.save()
        except Exception as exc:
            entry["discoveryImportError"] = str(exc)
            self.save()
            raise
        return battle_id, combat

    def _known_child_groups(self, scope: str, previous: dict[str, Any]) -> dict[str, Any]:
        """Recover old child work without calling it visible in the new list."""
        # Filtered child pagination cannot acknowledge the ALL-mode backlog.
        # Keep independent cursors so an empty PVP page never marks previously
        # missing PVE children recovered when the user later switches back.
        mode = "pvp_only" if self.pvp_only else "all"
        recovery_key = f"{scope}:pvp_only" if self.pvp_only else scope
        groups = self.state.setdefault("childRecovery", {}).setdefault(recovery_key, {})
        compatible_previous = previous.get("selectionMode", "all") == mode
        known_children: dict[str, set[str]] = {}
        observed_roots: dict[str, dict[str, Any]] = {}
        for battle_id, entry in self.state["reports"].items():
            for observation in entry.get("observations", {}).values():
                if observation.get("scope") != scope:
                    continue
                if observation.get("parent") is not None:
                    known_children.setdefault(str(observation["parent"]), set()).add(str(battle_id))
                    continue
                row = observation.get("row", {})
                combat = row.get("combatInfo", {})
                child_num = row.get("childNum", combat.get("childNum", 0))
                if (isinstance(child_num, bool) or not isinstance(child_num, (int, float))
                        or not math.isfinite(child_num) or child_num <= 0):
                    continue
                parent = combat.get("combatHashKey") if scope == "star" else battle_id
                if parent is not None:
                    observed_roots[str(parent)] = {"battleID": str(battle_id), "childNum": child_num,
                                                   "lastObservedAt": entry.get("lastSeenAt", 0)}
        # Old versions kept this information only inside the latest scope.
        # Copy it before a failed head-page response replaces that scope.
        for parent, root in previous.get("roots", {}).items():
            observed_roots.setdefault(str(parent), dict(root))
        for parent, root in observed_roots.items():
            group = groups.get(parent)
            if group is None:
                old_child = previous.get("children", {}).get(parent) if compatible_previous else None
                already_known = compatible_previous and len(known_children.get(parent, ())) >= root.get("childNum", 0)
                groups[parent] = {**root, "complete": bool(old_child.get("complete")) if old_child else already_known,
                                  "attempts": 0, "source": "saved_observations"}
                if old_child:
                    groups[parent].update({key: value for key, value in old_child.items()
                                           if key in {"pages", "index", "error"}})
            else:
                if root.get("childNum", 0) > group.get("childNum", 0):
                    group["complete"] = False
                    group.pop("nextRetryAt", None)
                group.update(root)
        return groups

    def discover_scope(self, scope: str, capabilities: dict[str, Any]) -> dict[str, Any]:
        required = ROUTES[scope][:2]
        if any(not capabilities["capabilities"].get(name) for name in required):
            raise RuntimeError(f"Current runtime lacks {scope} list/child response methods")
        segment, relation = capabilities["segmentLength"], capabilities["relationAll"]
        relation_pvp = capabilities.get("relationPvp")
        if (self.pvp_only and scope != "star" and isinstance(relation_pvp, int)
                and not isinstance(relation_pvp, bool)):
            relation = relation_pvp
        if not isinstance(segment, int) or segment <= 0 or not isinstance(relation, int):
            raise ValueError("Current pagination constants are invalid")
        # No union is an explicit non-applicable scope, never a silent RPC timeout.
        if scope == "union" and capabilities.get("unionId") == 0:
            result = {"complete": True, "applicable": False, "reason": "current_account_has_no_union", "pages": 0}
            self.state["scopes"][scope] = result
            self.save()
            return result
        # Offsets are not durable cursors: insertion, deletion and expiry can
        # move every page. Rescan metadata from the head, reusing stored detail.
        previous = self.state["scopes"].get(scope, {})
        child_groups = self._known_child_groups(scope, previous)
        index: int | None = 1
        roots = {}
        progress = {"complete": False, "applicable": True, "pages": 0, "index": index,
                    "children": {}, "historicalChildren": {}, "roots": roots, "rootComplete": False,
                    "errors": [], "startedAt": time.time(), "relation": relation,
                    "selectionMode": "pvp_only" if self.pvp_only else "all"}
        self.state["scopes"][scope] = progress

        def remember_roots(rows: list[dict[str, Any]], response: dict[str, Any], path: Path) -> set[str]:
            page_ids = set()
            for row in rows:
                battle_id, combat = self.remember(scope, row, response, path)
                page_ids.add(battle_id)
                child_num = row.get("childNum", combat.get("childNum", 0))
                if not isinstance(child_num, (int, float)) or child_num < 0:
                    raise ValueError("Invalid childNum; cannot prove list coverage")
                if child_num > 0:
                    parent = combat.get("combatHashKey") if scope == "star" else battle_id
                    if parent is None:
                        raise ValueError("Favourite block has children but no combatHashKey")
                    roots[str(parent)] = {"battleID": battle_id, "childNum": child_num}
            return page_ids

        while index is not None and not self.stopped():
            progress["index"] = index
            try:
                if progress["pages"] >= self.max_pages:
                    raise RuntimeError(f"{scope}: page limit reached before end of list")
                received_index = index
                rows, next_index, response, path = self.page(scope, index, relation, segment)
                page_ids = remember_roots(rows, response, path)
                progress["pages"] += 1
                index = next_index
                progress["index"] = index
                self.save()
                self._notify_progress({"phase": "page_received", "scope": scope,
                                       "index": received_index, "count": len(rows)})
                # The decoded response and the entire page are durable, and
                # poll_page has cleared the pending list request. Components
                # may now be fetched without interleaving a half-saved page.
                if self._collect_pages:
                    self._collect_batch(page_ids, retry_budget_seconds=max(1.0, self.wait_seconds))
            except RuntimeBridgeUnavailable:
                raise
            except Exception as exc:
                progress["errors"].append({"index": index, "error": str(exc), "phase": "root"})
                # Still enumerate every already known child group below.
                break
        progress["rootComplete"] = index is None
        def collect_child_group(parent: str, root: dict[str, Any], *, historical: bool = False) -> None:
            child_index: int | None = 1
            child_state = {"complete": False, "index": child_index, "pages": 0, "battleID": root["battleID"]}
            target = progress["historicalChildren"] if historical else progress["children"]
            target[parent] = child_state
            if historical:
                child_state["historicalRecovery"] = True
            group = child_groups.setdefault(parent, {})
            group.update({**root, "complete": False, "lastAttemptAt": time.time(),
                          "attempts": group.get("attempts", 0) + 1})
            group_checkpointed = False
            def update_group() -> None:
                group.update(child_state)
                if group["complete"]:
                    group.pop("error", None)
                    group.pop("nextRetryAt", None)
                else:
                    group["nextRetryAt"] = time.time() + min(3600, 30 * 2 ** min(group["attempts"] - 1, 7))
            try:
                while child_index is not None and not self.stopped():
                    if child_state["pages"] >= self.max_pages:
                        raise RuntimeError("Child page limit reached before end of list")
                    child_state["index"] = child_index
                    received_index = child_index
                    rows, following, response, path = self.page(scope, child_index, relation, segment, parent)
                    page_ids = set()
                    for row in rows:
                        battle_id, _ = self.remember(scope, row, response, path, parent, visible=not historical)
                        page_ids.add(battle_id)
                    child_state["pages"] += 1
                    child_index = following
                    child_state["index"] = child_index
                    if child_index is None:
                        # The final page's originals and pending imports are
                        # already preserved. Publish its cursor, import receipts
                        # and completed child group in one account checkpoint.
                        child_state["complete"] = True
                        update_group()
                    self.save()
                    group_checkpointed = child_index is None
                    self._notify_progress({"phase": "page_received", "scope": scope,
                                           "parent": parent, "index": received_index, "count": len(rows),
                                           "historicalRecovery": historical})
                    if self._collect_pages:
                        self._collect_batch(page_ids, retry_budget_seconds=max(1.0, self.wait_seconds))
                child_state["complete"] = child_index is None
            except RuntimeBridgeUnavailable:
                raise
            except Exception as exc:
                child_state["complete"] = False
                child_state["error"] = str(exc)
                group_checkpointed = False
                progress["errors"].append({"parent": parent, "error": str(exc),
                                           "historicalRecovery": historical})
            if not group_checkpointed:
                update_group()
                self.save()

        # A failing child group does not prevent coverage of other groups/scopes.
        for parent, root in roots.items():
            if self.stopped():
                break
            collect_child_group(parent, root)
        if index is not None and not self.stopped():
            # The head list can temporarily fail while old child endpoints are
            # still readable. Retry a bounded, fair backlog using saved sources.
            # These reports must not inflate this cycle's visible-list coverage.
            pending_groups = [(parent, group) for parent, group in child_groups.items()
                              if parent not in roots and not group.get("complete")
                              and group.get("nextRetryAt", 0) <= time.time()]
            pending_groups.sort(key=lambda item: (item[1].get("attempts", 0), -item[1].get("lastObservedAt", 0)))
            progress["historicalPendingGroups"] = len(pending_groups)
            for parent, root in pending_groups[:segment]:
                if self.stopped():
                    break
                collect_child_group(parent, dict(root), historical=True)
        progress["complete"] = (index is None and not progress["errors"]
                                and len(progress["children"]) == len(roots)
                                and all(item["complete"] for item in progress["children"].values()) and not self.stopped())
        progress["finishedAt"] = time.time()
        self.save()
        return progress

    @staticmethod
    def _empty_collection_counts() -> dict[str, int]:
        return {"exported": 0, "complete": 0, "partial": 0, "failed": 0, "skipped": 0}

    def _ordered_pending_entries(self, selected: set[str] | None = None) -> list[dict[str, Any]]:
        # Local recovery and complete metadata refreshes come before network
        # work. New reports get their first attempt promptly; old retries rotate
        # by attempt count and last attempt time before favouring fewer missing
        # components. Otherwise a bounded batch can starve a report missing more
        # components indefinitely behind an actively refreshed partial backlog.
        entries = self.state["reports"].values()
        if selected is not None:
            entries = (item for item in entries if str(item.get("battleID", "")) in selected)
        return sorted(entries, key=lambda item: (
            not bool(item.get("pendingImport") or item.get("pendingDiscoveryImport")),
            not bool(item.get("metadataDirty") and item.get("lastResult", {}).get("complete")),
            item.get("attempts", 0) > 0,
            item.get("attempts", 0), item.get("lastAttemptAt", 0),
            self._missing_component_count(item),
            item.get("lastSeenCycle") != self.state["cycles"], -item.get("lastSeenAt", 0)))

    @staticmethod
    def _missing_component_count(entry: dict[str, Any]) -> int:
        result = entry.get("pendingImport", {}).get("result") or entry.get("lastResult", {})
        if result.get("complete"):
            return 0  # Metadata refresh can reuse a fully captured file.
        statuses = result.get("componentStatus", {})
        return sum(not statuses.get(name, {}).get("complete") or bool(statuses.get(name, {}).get("error"))
                   for name in ("basic", "detail", "all", "statics", "snapshots"))

    def _initial_pending_ids(self, limit: int) -> list[str]:
        """Give existing incomplete work a bounded head start before new lists."""
        now, selected = time.time(), []
        for entry in self._ordered_pending_entries():
            if "battleID" not in entry:
                continue
            if (entry.get("complete") and not entry.get("metadataDirty")
                    and not entry.get("pendingImport") and not entry.get("pendingDiscoveryImport")):
                continue
            durable_discovery = entry.get("pendingImport") or entry.get("pendingDiscoveryImport") or (
                entry.get("observations") and not entry.get("discoveryArchivePath"))
            if not durable_discovery and entry.get("nextRetryAt", 0) > now:
                continue
            selected.append(str(entry["battleID"]))
            if len(selected) == limit:
                break
        return selected

    def _collect_batch(self, battle_ids=None, *, retry_budget_seconds: float | None = None) -> dict[str, int]:
        if battle_ids is not None:
            battle_ids = tuple(battle_ids)
            if not battle_ids:
                return self._empty_collection_counts()
        counts = self.collect_pending(battle_ids=battle_ids, retry_budget_seconds=retry_budget_seconds)
        if self._cycle_collection is not None:
            for name in self._cycle_collection:
                self._cycle_collection[name] += counts.get(name, 0)
        return counts

    def _validate_capture_payload(self, entry: dict[str, Any], payload: dict[str, Any]) -> None:
        if str(payload.get("battleID")) != str(entry["battleID"]):
            raise ValueError("Capture belongs to another battle")
        if "namespace" in payload and payload["namespace"] != self.namespace:
            raise ValueError("Capture belongs to another account namespace")
        expected = self.state.get("runtimeIdentity")
        if expected and "runtimeIdentity" in payload and payload["runtimeIdentity"] != expected:
            raise ValueError("Capture belongs to another runtime account")

    def _capture_checkpoint(self, entry: dict[str, Any], path: Path) -> Path:
        """Archive a received checkpoint's exact bytes, without metadata copies.

        Final captures carry discovery links. Rewriting every intermediate
        response with those changing links creates another original per list
        refresh, and leaves the actual received bytes for startup recovery.
        """
        raw = path.read_bytes()
        self._validate_capture_payload(entry, json.loads(raw))
        battle_id = str(validate_battle_id(entry["battleID"]))
        original = self.out_dir / "captures" / battle_id / f"{hashlib.sha256(raw).hexdigest()}.json"
        if original.resolve() != path.resolve():
            if original.exists():
                if original.read_bytes() != raw:
                    raise ValueError("Checkpoint SHA path contains different bytes")
            else:
                atomic_bytes(original, raw)
        return original

    def _capture_revision(self, entry: dict[str, Any], path: Path, *, update_alias=False) -> Path:
        payload = json.loads(path.read_text(encoding="utf-8"))
        self._validate_capture_payload(entry, payload)
        payload["discovery"] = list(entry.get("observations", {}).values())
        payload["namespace"] = self.namespace
        # Cache-before-retry files are already immutable. Enrich a new revision
        # without rewriting the exact bytes preserved before the client's retry.
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        revision = self.out_dir / "captures" / str(entry["battleID"]) / f"{hashlib.sha256(raw).hexdigest()}.json"
        if not revision.exists():
            atomic_bytes(revision, raw)
        if update_alias:
            atomic_bytes(path, raw)
        return revision

    def _import_capture_path(self, path: Path) -> None:
        import_result = self.on_export(path)
        if isinstance(import_result, dict) and (import_result.get("error") or
                import_result.get("status") in ("error", "failed", "invalid", "rejected")):
            raise RuntimeError(f"Database rejected capture: {import_result}")

    def _replay_pending_capture(self, entry: dict[str, Any]) -> dict[str, Any]:
        pending = entry["pendingImport"]
        checkpoints = pending.setdefault("checkpointPaths", [])
        while checkpoints:
            self._import_capture_path(Path(checkpoints[0]))
            self._notify_progress({"phase": "cache_checkpoint_imported", "battleID": entry.get("battleID"),
                                   "path": checkpoints[0], "checkpointOnly": not bool(pending.get("path"))})
            checkpoints.pop(0)
            # The full list was durably saved before the first import. Keep
            # acknowledgements in memory until this battle's normal success or
            # failure checkpoint; a crash safely replays committed bytes by SHA
            # instead of rewriting the entire account state after every page.
        if pending.get("path") is not None:
            self._import_capture_path(Path(pending["path"]))
        entry.pop("pendingImport", None)
        return pending["result"]

    def _store_captured_result(self, entry: dict[str, Any], result: dict[str, Any], counts: dict[str, int]) -> None:
        """Publish and import one battle, shared by normal and stop-time exports."""
        checkpoints = []
        for checkpoint in dict.fromkeys(result.get("cacheCheckpointPaths", [])):
            original = self._capture_checkpoint(entry, Path(checkpoint))
            if str(original) not in checkpoints:
                checkpoints.append(str(original))
        if result.get("exported"):
            revision = self._capture_revision(entry, Path(result["path"]), update_alias=True)
            checkpoints = [path for path in checkpoints if path != str(revision)]
            result["archivePath"] = str(revision)
            if self.on_export:
                entry["pendingImport"] = {"path": str(revision), "result": result,
                                          "checkpointPaths": checkpoints}
                self.save()
                self._replay_pending_capture(entry)
            entry.pop("metadataDirty", None)
            counts["exported"] += 1
            counts["complete" if result.get("complete") else "partial"] += 1
        else:
            # A new request may time out after the client naturally removed its
            # prior partial cache. Those durable pre-request pages are still
            # real received data and must reach SQLite now, not only at restart.
            if checkpoints and self.on_export:
                entry["pendingImport"] = {"path": None, "result": result, "checkpointPaths": checkpoints}
                self.save()
                self._replay_pending_capture(entry)
            counts["failed"] += 1
            self._notify_capture_failed(entry, RuntimeError(str(result.get("error") or "Battle export returned no data")))
        entry["lastResult"] = result
        entry["complete"] = bool(result.get("complete"))
        entry.pop("error", None)

    def _defer_old_retry(self, entry: dict[str, Any], battle_id: str, deadline: float | None,
                         metadata_only: bool, inflight: dict[str, Any]) -> bool:
        # A batch limit is a boundary between *starting* old network attempts.
        # It never cancels a request already sent, a brand-new report, a local
        # pending import, or a complete file's metadata-only refresh.
        if (deadline is None or metadata_only or battle_id in inflight
                or entry.get("attempts", 0) <= 0 or time.monotonic() < deadline):
            return False
        if self._cycle_deferred_retries is not None:
            self._cycle_deferred_retries.add(battle_id)
        self._notify_progress({"phase": "retry_deferred", "battleID": battle_id,
                               "reason": "old_retry_start_budget_exhausted", "complete": False})
        return True

    def _finish_attempt(self, entry: dict[str, Any]) -> None:
        if not entry.get("complete"):
            delay = min(3600, 30 * 2 ** min(max(0, entry.get("attempts", 1) - 1), 7))
            entry["nextRetryAt"] = time.time() + delay
        else:
            entry.pop("nextRetryAt", None)
        self.save()

    def _parallel_candidates(self, entries: list[dict[str, Any]], selected: set[str] | None,
                             current_id: str, *, old_retry_group: bool | None = None) -> list[str]:
        """Reserve only work that will need component requests in this batch."""
        group, now = [], time.time()
        for candidate in entries:
            cid = str(candidate.get("battleID", ""))
            if not cid or (selected is not None and cid not in selected):
                continue
            if cid != current_id:
                if old_retry_group is not None and (candidate.get("attempts", 0) > 0) != old_retry_group:
                    continue
                if candidate.get("complete") or candidate.get("metadataDirty") or candidate.get("pendingImport"):
                    continue
                if candidate.get("pendingDiscoveryImport") or candidate.get("discoveryImportError"):
                    continue
                if candidate.get("observations") and not candidate.get("discoveryArchivePath"):
                    continue
                if candidate.get("nextRetryAt", 0) > now:
                    continue
                if self._cycle_attempted is not None and cid in self._cycle_attempted:
                    continue
            if not self._network_selected(candidate):
                continue
            validate_battle_id(cid)
            if cid not in group:
                group.append(cid)
            if len(group) >= self.parallelism:
                break
        return group

    def _prefetch_failure_metadata(self, battle_id: str, error: str) -> dict[str, dict[str, Any]]:
        return {name: {"battleID": int(battle_id), "component": name, "requested": False,
                       "error": error, "stage": "prefetch_batch",
                       "min_request_interval": max(1.0, self.request_delay),
                       "min_dispatch_interval": max(1.0, self.request_delay)}
                for name in ("all", "statics", "snapshots")}

    def _harvest_inflight(self, inflight: dict[str, dict[str, Any]], counts: dict[str, int],
                          attempted: set[str]) -> list[dict[str, Any]]:
        """Export received caches on stop/error, without dispatching new RPCs."""
        errors = []
        for battle_id, pending in list(inflight.items()):
            entry = self.state["reports"].get(battle_id)
            if entry is None:
                continue
            if battle_id not in attempted:
                entry["attempts"] = entry.get("attempts", 0) + 1
                entry["lastAttemptAt"] = time.time()
            if self._cycle_attempted is not None:
                self._cycle_attempted.add(battle_id)
            failed_before = counts["failed"]
            try:
                try:
                    result = capture_report(self.runner, int(battle_id), self.out_dir, self.wait_seconds,
                        self.poll_seconds, expected_identity=self.state.get("runtimeIdentity"),
                        prefetched_requests=pending.get("metadata"), request_missing=False)
                except ValueError as exc:
                    # Bad request metadata must never attach another battle's
                    # acknowledgement. Retain this ID's own cache with an
                    # explicit failure, still without issuing fresh requests.
                    result = capture_report(self.runner, int(battle_id), self.out_dir, self.wait_seconds,
                        self.poll_seconds, expected_identity=self.state.get("runtimeIdentity"),
                        prefetched_requests=self._prefetch_failure_metadata(battle_id, str(exc)),
                        request_missing=False)
                self._store_captured_result(entry, result, counts)
            except Exception as exc:
                entry["error"] = str(exc)
                entry["complete"] = False
                counts["failed"] += 1
                self._notify_capture_failed(entry, exc)
                errors.append({"battleID": battle_id, "phase": "prefetch_recovery", "error": str(exc)})
            try:
                self._finish_attempt(entry)
            except Exception as exc:
                # An unavailable state checkpoint must not prevent exporting
                # another already-prefetched ID's cache into immutable files.
                entry["checkpointError"] = str(exc)
                entry["error"] = str(exc)
                entry["complete"] = False
                if counts["failed"] == failed_before:
                    counts["failed"] += 1
                errors.append({"battleID": battle_id, "phase": "prefetch_recovery_checkpoint", "error": str(exc)})
            inflight.pop(battle_id, None)
        return errors

    def _record_prefetch_recovery_errors(self, errors: list[dict[str, Any]]) -> None:
        self.state["prefetchRecoveryErrors"] = errors
        # The large state file may be locked while the directory and immutable
        # capture files are writable. Keep a separate durable error record.
        path = self.out_dir / "recovery_errors" / f"{time.time_ns()}_{uuid.uuid4().hex}.json"
        try:
            atomic_json(path, {"at": time.time(), "namespace": self.namespace, "errors": errors})
            self.state["prefetchRecoveryErrorLog"] = str(path)
        except Exception as exc:
            errors.append({"phase": "prefetch_recovery_error_log", "error": str(exc)})

    def collect_pending(self, battle_ids=None, *, retry_budget_seconds: float | None = None) -> dict[str, int]:
        """Collect all pending work, or only the explicitly selected report IDs."""
        if retry_budget_seconds is not None and (isinstance(retry_budget_seconds, bool)
                or not isinstance(retry_budget_seconds, (int, float))
                or not math.isfinite(retry_budget_seconds) or retry_budget_seconds < 0):
            raise ValueError("retry_budget_seconds must be a nonnegative finite number")
        retry_remaining = retry_budget_seconds
        def retry_deadline():
            # Only old network work spends this batch's retry allowance. New
            # reports, metadata refreshes and local database recovery retain
            # their own time without exhausting an old report's first turn.
            return None if retry_remaining is None else time.monotonic() + max(0.0, retry_remaining)
        counts = self._empty_collection_counts()
        selected = None
        if battle_ids is not None:
            if isinstance(battle_ids, (str, int)):
                battle_ids = (battle_ids,)
            selected = {str(validate_battle_id(value)) for value in battle_ids}
        entries = self._ordered_pending_entries(selected)
        inflight: dict[str, dict[str, Any]] = {}
        attempted: set[str] = set()
        primary_error: BaseException | None = None
        active_capture: dict[str, Any] | None = None
        def finish_capture(outcome: str) -> None:
            nonlocal active_capture
            capture, active_capture = active_capture, None
            if capture is None:
                return
            try:
                event = {"phase": "capture_finished", "battleID": capture["battleID"],
                    "elapsed_seconds": max(0.0, time.monotonic() - capture["startedAt"]),
                    "outcome": outcome, "started_at": capture["started_at"],
                    "finished_at": datetime.now(timezone.utc).isoformat()}
                if outcome == "failed":
                    event.update({key: capture[key] for key in ("error", "error_type") if key in capture})
                self._notify_progress(event)
            except Exception:
                # Timing is optional UI telemetry. Neither recording it nor a
                # consumer failure may change the committed data or exception.
                pass
        try:
            for position, entry in enumerate(entries):
                if self.stopped():
                    break
                battle_id = str(entry.get("battleID", ""))
                if selected is not None and battle_id not in selected:
                    continue
                # A process can stop after saving observations but before writing
                # their partial capture. Repair old/interrupted state before any
                # completeness/backoff skip or new request for detail components.
                if entry.get("observations") and (not entry.get("discoveryArchivePath") or (
                        entry.get("discoveryImportError") and not entry.get("pendingDiscoveryImport"))):
                    try:
                        discovery = self.archive_discovery(entry)
                    except RuntimeBridgeUnavailable:
                        raise
                    except Exception as exc:
                        entry["discoveryImportError"] = str(exc)
                        self.save()
                        counts["failed"] += 1
                        continue
                    if discovery.get("status") == "failed":
                        counts["failed"] += 1
                        continue
                if entry.get("pendingDiscoveryImport") and self.on_export:
                    if self.retry_discovery_import(entry).get("status") == "failed":
                        counts["failed"] += 1
                        continue
                pending_import = bool(entry.get("pendingImport") and self.on_export)
                if ((entry.get("complete") and not entry.get("metadataDirty") and not pending_import)
                        or (entry.get("nextRetryAt", 0) > time.time() and not pending_import)):
                    counts["skipped"] += 1
                    continue
                metadata_only = bool(entry.get("metadataDirty") and entry.get("lastResult", {}).get("complete"))
                if not pending_import and not metadata_only and not self._network_selected(entry):
                    counts["skipped"] += 1
                    continue
                if not pending_import and self._defer_old_retry(entry, battle_id, retry_deadline(), metadata_only, inflight):
                    counts["skipped"] += 1
                    continue
                if self._cycle_attempted is not None:
                    if battle_id in self._cycle_attempted and battle_id not in inflight and not metadata_only:
                        counts["skipped"] += 1
                        continue
                    self._cycle_attempted.add(battle_id)
                attempt_started = False
                old_retry_started_at = None
                try:
                    # Database failure must retry the already preserved file before
                    # making any new game request or replacing its latest alias.
                    pending = entry.get("pendingImport")
                    if pending and self.on_export:
                        self._replay_pending_capture(entry)
                        entry.pop("error", None)
                        entry["lastResult"] = pending["result"]
                        entry["complete"] = bool(pending["result"].get("complete"))
                        # Recovery may reveal that a former partial capture is
                        # already complete. Its pending metadata refresh is now
                        # entirely local and must bypass network retry limits.
                        metadata_only = bool(entry.get("metadataDirty") and entry["complete"])
                        if not pending["result"].get("exported", True):
                            # This was preservation of pre-request cache pages,
                            # not a successful final export. Clear the local
                            # import backlog without inventing a game success.
                            counts["failed"] += 1
                            self.save()
                            continue
                        if entry["complete"] and not entry.get("metadataDirty"):
                            counts["complete"] += 1
                            counts["exported"] += 1
                            entry.pop("nextRetryAt", None)
                            self.save()
                            continue
                        if entry.get("nextRetryAt", 0) > time.time() and not metadata_only:
                            # Replaying durable bytes is local recovery. It does
                            # not waive the missing component's network backoff.
                            counts["partial"] += 1
                            counts["exported"] += 1
                            self.save()
                            continue
                    # Already downloaded bytes still recover locally when a
                    # filter changes. Their missing components must not start
                    # another request unless the current filter selects them.
                    if not metadata_only and not self._network_selected(entry):
                        counts["skipped"] += 1
                        if pending and self.on_export:
                            self.save()
                        continue
                    if self._defer_old_retry(entry, battle_id, retry_deadline(), metadata_only, inflight):
                        if self._cycle_attempted is not None:
                            self._cycle_attempted.discard(battle_id)
                        counts["skipped"] += 1
                        if pending and self.on_export:
                            # Local recovery succeeded even when the remaining
                            # partial report cannot start a network retry yet.
                            self.save()
                        continue
                    old_network_attempt = not metadata_only and entry.get("attempts", 0) > 0
                    if old_network_attempt:
                        old_retry_started_at = time.monotonic()
                    entry["attempts"] = entry.get("attempts", 0) + 1
                    entry["lastAttemptAt"] = time.time()
                    attempted.add(battle_id)
                    if self._cycle_deferred_retries is not None:
                        self._cycle_deferred_retries.discard(battle_id)
                    attempt_started = True
                    self.save()
                    if entry.get("metadataDirty") and entry.get("lastResult", {}).get("complete"):
                        result = dict(entry["lastResult"])
                        # These originals were already committed with the
                        # successful capture. A list-only metadata change has
                        # no new component responses to publish or reindex.
                        result["cacheCheckpointPaths"] = []
                        original_path = Path(result.get("archivePath", result["path"]))
                        payload = json.loads(original_path.read_text(encoding="utf-8"))
                        atomic_json(Path(result["path"]), payload)
                    else:
                        active_capture = {"battleID": entry["battleID"], "startedAt": time.monotonic(),
                                          "outcome": "failed", "started_at": datetime.now(timezone.utc).isoformat()}
                        self._notify_progress({"phase": "capture_started", "battleID": entry["battleID"]})
                        if self.parallelism > 1:
                            if battle_id not in inflight:
                                group = self._parallel_candidates(entries[position:], selected, battle_id,
                                    old_retry_group=old_network_attempt if retry_remaining is not None else None)
                                for bid in group:
                                    inflight[bid] = {"metadata": None, "request_missing": True}
                                if self._cycle_attempted is not None:
                                    self._cycle_attempted.update(group)
                                try:
                                    prefetched = prefetch_reports(self.runner, group, wait_seconds=self.wait_seconds,
                                        expected_identity=self.state.get("runtimeIdentity"),
                                        min_request_interval=max(1.0, self.request_delay),
                                        min_dispatch_interval=max(1.0, self.request_delay),
                                        prefetch_snapshots=False)
                                except RuntimeBridgeUnavailable:
                                    raise
                                except Exception as exc:
                                    for bid in group:
                                        inflight[bid] = {"metadata": self._prefetch_failure_metadata(bid, str(exc)),
                                                         "request_missing": False}
                                else:
                                    for bid in group:
                                        meta = prefetched.get(bid) if isinstance(prefetched, dict) else None
                                        if isinstance(meta, dict):
                                            inflight[bid]["metadata"] = meta
                                        else:
                                            inflight[bid] = {"metadata": self._prefetch_failure_metadata(
                                                bid, "Prefetch did not acknowledge this battle"), "request_missing": False}
                            pending_prefetch = inflight.get(battle_id, {})
                            result = capture_report(self.runner, int(entry["battleID"]), self.out_dir, self.wait_seconds,
                                self.poll_seconds, expected_identity=self.state.get("runtimeIdentity"),
                                prefetched_requests=pending_prefetch.get("metadata"),
                                request_missing=pending_prefetch.get("request_missing", False))
                            inflight.pop(battle_id, None)  # A returned export has already reached disk.
                        else:
                            result = capture_report(self.runner, int(entry["battleID"]), self.out_dir, self.wait_seconds,
                                                    self.poll_seconds, expected_identity=self.state.get("runtimeIdentity"))
                    self._store_captured_result(entry, result, counts)
                    if active_capture is not None:
                        active_capture["outcome"] = ("complete" if result.get("complete") else "partial") if result.get("exported") else "failed"
                        if not result.get("exported"):
                            active_capture.update(error=str(result.get("error") or "Battle export returned no data"),
                                                  error_type="RuntimeError")
                except RuntimeBridgeUnavailable as exc:
                    entry["error"] = str(exc)
                    entry["complete"] = False
                    self._notify_capture_failed(entry, exc)
                    # Do not walk the rest of the account on an unusable
                    # bridge. Outer cleanup retains files and reconnects.
                    try:
                        self.save()
                    except Exception:
                        pass
                    raise
                except Exception as exc:
                    if active_capture is not None:
                        active_capture.update(error=str(exc), error_type=type(exc).__name__)
                    entry["error"] = str(exc)
                    entry["complete"] = False
                    counts["failed"] += 1
                    self._notify_capture_failed(entry, exc)
                if attempt_started:
                    self._finish_attempt(entry)
                else:
                    self.save()
                finish_capture(active_capture["outcome"] if active_capture is not None else "failed")
                if retry_remaining is not None and old_retry_started_at is not None:
                    retry_remaining -= max(0.0, time.monotonic() - old_retry_started_at)
        except BaseException as exc:
            primary_error = exc
            if active_capture is not None:
                active_capture.update(error=str(exc), error_type=type(exc).__name__)
            raise
        finally:
            try:
                recovery_errors = self._harvest_inflight(inflight, counts, attempted)
                if recovery_errors:
                    self._record_prefetch_recovery_errors(recovery_errors)
                    if primary_error is None:
                        raise RuntimeError("Prefetched cache recovery failed: " +
                                           json.dumps(recovery_errors, ensure_ascii=True))
            finally:
                finish_capture("failed")
        return counts

    def run_cycle(self) -> dict[str, Any]:
        self.state["cycles"] += 1
        summary: dict[str, Any] = {"cycle": self.state["cycles"], "scopes": {}, "startedAt": time.time(),
                                   "collection": self._empty_collection_counts()}
        self._cycle_attempted = set()
        self._cycle_collection = summary["collection"]
        self._cycle_deferred_retries = set()
        self.state.pop("prefetchRecoveryErrors", None)
        self.state.pop("prefetchRecoveryErrorLog", None)
        primary_error: BaseException | None = None
        try:
            capabilities = decode_envelope(run_checked(self.raw_runner, install_lua(self.owner, self.lease_seconds)))["payload"]
            if capabilities.get("error"):
                raise RuntimeError(capabilities["error"])
            identity = capabilities.get("runtimeIdentity")
            if not isinstance(identity, dict) or not all(identity.get(key) for key in ("avatarId", "serverId", "scenarioId")):
                raise RuntimeError("game not logged in or unavailable: runtime account identity is missing")
            if self.state.get("runtimeIdentity") and self.state["runtimeIdentity"] != identity:
                raise RuntimeError("Collector state is bound to a different account/server/scenario; use a separate namespace")
            self.state["runtimeIdentity"] = identity
            self.state["runtimeCapabilities"] = capabilities
            # General state checkpoints also change while reports are being
            # imported. Keep the actual identity/union read time separately.
            self.state["runtimeCapabilitiesObservedAt"] = time.time()
            if self.pvp_only:
                enums = capabilities.get("battleMatchTypes")
                pvp = enums.get("PVP") if isinstance(enums, dict) else None
                if isinstance(pvp, bool) or not isinstance(pvp, int):
                    raise RuntimeError("PVP-only collection requires the current client's PVP matchType constant")
            # Read current lists before any historical network attempt. Each
            # discovered page still captures its new reports immediately, and
            # the final fair batch recovers local pending imports and retries
            # older work without blocking the first accessible page.
            self._collect_pages = True
            for scope in self.scopes:
                if self.stopped():
                    break
                try:
                    summary["scopes"][scope] = self.discover_scope(scope, capabilities)
                except RuntimeBridgeUnavailable:
                    raise
                except Exception as exc:
                    previous = self.state["scopes"].get(scope, {})
                    failed = {**previous, "complete": False, "error": str(exc), "failedAt": time.time()}
                    self.state["scopes"][scope] = failed
                    summary["scopes"][scope] = failed
                    self.save()
            remaining = [battle_id for battle_id in self.state["reports"]
                         if battle_id not in self._cycle_attempted]
            self._collect_batch(remaining, retry_budget_seconds=max(60.0, self.wait_seconds * 4))
        except RuntimeBridgeUnavailable as exc:
            primary_error = exc
            summary["error"] = str(exc)
            summary["runtime_unavailable"] = True
            raise
        except Exception as exc:
            summary["error"] = str(exc)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            self._collect_pages = False
            self._cycle_attempted = None
            self._cycle_collection = None
            summary["deferredRetries"] = {"count": len(self._cycle_deferred_retries or ()),
                "battleIDs": sorted(self._cycle_deferred_retries or ()),
                "reason": "old_retry_start_budget_exhausted"}
            self._cycle_deferred_retries = None
            if summary.get("runtime_unavailable"):
                summary["cleanupDeferred"] = True
            else:
                try:
                    run_checked(self.raw_runner, cleanup_lua(self.owner))
                except Exception as exc:
                    summary["cleanupError"] = str(exc)
            summary["finishedAt"] = time.time()
            summary["discoveredReports"] = len(self.state["reports"])
            summary["completeReports"] = sum(bool(e.get("complete")) for e in self.state["reports"].values())
            current = [e for e in self.state["reports"].values() if e.get("lastSeenCycle") == self.state["cycles"]]
            summary["filter"] = self.filter_summary(current)
            summary["allVisibleReports"] = len(current)
            if self.pvp_only:
                current = [entry for entry in current if self._network_selected(entry)]
            summary["visibleReports"] = len(current)
            summary["completeVisibleReports"] = sum(bool(e.get("complete")) for e in current)
            summary["enumerationComplete"] = (
                set(summary["scopes"]) == set(self.scopes)
                and all(value.get("complete") for value in summary["scopes"].values())
                and not summary.get("error") and not summary.get("cleanupError")
            )
            summary["coverageComplete"] = (summary["enumerationComplete"]
                and (summary["visibleReports"] > 0 or (self.pvp_only and summary["allVisibleReports"] > 0
                                                     and not summary["filter"]["unknownReports"]))
                and summary["completeVisibleReports"] == summary["visibleReports"]
                and not any(e.get("discoveryImportError") or e.get("pendingDiscoveryImport") for e in current)
                and not self.state.get("prefetchRecoveryErrors"))
            if self.pvp_only and summary["filter"]["unknownReports"]:
                summary["coverageComplete"] = False
            summary["warnings"] = [] if current else ["no_reports_observed_end_to_end_collection_unverified"]
            if not current and self.pvp_only and summary["allVisibleReports"]:
                summary["warnings"] = ["no_confirmed_pvp_reports_in_visible_lists"]
            if self.pvp_only and summary["filter"]["unknownReports"]:
                summary["warnings"].append("unclassified_reports_skipped_without_component_requests")
            summary["complete"] = summary["coverageComplete"]
            summary["errors"] = [
                {"scope": scope, "error": value.get("error", "scope enumeration incomplete")}
                for scope, value in summary["scopes"].items() if not value.get("complete")
            ]
            if summary.get("error"):
                summary["errors"].append({"error": summary["error"]})
            if summary.get("cleanupError"):
                summary["errors"].append({"error": summary["cleanupError"]})
            summary["errors"].extend(
                {"battleID": e.get("battleID"), "phase": "discovery_import", "error": e["discoveryImportError"]}
                for e in self.state["reports"].values() if e.get("discoveryImportError"))
            summary["errors"].extend(self.state.get("prefetchRecoveryErrors", []))
            if self.state.get("prefetchRecoveryErrorLog"):
                summary["prefetchRecoveryErrorLog"] = self.state["prefetchRecoveryErrorLog"]
            self.state["lastCycle"] = summary
            try:
                self.save()
            except Exception as exc:
                summary["complete"] = summary["coverageComplete"] = False
                summary["errors"].append({"phase": "cycle_checkpoint", "error": str(exc)})
                if primary_error is None:
                    raise
            try:
                atomic_json(self.out_dir / "collector_manifest.json", summary)
            except Exception:
                if primary_error is None:
                    raise
        return summary

    def watch(self, interval: float = 60.0, max_cycles: int | None = None, stop_event: Any = None) -> None:
        if (not math.isfinite(interval) or interval <= 0 or (max_cycles is not None
                and (isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles <= 0))):
            raise ValueError("Watch interval/max_cycles must be positive")
        self._stop_event = stop_event
        count = 0
        try:
            while not self.stopped():
                summary = self.run_cycle()
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                count += 1
                if max_cycles is not None and count >= max_cycles:
                    break
                deadline = time.monotonic() + interval
                while not self.stopped() and time.monotonic() < deadline:
                    time.sleep(min(0.25, deadline-time.monotonic()))
        except KeyboardInterrupt:
            self.state["stoppedAt"] = time.time()
            self.save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--scopes", default="personal,union,star")
    parser.add_argument("--wait", type=float, default=20.0)
    parser.add_argument("--poll", type=float, default=0.5)
    parser.add_argument("--request-delay", type=float, default=0.5)
    parser.add_argument("--max-pages", type=int, default=10000)
    parser.add_argument("--pvp-only", action="store_true", help="仅请求当前客户端明确标为PVP的战报正文，PVE及未知类型跳过")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--max-cycles", type=int)
    args = parser.parse_args()
    if (not all(math.isfinite(value) for value in (args.wait, args.poll, args.request_delay, args.interval))
            or args.wait < 0 or args.poll <= 0 or args.request_delay < 0 or args.interval <= 0
            or args.max_pages <= 0 or (args.max_cycles is not None and args.max_cycles <= 0)):
        parser.error("Wait/delay must be nonnegative; poll/interval/page limits must be positive; all timers must be finite")
    scopes = tuple(args.scopes.split(","))
    if not scopes or any(scope not in SCOPES for scope in scopes):
        parser.error(f"Scopes must be chosen from {SCOPES}")
    args.out_dir = safe_output_path(args.out_dir)
    if args.state_file is not None:
        args.state_file = safe_output_path(args.state_file)
    try:
        from .run_lua_in_game import GameLuaRunner
    except ImportError:
        from run_lua_in_game import GameLuaRunner
    with GameLuaRunner(args.pid) as runner:
        collector = BattleCollector(runner, args.out_dir, args.state_file,
            scopes, args.wait, args.poll, args.request_delay,
            args.max_pages, namespace=args.namespace, pvp_only=args.pvp_only)
        if args.watch:
            collector.watch(args.interval, args.max_cycles)
        else:
            summary = collector.run_cycle()
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            if not summary["coverageComplete"]:
                raise SystemExit(2)


if __name__ == "__main__":
    main()
