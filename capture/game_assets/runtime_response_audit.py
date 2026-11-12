"""Short-lived, target-bound telemetry for client battle component requests.

This module does not send packets or remove caches. Lua hooks retain their
original own attributes and are restored only while our exact wrapper is live.
"""
from __future__ import annotations

import os
import uuid


OWNER = f"{os.getpid()}:{uuid.uuid4().hex}"
METHODS = {
    "all": "RPCGetAllCombatInfo", "detail": "RPCGetDetailCombatInfo",
    "statics": "RPCGetStaticsCombatInfo", "snapshots": "RPCGetHeroBattleSnapRecords",
}


# This journal deliberately outlives the temporary observer. Entries are
# immutable JSON strings, not client-owned table references. Only a durable
# Python-side write followed by an explicit acknowledgement may remove one.
IDENTITY_LUA = r'''
local function retainedIdentity()
  local cp=ClientProxy
  if type(cp)~='table' or cp.avatarId==nil or type(cp.GetServerId)~='function' or G_SCENARIO_ID==nil then return nil end
  local ok,server=pcall(cp.GetServerId,cp)
  if not ok or server==nil then return nil end
  return {avatarId=tostring(cp.avatarId),serverId=tostring(server),scenarioId=tostring(G_SCENARIO_ID)}
end
local function retainedSameIdentity(a,b)
  return type(a)=='table' and type(b)=='table' and a.avatarId==b.avatarId
    and a.serverId==b.serverId and a.scenarioId==b.scenarioId
end
'''


def retained_lua(battle_id: int) -> str:
    """Read matching bodies only at export/cleanup, never at status polling."""
    return IDENTITY_LUA + f'''
local retainedRecords={{}}
local retainedJournal=__codexBattleReportRetained
local retainedScope=retainedIdentity()
if type(retainedJournal)=='table' and type(retainedJournal.entries)=='table' and retainedScope then
  for ticket,entry in pairs(retainedJournal.entries) do
    if tostring(entry.battleID)==tostring({battle_id}) and retainedSameIdentity(entry.identity,retainedScope) then
      retainedRecords[#retainedRecords+1]={{ticket=ticket,battleID=entry.battleID,identity=entry.identity,raw=entry.raw}}
    end
  end
  table.sort(retainedRecords,function(a,b) return a.ticket<b.ticket end)
end
'''


def release_owned_lua(owner: str, battle_id: int, quote) -> str:
    """A recovered full-cache fast path can detach our previously lost hook."""
    return IDENTITY_LUA + f'''
local previousObserver=__codexBattleReportAudit
if previousObserver and previousObserver.owner=={quote(owner)}
  and tostring(previousObserver.battleID)==tostring({battle_id})
  and retainedSameIdentity(previousObserver.identity,retainedIdentity()) then
  previousObserver.closed=true
  for _,hook in ipairs(previousObserver.hooks or {{}}) do
    if hook.object[hook.name]==hook.wrapper then hook.object[hook.name]=hook.own end
  end
  __codexBattleReportAudit=nil
end
'''


def acknowledge_lua(battle_id: int, identity: dict, tickets: list[int], quote) -> str:
    scope = '{' + ','.join(f'{key}={quote(str(identity[key]))}' for key in ('avatarId', 'serverId', 'scenarioId')) + '}'
    selected = '{' + ','.join(str(ticket) for ticket in tickets) + '}'
    return IDENTITY_LUA + f'''
local scope={scope}
if not retainedSameIdentity(scope,retainedIdentity()) then error('Retained response account changed; originals remain pending') end
local journal=__codexBattleReportRetained
local count=0
if type(journal)=='table' and type(journal.entries)=='table' then
  for _,ticket in ipairs({selected}) do
    local entry=journal.entries[ticket]
    if entry and tostring(entry.battleID)==tostring({battle_id}) and retainedSameIdentity(entry.identity,scope) then
      journal.entries[ticket]=nil
      if journal.byRaw[entry.raw]==ticket then journal.byRaw[entry.raw]=nil end
      count=count+1
    end
  end
end
return envelope({{acknowledged=count}})
'''


def supported(runner) -> bool:
    """Existing custom runners remain usable without pretending to observe RPCs."""
    seen = set()
    for _ in range(8):
        if id(runner) in seen:
            break
        seen.add(id(runner))
        attributes = vars(runner) if hasattr(runner, "__dict__") else {}
        if callable(getattr(type(runner), "info", None)) or attributes.get("supports_response_audit") is True:
            return True
        nested = attributes.get("runner")
        if nested is None:
            break
        runner = nested
    return False


def install_lua(token: str, owner: str, battle_id: int, quote) -> str:
    return IDENTITY_LUA + f'''
local cp = ClientProxy
if type(cp) ~= 'table' then return envelope({{error='Component audit requires ClientProxy'}}) end
local old = __codexBattleReportAudit
local function restore(st)
  st.closed=true
  for _,h in ipairs(st.hooks or {{}}) do
    if h.object[h.name] == h.wrapper then h.object[h.name] = h.own end
  end
end
if old then
  local age=os.time()-(old.lastSeen or os.time())
  if old.owner ~= {quote(owner)} and age>=-120 and age<=120 then
    return envelope({{error='Another component observer is active'}})
  end
  restore(old)
  if __codexBattleReportAudit==old then __codexBattleReportAudit=nil end
end
local st = {{owner={quote(owner)},token={quote(token)},battleID={battle_id},proxy=cp,
 hooks={{}},responses={{}},notices={{}},dispatches={{}},capabilities={{}},lastSeen=os.time(),seq=0}}
st.identity=retainedIdentity()
if not st.identity then return envelope({{error='Component observer account identity unavailable'}}) end
__codexBattleReportAudit = st
local journal=__codexBattleReportRetained
if type(journal)~='table' then
  journal={{entries={{}},byRaw={{}},seq=0}};__codexBattleReportRetained=journal
end
local function cachedFragment(self,part,id)
  if part=='snapshots' then return self.allBattleRecordInfo and self.allBattleRecordInfo[id] end
  if part=='all' then return self._allCombatSlices and self._allCombatSlices[id] end
  if part=='detail' then return self._detailCombatSlices and self._detailCombatSlices[id] end
end
-- Serializing before handing a table to the original handler also protects
-- against its in-place page merge, decoder mutation and error-driven clearing.
st.preserve=function(part,phase,proto,cache)
  if st.closed or not retainedSameIdentity(st.identity,retainedIdentity()) then return end
  local components,fragments={{}},{{}}
  if cache~=nil then
    if part=='snapshots' then components.snapshots=cache else fragments[part]=cache end
  end
  local source=phase=='response' and 'response_fragments' or 'cache_fragments'
  local body={{captureVersion=2,battleID=st.battleID,source=source,eventCount=-1,
    captureKind=phase=='response' and 'response_before_handler' or 'cache_at_dispatch',
    captureComplete=false,captureWarnings={{'unmerged_original_fragments_not_a_complete_report'}},
    runtimeIdentity=st.identity,components=components,
    componentFragments=next(fragments) and fragments or nil,
    responseFragments=proto and {{[part]=proto}} or nil,
    data={{component=part,phase=phase,response=proto,cache=cache}}}}
  local raw=isolated_envelope(body)
  if journal.byRaw[raw] then return end
  journal.seq=journal.seq+1
  local ticket=journal.seq
  journal.entries[ticket]={{battleID=st.battleID,identity=st.identity,raw=raw}}
  journal.byRaw[raw]=ticket
end
st.initialCaches={{snapshots=cp.allBattleRecordInfo and cp.allBattleRecordInfo[{battle_id}],
 all=cp._allCombatSlices and cp._allCombatSlices[{battle_id}],detail=cp._detailCombatSlices and cp._detailCombatSlices[{battle_id}]}}
st.initialIndices={{}}
for part,cache in pairs(st.initialCaches) do
  local indices={{}};st.initialIndices[part]=indices
  local parts=type(cache)=='table' and (part=='snapshots' and cache.pages or cache)
  if type(parts)=='table' then for index,_ in pairs(parts) do
    if type(index)=='number' and index>=1 and index%1==0 and index<=10000 then indices[#indices+1]=index end
  end end
end
local methods = {{all='RPCGetAllCombatInfo',detail='RPCGetDetailCombatInfo',
 statics='RPCGetStaticsCombatInfo',snapshots='RPCGetHeroBattleSnapRecords'}}
local reverse = {{}}
for component,method in pairs(methods) do reverse[method]=component end
local function install(object,name,build)
  local original=object and object[name]
  if type(original) ~= 'function' then return false end
  local wrapper=build(original)
  st.hooks[#st.hooks+1]={{object=object,name=name,own=rawget(object,name),wrapper=wrapper}}
  object[name]=wrapper
  return true
end
local function target(proto)
  if type(proto) ~= 'table' then return nil end
  local id=proto.battleID
  if (id==nil or id==0) and type(proto.combatInfo)=='table' then id=proto.combatInfo.battleID end
  return id
end
for component,method in pairs(methods) do
  local part=component
  st.capabilities[part]=install(cp,method..'Response',function(original)
    return function(self,msgId,proto,...)
      local id=target(proto)
      if not st.closed and type(proto)=='table' then
        local code=proto.code
        local success
        if code==nil then success=true
        elseif ErrCode and ErrCode.SUCCESS~=nil then success=code==ErrCode.SUCCESS end
        local row={{component=part,battleID=id,code=code,success=success,
          page=proto.page,maxPage=proto.maxPage,sliceIndex=proto.sliceIndex,sliceCount=proto.sliceCount,tp=proto.tp}}
        if part=='statics' then
          -- ImpBattle rejects a missing payload or fewer than four bytes
          -- before Stream.Loads; such a reply cannot populate its cache.
          if not proto.binStatics then
            row.emptyComponent=true;row.emptyReason='missing_bin_statics'
          elseif type(proto.binStatics)=='string' then
            row.binStaticsBytes=#proto.binStatics
            if row.binStaticsBytes<4 then
              row.emptyComponent=true;row.emptyReason='short_bin_statics'
            end
          end
        end
        local submitted=st.dispatches[part]
        row.generation=submitted and (st.sendPacketObserved and submitted.sendPacket or submitted.callServer) or 0
        if code~=nil and type(GetErrCodeByNum)=='function' then
          local ok,name=pcall(GetErrCodeByNum,code);if ok then row.codeName=name end
        end
        if tostring(id)==tostring(st.battleID) then
          local cache=cachedFragment(self,part,id)
          -- Normal pages preserve only the new raw proto, avoiding repeatedly
          -- serializing an ever-growing cache. Error/invalid responses may
          -- clear earlier pages, so freeze that cache in the same Lua frame.
          local index,total=proto.page,proto.maxPage
          if part=='all' or part=='detail' then index,total=proto.sliceIndex or 1,proto.sliceCount or 1 end
          local invalid=part~='statics' and (type(index)~='number' or type(total)~='number'
            or index<1 or index%1~=0 or total<index or total%1~=0 or total>10000)
          st.preserve(part,'response',proto,(success~=true or invalid) and cache or nil)
          if st.callServerObserved and st.sendPacketObserved and cache~=nil and cache==st.initialCaches[part]
            and (not submitted or (submitted.sendPacket==0 and submitted.callServer==0)) then
            row.cachedIndices=st.initialIndices[part]
          end
          st.seq=st.seq+1;row.seq=st.seq
          if #st.responses<512 then st.responses[#st.responses+1]=row else st.overflow=true end
        elseif not success and (id==nil or id==0) then
          if #st.notices<64 then st.notices[#st.notices+1]=row else st.noticeOverflow=true end
        end
      end
      return original(self,msgId,proto,...)
    end
  end)
end
st.callServerObserved=install(cp,'CallServer',function(original)
 return function(self,method,proto,...)
  local part=reverse[method]
  if not st.closed and part and tostring(target(proto))==tostring(st.battleID) then
    local d=st.dispatches[part] or {{callServer=0,sendPacket=0}};st.dispatches[part]=d;d.callServer=d.callServer+1
  end
  return original(self,method,proto,...)
 end
end)
st.sendPacketObserved=install(NetMgr,'SendPacket',function(original)
 return function(self,method,proto,...)
  local part=reverse[method]
  if not st.closed and part and tostring(target(proto))==tostring(st.battleID) then
    local d=st.dispatches[part] or {{callServer=0,sendPacket=0}};st.dispatches[part]=d;d.sendPacket=d.sendPacket+1
  end
  return original(self,method,proto,...)
 end
end)
return envelope({{installed=true,capabilities=st.capabilities,identity=st.identity}})
'''


def status_lua(token: str, quote) -> str:
    return f'''
local st=__codexBattleReportAudit
if st and st.token=={quote(token)} and st.proxy==ClientProxy then
  st.lastSeen=os.time()
  status.requestAudit={{battleID=st.battleID,responses=st.responses,notices=st.notices,capabilities=st.capabilities,
    dispatches=st.dispatches,callServerObserved=st.callServerObserved,sendPacketObserved=st.sendPacketObserved,
    overflow=st.overflow or false,noticeOverflow=st.noticeOverflow or false,seq=st.seq}}
end
'''


def close_lua(token: str, quote, battle_id: int) -> str:
    return f'''
local st=__codexBattleReportAudit
if st and st.token=={quote(token)} then
  st.closed=true
  for _,h in ipairs(st.hooks or {{}}) do
    if h.object[h.name]==h.wrapper then h.object[h.name]=h.own end
  end
  __codexBattleReportAudit=nil
end
''' + retained_lua(battle_id) + 'return envelope({detached=true},retainedRecords)\n'


def dispatch_lua(token: str, battle_id: int, component: str, method: str, arguments: str, quote) -> str:
    """Observe submission and fresh partial suppression in the original RPC."""
    return f'''
local st=__codexBattleReportAudit
if not st or st.token~={quote(token)} or st.proxy~=ClientProxy then error('Component observer changed') end
local before=st.dispatches[{quote(component)}] or {{callServer=0,sendPacket=0}}
local beforeCall,beforeSend=before.callServer,before.sendPacket
local part={quote(component)}
local info
if part=='snapshots' then info=ClientProxy.allBattleRecordInfo and ClientProxy.allBattleRecordInfo[{battle_id}]
elseif part=='all' then info=ClientProxy._allCombatSlices and ClientProxy._allCombatSlices[{battle_id}]
elseif part=='detail' then info=ClientProxy._detailCombatSlices and ClientProxy._detailCombatSlices[{battle_id}] end
local age,ttl
if type(info)=='table' and (part~='snapshots' or info.isComplete~=true)
 and GameTime and type(GameTime.svrTime)=='number' and ClientConst and type(ClientConst.BattleOutOfTime)=='number' then
  local started=type(info.time)=='number' and info.time or 0
  age=GameTime.svrTime-started;ttl=ClientConst.BattleOutOfTime
end
local invalidCache=false
if part=='snapshots' then
  invalidCache=type(info)=='table' and info.isComplete==true and type(info.battleSnapData)~='table'
elseif part=='all' or part=='detail' then
  local getter=part=='all' and ClientProxy.GetAllCombatInfo or ClientProxy.GetDetailCombatInfo
  local cached=type(getter)=='function' and getter(ClientProxy,{battle_id}) or nil
  invalidCache=not not (cached and (type(cached)~='table' or type(cached.eventDataList)~='table'))
elseif part=='statics' then
  local cached=ClientProxy.reportStaticsInfo and ClientProxy.reportStaticsInfo[{battle_id}]
  invalidCache=not not (cached and type(cached)~='table')
end
-- Pacing and IPC may have allowed more pages to arrive after Python's earlier
-- fsynced checkpoint. Freeze the actual cache immediately before the original
-- RPC is allowed to clear it. No event callback can interleave these steps.
if info~=nil then st.preserve(part,'dispatch',nil,info) end
ClientProxy:{method}({arguments})
local after=st.dispatches[{quote(component)}] or {{callServer=0,sendPacket=0}}
local send=after.sendPacket-beforeSend
local calls=after.callServer-beforeCall
local suppressed=info~=nil and st.callServerObserved and st.sendPacketObserved
 and calls==0 and send==0 and age~=nil and ttl~=nil and age>=0 and age<ttl
local cachedUnrefreshable=invalidCache
 and st.callServerObserved and st.sendPacketObserved and calls==0 and send==0
local result={{requested=true,battleID={battle_id},component={quote(component)},
 callServerCount=calls,sendPacketCount=send,dispatch_observed=st.sendPacketObserved,
 callServer_observed=st.callServerObserved,
 cache_suppressed=not not suppressed,cached_unrefreshable=not not cachedUnrefreshable}}
if st.sendPacketObserved and send>0 then result.dispatched=true
elseif suppressed or cachedUnrefreshable then result.dispatched=false end
if suppressed then result.retry_after=ttl-age end
return envelope(result)
'''


def coverage(component: str, audit: dict | None) -> dict:
    audit = audit or {}
    target = audit.get("battleID")
    rows = [row for row in audit.get("responses", []) if row.get("component") == component
            and (target is None or str(row.get("battleID")) == str(target))]
    historical_count = 0
    dispatches = audit.get("dispatches", {})
    dispatch = dispatches.get(component, {}) if isinstance(dispatches, dict) else {}
    counter = "callServer" if audit.get("sendPacketObserved") is False else "sendPacket"
    generation = dispatch.get(counter) if isinstance(dispatch, dict) else None
    if (isinstance(generation, int) and not isinstance(generation, bool)
            and any("generation" in row for row in rows)):
        current_rows = [row for row in rows if row.get("generation") == generation]
        historical_count = len(rows) - len(current_rows)
        rows = current_rows
    errors = [row for row in rows if row.get("success") is False]
    result = {"observed": bool(rows), "response_count": len(rows), "complete": None}
    if historical_count:
        result.update(historical_response_count=historical_count, generation=generation)
    if errors:
        row = errors[-1]
        result.update(complete=False, error=f"server_rejected:{row.get('codeName') or row.get('code')}", server_error=row)
        return result
    if audit.get("overflow"):
        result.update(complete=False, error="response_observation_overflow")
        return result
    good = [row for row in rows if row.get("success") is True]
    if not good:
        return result
    if component == "statics":
        # An unattributed/global notice or a response from before this send
        # cannot terminate the current target's genuine network wait.
        empty = [row for row in good if row.get("emptyComponent") is True
                 and target is not None and str(row.get("battleID")) == str(target)
                 and isinstance(generation, int) and not isinstance(generation, bool)
                 and generation > 0 and row.get("generation") == generation]
        if empty:
            result.update(complete=False, error="server_empty_component",
                          empty_reason=empty[-1].get("emptyReason"), server_response=empty[-1])
            return result
        result["complete"] = True
        return result
    page, maximum = ("page", "maxPage") if component == "snapshots" else ("sliceIndex", "sliceCount")
    expected, received, invalid = set(), set(), False
    for row in good:
        index, total = row.get(page), row.get(maximum)
        if component != "snapshots":
            index = 1 if index is None else index
            total = 1 if total is None else total
        if (isinstance(index, bool) or not isinstance(index, int) or isinstance(total, bool)
                or not isinstance(total, int) or not 1 <= index <= total <= 10000):
            invalid = True
            continue
        expected.add(total)
        received.add(index)
        for cached in row.get("cachedIndices", []):
            if isinstance(cached, int) and not isinstance(cached, bool) and 1 <= cached <= total:
                received.add(cached)
            else:
                invalid = True
    total = next(iter(expected)) if len(expected) == 1 else None
    missing = [index for index in range(1, total + 1) if index not in received] if total is not None else []
    result.update(expected=total, received=sorted(received), missing=missing,
                  complete=not invalid and total is not None and not missing,
                  invalid_metadata=invalid, conflicting_totals=len(expected) > 1)
    if invalid or len(expected) > 1:
        result["error"] = "invalid_or_conflicting_response_pages"
    return result
