"""Loss-aware export of every available battle component through the live client."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any
from types import SimpleNamespace

try:
    from .atomic_files import replace_with_retry
    from .runtime_errors import RuntimeBridgeUnavailable, is_bridge_exception
    from . import runtime_response_audit as response_audit
except ImportError:
    from atomic_files import replace_with_retry
    from runtime_errors import RuntimeBridgeUnavailable, is_bridge_exception
    import runtime_response_audit as response_audit

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = ROOT / "capture" / "output" / "processed" / "battle_reports"
COMPONENTS = ("basic", "detail", "all", "statics", "snapshots")


def safe_output_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    return path


def lua_string(value: str) -> str:
    # Lua 5.1 does not accept JSON \u escapes. Decimal byte escapes are portable.
    return '"' + ''.join('\\%03d' % b for b in value.encode("utf-8")) + '"'


def validate_battle_id(value: Any) -> int:
    text = str(value)
    if isinstance(value, bool) or not text.isascii() or not text.isdigit():
        raise ValueError(f"Invalid battle ID: {text!r}")
    number = int(text)
    if not 0 < number <= 9007199254740991:
        raise ValueError("Battle ID is outside Lua's exact integer range")
    return number


def atomic_bytes(path: Path, raw: bytes) -> None:
    """Publish already encoded bytes without repeating JSON serialization.

    The complete temporary file is flushed before replacement. On replacement
    failure it and the previous destination are retained for recovery.
    """
    path = safe_output_path(path)
    if not isinstance(raw, bytes):
        raise TypeError("atomic_bytes requires encoded bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    replace_with_retry(temporary, path)


def atomic_json(path: Path, value: Any, *, compact: bool = False) -> None:
    path = safe_output_path(path)
    options = {"separators": (",", ":")} if compact else {"indent": 2}
    # json.dumps emits the same UTF-8/LF bytes as the old streaming json.dump,
    # but avoids a Python file-write call for every small encoder fragment.
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, **options).encode("utf-8")
    atomic_bytes(path, raw)


def identity_guard_lua(expected: dict[str, Any]) -> str:
    return f'''
local __cp = ClientProxy
if type(__cp) ~= 'table' or type(__cp.GetServerId) ~= 'function' then error('account unavailable') end
if tostring(__cp.avatarId) ~= {lua_string(str(expected['avatarId']))}
 or tostring(__cp:GetServerId()) ~= {lua_string(str(expected['serverId']))}
 or tostring(G_SCENARIO_ID) ~= {lua_string(str(expected['scenarioId']))} then
 error('account/server/scenario changed; collection must restart in a separate namespace')
end
'''


class IdentityGuardRunner:
    def __init__(self, runner: Any, identity: dict[str, Any]):
        self.runner, self.identity = runner, identity

    def execute_on_update(self, code: str, timeout: float = 10.0) -> Any:
        return self.runner.execute_on_update(identity_guard_lua(self.identity) + code, timeout=timeout)

    def execute(self, code: str) -> Any:
        return self.runner.execute(identity_guard_lua(self.identity) + code)


LUA_JSON = r'''
local issues = {}
local keyTypes = {}
local function esc(s)
  return '"' .. tostring(s):gsub('[%z\1-\31\\"]', function(c)
    if c == '"' then return '\\"' end
    if c == '\\' then return '\\\\' end
    return string.format('\\u%04x', string.byte(c))
  end) .. '"'
end
local function valid_utf8(s)
  local i = 1
  while i <= #s do
    local b = s:byte(i)
    local n, lo, hi = 0, 128, 191
    if b < 128 then n = 0
    elseif b >= 194 and b <= 223 then n = 1
    elseif b >= 224 and b <= 239 then
      n = 2
      if b == 224 then lo = 160 elseif b == 237 then hi = 159 end
    elseif b >= 240 and b <= 244 then
      n = 3
      if b == 240 then lo = 144 elseif b == 244 then hi = 143 end
    else return false end
    if i + n > #s then return false end
    if n > 0 and (s:byte(i+1) < lo or s:byte(i+1) > hi) then return false end
    for j = 2, n do
      local c = s:byte(i+j)
      if c < 128 or c > 191 then return false end
    end
    i = i + n + 1
  end
  return true
end
local seen = {}
local function encode(v, depth, path)
  local tv = type(v)
  if tv == 'nil' then return 'null' end
  if tv == 'boolean' then return tostring(v) end
  if tv == 'number' then
    if v ~= v or v == math.huge or v == -math.huge then
      return '{"$luaNumber":' .. esc(tostring(v)) .. '}'
    end
    return string.format('%.17g', v)
  end
  if tv == 'string' then
    if valid_utf8(v) then return esc(v) end
    local hex = v:gsub('.', function(c) return string.format('%02x', c:byte()) end)
    return '{"$luaBytesHex":' .. esc(hex) .. '}'
  end
  if tv ~= 'table' then
    issues[#issues+1] = {path=path, kind='unsupported', luaType=tv}
    return '{"$luaType":' .. esc(tv) .. ',"representation":' .. esc(tostring(v)) .. '}'
  end
  if seen[v] then
    issues[#issues+1] = {path=path, kind='cycle', target=seen[v]}
    return '{"$luaReference":' .. esc(seen[v]) .. '}'
  end
  if depth <= 0 then
    issues[#issues+1] = {path=path, kind='max_depth'}
    return '{"$luaTruncated":true}'
  end
  seen[v] = path
  local max, count, array = 0, 0, true
  local names, typed, collision = {}, {}, false
  for k, _ in pairs(v) do
    count = count + 1
    if type(k) ~= 'number' or k < 1 or k % 1 ~= 0 then array = false
    elseif k > max then max = k end
    local name = type(k) == 'number' and string.format('%.17g',k) or tostring(k)
    if names[name] or (type(k) ~= 'string' and type(k) ~= 'number') then collision = true end
    if type(k) == 'string' and not valid_utf8(k) then collision = true end
    names[name] = true
    if type(k) ~= 'string' then typed[name] = type(k) end
  end
  local out = {}
  if array and max == count then
    for i=1,max do out[#out+1] = encode(v[i], depth-1, path .. '/' .. (i-1)) end
    seen[v] = nil
    return '[' .. table.concat(out, ',') .. ']'
  end
  if collision then
    for k,val in pairs(v) do
      local p = path .. '/$luaTableEntries/' .. #out
      out[#out+1] = '[' .. encode(k,depth-1,p .. '/0') .. ',' .. encode(val,depth-1,p .. '/1') .. ']'
    end
    seen[v] = nil
    return '{"$luaTableEntries":[' .. table.concat(out, ',') .. ']}'
  end
  if next(typed) then keyTypes[path] = typed end
  for k,val in pairs(v) do
    local name = type(k) == 'number' and string.format('%.17g',k) or tostring(k)
    local p = path .. '/' .. name:gsub('~','~0'):gsub('/','~1')
    out[#out+1] = esc(name) .. ':' .. encode(val,depth-1,p)
  end
  table.sort(out)
  seen[v] = nil
  return '{' .. table.concat(out, ',') .. '}'
end
local function envelope(value, retained)
  local body = encode(value, 180, '')
  local problemJson = encode(issues, 20, '/serializationIssues')
  local keyJson = encode(keyTypes, 30, '/luaTableKeyTypes')
  local extra = retained and #retained>0 and ',"retainedRecords":' .. encode(retained, 30, '/retainedRecords') or ''
  return '{"payload":' .. body .. ',"serializationIssues":' .. problemJson .. ',"luaTableKeyTypes":' .. keyJson .. extra .. '}'
end
local function isolated_envelope(value)
  local previousIssues,previousKeys,previousSeen=issues,keyTypes,seen
  issues,keyTypes,seen={},{},{}
  local ok,result=pcall(envelope,value)
  issues,keyTypes,seen=previousIssues,previousKeys,previousSeen
  if not ok then error(result) end
  return result
end
'''


def request_lua(battle_id: int, method: str, *, audit_token: str | None = None) -> str:
    battle_id = validate_battle_id(battle_id)
    methods = {"all": "RPCGetAllCombatInfo", "detail": "RPCGetDetailCombatInfo",
               "statics": "RPCGetStaticsCombatInfo", "snapshots": "RPCGetHeroBattleSnapRecords"}
    name = methods[method]
    arguments = str(battle_id)
    if method == "snapshots":
        arguments += ", ClientConst.ReqBattleDiffType.Normal"
    if audit_token is not None:
        return LUA_JSON + response_audit.dispatch_lua(audit_token, battle_id, method, name, arguments, lua_string)
    return f'''
if not ClientProxy then return 'missing ClientProxy' end
if type(ClientProxy.{name}) ~= 'function' then return 'missing {name}' end
if {lua_string(method)} == 'snapshots' and not (ClientConst and ClientConst.ReqBattleDiffType and ClientConst.ReqBattleDiffType.Normal) then
  return 'missing snapshot request enum'
end
ClientProxy:{name}({arguments})
return 'requested {method} {battle_id}'
'''


def component_lua(battle_id: int) -> str:
    return f'''
local id = {validate_battle_id(battle_id)}
local cp = ClientProxy
local function getter(name)
  if cp and type(cp[name]) == 'function' then return cp[name](cp,id) end
end
local statics,snapshots
if cp and cp.reportStaticsInfo then statics=cp.reportStaticsInfo[id] end
if cp and cp.allBattleRecordInfo then snapshots=cp.allBattleRecordInfo[id] end
local components = {{
  basic = getter('GetCombatInfo'), detail = getter('GetDetailCombatInfo'),
  all = getter('GetAllCombatInfo'),
  statics = statics, snapshots = snapshots
}}
local componentFragments = {{}}
if cp and cp._allCombatSlices then componentFragments.all=cp._allCombatSlices[id] end
if cp and cp._detailCombatSlices then componentFragments.detail=cp._detailCombatSlices[id] end
'''


def status_lua(battle_id: int, audit_token: str | None = None) -> str:
    return LUA_JSON + component_lua(battle_id) + '''
local status = {}
for _,name in ipairs({'basic','detail','all','statics','snapshots'}) do
  local value = components[name]
  status[name] = {available=value ~= nil, complete=type(value) == 'table'}
  if name == 'all' or name == 'detail' then
    status[name].eventCount = type(value) == 'table' and type(value.eventDataList) == 'table' and #value.eventDataList or -1
    status[name].complete = status[name].eventCount >= 0
    local fragment=componentFragments[name]
    status[name].partialAvailable=type(fragment)=='table' and not status[name].complete
    if status[name].partialAvailable and GameTime and type(GameTime.svrTime)=='number'
      and ClientConst and type(ClientConst.BattleOutOfTime)=='number' then
      local started=type(fragment.time)=='number' and fragment.time or 0
      status[name].retryAfter=ClientConst.BattleOutOfTime-(GameTime.svrTime-started)
    end
  elseif name == 'snapshots' then
    status[name].complete = type(value) == 'table' and value.isComplete == true and type(value.battleSnapData) == 'table'
    status[name].partialAvailable=type(value)=='table' and not status[name].complete
    if status[name].partialAvailable
      and GameTime and type(GameTime.svrTime)=='number' and ClientConst and type(ClientConst.BattleOutOfTime)=='number' then
      local started=type(value.time)=='number' and value.time or 0
      status[name].retryAfter=ClientConst.BattleOutOfTime-(GameTime.svrTime-started)
    end
  end
end
''' + (response_audit.status_lua(audit_token, lua_string) if audit_token is not None else '') + 'return envelope(status)\n'


def export_lua(battle_id: int, out_path: Path | None = None, *, audit_token: str | None = None) -> str:
    # Python owns file I/O and validates the returned data before atomic rename.
    audit = ''
    if audit_token is not None:
        audit = f"local status={{}}\n{response_audit.status_lua(audit_token, lua_string)}\nlocal requestAudit=status.requestAudit\n"
    else:
        audit = 'local requestAudit=nil\n'
    recovery_cleanup = (response_audit.release_owned_lua(response_audit.OWNER, battle_id, lua_string)
                        if audit_token is None else '')
    return LUA_JSON + component_lua(battle_id) + audit + recovery_cleanup + response_audit.retained_lua(battle_id) + '''
local source,data='none',nil
for _,name in ipairs({'all','detail','basic','statics','snapshots'}) do
  if type(components[name])=='table' then source,data=name,components[name];break end
end
if source=='none' then
  for _,name in ipairs({'all','detail','basic','statics','snapshots'}) do
    if components[name]~=nil then source,data=name,components[name];break end
  end
end
if source=='none' then
  for _,name in ipairs({'all','detail'}) do
    if componentFragments[name]~=nil then
      source=name..'_fragments';data={[source]=componentFragments[name]};break
    end
  end
end
if source=='none' then
  if #retainedRecords>0 then return envelope({battleID=id,notFound=true},retainedRecords) end
  return 'not_found ' .. tostring(id)
end
-- The archive's data node is a record. Retain unusual scalar/array/empty
-- component values under their real component name instead of inventing a
-- basic report or discarding the only captured data.
local record=false
if type(data)=='table' then
  for key,_ in pairs(data) do if type(key)=='string' then record=true;break end end
end
if not record then data={[source]=data} end
local count = type(data.eventDataList) == 'table' and #data.eventDataList or -1
return envelope({
  captureVersion=2, exportedAt=os.date('!%Y-%m-%dT%H:%M:%SZ'),
  battleID=id, source=source, hasAll=components.all ~= nil,
  hasDetail=components.detail ~= nil, hasBasic=components.basic ~= nil,
  eventCount=count, data=data, components=components,
  componentFragments=next(componentFragments) and componentFragments or nil, requestAudit=requestAudit
},retainedRecords)
'''


def run_checked(runner: Any, code: str, timeout: float = 10.0, direct: bool = False) -> str:
    try:
        result = runner.execute(code) if direct else runner.execute_on_update(code, timeout=timeout)
    except RuntimeBridgeUnavailable:
        raise
    except Exception as exc:
        if is_bridge_exception(exc):
            raise RuntimeBridgeUnavailable(str(exc)) from exc
        raise
    if not result.get("ok"):
        if ((result.get("stage") == "timeout" and result.get("cancelled") is True)
                or result.get("runtime_unavailable") is True
                or result.get("stage") in {"transport", "detached", "script_destroyed", "runtime_unavailable", "lua_hook"}):
            error = RuntimeBridgeUnavailable(f"{result.get('stage')}: {result.get('error')}")
            error.result = dict(result)
            raise error
        raise RuntimeError(f"{result.get('stage')}: {result.get('error')}")
    return str(result.get("result") or "")


def decode_envelope(text: str) -> dict[str, Any]:
    value = json.loads(text)
    if not isinstance(value, dict) or "payload" not in value:
        raise ValueError("Invalid runtime JSON envelope")
    return value


def read_status(runner: Any, battle_id: int, direct: bool = False, *, audit_token: str | None = None) -> dict[str, Any]:
    return decode_envelope(run_checked(runner, status_lua(battle_id, audit_token), direct=direct))["payload"]


def wait_for(runner: Any, battle_id: int, want: str, wait_seconds: float,
             poll_seconds: float, direct: bool = False) -> tuple[bool, dict[str, Any]]:
    if not math.isfinite(wait_seconds) or wait_seconds < 0 or not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("wait/poll must be finite, with wait >= 0 and poll > 0")
    deadline = time.monotonic() + wait_seconds
    while True:
        status = read_status(runner, battle_id, direct)
        if status[want]["complete"]:
            return True, status
        if time.monotonic() >= deadline:
            return False, status
        time.sleep(max(0.01, min(poll_seconds, deadline - time.monotonic())))


def _dispatch_rates(min_request_interval: float, min_dispatch_interval: float) -> tuple[float, float]:
    for name, value in (("min_request_interval", min_request_interval), ("min_dispatch_interval", min_dispatch_interval)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    return float(min_request_interval), float(min_dispatch_interval)


def _dispatch_component(runner: Any, battle_id: int, component: str, *, rate_owner: Any,
                        min_request_interval: float, min_dispatch_interval: float,
                        direct: bool = False, audit_token: str | None = None,
                        dispatch_metadata: dict | None = None) -> float:
    """Apply the runner's shared pacing before one game-thread RPC dispatch."""
    method_gap, global_gap = _dispatch_rates(min_request_interval, min_dispatch_interval)
    # Validation does not consume a dispatch slot or wait behind earlier work.
    code = request_lua(battle_id, component, audit_token=audit_token)
    ledger = getattr(rate_owner, "_battle_prefetch_dispatch_times", None)
    if not isinstance(ledger, dict):
        ledger = {}
        setattr(rate_owner, "_battle_prefetch_dispatch_times", ledger)
    while True:
        earliest = time.monotonic()
        if component in ledger:
            earliest = max(earliest, ledger[component] + method_gap)
        if "__all_methods__" in ledger:
            earliest = max(earliest, ledger["__all_methods__"] + global_gap)
        remaining = earliest - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(remaining)
    try:
        result = run_checked(runner, code, direct=direct)
        if audit_token is not None:
            metadata = decode_envelope(result)["payload"]
            if (not isinstance(metadata, dict) or str(metadata.get("battleID")) != str(battle_id)
                    or metadata.get("component") != component or metadata.get("requested") is not True):
                raise ValueError("Component dispatch acknowledgement has the wrong target")
            if dispatch_metadata is not None:
                dispatch_metadata.update(metadata)
        elif not result.startswith("requested "):
            raise RuntimeError(result)
    finally:
        # A client exception or lost IPC reply does not prove that the RPC was
        # never sent. Conservatively pace later attempts from completion too.
        dispatched_at = time.monotonic()
        ledger[component] = dispatched_at
        ledger["__all_methods__"] = dispatched_at
    return dispatched_at


class _ComponentAudit:
    def __init__(self, runner, battle_id: int, direct: bool):
        self.runner, self.cleanup_runner, self.battle_id, self.direct = runner, runner, battle_id, direct
        self.token, self.active = uuid.uuid4().hex, False
        self.out_dir = None
        self.expected_identity = None
        self.identity = None
        self.saved_paths = []

    def ensure(self):
        if self.active:
            return
        self.active = True  # Cleanup must also restore partially installed hooks.
        result = decode_envelope(run_checked(self.runner, LUA_JSON + response_audit.install_lua(
            self.token, response_audit.OWNER, self.battle_id, lua_string), direct=self.direct))["payload"]
        if not isinstance(result, dict) or result.get("installed") is not True:
            raise RuntimeError(str(result.get("error", "Unable to install component observer")))
        self.identity = result["identity"]

    def persist_retained(self, records):
        if not records:
            return
        if not isinstance(records, list) or self.out_dir is None:
            raise ValueError("Invalid retained response records")
        tickets = []
        identity = self.expected_identity or self.identity
        for record in records:
            if (not isinstance(record, dict) or str(record.get("battleID")) != str(self.battle_id)
                    or not isinstance(record.get("ticket"), int) or isinstance(record["ticket"], bool)
                    or record["ticket"] <= 0 or not isinstance(record.get("raw"), str)):
                raise ValueError("Retained response has the wrong battle or ticket")
            scope = record.get("identity")
            keys = ("avatarId", "serverId", "scenarioId")
            if not isinstance(scope, dict) or any(key not in scope for key in keys):
                raise ValueError("Retained response has no account identity")
            if identity is None:
                identity = scope
            if any(str(scope[key]) != str(identity[key]) for key in keys):
                raise ValueError("Retained response belongs to another account; originals remain pending")
            envelope = decode_envelope(record["raw"])
            payload = envelope["payload"]
            actual = payload.get("runtimeIdentity", {})
            if (str(payload.get("battleID")) != str(self.battle_id)
                    or any(str(actual.get(key)) != str(scope[key]) for key in keys)):
                raise ValueError("Retained original identity does not match its receipt")
            if payload.get("source") not in ("cache_fragments", "response_fragments"):
                raise ValueError("Retained original has an invalid source")
            components = payload.get("components")
            components = components if isinstance(components, dict) else {}
            payload.update(components=components, captureComplete=False,
                           componentStatus={name: {"available": components.get(name) is not None,
                                                   "complete": False, "requested": False}
                                            for name in COMPONENTS},
                           serializationIssues=envelope.get("serializationIssues", []),
                           luaTableKeyTypes=envelope.get("luaTableKeyTypes", {}))
            # Keep the identity frozen by Lua. Never re-label old pending bytes
            # with the account currently supplied by a caller.
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            path = self.out_dir / "captures" / str(self.battle_id) / (hashlib.sha256(raw).hexdigest() + ".json")
            if path.exists():
                if path.read_bytes() != raw:
                    raise ValueError("Retained checkpoint hash path contains different bytes; originals remain pending")
            else:
                atomic_bytes(path, raw)
            self.saved_paths.append(str(path))
            tickets.append(record["ticket"])
        # Only confirmed durable paths may release their Lua-side originals.
        run_checked(self.cleanup_runner, LUA_JSON + response_audit.acknowledge_lua(
            self.battle_id, identity, tickets, lua_string), direct=self.direct)

    def close(self):
        if self.active:
            # Detach and collect in one game-thread call: no response can fall
            # between the last collection and removal of our hooks. Closing
            # never deletes originals, including on write/IPC failure.
            result = decode_envelope(run_checked(self.cleanup_runner, LUA_JSON + response_audit.close_lua(
                self.token, lua_string, self.battle_id), direct=self.direct))
            self.active = False
            self.persist_retained(result.get("retainedRecords", []))


def _wait_observed(runner, battle_id, component, wait_seconds, poll_seconds, direct, token):
    """Allow genuine new pages to arrive, with a finite total and idle budget."""
    began = time.monotonic()
    deadline = began + wait_seconds
    hard_deadline = began + wait_seconds * 3
    progress = 0
    while True:
        status = read_status(runner, battle_id, direct, audit_token=token)
        audit = status.get("requestAudit", {})
        coverage = response_audit.coverage(component, audit)
        if coverage.get("error"):
            return False, status, coverage["error"]
        if status[component]["complete"] and coverage.get("complete") is not False:
            return True, status, None
        count = len(coverage.get("received", []))
        now = time.monotonic()
        if count > progress:
            progress = count
            deadline = min(hard_deadline, max(deadline, now + wait_seconds))
        if now >= min(deadline, hard_deadline):
            return False, status, "timeout"
        time.sleep(max(.01, min(poll_seconds, min(deadline, hard_deadline) - now)))


def _checkpoint_before_cache_retry(runner, battle_id, out_dir, direct, token, expected_identity):
    envelope = decode_envelope(run_checked(runner, export_lua(battle_id, audit_token=token), timeout=45, direct=direct))
    payload = envelope["payload"]
    if str(payload.get("battleID")) != str(battle_id):
        raise ValueError("Cache checkpoint returned another battle")
    payload.update(captureKind="cache_before_retry", captureComplete=False,
                   captureWarnings=["partial_cache_checkpoint_before_natural_expiry_retry"],
                   serializationIssues=envelope.get("serializationIssues", []),
                   luaTableKeyTypes=envelope.get("luaTableKeyTypes", {}))
    statuses = {}
    captured = payload.get("components")
    captured = captured if isinstance(captured, dict) else {}
    for name in COMPONENTS:
        value = captured.get(name)
        item = {"available": value is not None, "complete": isinstance(value, (dict, list)), "requested": False}
        if name in ("all", "detail"):
            events = value.get("eventDataList") if isinstance(value, dict) else None
            item.update(complete=isinstance(events, list), eventCount=len(events) if isinstance(events, list) else -1)
        elif name == "snapshots":
            item["complete"] = isinstance(value, dict) and value.get("isComplete") is True and isinstance(value.get("battleSnapData"), (dict, list))
        statuses[name] = item
    payload["componentStatus"] = statuses
    if expected_identity is not None:
        payload["runtimeIdentity"] = expected_identity
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    path = out_dir / "captures" / str(battle_id) / (hashlib.sha256(raw).hexdigest() + ".json")
    if not path.exists():
        atomic_bytes(path, raw)
    return str(path)


def _wait_natural_expiry(runner, battle_id, component, wait_seconds, poll_seconds, direct, token):
    """Never delete a cache: retry only once its own client clock says expired."""
    deadline = time.monotonic() + wait_seconds
    while True:
        status = read_status(runner, battle_id, direct, audit_token=token)
        if status[component]["complete"]:
            return True, status
        if response_audit.coverage(component, status.get("requestAudit")).get("error"):
            return None, status
        if not status[component].get("partialAvailable", status[component].get("available", False)):
            return False, status
        remaining = status[component].get("retryAfter")
        if remaining is not None and remaining <= 0:
            return False, status
        if time.monotonic() >= deadline:
            return None, status
        time.sleep(max(.01, min(poll_seconds, deadline-time.monotonic())))


def prefetch_reports(runner: Any, battle_ids, wait_seconds: float = 20.0,
                     expected_identity: dict[str, Any] | None = None,
                     min_request_interval: float = 1.0,
                     min_dispatch_interval: float = 0.0,
                     prefetch_snapshots: bool = True) -> dict[str, dict[str, dict[str, Any]]]:
    """Sequentially dispatch missing requests; network replies may overlap.

    Every Lua operation still uses the caller's single game-thread runner.
    The caller bounds the group size. A request's waiting budget starts after
    its dispatch has returned, so queueing/throttling cannot shorten the network
    budget. Same-method dispatches are spaced on the Python side, with the
    ledger retained on this runner across batches. The game thread never sleeps
    and cache hits do not consume a dispatch slot.
    """
    if not math.isfinite(wait_seconds) or wait_seconds < 0:
        raise ValueError("wait_seconds must be finite and nonnegative")
    method_gap, global_gap = _dispatch_rates(min_request_interval, min_dispatch_interval)
    if not isinstance(prefetch_snapshots, bool):
        raise ValueError("prefetch_snapshots must be a boolean")
    ids = tuple(dict.fromkeys(validate_battle_id(value) for value in battle_ids))
    # Keep pacing on the original stable runner, outside the identity wrapper.
    rate_owner = runner
    if expected_identity is not None:
        runner = IdentityGuardRunner(runner, expected_identity)
    results: dict[str, dict[str, dict[str, Any]]] = {}
    for battle_id in ids:
        requests: dict[str, dict[str, Any]] = {}
        results[str(battle_id)] = requests
        try:
            status = read_status(runner, battle_id)
        except RuntimeBridgeUnavailable:
            raise
        except Exception as exc:
            for name in ("all", "statics", "snapshots"):
                requests[name] = {"battleID": battle_id, "component": name, "requested": False,
                                  "min_request_interval": method_gap, "min_dispatch_interval": global_gap}
                if name == "snapshots" and not prefetch_snapshots:
                    requests[name]["deferred"] = True
                else:
                    requests[name].update(error=str(exc), stage="prefetch_status")
            continue
        for name in ("all", "statics", "snapshots"):
            meta: dict[str, Any] = {"battleID": battle_id, "component": name, "requested": False,
                                    "min_request_interval": method_gap, "min_dispatch_interval": global_gap}
            requests[name] = meta
            if name == "snapshots" and not prefetch_snapshots:
                meta["deferred"] = True
                continue
            if status[name]["complete"]:
                meta["cached"] = True
                continue
            if status[name].get("partialAvailable") or (
                    name == "snapshots" and status[name].get("available") and not status[name].get("complete")):
                # Prefetch has no output directory, so it cannot durably retain
                # a cache that the original RPC may clear. Let capture_report
                # checkpoint this component before it performs the request.
                meta.update(deferred=True, reason="partial_cache_requires_checkpoint")
                continue
            try:
                meta["requested"] = True
                dispatched_at = _dispatch_component(runner, battle_id, name, rate_owner=rate_owner,
                                                    min_request_interval=method_gap, min_dispatch_interval=global_gap)
                meta["deadline"] = dispatched_at + wait_seconds
            except RuntimeBridgeUnavailable:
                raise
            except Exception as exc:
                meta["error"] = str(exc)
    return results


def _validate_prefetched_requests(battle_id: int, requests: dict | None) -> dict[str, dict[str, Any]]:
    if requests is None:
        return {}
    if not isinstance(requests, dict) or set(requests) - {"all", "statics", "snapshots"}:
        raise ValueError("prefetched_requests must contain only all/statics/snapshots metadata for one battle")
    result = {}
    rates = None
    for component, original in requests.items():
        if not isinstance(original, dict):
            raise ValueError("Invalid prefetched request metadata")
        meta = dict(original)
        if {"available", "complete", "eventCount"} & set(meta):
            raise ValueError("Prefetched request metadata cannot override actual component status")
        if validate_battle_id(meta.get("battleID")) != battle_id or meta.get("component") != component:
            raise ValueError("Prefetched request belongs to another battle or component")
        if not isinstance(meta.get("requested"), bool):
            raise ValueError("Prefetched request metadata must declare requested as a boolean")
        current_rates = _dispatch_rates(meta.get("min_request_interval"), meta.get("min_dispatch_interval"))
        if rates is not None and rates != current_rates:
            raise ValueError("Prefetched request metadata has conflicting dispatch intervals")
        rates = current_rates
        if "deferred" in meta and (not isinstance(meta["deferred"], bool) or (meta["deferred"] and meta["requested"])):
            raise ValueError("Invalid deferred prefetched request metadata")
        deadline = meta.get("deadline")
        if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
            raise ValueError("Prefetched request deadline must be finite")
        if meta["requested"] and not meta.get("error") and deadline is None:
            raise ValueError("A successfully prefetched request must retain its deadline")
        result[component] = meta
    return result


def _capture_report_impl(runner: Any, battle_id: int, out_dir: Path,
                   wait_seconds: float = 20.0, poll_seconds: float = 0.5,
                   direct: bool = False, expected_identity: dict[str, Any] | None = None,
                   prefetched_requests: dict | None = None, request_missing: bool = True,
                   _audit: _ComponentAudit | None = None) -> dict[str, Any]:
    """Retain partial data and expose component errors for bounded retries."""
    if not math.isfinite(wait_seconds) or wait_seconds < 0 or not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("wait/poll must be finite, with wait >= 0 and poll > 0")
    battle_id = validate_battle_id(battle_id)
    prefetched = _validate_prefetched_requests(battle_id, prefetched_requests)
    if not isinstance(request_missing, bool):
        raise ValueError("request_missing must be a boolean")
    out_dir = safe_output_path(out_dir)
    rate_owner = runner if hasattr(runner, "__dict__") else SimpleNamespace()
    if expected_identity is not None:
        runner = IdentityGuardRunner(runner, expected_identity)
    if _audit is not None:
        _audit.runner = runner
        _audit.out_dir = out_dir
        _audit.expected_identity = expected_identity
    requested: dict[str, Any] = {}
    cache_checkpoints: list[str] = []
    status: dict[str, Any] | None = None
    for name in ("all", "detail", "statics", "snapshots"):
        prior = prefetched.get(name)
        if not request_missing:
            requested[name] = dict(prior) if prior else {"requested": False}
            continue
        try:
            # Each status read (including wait_for's final read) describes all
            # components. Reuse it while this battle/account stays unchanged.
            if status is None:
                status = read_status(runner, battle_id, direct)
            if status[name]["complete"]:
                requested[name] = dict(prior) if prior else {"requested": False, "cached": True}
                continue
            if prior and prior["requested"]:
                requested[name] = dict(prior)
                if prior.get("error"):
                    continue
                remaining = max(0.0, prior["deadline"] - time.monotonic())
                ok, status = wait_for(runner, battle_id, name, remaining, poll_seconds, direct)
                if not ok:
                    requested[name]["error"] = "timeout"
                continue
            rates = next(iter(prefetched.values())) if prefetched else {"min_request_interval": 1.0, "min_dispatch_interval": 1.0}
            if _audit is not None:
                _audit.ensure()
            token = _audit.token if _audit is not None and _audit.active else None
            attempt = {"requested": True}
            requested[name] = attempt
            # The original RPC clears an expired partial cache before sending.
            # Preserve it first even when this initial call will not be blocked.
            # No cache is modified or manufactured by the exporter.
            if status[name].get("partialAvailable") or (
                    name == "snapshots" and status[name].get("available") and not status[name].get("complete")):
                checkpoint = _checkpoint_before_cache_retry(runner, battle_id, out_dir, direct, token, expected_identity)
                cache_checkpoints.append(checkpoint)
                attempt["cache_checkpoint_path"] = checkpoint
            _dispatch_component(runner, battle_id, name, rate_owner=rate_owner,
                                min_request_interval=rates["min_request_interval"],
                                min_dispatch_interval=rates["min_dispatch_interval"], direct=direct,
                                audit_token=token, dispatch_metadata=attempt)
            if token is not None and attempt.get("cached_unrefreshable") and attempt.get("dispatched") is False:
                attempt["error"] = "client_cached_incomplete_component"
                continue
            if token is not None and attempt.get("cache_suppressed") and attempt.get("dispatched") is False:
                if not attempt.get("cache_checkpoint_path"):
                    checkpoint = _checkpoint_before_cache_retry(runner, battle_id, out_dir, direct, token, expected_identity)
                    cache_checkpoints.append(checkpoint)
                    attempt["cache_checkpoint_path"] = checkpoint
                ready, status = _wait_natural_expiry(runner, battle_id, name, wait_seconds, poll_seconds, direct, token)
                if ready is True:
                    continue
                if ready is not False or wait_seconds == 0:
                    observed = response_audit.coverage(name, status.get("requestAudit"))
                    attempt["error"] = observed.get("error") or "partial_cache_wait_timeout"
                    continue
                # More fragments may have arrived while the cache aged. Save
                # those too immediately before its original RPC clears them.
                if status[name].get("partialAvailable", status[name].get("available", False)):
                    checkpoint = _checkpoint_before_cache_retry(runner, battle_id, out_dir, direct, token, expected_identity)
                    cache_checkpoints.append(checkpoint)
                    attempt["cache_checkpoint_path"] = checkpoint
                first_attempt = dict(attempt)
                attempt = {"requested": True, "natural_expiry_retry": True,
                           "retryHistory": [first_attempt], "cache_checkpoint_path": first_attempt["cache_checkpoint_path"]}
                requested[name] = attempt
                _dispatch_component(runner, battle_id, name, rate_owner=rate_owner,
                                    min_request_interval=rates["min_request_interval"],
                                    min_dispatch_interval=rates["min_dispatch_interval"], direct=direct,
                                    audit_token=token, dispatch_metadata=attempt)
                if attempt.get("dispatched") is False:
                    attempt["error"] = "client_suppressed_retry"
                    continue
            if token is not None:
                ok, status, error = _wait_observed(runner, battle_id, name, wait_seconds, poll_seconds, direct, token)
            else:
                ok, status = wait_for(runner, battle_id, name, wait_seconds, poll_seconds, direct)
                error = "timeout" if not ok else None
            if not ok:
                requested[name]["error"] = error or "timeout"
        except RuntimeBridgeUnavailable:
            raise
        except Exception as exc:
            requested[name] = {**requested.get(name, prior or {}), "requested": True, "error": str(exc)}
            # An interrupted request may have changed caches before failing.
            # Recheck them before deciding whether to request the next part.
            status = None
    token = _audit.token if _audit is not None and _audit.active else None
    result = run_checked(runner, export_lua(battle_id, audit_token=token), timeout=45.0, direct=direct)
    envelope = None if result.startswith("not_found ") else decode_envelope(result)
    if envelope is not None and _audit is not None:
        _audit.persist_retained(envelope.get("retainedRecords", []))
        cache_checkpoints.extend(_audit.saved_paths)
    if result.startswith("not_found ") or (envelope is not None and envelope["payload"].get("notFound") is True):
        status = (read_status(runner, battle_id, direct, audit_token=token)
                  if token is not None else read_status(runner, battle_id, direct))
        missing = {"battleID": battle_id, "exported": False, "complete": False, "error": f"not_found {battle_id}",
                   "componentStatus": {name: {**status[name], **requested.get(name, {})} for name in COMPONENTS}}
        if "requestAudit" in status:
            missing["requestAudit"] = status["requestAudit"]
        missing["cacheCheckpointPaths"] = list(dict.fromkeys(cache_checkpoints))
        return missing
    payload = envelope["payload"]
    if str(payload.get("battleID")) != str(battle_id):
        raise ValueError("Runtime export returned a different battle ID")
    if prefetched:
        payload["prefetchRequests"] = prefetched
    if not request_missing:
        payload["captureMode"] = "export_only"
    # Completeness refers to this exported snapshot, not caches that may have
    # arrived between serialization and a later status poll.
    captured = payload.get("components", {})
    captured = captured if isinstance(captured, dict) else {}
    status = {}
    for name in COMPONENTS:
        value = captured.get(name)
        item = {"available": value is not None, "complete": isinstance(value, (dict, list))}
        if name in ("all", "detail"):
            events = value.get("eventDataList") if isinstance(value, dict) else None
            item["eventCount"] = len(events) if isinstance(events, list) else -1
            item["complete"] = isinstance(events, list)
        elif name == "snapshots":
            item["complete"] = isinstance(value, dict) and value.get("isComplete") is True and isinstance(value.get("battleSnapData"), (dict, list))
        status[name] = item
    component_status = {}
    resolved_requests = []
    for name in COMPONENTS:
        attempt = dict(requested.get(name, {"requested": False}))
        coverage = (response_audit.coverage(name, payload.get("requestAudit"))
                    if token is not None else {})
        # A bounded wait can expire just before a reply arrives while another
        # component is loading. Completeness describes this actual export;
        # retain either local wait timeout as history once its component is
        # demonstrably complete here. An audited request also needs proved
        # response coverage; cached data alone cannot clear a failed wait.
        # Server, page and decoding failures stay active and conservative.
        arrived_complete = status[name]["complete"] and (
            name not in ("all", "detail") or status[name].get("eventCount", 0) > 0)
        response_proved = token is None or coverage.get("complete") is True
        if (attempt.get("error") in ("timeout", "partial_cache_wait_timeout")
                and arrived_complete and response_proved):
            resolved_requests.append({"component": name, **attempt, "resolved": True,
                                      "resolution": "component_complete_in_export"})
            attempt.pop("error")
            attempt["resolvedAfterTimeout"] = True
        component_status[name] = {**status[name], **attempt}
        if token is not None:
            if coverage.get("observed"):
                component_status[name]["responseCoverage"] = coverage
            observed_capability = payload.get("requestAudit", {}).get("capabilities", {}).get(name)
            if coverage.get("complete") is False or (attempt.get("dispatched") is True
                    and observed_capability is True and coverage.get("complete") is not True):
                component_status[name]["complete"] = status[name]["complete"] = False
                component_status[name]["error"] = coverage.get("error") or "response_page_coverage_incomplete"
    payload["componentStatus"] = component_status
    if resolved_requests:
        payload["requestHistory"] = resolved_requests
    payload["serializationIssues"] = envelope.get("serializationIssues", [])
    payload["luaTableKeyTypes"] = envelope.get("luaTableKeyTypes", {})
    issues = payload["serializationIssues"]
    # Empty event history may indicate retention limits. Preserve it, but never
    # infer recovery of the original history from source='all' alone.
    complete = (all(status[name]["complete"] for name in COMPONENTS)
                and not any(item.get("error") for item in component_status.values())
                and not issues and payload.get("eventCount", -1) > 0)
    payload["captureComplete"] = complete
    payload["captureWarnings"] = [] if payload.get("eventCount", -1) > 0 else ["event_history_empty_or_unavailable", "retention_or_cached_history_unverified_no_force_refresh_protocol"]
    if expected_identity is not None:
        payload["runtimeIdentity"] = expected_identity
    path = out_dir / f"battle_{battle_id}_decrypted.json"
    if path.exists():
        previous = path.read_bytes()
        previous_path = out_dir / "captures" / str(battle_id) / (hashlib.sha256(previous).hexdigest() + ".json")
        previous_path.parent.mkdir(parents=True, exist_ok=True)
        if not previous_path.exists():
            # Preserve exact original bytes even if an earlier run never reached
            # database ingestion or used a different JSON layout.
            with previous_path.open("xb") as handle:
                handle.write(previous)
                handle.flush()
                os.fsync(handle.fileno())
    atomic_json(path, payload, compact=True)
    return {"battleID": battle_id, "exported": True, "complete": complete, "path": str(path),
            "eventCount": payload.get("eventCount"), "componentStatus": payload["componentStatus"],
            "serializationIssues": issues, "warnings": payload["captureWarnings"],
            "cacheCheckpointPaths": list(dict.fromkeys(cache_checkpoints))}


def capture_report(runner: Any, battle_id: int, out_dir: Path,
                   wait_seconds: float = 20.0, poll_seconds: float = 0.5,
                   direct: bool = False, expected_identity: dict[str, Any] | None = None,
                   prefetched_requests: dict | None = None, request_missing: bool = True) -> dict[str, Any]:
    audit = _ComponentAudit(runner, battle_id, direct) if response_audit.supported(runner) else None
    primary = None
    result = None
    try:
        result = _capture_report_impl(runner, battle_id, out_dir, wait_seconds, poll_seconds,
                                      direct, expected_identity, prefetched_requests, request_missing, audit)
        return result
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if audit is not None:
            try:
                audit.close()
            except Exception as cleanup_error:
                if primary is None:
                    raise
                if hasattr(primary, "add_note"):
                    primary.add_note(f"Retained originals remain pending after cleanup failure: {cleanup_error}")
            finally:
                if result is not None and audit.saved_paths:
                    result["cacheCheckpointPaths"] = list(dict.fromkeys(
                        result.get("cacheCheckpointPaths", []) + audit.saved_paths))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("battle_ids", nargs="+", type=validate_battle_id)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--wait", type=float, default=20.0)
    parser.add_argument("--poll", type=float, default=0.5)
    parser.add_argument("--basic-on-timeout", action="store_true", help="Compatibility flag: partial data is always retained and labelled.")
    parser.add_argument("--direct", action="store_true")
    args = parser.parse_args()
    args.out_dir = safe_output_path(args.out_dir)
    if not math.isfinite(args.wait) or not math.isfinite(args.poll) or args.wait < 0 or args.poll <= 0:
        parser.error("--wait must be nonnegative and --poll must be positive")
    try:
        from .run_lua_in_game import GameLuaRunner
    except ImportError:
        from run_lua_in_game import GameLuaRunner
    records = []
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with GameLuaRunner(args.pid) as runner:
        for battle_id in dict.fromkeys(args.battle_ids):
            try:
                record = capture_report(runner, battle_id, args.out_dir, args.wait, args.poll, args.direct)
            except Exception as exc:
                record = {"battleID": battle_id, "exported": False, "complete": False, "error": str(exc)}
            records.append(record)
            atomic_json(args.out_dir / "runtime_export_manifest.json", records)
            print(json.dumps(record, ensure_ascii=False))
    if any(not record.get("exported") for record in records):
        raise SystemExit(1)
    if any(not record.get("complete") for record in records):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
