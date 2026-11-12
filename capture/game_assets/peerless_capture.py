"""Read the loaded client's own peerless verdict alongside a received page.

This never requests a report, requires a module, edits a protocol row, or uses
an assumed threshold.  An unavailable current helper stays explicitly unknown.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any


EVALUATOR = "client_BattleReportHelper.IsLegend"

class PeerlessPageResponse(dict):
    """Keep a local sidecar out of the server's response-field namespace."""

    def __init__(self, response, client_peerless):
        super().__init__(response)
        self.client_peerless = client_peerless


def page_evaluation_lua() -> str:
    """Evaluate root-block metadata inside the existing page poll's Lua call."""
    return r'''
do
  local response=type(value)=='table' and value.response or nil
  local rows=type(response)=='table' and response.blockInfoList or nil
  if type(rows)=='table' and value.clientProcessingComplete==true then
    local function finite(v)
      return type(v)=='number' and v==v and v~=math.huge and v~=-math.huge
    end
    local function positive_id(v)
      if finite(v) then return v>0 and v%1==0 end
      if type(v)=='string' and v:match('^%d+$') then
        local number=tonumber(v)
        return finite(number) and number>0 and number%1==0
      end
      return false
    end
    local function config_number(name)
      local entry=type(game_config_battle_config)=='table' and game_config_battle_config[name] or nil
      if type(entry)~='table' or (type(entry.content)~='number' and type(entry.content)~='string') then return nil end
      local number=tonumber(entry.content)
      if finite(number) then return number end
    end
    local enums=type(CommonConst)=='table' and CommonConst.BattleContinousReportType or nil
    local normal=type(enums)=='table' and enums.Normal or nil
    local barbarian=type(enums)=='table' and enums.PVBarbarianArmy or nil
    if not finite(normal) then normal=nil end
    if not finite(barbarian) then barbarian=nil end
    local validEnums=finite(normal) and finite(barbarian) and normal~=barbarian
    local bannerBattles=type(ClientConst)=='table' and ClientConst.BattleHFMinBattleNum or nil
    if not finite(bannerBattles) then bannerBattles=nil end
    local rule={normalType=normal,pvBarbarianArmyType=barbarian,
      maxWarContribute=config_number('battle_broadcast_max_war_contribute'),
      maxGame=config_number('battle_broadcast_max_game'),
      bannerMinFeat=config_number('killstreaks_banner_min_feat'),
      bannerMinBattles=bannerBattles}
    local loaded=type(package)=='table' and package.loaded or nil
    local helper=type(loaded)=='table' and loaded['common_lib.battlelog_lib.BattleReportHelper'] or nil
    local fn=type(helper)=='table' and helper.IsLegend or nil
    local evaluations={}
    local evaluator={version=1,name='client_BattleReportHelper.IsLegend',rule=rule}
    local evaluatorRecorded=false
    for _,row in ipairs(rows) do
      local other=type(row)=='table' and row.otherInfo or nil
      local continuous=type(other)=='table' and other.continuousInfo or nil
      if type(continuous)=='table' then
        if not evaluatorRecorded then
          evaluatorRecorded=true
          if type(fn)=='function' then
            if type(debug)=='table' and type(debug.getinfo)=='function' then
              local ok,info=pcall(debug.getinfo,fn,'S')
              if ok and type(info)=='table' then
                evaluator.source={source=info.source,linedefined=info.linedefined,lastlinedefined=info.lastlinedefined}
              end
            end
            if type(string.dump)=='function' then
              local ok,bytecode=pcall(string.dump,fn)
              if ok then evaluator.function_bytecode=bytecode end
            end
          end
        end
        local combat=type(row.combatInfo)=='table' and row.combatInfo or nil
        local result={version=1,evaluator='client_BattleReportHelper.IsLegend',
          battleID=combat and combat.battleID or nil,avatarId=continuous.avatarId,
          isMuti=continuous.isMuti,zgNum=continuous.zgNum,winBattleNum=continuous.winBattleNum,
          endTimeStamp=combat and combat.endTimeStampTime or nil,
          blockHash=row.blockHash,status='unavailable',category='unknown',category_state='missing_enum'}
        local kind=continuous.continuousInfoType
        if kind==nil and validEnums then kind=normal;result.continuousInfoTypeDefaulted=true end
        result.continuousInfoType=kind
        if validEnums then
          result.category_state='runtime_enum'
          if kind==normal then result.category='normal'
          elseif kind==barbarian then result.category='barbarian'
          else result.category_state='unknown_value' end
        end
        if result.category=='barbarian' then
          result.status='excluded';result.reason='barbarian_legend_is_not_peerless'
        elseif continuous.isMuti==false or continuous.isMuti==nil then
          result.status='excluded';result.reason='not_multi_battle_summary'
        elseif continuous.isMuti~=true then
          result.status='invalid';result.reason='isMuti_is_not_boolean'
        elseif result.category~='normal' then
          result.reason='continuous_type_unverified'
        elseif not positive_id(result.battleID) or not positive_id(result.avatarId) then
          result.status='invalid';result.reason='battle_or_player_id_invalid'
        elseif not finite(result.zgNum) or result.zgNum<0 or not finite(result.winBattleNum)
            or result.winBattleNum<0 or result.winBattleNum%1~=0 then
          result.status='invalid';result.reason='continuous_settlement_values_invalid'
        elseif not finite(result.endTimeStamp) or result.endTimeStamp<=0 then
          -- Missing historical time cannot safely use today's date at the
          -- client's historical-rule switch, nor introduce a volatile clock.
          result.reason='battle_end_time_unavailable'
        elseif type(fn)~='function' then
          result.reason='current_client_helper_not_loaded'
        else
          local ok,legend=pcall(fn,result.zgNum,result.winBattleNum,result.endTimeStamp)
          if ok and type(legend)=='boolean' then
            result.status='evaluated';result.legend=legend
          else
            result.reason=ok and 'client_helper_returned_nonboolean' or 'client_helper_failed'
            if not ok then result.error=tostring(legend) end
          end
        end
        evaluations[#evaluations+1]=result
      end
    end
    if #evaluations>0 then
      value.clientPeerlessEvaluations=evaluations
      value.clientPeerlessEvaluator=evaluator
    end
  end
end
'''


def evaluation_metadata(value: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach stable provenance while leaving the complete envelope unchanged."""
    items = value.get("clientPeerlessEvaluations")
    if not isinstance(items, list):
        return []
    evaluator = value.get("clientPeerlessEvaluator")
    evaluator = evaluator if isinstance(evaluator, dict) else {}
    raw = evaluator.get("function_bytecode")
    binary = None
    if isinstance(raw, str):
        binary = raw.encode("utf-8")
    elif isinstance(raw, dict) and set(raw) == {"$luaBytesHex"}:
        try:
            binary = bytes.fromhex(raw["$luaBytesHex"])
        except (ValueError, TypeError):
            pass
    digest = hashlib.sha256(binary).hexdigest() if binary is not None else None
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        copied = deepcopy(item)
        if isinstance(evaluator.get("rule"), dict):
            copied["rule"] = deepcopy(evaluator["rule"])
        if isinstance(evaluator.get("source"), dict):
            copied["helper_source"] = deepcopy(evaluator["source"])
        if digest is not None:
            copied["helper_bytecode_sha256"] = digest
        result.append(copied)
    return result
