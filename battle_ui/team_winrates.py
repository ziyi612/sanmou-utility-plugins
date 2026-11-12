"""Read-only lineup results from the archive's current best report versions.

Red evolution and enlighten quality are independent per-hero values. Results
come only from the client's settlement.winer army ID, never casualty estimates.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from functools import lru_cache

from .data import BattleData, BattleQueryTimeout
from .daily import _valid_day, _independent_settlement
from .opponents import OpponentRoster
from .winrate_sorting import win_rate_sort_key


_CHINA = timezone(timedelta(hours=8))
_PREFIXES = ("/data/", "/components/all/", "/components/detail/", "/components/basic/")
_FIELDS = ("timeStamp", "endTimeStampTime", "childNum", "battleID", "battleJobId", "matchType", "battleSettlement/winer")
_POINTERS = tuple(prefix + key for prefix in _PREFIXES for key in _FIELDS)
_MATCH_POINTERS = tuple(prefix + "matchType" for prefix in _PREFIXES)
_SIDES = {"attacker": "attacker", "defensive": "defender", "defender": "defender"}
_TIERS = {"high": "高满", "medium": "中红", "low": "低红", "unknown": "红/品信息未知", "unclassified": "未设置门槛"}


@lru_cache(maxsize=1)
def _skill_get_types():
    """Source-backed Util.IsSkill types; absent/new IDs remain unverified."""
    from parser.id_semantics import _records
    root = Path(__file__).resolve().parents[1]
    for path in (root / "output/reports/expedition_20261109/sources/hero_skill_source/skills.lua",
                 root / "capture/output/processed/decompiled_lua/battle_focus/6328_skills_e0ab7440.lua"):
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                continue
            entries = {str(uid): fields["get_type"] for table, uid, fields, _ in _records(path.read_text(encoding="utf-8-sig"))
                       if table == "skills_skills" and _integer(fields.get("get_type")) is not None}
            if entries:
                return entries
        except (OSError, ValueError):
            pass
    return {}


def _integer(value, low=0, high=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        return None
    return value if high is None or value <= high else None


def _uid(value, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    if not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    return str(number) if number > 0 or allow_zero and number == 0 else None


def _army_uid(value, allow_zero=False):
    # The client uses negative army IDs for NPC troops. -1/-2 remain the
    # documented error results, and are never interpreted as winning armies.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    digits = text[1:] if text.startswith(("-", "+")) else text
    if not digits.isascii() or not digits.isdigit():
        return None
    number = int(text)
    return str(number) if number not in (-1, -2) and (number != 0 or allow_zero) else None


def _thresholds(high_red, high_quality, medium_red, medium_quality):
    values = dict(high_red=high_red, high_quality=high_quality,
                  medium_red=medium_red, medium_quality=medium_quality)
    if all(value is None for value in values.values()):
        return values
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or not 0 <= value <= 15 for value in values.values()):
        raise ValueError("红度/品度门槛须全部填写0至15的数值，或全部留空")
    if high_red < medium_red or high_quality < medium_quality:
        raise ValueError("高档的总红、总品门槛均不能低于中档")
    return values


def _values(node, field):
    return [node["metadata"][prefix + field] for prefix in _PREFIXES
            if prefix + field in node["metadata"]]


def _one_uid(values, allow_zero=False):
    known = {_army_uid(value, allow_zero) for value in values}
    if None in known:
        return None
    return next(iter(known)) if len(known) == 1 else None


def _result(node, attacker, defender):
    winner = _one_uid(_values(node, "battleSettlement/winer"), allow_zero=True)
    if winner == "0":
        return {"attacker": "draw", "defender": "draw"}
    attacking, defending = _army_uid(attacker.get("army_uid")), _army_uid(defender.get("army_uid"))
    if not winner or not attacking or not defending or attacking == defending:
        return {"attacker": "unknown", "defender": "unknown"}
    if winner == attacking:
        return {"attacker": "win", "defender": "loss"}
    if winner == defending:
        return {"attacker": "loss", "defender": "win"}
    return {"attacker": "unknown", "defender": "unknown"}


def _stamp(node):
    for field in ("endTimeStampTime", "timeStamp"):
        for value in _values(node, field):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                continue
            try:
                day = datetime.fromtimestamp(value, timezone.utc).astimezone(_CHINA).date().isoformat()
                return day, value
            except (ValueError, OverflowError, OSError):
                pass
    return None, None


def _tier(red, quality, thresholds):
    if red is None or quality is None:
        return "unknown"
    if thresholds["high_red"] is None:
        return "unclassified"
    if red >= thresholds["high_red"] and quality >= thresholds["high_quality"]:
        return "high"
    if red >= thresholds["medium_red"] and quality >= thresholds["medium_quality"]:
        return "medium"
    return "low"


class TeamWinrateService:
    """Indexed metadata and small side/hero rows; no game reads or DB writes."""

    def __init__(self, db_path, timeout_seconds=20, *, runtime_dir=None, skill_types=None):
        self.data = BattleData(db_path, timeout_seconds=timeout_seconds)
        self.runtime_dir = (Path(runtime_dir) if runtime_dir is not None else
                            self.data.db_path.parent.parent / "incoming" / "runtime")
        self.skill_types = skill_types

    def _match_types(self, namespace):
        folder = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:20]
        path = self.runtime_dir / folder / "collector_state.json"
        try:
            if path.stat().st_size > 64 * 1024 * 1024:
                return {}
            state = json.loads(path.read_text(encoding="utf-8"))
            identity = state.get("runtimeIdentity", {})
            canonical = "server:{serverId}/scenario:{scenarioId}/avatar:{avatarId}".format(**identity)
            enums = state.get("runtimeCapabilities", {}).get("battleMatchTypes")
            if state.get("namespace") == namespace == canonical and isinstance(enums, dict):
                return {key: value for key, value in enums.items() if _integer(value) is not None}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        return {}

    @staticmethod
    def _battle_type(node, enums):
        pvp = enums.get("PVP")
        if _integer(pvp) is None:
            return "unknown"
        known = set(enums.values())
        kinds = {value for value in _values(node, "matchType") if _integer(value) is not None}
        if len(kinds) != 1 or not kinds <= known:
            return "unknown"
        return "pvp" if next(iter(kinds)) == pvp else "pve"

    def _team(self, connection, node, side, names_cache):
        names = OpponentRoster._names(connection, node["snapshot_id"], names_cache)
        heroes = []
        for row in connection.execute(
                "SELECT ordinal,hero_uid,position,hero_name,raw_json FROM report_heroes WHERE side_id=? ORDER BY ordinal",
                (side["id"],)):
            try:
                raw = json.loads(row["raw_json"])
            except (ValueError, TypeError):
                raw = {}
            raw = raw if isinstance(raw, dict) else {}
            uid = _uid(row["hero_uid"])
            source_skills = raw.get("skills")
            skills = []
            valid_skills = node["serialization_ok"] and node["body_complete"] and isinstance(source_skills, list) and bool(source_skills)
            excluded_effects = []
            skill_types = self.skill_types if self.skill_types is not None else _skill_get_types()
            for skill in source_skills[:3] if isinstance(source_skills, list) else []:
                skill = skill if isinstance(skill, dict) else {}
                skill_uid = _uid(skill.get("skillId"))
                valid_skills = valid_skills and skill_uid is not None
                get_type = skill_types.get(skill_uid)
                if _integer(get_type) is None:
                    valid_skills = False
                elif get_type not in (0, 3, 4):
                    excluded_effects.append(skill)
                    continue
                skills.append({"skill_uid": skill_uid, "name_zh": names.get(("skill", str(skill_uid))),
                               "level": skill.get("level"), "order_level": skill.get("orderLevel"),
                               "tactic_type_known": _integer(get_type) is not None})
            heroes.append({"hero_uid": uid, "name_zh": names.get(("hero", str(uid))) or row["hero_name"],
                           "position": _integer(row["position"]), "ordinal": row["ordinal"],
                           "red": _integer(raw.get("evolutionNum"), high=5),
                           "quality": _integer(raw.get("enlighten"), high=5),
                           "evolution_num": _integer(raw.get("evolutionNum"), high=5),
                           "enlighten": _integer(raw.get("enlighten"), high=5),
                           "skills": skills, "skills_complete": bool(valid_skills and skills),
                           "extra_effects": excluded_effects + (source_skills[3:] if isinstance(source_skills, list) else [])})
        complete = (len(heroes) == 3 and len({hero["hero_uid"] for hero in heroes}) == 3
                    and all(hero["hero_uid"] is not None for hero in heroes))
        # A lineup is its three actual hero IDs. Formation and list order do
        # not create another team; skills stay attached to their own hero.
        heroes.sort(key=lambda hero: (hero["hero_uid"] is None, int(hero["hero_uid"] or 0), hero["ordinal"]))
        red = sum(hero["red"] for hero in heroes) if complete and all(hero["red"] is not None for hero in heroes) else None
        quality = sum(hero["quality"] for hero in heroes) if complete and all(hero["quality"] is not None for hero in heroes) else None
        return {"heroes": heroes, "lineup_complete": complete, "total_red": red, "total_quality": quality,
                "skills_complete": complete and all(hero["skills_complete"] for hero in heroes)}

    @staticmethod
    def _aggregate(samples, with_skills):
        def new_group(key, sample, heroes):
            return {"key": key, "tier": sample["tier"], "tier_label": _TIERS[sample["tier"]],
                    "label": " / ".join(hero["name_zh"] or ("ID " + str(hero["hero_uid"] or "未知")) for hero in heroes),
                    "heroes": heroes, "skills_complete": sample["team"]["skills_complete"],
                    "samples": 0, "wins": 0, "draws": 0, "losses": 0, "unknown_results": 0,
                    "full_red_full_quality_samples": 0, "sources": [], "_reds": set(), "_qualities": set()}

        def add_sample(group, sample):
            team = sample["team"]
            group["samples"] += 1
            group["skills_complete"] = group["skills_complete"] and team["skills_complete"]
            group["full_red_full_quality_samples"] += team["total_red"] == team["total_quality"] == 15
            outcome = {"win": "wins", "draw": "draws", "loss": "losses", "unknown": "unknown_results"}[sample["outcome"]]
            group[outcome] += 1
            group["_reds"].add(team["total_red"])
            group["_qualities"].add(team["total_quality"])
            group["sources"].append({name: sample[name] for name in ("namespace", "battle_uid", "side", "snapshot_id", "day", "outcome")})

        def finish_group(group):
            group["known_results"] = group["wins"] + group["draws"] + group["losses"]
            denominator = group["known_results"]
            group["win_rate"] = group["wins"] / denominator if denominator else None
            group["draw_rate"] = group["draws"] / denominator if denominator else None
            for key, name in (("_reds", "total_red"), ("_qualities", "total_quality")):
                values = group.pop(key)
                group[name] = next(iter(values)) if len(values) == 1 else None
                known = [value for value in values if value is not None]
                group[name + "_min"] = min(known) if known else None
                group[name + "_max"] = max(known) if known else None
            group["full_red_full_quality"] = group["total_red"] == group["total_quality"] == 15

        groups, seen = {}, set()
        for sample in samples:
            source = (sample["namespace"], sample["battle_uid"], sample["side"])
            if source in seen:
                continue
            seen.add(source)
            team = sample["team"]
            heroes = sorted(team["heroes"], key=lambda hero: (
                hero["hero_uid"] is None, int(hero["hero_uid"] or 0), hero["ordinal"]))
            signature = [hero["hero_uid"] for hero in heroes]
            discriminator = None if team["lineup_complete"] else source
            key = json.dumps([sample["tier"], signature, discriminator], ensure_ascii=False, separators=(",", ":"))
            if key not in groups:
                groups[key] = {**new_group(key, sample, heroes), "_variants": {}}
            group = groups[key]
            add_sample(group, sample)
            if with_skills:
                # Canonicalize hero order before skills, never flatten all
                # skills into a shared bag that could move one hero's tactics.
                skill_signature = [(hero["hero_uid"], [skill["skill_uid"] for skill in hero["skills"]]) for hero in heroes]
                variant_discriminator = None if team["skills_complete"] else source
                variant_key = json.dumps([skill_signature, variant_discriminator], ensure_ascii=False, separators=(",", ":"))
                if variant_key not in group["_variants"]:
                    group["_variants"][variant_key] = new_group(variant_key, sample, heroes)
                add_sample(group["_variants"][variant_key], sample)
        for group in groups.values():
            finish_group(group)
            variants = list(group.pop("_variants").values())
            for variant in variants:
                finish_group(variant)
            group["skill_variants"] = sorted(variants, key=win_rate_sort_key)
            group["skill_variants_count"] = len(variants)
            group["known_skill_variants_count"] = sum(variant["skills_complete"] for variant in variants)
        return sorted(groups.values(), key=win_rate_sort_key)

    def summary(self, namespace=None, day=None, battle_type="pvp", high_red=12, high_quality=12,
                medium_red=6, medium_quality=6):
        day = _valid_day(day)
        if namespace is not None and (not isinstance(namespace, str) or not namespace or len(namespace) > 500):
            raise ValueError("账号范围无效")
        if battle_type not in ("pvp", "all"):
            raise ValueError("战报类型须为pvp或all")
        thresholds = _thresholds(high_red, high_quality, medium_red, medium_quality)
        result = {"available": False, "namespace": namespace, "day": day, "battle_type": battle_type,
                  "dates": [], "without_skills": [], "with_skills": [], "totals": {}, "coverage": {},
                  "rule": {"thresholds": thresholds, "red_field": "army_member[].evolutionNum",
                           "quality_field": "army_member[].enlighten", "grading": "三将总红和总品同时满足门槛；默认门槛为可调分析规则",
                           "result": "battleSettlement.winer匹配army_id；0为平局；其他未证结果为未知",
                           "denominator": "胜+平+负；未知结果单列", "sides": "双方均计，不筛阵营",
                           "skills": "每将skills按原始顺序取前三项战法，后续额外效果不作为战法组合",
                           "skill_type_verification": "按本地客户端Util.IsSkill的get_type=0/3/4过滤；本地配置未覆盖的新ID单列未知战法组合",
                           "deduplication": "只读当前最佳版本，同账号同战报同一侧只计一次；每档按三将ID集合一队一行，忽略排列站位",
                           "skill_variants": "战法配置按对应武将ID绑定；同主队内分别统计各配置，未知配置独立保留"}}
        if not self.data.db_path.exists():
            return result
        started = time.monotonic()
        deadline = started + self.data.timeout_seconds
        def budget():
            if time.monotonic() >= deadline:
                raise BattleQueryTimeout("队伍胜率统计超过时间预算，请按账号或日期缩小范围。")
        nodes = {}
        params = []
        condition = " WHERE r.namespace=?" if namespace is not None else ""
        if namespace is not None:
            params.append(namespace)
        with self.data._connection() as connection:
            # BattleData pins one consistent current-version WAL read snapshot.
            for row in connection.execute("SELECT r.namespace,r.battle_uid,r.current_snapshot_id snapshot_id,s.source,s.capture_version,s.event_count,s.issues_json "
                    "FROM report_records r JOIN report_snapshots s ON s.id=r.current_snapshot_id" + condition, params):
                node = {**dict(row), "metadata": {}}
                issues = json.loads(node.pop("issues_json"))
                capture_only = {"event_list_missing_or_not_array", "event_count_mismatch", "collector_reported_incomplete", "collector_warnings"}
                node["serialization_ok"] = all(issue.get("reason") in capture_only for issue in issues)
                nodes[row["snapshot_id"]] = node
            enums = {ns: self._match_types(ns) for ns in {node["namespace"] for node in nodes.values()}}
            def read_metadata(pointers, selected_ids=None):
                placeholders = ",".join("?" for _ in pointers)
                chunks = ([None] if selected_ids is None else
                          [selected_ids[index:index + 200] for index in range(0, len(selected_ids), 200)])
                for chunk in chunks:
                    where = condition
                    extra = []
                    if chunk is not None:
                        where += (" AND " if where else " WHERE ") + "r.current_snapshot_id IN (" + ",".join("?" for _ in chunk) + ")"
                        extra = chunk
                    for row in connection.execute("SELECT r.current_snapshot_id snapshot_id,f.pointer,f.value_json FROM report_records r "
                            "CROSS JOIN report_fields AS f INDEXED BY sqlite_autoindex_report_fields_1 "
                            "ON f.snapshot_id=r.current_snapshot_id AND f.pointer IN (" + placeholders + ")" + where,
                            (*pointers, *params, *extra)):
                        budget()
                        try:
                            nodes[row["snapshot_id"]]["metadata"][row["pointer"]] = json.loads(row["value_json"])
                        except (ValueError, TypeError):
                            pass
            # PvP is the normal view: classify with four scalar index probes,
            # then avoid reading dates/results/lineups for excluded PvE rows.
            read_metadata(_MATCH_POINTERS if battle_type == "pvp" else _POINTERS)
            for node in nodes.values():
                node["battle_type"] = self._battle_type(node, enums[node["namespace"]])
            selected_ids = [snapshot_id for snapshot_id, node in nodes.items()
                            if battle_type == "all" or node["battle_type"] == "pvp"]
            if battle_type == "pvp":
                read_metadata(tuple(pointer for pointer in _POINTERS if pointer not in _MATCH_POINTERS), selected_ids)
            sides = defaultdict(list)
            for node in nodes.values():
                node["body_complete"] = False
            for index in range(0, len(selected_ids), 200):
                chunk = selected_ids[index:index + 200]
                where = condition + (" AND " if condition else " WHERE ") + "r.current_snapshot_id IN (" + ",".join("?" for _ in chunk) + ")"
                for row in connection.execute("SELECT q.id,q.snapshot_id,q.side,q.army_uid,q.player_uid,q.player_name,q.raw_json "
                        "FROM report_records r JOIN report_sides q ON q.snapshot_id=r.current_snapshot_id" + where, (*params, *chunk)):
                    sides[row["snapshot_id"]].append(dict(row))
                for row in connection.execute("SELECT q.snapshot_id,q.status_json FROM report_records r "
                        "JOIN report_components q ON q.snapshot_id=r.current_snapshot_id "
                        "AND q.name IN ('basic','all','detail') AND q.available=1 AND q.complete=1" + where, (*params, *chunk)):
                    status = json.loads(row["status_json"])
                    if not status.get("error"):
                        nodes[row["snapshot_id"]]["body_complete"] = True
            coverage = defaultdict(int)
            samples, dates, names_cache = [], set(), {}
            for snapshot_id, node in nodes.items():
                budget()
                coverage["total_reports"] += 1
                kind = node["battle_type"]
                coverage[kind + "_reports"] += 1
                if battle_type == "pvp" and kind != "pvp":
                    continue
                node["day"], node["timestamp"] = _stamp(node)
                if node["day"]:
                    dates.add(node["day"])
                else:
                    coverage["invalid_time_reports"] += 1
                if day is not None and node["day"] != day:
                    continue
                child_counts = [_integer(value) for value in _values(node, "childNum")]
                unique = {}
                for side in sides[snapshot_id]:
                    canonical = _SIDES.get(side["side"])
                    if canonical is None:
                        continue
                    if canonical in unique:
                        coverage["duplicate_side_aliases"] += 1
                        if unique[canonical]["raw_json"] != side["raw_json"]:
                            unique[canonical]["conflicting_alias"] = True
                        continue
                    unique[canonical] = side
                if any(value is not None and value > 0 for value in child_counts):
                    node["settlement_headers"] = [node["metadata"].get(prefix + field)
                        for prefix in _PREFIXES for field in ("battleID", "battleJobId", "battleSettlement/winer")]
                    armies = {_army_uid(side["army_uid"]) for side in unique.values()}
                    armies.discard(None)
                    if not _independent_settlement(node, armies):
                        coverage["unverified_parent_reports"] += 1
                        coverage["unverified_parent_samples"] += len(unique)
                        if node["source"] not in ("all", "detail") or node["event_count"] == 0:
                            coverage["summary_only_reports"] += 1
                        continue
                coverage["selected_reports"] += 1
                outcomes = _result(node, unique.get("attacker", {}), unique.get("defender", {}))
                for canonical, side in unique.items():
                    budget()
                    team = self._team(connection, node, side, names_cache)
                    if side.get("conflicting_alias"):
                        team.update(lineup_complete=False, total_red=None, total_quality=None, skills_complete=False)
                    if not team["lineup_complete"]:
                        coverage["incomplete_lineup_samples"] += 1
                    if not team["skills_complete"]:
                        coverage["unknown_skills_samples"] += 1
                    if team["total_red"] is None:
                        coverage["unknown_red_samples"] += 1
                    if team["total_quality"] is None:
                        coverage["unknown_quality_samples"] += 1
                    outcome = "unknown" if side.get("conflicting_alias") else outcomes[canonical]
                    coverage["unknown_result_samples"] += outcome == "unknown"
                    samples.append({"namespace": node["namespace"], "battle_uid": node["battle_uid"],
                                    "snapshot_id": snapshot_id, "side": canonical, "day": node["day"],
                                    "team": team, "outcome": outcome,
                                    "tier": _tier(team["total_red"], team["total_quality"], thresholds)})
            result.update(available=True, dates=sorted(dates, reverse=True), coverage=dict(coverage),
                          without_skills=self._aggregate(samples, False), with_skills=self._aggregate(samples, True))
        result["totals"] = {"samples": len(samples), "reports": len({(row["namespace"], row["battle_uid"]) for row in samples}),
                            "wins": sum(row["outcome"] == "win" for row in samples),
                            "draws": sum(row["outcome"] == "draw" for row in samples),
                            "losses": sum(row["outcome"] == "loss" for row in samples),
                            "unknown_results": sum(row["outcome"] == "unknown" for row in samples)}
        result["elapsed_seconds"] = round(time.monotonic() - started, 4)
        return result
