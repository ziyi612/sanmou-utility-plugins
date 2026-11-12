"""Source-backed Chinese explanations of NSLG battle events.

This adds annotations only. Event IDs, indentation and argument values are
always retained; unknown events/arguments are never silently discarded.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

SEMANTICS_VERSION = "event-semantics-2"
HISTORICAL_SOURCE_DATE = "2026-05-25"
DEFAULT_SOURCE_DIR = Path(__file__).resolve().parents[1] / "capture/output/processed/decompiled_lua/battle_focus"

PARAMETER_LABELS = {
    "teamId": "所属部队ID", "heroId": "武将ID", "armyLeaderHeroId": "主将ID", "giverHeroId": "授予战法的武将ID",
    "round": "回合数", "winCampType": "获胜阵营", "campType": "阵营类型",
    "loseTeamId": "战败部队ID", "deadToAliveNum": "复活兵力", "morale": "士气",
    "damageChangePercent": "伤害变化比例", "teamType": "部队类型",
    "formationId": "阵型ID", "changeEnergyNum": "体力变化值", "resultEnergyNum": "变化后体力",
    "countryP": "阵营加成参数", "groupId": "势力ID", "fateId": "缘分ID", "roleId": "职业ID",
    "bookId": "兵书ID", "equipId": "装备ID", "horseId": "坐骑ID", "skillId": "战法ID",
    "changeSoldierNum": "兵力变化量", "resultSoldierNum": "变化后兵力", "woundSoldierNum": "伤兵数量",
    "srcTeamId": "来源部队ID", "srcHeroId": "来源武将ID", "sourceReason": "来源原因编码",
    "targetHeroId": "目标武将ID", "targetTeamId": "目标部队ID", "team2Id": "另一部队ID",
    "buffId": "状态ID", "overHeadInfoType": "飘字信息类型", "damageSourceTeamId": "伤害来源部队ID",
    "damageSourceHeroId": "伤害来源武将ID", "propertyId": "属性ID", "fightPropId": "状态属性ID",
    "changePropertyValue": "属性变化值", "resultPropertyValue": "变化后属性值", "maxValue": "属性上限",
    "dispel": "驱散标记", "ownerHeroId": "效果持有者武将ID", "curNum": "当前层数",
    "totalNum": "总层数", "showType": "层数显示类型", "badlyHurtRatio": "重伤比例原值",
    "weakRatio": "虚弱比例原值", "targetBuffId": "目标状态ID", "isInterrupted": "是否打断标记",
    "applyTeamId": "受作用部队ID", "applyHeroId": "受作用武将ID", "provideTeamId": "提供效果部队ID",
    "provideHeroId": "提供效果武将ID", "resist": "抵御参数", "resistRate": "抵御伤害减免比例",
    "damageType": "伤害类型", "fatal": "会心或奇谋伤害比例", "atkDamage": "攻击伤害值",
    "propId": "功能属性ID", "type": "类型编码", "changeNum": "变化数量", "resultNum": "变化后数量",
    "count": "次数", "beMaskBuffId": "被覆盖状态ID", "changeValue": "状态属性变化值",
    "resultValue": "变化后状态属性值",
}

# Event names are translations of the source enum/decoder action. The original
# localized client template is retained separately, including display tokens.
EVENT_NAMES = {
    "BattleTeamFormat": "参战部队编排", "BattleRound": "回合开始", "BattleStartAct": "武将开始行动",
    "BattleResult": "战斗结果", "BattleDraw": "战斗平局", "BattleDeadToAlive": "兵力复活",
    "BattleRoundActionEnd": "回合行动结束", "BattleMoraleAffect": "士气影响",
    "BattleHeroSolderNumToZero": "武将兵力归零", "BattleFormationAffect": "阵型影响",
    "BattleEnergyBalance": "体力结算", "BattleHeroEnergyCal": "武将体力变化",
    "BattleHeroEnergyBuff": "武将体力状态", "BattleCountryBuff": "势力加成", "BattleRelationBuff": "缘分加成",
    "BattleRoleAffect": "职业影响", "BattleWarBookEffect": "兵书效果", "BattleHeroGetSkill": "武将获得战法",
    "BattleBuildAdd": "建筑加成", "BattleSoldierType": "兵种效果", "BattleDoEquipEffect": "装备效果",
    "BattleBuffTypeRebuild": "重建加成", "BattleBuffTypeUnionStrategy": "同盟计策加成",
    "BattleBuffTypeNpcCityBuild": "城池建筑加成", "BattleDoHorseEffect": "坐骑效果",
    "BattleBuffTypeUnionSkill": "同盟技能加成", "BattleSoldierAdvanceType": "进阶兵种效果",
    "BattleBuffTypeBirthState": "出生州加成", "BattleBuffTypeAcropolis": "卫城加成",
    "BattleBuffTypePolicy": "政策加成", "BattleBuffTypeNationalParliament": "国策加成",
    "BattleBuffTypeImprint": "印记加成", "BattleBuffTypeBirthLevelProtect": "出生等级保护",
    "BattleBuffTypePopularityIndex": "民心加成", "BattleBuffTypeCounterAttackSignal": "反击信号加成",
    "BattleBuffTypeUnionFortressMarch": "同盟要塞行军加成", "BattleBuffTypeJNMJ": "锦囊妙计加成",
    "BattleTheBestOne": "独占鳌头效果", "BattleBuffTypeInfluenceSpecialTalent": "势力专属天赋加成",
    "BattleBaGua": "八卦效果", "BattleDoHeroEnlightenEffect": "武将觉醒效果",
    "BattleHeroProposeSkill": "武将推荐战法效果", "BattleNPCGrowUp": "守军成长效果",
    "BattleBuffTypeUnionFireTower": "同盟烽火台加成", "BattleBuffTypeWeatherSeason": "天气时令加成",
    "BattleBuffTypeWeatherSeasonUnionStrategy": "天气时令同盟计策加成", "BattleBuffTypeUnionRage": "同盟怒气加成",
    "BattleBuffTypeUnionPass": "同盟关隘加成", "BattleBuffTypeCommandPlatform": "指挥台加成",
    "BattleBuffTypePVECrusade": "讨伐加成", "BattleBuffTypeOfficialSeal": "官印加成",
    "BattleUserDefineHero": "自定义武将效果", "BattleUserDefineHeroExtraSkill": "自定义武将额外战法",
    "BattleBuffTypeSpecialEvent": "特殊事件加成", "BattleBuffTypeMilitaryCampEnhance": "军营增强效果",
    "BattleBuffTypeCustomHero": "自定义武将加成", "BattleBuffTypeStrongholdAssault": "据点突袭加成",
    "BattleHeroSpeedReady": "武将行动次序", "BattleS12HeroTalentSkill": "赛季武将天赋战法",
    "BattleS13SuperSkill": "赛季强化战法", "BattleRecruitTalentedPeopleSkill": "招贤纳士战法",
    "PrepareSkillInitiate": "准备发动战法", "SkillLaunch": "发动战法", "RecoverHeroSoldierNum": "恢复武将兵力",
    "SoldierNumLossByHeroSkill": "战法造成兵力损失", "NormalAttack": "普通攻击",
    "SoldierNumLossNormal": "普通攻击兵力损失", "BuffAdd": "施加状态", "BuffRefresh": "刷新状态",
    "DoTargetSkillTargetBuff": "战法状态生效", "SoldierNumLossByBuff": "状态造成兵力损失",
    "HeroPorpertyIncrease": "武将属性提高", "HeroPorpertyDecrease": "武将属性降低", "BuffGone": "状态消失",
    "BuffTakeEffectFailedByProb": "状态概率判定未生效", "TriggerBuffEffect": "触发状态效果", "BuffMerge": "状态层数合并",
    "EvadeDamageFailed": "规避伤害失败", "EvadeDamageSuccess": "规避伤害成功", "ForbidAction": "无法行动",
    "ForbidAttack": "无法普通攻击", "ForbidActiveSkill": "无法发动主动战法", "ForbidHeal": "无法治疗",
    "ForbidDamage": "无法造成伤害", "ConfuseAtk": "混乱攻击", "ClarityBuffNoEffect": "清醒使状态无效",
    "AttackDouble": "连击效果", "SkillInterrupted": "战法被打断", "LockNormalAtk": "施加锁定普攻效果",
    "DoLockNormalAtk": "锁定普攻生效", "UnderNormalAtk": "施加承受普攻效果", "DoUnderNormalAtk": "承受普攻效果生效",
    "ShareDamage": "分担伤害", "DoShareDamage": "分担伤害生效", "DoProvideShareDamage": "提供分担伤害效果",
    "ResistEffect": "抵御效果", "FatalEffect": "会心或奇谋效果", "SkillLaunchFailed": "战法发动失败",
    "NormalAttackDouble": "追加普通攻击", "UnMatchedState": "不匹配状态", "CauseFuncProp": "触发功能属性",
    "FightBack": "反击", "SkipSkillPrepareRound": "跳过战法准备回合", "ArmyShaked": "部队震慑",
    "SkillDoubleLaunch": "战法重复发动", "SkillTripleLaunch": "战法三次发动",
    "HeroPorpertySetIncrease": "设置并提高武将属性", "HeroPorpertySetDecrease": "设置并降低武将属性",
    "UnderDamage": "承受伤害效果", "NoMissResist": "必中抵御效果", "NoMissDodge": "必中规避效果",
    "HeroPropertyUnChanged": "武将属性未变化", "DamageChange": "伤害数值变化", "EvadeDamage2Success": "规避伤害效果成功",
    "SkillNoTargets": "战法无可选目标", "BuffBeMask": "状态被覆盖", "BuffTransfer": "状态转移",
    "BuffAttrChange": "状态属性变化", "BattleGVGPretrait": "国战预备特性", "BattleBuffTypeNewNationalParliament": "新国策加成",
    "BattleRecruitTalentedExtraAttrBuff": "招贤额外属性加成", "BattleBuffTypeRecruitTalentedPeople": "招贤纳士加成",
    "BattleBuffTypeStrongholdEffectBuff": "据点效果加成", "BattleFightBoat": "战船效果",
    "BattleBuffTypeIronChain": "铁索效果", "EmptyTagBuffAdd": "施加无标签状态",
    "BattleS16BondSKill": "星命额外效果", "BattleBuffTypeTianJiBox": "天机盒额外效果",
    "BattleSoulFormatSkill": "阵型改造额外效果", "BattleHeroGrantedSkill": "武将获授战法",
    "BattleHeroGrantSkillRepeated": "授予战法失败（已携带相同战法）", "ShareTroopNumChange": "分兵效果",
    "TroopNumAdd": "兵力增加", "TroopNumReduce": "兵力减少", "ReAction": "武将再动",
    "BattleBuffTypeHeavyWall": "高墙重垣效果", "ClientHighLightDamage": "客户端高亮伤害标记",
    "ClientHighLightHeal": "客户端高亮治疗标记",
}

_HERO_FIELDS = {"heroId", "armyLeaderHeroId", "srcHeroId", "targetHeroId", "ownerHeroId", "damageSourceHeroId", "applyHeroId", "provideHeroId", "giverHeroId"}
_TEAM_FIELDS = {"teamId", "loseTeamId", "srcTeamId", "targetTeamId", "team2Id", "damageSourceTeamId", "applyTeamId", "provideTeamId"}
_ID_KINDS = {"skillId": "skill", "buffId": "buff", "targetBuffId": "buff", "beMaskBuffId": "buff",
             "propertyId": "property", "fightPropId": "buff_property", "formationId": "formation", "groupId": "group",
             "fateId": "fate", "roleId": "role", "bookId": "warbook", "equipId": "equipment", "horseId": "horse", "propId": "property"}
_SCALED_PROPERTIES = {"HeroPorpertyIncrease", "HeroPorpertyDecrease", "HeroPorpertySetIncrease", "HeroPorpertySetDecrease", "HeroPropertyUnChanged"}


def _unescape(value: str) -> str:
    escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}
    return re.sub(r"\\(\d{1,3}|.)", lambda m: chr(int(m[1])) if m[1].isdigit() else escapes.get(m[1], "\\" + m[1]), value)


def _table(text: str, marker: str) -> tuple[str, int]:
    start = text.index("{", text.index(marker))
    depth, quote, escape, comment = 0, None, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if comment:
            if ch == "\n": comment = False
            continue
        if quote:
            if escape: escape = False
            elif ch == "\\": escape = True
            elif ch == quote: quote = None
            continue
        if text[i:i + 2] == "--": comment = True; continue
        if ch in "\"'": quote = ch
        elif ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0: return text[start:i + 1], start
    raise ValueError(f"Unterminated Lua table after {marker}")


def _find(source_dir: Path, pattern: str) -> Path:
    values = sorted(source_dir.glob(pattern))
    if len(values) != 1: raise ValueError(f"Expected one {pattern} source, found {len(values)}")
    return values[0]


def load_event_catalog(source_dir: str | Path = DEFAULT_SOURCE_DIR, runtime_catalog: dict | str | Path | None = None) -> dict:
    """Load enum, positional schema and Chinese templates from client sources.

    A runtime export may supply ET, ETARGS, templates, constants and source.
    Runtime schemas replace historical schemas and their validation is explicit.
    """
    directory = Path(source_dir)
    definition = _find(directory, "*_BattleDef_*.lua")
    parser = _find(directory, "*_BattleLogParse_*.lua")
    logger = _find(directory, "*_BattleLog_*.lua")
    records = _find(directory, "*_battle_record_*.lua")
    definition_text, parser_text = definition.read_text(encoding="utf-8"), parser.read_text(encoding="utf-8")
    enum_text, enum_start = _table(definition_text, "battleLogDef.ET =")
    enum = {name: int(number) for name, number in re.findall(r"\b(\w+)\s*=\s*(\d+)", enum_text)}
    argument_text, argument_start = _table(definition_text, "battleLogDef.ETARGS =")
    arguments, argument_lines = {}, {}
    for match in re.finditer(r"\[battleLogDef\.ET\.(\w+)\]\s*=\s*\{(.*?)\}", argument_text, re.S):
        symbol = match[1]
        if symbol not in enum: continue
        arguments[str(enum[symbol])] = re.findall(r'"([^"\n]+)"', match[2])
        argument_lines[symbol] = definition_text.count("\n", 0, argument_start + match.start()) + 1
    templates, hashes = {}, {}
    for match in re.finditer(r"battle_record_battle_record\[(\d+)\]\s*=\s*setmetatable\(\{(.*?)\},", records.read_text(encoding="utf-8"), re.S):
        h = re.search(r'express_hash\s*=\s*"([^"\n]+)"', match[2])
        if h: hashes[h[1]] = match[1]
    sources = [definition, parser, logger, records]
    translation_sources = {}
    translation_pattern = re.compile(r'translate_(?:prefab|excel_design|extra)(?:\["([^"]+)"\]|\.(\w+))\s*=\s*\{\s*ID\s*=\s*"[^"]*",\s*message_cn\s*=\s*"((?:\\.|[^"\\])*)"', re.S)
    for path in sorted(directory.glob("*_translate_*.lua")):
        text = path.read_text(encoding="utf-8")
        used = False
        for match in translation_pattern.finditer(text):
            key = match[1] or match[2]
            if key in hashes:
                templates[hashes[key]] = _unescape(match[3])
                translation_sources[hashes[key]] = {"path": str(path.resolve()), "line": text.count("\n", 0, match.start()) + 1}
                used = True
        if used: sources.append(path)
    constant = re.search(r"battleDef\.NumDisPlayerPecentageNum\s*=\s*(\d+)", definition_text)
    constants = {"NumDisPlayerPecentageNum": int(constant[1]) if constant else None}
    runtime_source = {}
    if isinstance(runtime_catalog, (str, Path)):
        runtime_path = Path(runtime_catalog).resolve()
        runtime_raw = runtime_path.read_bytes()
        runtime_catalog = json.loads(runtime_raw)
        runtime_source = {"path": str(runtime_path), "sha256": hashlib.sha256(runtime_raw).hexdigest()}
    outer = runtime_catalog or {}
    runtime_payload = outer.get("payload", outer)
    runtime = runtime_payload.get("event_catalog", runtime_payload)
    runtime_source = {**(runtime.get("source") or {}), **runtime_source}
    if runtime_payload.get("capturedAt"): runtime_source["observed_at"] = runtime_payload["capturedAt"]
    if runtime_payload.get("runtimeIdentity"): runtime_source["runtime_identity"] = runtime_payload["runtimeIdentity"]
    if not runtime_source and runtime: runtime_source = {"kind": "runtime_export"}
    runtime_enum = runtime.get("ET") or {}
    runtime_arguments = runtime.get("ETARGS") or {}
    runtime_templates = runtime.get("templates") or runtime.get("battle_record") or {}
    pointer_prefix = "/payload/event_catalog" if "event_catalog" in runtime_payload else ""
    if runtime_enum:
        effective_enum = {str(k): int(v) for k, v in runtime_enum.items() if isinstance(v, int) and not isinstance(v, bool)}
    else: effective_enum = enum
    constants.update(runtime.get("constants") or {})
    events = {}
    for symbol, event_id in effective_enum.items():
        key = str(event_id)
        runtime_fields = runtime_arguments.get(key, runtime_arguments.get(event_id, runtime_arguments.get(symbol)))
        if runtime_fields == {}: runtime_fields = []  # An explicitly empty Lua table.
        if runtime_fields is not None and (not isinstance(runtime_fields, list) or any(not isinstance(f, str) for f in runtime_fields)):
            raise ValueError(f"Invalid runtime ETARGS for {symbol}")
        fields = runtime_fields if runtime_fields is not None else arguments.get(key)
        has_runtime_schema = symbol in runtime_enum and runtime_fields is not None
        validation = "historical_source_unverified_current"
        if has_runtime_schema:
            validation = "runtime_schema_matched" if enum.get(symbol) == event_id and fields == arguments.get(key) else "runtime_schema_changed"
        elif runtime_enum.get(symbol) == event_id and symbol == "BattleHeroSpeedReady":
            validation = "runtime_enum_matched_dynamic_schema_unverified"
        decode_pattern = re.search(r"battleDef\.(?:eventDecode|replayEventDecode)\[BLD\.BattleLogDef\.ET\." + re.escape(symbol) + r"\]", parser_text)
        historical_definition = {"path": str(definition.resolve()), "line": argument_lines.get(symbol, definition_text.count("\n", 0, enum_start) + 1)}
        source = {"definition": historical_definition,
                  "decoder": {"path": str(parser.resolve()), "line": parser_text.count("\n", 0, decode_pattern.start()) + 1 if decode_pattern else None,
                              "source_date": HISTORICAL_SOURCE_DATE},
                  "template": translation_sources.get(key), "source_date": HISTORICAL_SOURCE_DATE, "validation": validation}
        if runtime_enum:
            source["runtime"] = runtime_source
            source["historical_definition"] = historical_definition if enum.get(symbol) == event_id else None
            source["definition"] = {**runtime_source, "pointer": f"{pointer_prefix}/ETARGS/{key}" if has_runtime_schema else f"{pointer_prefix}/ET/{symbol}"}
            source["schema_origin"] = "current_runtime" if has_runtime_schema else "historical_dynamic_decoder_with_runtime_enum"
            source["source_date"] = str(runtime_source.get("observed_at", "2026-11-07"))[:10]
        template = runtime_templates.get(key, runtime_templates.get(event_id, templates.get(key)))
        if isinstance(template, dict): template = template.get("express")
        if key in runtime_templates or event_id in runtime_templates:
            source["template"] = {**runtime_source, "pointer": f"{pointer_prefix}/templates/{key}"}
        events[key] = {"event_id": event_id, "symbol": symbol, "event_name_zh": EVENT_NAMES.get(symbol, symbol),
                       "fields": fields, "template_zh": template, "source": source,
                       "dynamic_pairs": symbol == "BattleHeroSpeedReady"}
    source_files = [{"path": str(p.resolve()), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sources]
    if runtime_source.get("path") and runtime_source.get("sha256"): source_files.append({"path": runtime_source["path"], "sha256": runtime_source["sha256"]})
    result = {"version": SEMANTICS_VERSION, "source_date": str(runtime_source.get("observed_at", HISTORICAL_SOURCE_DATE))[:10], "source_files": source_files,
              "events": events, "constants": constants, "parameter_labels": dict(PARAMETER_LABELS),
              "tab_semantics": {"label_zh": "日志嵌套缩进层级", "source": {"path": str(logger.resolve()), "line": 32}, "not_round_number": True}}
    result["digest"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return result


def _lookup(resolver: Callable | None, kind: str, value: Any) -> dict:
    if resolver is None: return {}
    resolved = resolver(kind, value)
    if isinstance(resolved, str): return {"name_zh": resolved}
    return resolved if isinstance(resolved, dict) else {}


def _settlement(context: dict | None) -> dict:
    context = context or {}
    return context.get("settlement") or (context.get("payload") or {}).get("data", {}).get("battleSettlement") or {}


def _army(team_id: Any, context: dict | None) -> tuple[str | None, dict]:
    for side, army in _settlement(context).items():
        if isinstance(army, dict) and str(army.get("army_id")) == str(team_id):
            return side, army
    return None, {}


def _team_name(value: Any, context: dict | None) -> str | None:
    side, army = _army(value, context)
    if not army: return None
    label = {"attacker": "进攻方", "defensive": "防守方", "defender": "防守方"}.get(side, side)
    player = (army.get("avatar_data") or {}).get("avatar_name")
    return f"{label}·{player}" if player else label


def _hero_name(value: Any, team_id: Any, context: dict | None, resolver: Callable | None) -> tuple[str | None, dict]:
    _, army = _army(team_id, context)
    for hero in army.get("army_member") or []:
        if isinstance(hero, dict) and str(hero.get("heros_id")) == str(value):
            personal_name = hero.get("name")
            resolved = _lookup(resolver, "hero", hero.get("heros_id"))
            if personal_name:
                return personal_name, {"mapped": True, "verification": "battle_payload", "source": {"kind": "battle_payload", "field": "army_member.name"}}
            return resolved.get("name_zh") or resolved.get("name"), resolved
    resolved = _lookup(resolver, "hero", value)
    return resolved.get("name_zh") or resolved.get("name"), resolved


def decode_event(event: dict, catalog: dict, id_resolver: Callable | None = None, context: dict | None = None) -> dict:
    """Annotate one event; every supplied argument remains in parameters/raw.

    Context may contain ``payload`` or ``settlement``. Team IDs are army IDs,
    and hero names are resolved against that army's heros_id before ID lookup.
    """
    if not isinstance(event, dict): raise TypeError("Event must be an object")
    event_id = event.get("eventId")
    schema = catalog.get("events", {}).get(str(event_id)) if not isinstance(event_id, bool) else None
    arguments = event.get("intList")
    issues = []
    if not isinstance(arguments, list):
        issues.append("argument_list_missing_or_not_array")
        arguments = []
    symbol = schema.get("symbol") if schema else None
    if schema and symbol not in EVENT_NAMES: issues.append("event_name_unmapped")
    tab = event.get("tab")
    if isinstance(tab, bool) or not isinstance(tab, int) or tab < 0: issues.append("invalid_log_indentation")
    fields = schema.get("fields") if schema else None
    dynamic = bool(schema and schema.get("dynamic_pairs"))
    if dynamic:
        fields = ["teamId" if i % 2 == 0 else "heroId" for i in range(len(arguments))]
        if len(arguments) % 2: issues.append("incomplete_action_order_pair")
    elif fields is None:
        issues.append("event_schema_unmapped")
        fields = []
    elif len(arguments) != len(fields):
        issues.append("argument_count_mismatch")
    raw_fields = dict(zip(fields, arguments))
    parameters = []
    denominator = catalog.get("constants", {}).get("NumDisPlayerPecentageNum")
    for i, value in enumerate(arguments):
        key = fields[i] if i < len(fields) else f"unknown_argument_{i + 1}"
        label = PARAMETER_LABELS.get(key, f"未映射参数{i + 1}")
        if dynamic: label = f"第{i // 2 + 1}位行动武将的" + ("部队ID" if i % 2 == 0 else "武将ID")
        item = {"position": i + 1, "key": key, "label_zh": label, "raw_value": value}
        if schema:
            # The catalog keeps the full path/hash/line provenance once. Avoid
            # repeating it for every parameter of every archived event.
            item["source"] = {"event_schema": str(event_id), "argument_position": i + 1}
        if i >= len(fields): item["status"] = "unmapped"
        elif key not in PARAMETER_LABELS:
            item["status"] = "unmapped"
            issues.append({"parameter_name_unmapped": key})
        if isinstance(value, bool) or not isinstance(value, int): issues.append({"non_integer_argument": i + 1})
        kind, resolved = _ID_KINDS.get(key), {}
        if key == "propertyId" and (symbol in {"HeroPorpertySetIncrease", "HeroPorpertySetDecrease"}
                                    or (symbol == "HeroPropertyUnChanged" and raw_fields.get("type") == 1)):
            kind = "report_property"
        if key in _TEAM_FIELDS:
            kind = "army"
            name = _team_name(value, context)
            if name: item["id_name_zh"] = name
        elif key in _HERO_FIELDS:
            kind = "hero"
            team_key = {"srcHeroId": "srcTeamId", "targetHeroId": "targetTeamId", "damageSourceHeroId": "damageSourceTeamId", "applyHeroId": "applyTeamId", "provideHeroId": "provideTeamId"}.get(key, "teamId")
            team_id = arguments[i - 1] if dynamic and i % 2 else raw_fields.get(team_key, raw_fields.get("teamId"))
            # For damage events the source hero is paired with a later field.
            if symbol in {"SoldierNumLossByHeroSkill", "SoldierNumLossByBuff"} and key == "heroId":
                team_id = raw_fields.get("team2Id", raw_fields.get("srcTeamId", team_id))
            if key == "heroId" and "ownerHeroId" in raw_fields and "srcTeamId" in raw_fields:
                team_id = raw_fields["srcTeamId"]
            name, resolved = _hero_name(value, team_id, context, id_resolver)
            if name: item["id_name_zh"] = name
        elif kind:
            resolved = _lookup(id_resolver, kind, value)
            name = resolved.get("name_zh") or resolved.get("name")
            if name: item["id_name_zh"] = name
        if kind:
            item["id_kind"] = kind
            item["id_status"] = "mapped" if item.get("id_name_zh") and resolved.get("mapped") is not False else "unmapped"
        if kind and resolved:
            # Full ID provenance is stored once in the ID catalog. A parameter
            # references its kind/ID and verification instead of duplicating
            # translation files, tables and descriptions on every occurrence.
            item["id_source"] = {"kind": kind, "id": value, "verification": resolved.get("verification", resolved.get("status"))}
            owner = getattr(id_resolver, "__self__", None)
            if getattr(owner, "version", None): item["id_source"]["catalog_version"] = owner.version
        if key in {"campType", "winCampType"} and value in (1, 2) and not isinstance(value, bool):
            item["decoded_value"] = "进攻方" if value == 1 else "防守方"
        if key in {"changeSoldierNum", "resultSoldierNum", "woundSoldierNum", "deadToAliveNum"}: item["unit"] = "兵"
        if symbol in {"TroopNumAdd", "TroopNumReduce"} and key in {"changeNum", "resultNum"}: item["unit"] = "兵"
        if key == "round": item["unit"] = "回合"
        scaled = ((symbol in _SCALED_PROPERTIES and key in {"changePropertyValue", "resultPropertyValue", "maxValue"})
                  or (symbol == "BuffAttrChange" and key in {"changeValue", "resultValue"}))
        if scaled and isinstance(value, (int, float)) and not isinstance(value, bool) and isinstance(denominator, (int, float)) and denominator > 0:
            item["decoded_value"] = value / denominator
            item["scale_divisor"] = denominator
            item["display_note_zh"] = "已按客户端固定精度还原；最终格式和上下限取决于属性配置"
        if symbol == "BattleMoraleAffect" and key in {"morale", "damageChangePercent"} and isinstance(value, (int, float)) and not isinstance(value, bool):
            item["decoded_value"] = value / 100
            item["scale_divisor"] = 100
            item["unit"] = "士气" if key == "morale" else "比例"
        if symbol == "FatalEffect" and key == "fatal" and isinstance(value, (int, float)) and not isinstance(value, bool):
            item["decoded_value"], item["scale_divisor"], item["unit"] = value / 10, 10, "%"
        if symbol == "FatalEffect" and key == "damageType" and value in (1, 2) and not isinstance(value, bool):
            item["decoded_value"] = "奇谋" if value == 2 else "会心"
        if key == "resistRate" and symbol == "ResistEffect": item["unit"] = "%"
        if key in {"badlyHurtRatio", "weakRatio"} and symbol in {"ForbidHeal", "ForbidDamage"}: item["unit"] = "%"
        parameters.append(item)
    if schema and len(arguments) < len(fields):
        issues.append({"missing_parameters": fields[len(arguments):]})
    name = schema.get("event_name_zh") if schema else "未映射事件"
    fragments = []
    for parameter in parameters:
        display = parameter.get("id_name_zh", parameter.get("decoded_value", parameter["raw_value"]))
        fragments.append(f"{parameter['label_zh']}：{display}" + (f"{parameter['unit']}" if parameter.get("unit") else ""))
    schema_status = "unmapped" if schema is None else "partial" if issues else "mapped"
    id_parameters = [p for p in parameters if p.get("id_kind")]
    unmapped_ids = [p["position"] for p in id_parameters if p.get("id_status") == "unmapped"]
    id_mapping_status = ("not_applicable" if not id_parameters else "mapped" if not unmapped_ids
                         else "unmapped" if len(unmapped_ids) == len(id_parameters) else "partial")
    if unmapped_ids: issues.append({"unmapped_id_parameters": unmapped_ids})
    status = "unmapped" if schema is None else "partial" if schema_status != "mapped" or unmapped_ids else "mapped"
    return {"version": SEMANTICS_VERSION, "catalog_digest": catalog.get("digest"), "event_id": event_id, "symbol": symbol,
            "event_name_zh": name, "name_zh": name, "summary_zh": name + ("；" + "；".join(fragments) if fragments else ""),
            "tab": event.get("tab"), "tab_meaning_zh": "日志嵌套缩进层级", "parameters": parameters,
            "template_zh": schema.get("template_zh") if schema else None,
            "status": status, "schema_status": schema_status, "id_mapping_status": id_mapping_status,
            "unmapped_id_parameters": unmapped_ids,
            "issues": issues, "source": schema.get("source") if schema else {"reason": "event_id_not_in_catalog"},
            "raw_event": event}
