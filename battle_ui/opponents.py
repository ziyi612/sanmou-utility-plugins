"""Latest observed day's opposing-player teams, without modifying the archive.

An opponent is identified from a real two-sided battle. Missing allegiance or
evolution data is reported as unknown, never filled from a guess or a nickname.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import re
import time
from typing import Any

from .data import BattleData, BattleQueryTimeout
from .daily import META_PATHS, _battle_date
from .current_metadata import read_current_metadata


_SIDE = {"attacker": "attacker", "defensive": "defender", "defender": "defender"}
_METADATA_POINTERS = tuple(path.removeprefix("$").replace(".", "/") for path in META_PATHS)
RULE = {
    "high_grade_minimum": 3,
    "single_full_red_evolution": 5,
    "whole_team_full_red": "恰好三名已识别武将，且三将进阶均为5",
    "time_scope": "所选账号范围最近有战报数据的一个北京时间自然日",
    "opponent_identity": "角色本人所在侧的另一侧；本人不在时，仅在同日自身同盟唯一且可确认己方侧时识别另一侧",
    "team_deduplication": "同账号、同玩家ID、同一武将排列只留该日最新一次；相同时刻优先完整快照",
    "npc_policy": "玩家ID必须为正整数，系统守军和未知ID不作为玩家",
}


def _positive_uid(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    if not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    return str(number) if number > 0 else None


def _player_identity(value: Any) -> tuple[str, str]:
    """Keep explicit NPC IDs distinct from absent or malformed player IDs."""
    if not isinstance(value, bool) and isinstance(value, (int, str)):
        text = str(value).strip()
        if text.isascii() and re.fullmatch(r"[+-]?[0-9]+", text):
            number = int(text)
            return ("player" if number > 0 else "npc", str(number))
    # Include the original type/value when comparing two aliases. NULL, an
    # empty string and a broken ID must not become the same inferred identity.
    return "unknown", json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hero_uid(value: Any) -> str | None:
    if isinstance(value, bool) or value is None or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    return text or None


def _evolution(value: Any) -> int | None:
    # A missing, boolean or nonintegral value cannot prove a red/grade level.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _owner(namespace: str) -> str | None:
    match = re.search(r"(?:^|/)avatar:([0-9]+)(?:/|$)", namespace)
    return _positive_uid(match.group(1)) if match else None


def _tags(heroes: list[dict]) -> list[str]:
    known = [hero for hero in heroes if _positive_uid(hero.get("hero_uid")) is not None]
    grades = [hero["evolution_num"] for hero in known]
    result = []
    if any(value is not None and value >= 3 for value in grades):
        result.append("高品（进阶≥3）")
    if any(value == 5 for value in grades):
        result.append("单将满红")
    if len(heroes) == 3 and len(known) == 3 and len({hero["hero_uid"] for hero in known}) == 3 and grades == [5, 5, 5]:
        result.append("整队满红")
    return result


class OpponentRoster:
    """A bounded read of current snapshots and their small relational rows."""

    def __init__(self, db_path, timeout_seconds: float = 10):
        self.data = BattleData(db_path, timeout_seconds=timeout_seconds)

    @staticmethod
    def _empty(namespace):
        return {"available": False, "namespace": namespace, "day": None, "players": [],
                "counts": {"reports": 0, "latest_day_reports": 0, "players": 0, "teams": 0,
                           "opponents_observed": 0, "single_full_red_players": 0,
                           "whole_team_full_red_teams": 0, "high_grade_teams": 0},
                "coverage": {}, "rule": dict(RULE),
                "note": "只保留最近有数据日的名单视图，不删除历史战报。名单代表战报中的观察，不代表实时阵容或全服名单。"}

    @staticmethod
    def _names(connection, snapshot_id: int, cache: dict, *, deadline=None) -> dict:
        if snapshot_id in cache:
            return cache[snapshot_id]
        names: dict[tuple[str, str], set[str]] = defaultdict(set)
        state = BattleData._semantics(connection, snapshot_id)
        if state.get("status") == "ready":
            # Event histories contain many repeated hero/skill references.
            # Resolve only actual settlement slots through the complete PK,
            # not every meaning attached to this snapshot.
            pointers, wanted = set(), {"hero": set(), "skill": set()}
            for row in connection.execute(
                    "SELECT q.side,h.ordinal hero_ordinal,h.hero_uid,k.ordinal skill_ordinal,k.skill_uid "
                    "FROM report_sides q CROSS JOIN report_heroes h ON h.side_id=q.id "
                    "LEFT JOIN report_skills k ON k.hero_id=h.id WHERE q.snapshot_id=?", (snapshot_id,)):
                wanted["hero"].add(_hero_uid(row["hero_uid"]))
                wanted["skill"].add(_hero_uid(row["skill_uid"]))
                for prefix in ("/data", "/components/all", "/components/detail", "/components/basic"):
                    base = f"{prefix}/battleSettlement/{row['side']}/army_member/{row['hero_ordinal']}"
                    pointers.add(base + "/heros_id")
                    if row["skill_ordinal"] is not None:
                        pointers.add(base + f"/skills/{row['skill_ordinal']}/skillId")
            pointers = sorted(pointers)
            for offset in range(0, len(pointers), 200):
                if deadline is not None and time.monotonic() >= deadline:
                    raise BattleQueryTimeout("对手名单名称查询超过时间预算，请按账号缩小范围。")
                chunk = pointers[offset:offset + 200]
                placeholders = ",".join("?" for _ in chunk)
                for row in connection.execute(
                        "SELECT kind,raw_value_json,name_zh FROM report_id_meanings "
                        "INDEXED BY sqlite_autoindex_report_id_meanings_1 "
                        "WHERE snapshot_id=? AND pointer IN (" + placeholders + ") AND version=? "
                        "AND mapped=1 AND kind IN ('hero','skill')", (snapshot_id, *chunk, state["version"])):
                    try:
                        uid = _hero_uid(json.loads(row["raw_value_json"]))
                    except (ValueError, TypeError):
                        continue
                    if uid is not None and uid in wanted[row["kind"]] and row["name_zh"]:
                        names[(row["kind"], uid)].add(row["name_zh"])
        # A conflicting catalog name is not silently picked.
        result = {key: next(iter(values)) for key, values in names.items() if len(values) == 1}
        cache[snapshot_id] = result
        return result

    @staticmethod
    def _team(connection, report: dict, side: dict, names_cache: dict, *, resolve_names=True) -> dict:
        snapshot_id = report["snapshot_id"]
        names = OpponentRoster._names(connection, snapshot_id, names_cache) if resolve_names else {}
        heroes = []
        for hero in connection.execute(
                "SELECT id,ordinal,hero_uid,position,hero_name,level,"
                "json_extract(raw_json,'$.evolutionNum') evolution_num,"
                "json_type(raw_json,'$.evolutionNum') evolution_type "
                "FROM report_heroes WHERE side_id=? ORDER BY ordinal", (side["id"],)):
            uid = _hero_uid(hero["hero_uid"])
            skills = [{"skill_uid": _hero_uid(skill["skill_uid"]),
                       "name_zh": names.get(("skill", str(skill["skill_uid"]))),
                       "level": skill["level"], "order_level": skill["order_level"]}
                      for skill in connection.execute(
                          "SELECT skill_uid,level,order_level FROM report_skills WHERE hero_id=? ORDER BY ordinal",
                          (hero["id"],))]
            grade = _evolution(hero["evolution_num"]) if hero["evolution_type"] == "integer" else None
            heroes.append({"hero_uid": uid, "name_zh": names.get(("hero", str(uid))) or hero["hero_name"] or None,
                           "ordinal": hero["ordinal"], "position": hero["position"], "level": hero["level"],
                           "evolution_num": grade, "evolutionNum": grade, "skills": skills})
        return {"namespace": report["namespace"], "player_uid": side["player_uid"],
                "player_name": side["player_name"] or None, "army_uid": side["army_uid"],
                "battle_uid": report["battle_uid"], "snapshot_id": snapshot_id,
                "side": side["side"], "day": report["day"], "last_seen": report["timestamp"],
                "last_seen_iso": datetime.fromtimestamp(report["timestamp"], timezone.utc).isoformat(),
                "source_time_pointer": report["time_pointer"], "completeness": report["completeness"],
                "heroes": heroes, "team_complete": len(heroes) == 3 and len({hero["hero_uid"] for hero in heroes}) == 3
                    and all(_positive_uid(hero["hero_uid"]) for hero in heroes),
                "unknown_evolution_heroes": sum(hero["evolution_num"] is None for hero in heroes),
                "tags": _tags(heroes)}

    def summary(self, namespace: str | None = None) -> dict:
        if namespace is not None and (not isinstance(namespace, str) or not namespace or len(namespace) > 500):
            raise ValueError("账号范围无效")
        result = self._empty(namespace)
        if not self.data.db_path.exists():
            return result
        started = time.monotonic()
        deadline = started + self.data.timeout_seconds
        coverage = {"invalid_time_reports": 0, "missing_side_reports": 0, "unknown_owner_reports": 0,
                    "unresolved_side_reports": 0, "npc_opponent_reports": 0,
                    "unknown_opponent_id_reports": 0,
                    "reports_with_player_opponents": 0, "incomplete_teams": 0,
                    "unknown_evolution_heroes": 0, "filtered_low_or_unknown_teams": 0,
                    "deduplicated_team_observations": 0}
        with self.data._connection() as connection:
            reports = []
            for row in read_current_metadata(connection, _METADATA_POINTERS, namespace, deadline=deadline):
                if time.monotonic() >= deadline:
                    raise BattleQueryTimeout("对手名单查询超过时间预算，请按账号缩小范围。")
                report = dict(row)
                day, stamp, pointer = _battle_date([report["metadata"].get(pointer) for pointer in _METADATA_POINTERS])
                if day is None:
                    coverage["invalid_time_reports"] += 1
                    continue
                report.update(day=day, timestamp=stamp, time_pointer=pointer)
                reports.append(report)
            result["counts"]["reports"] = len(reports) + coverage["invalid_time_reports"]
            latest = max((row["day"] for row in reports), default=None)
            selected = [row for row in reports if row["day"] == latest]
            sides_by_snapshot = {}
            owner_unions: dict[str, set[str]] = defaultdict(set)
            for report in selected:
                rows = []
                for row in connection.execute(
                        "SELECT id,side,army_uid,player_uid,player_name,"
                        "json_extract(raw_json,'$.avatar_data.avatar_union_id') union_uid "
                        "FROM report_sides WHERE snapshot_id=? ORDER BY side", (report["snapshot_id"],)):
                    value = dict(row)
                    value["canonical_side"] = _SIDE.get(value["side"])
                    value["player_identity"] = _player_identity(value["player_uid"])
                    value["player_kind"] = value["player_identity"][0]
                    value["player_uid"] = value["player_identity"][1] if value["player_kind"] == "player" else None
                    value["union_uid"] = _positive_uid(value["union_uid"])
                    if value["canonical_side"] is not None:
                        rows.append(value)
                sides_by_snapshot[report["snapshot_id"]] = rows
                owner_uid = _owner(report["namespace"])
                for side in rows:
                    if side["player_uid"] == owner_uid and owner_uid and side["union_uid"]:
                        owner_unions[report["namespace"]].add(side["union_uid"])
            latest_teams = {}
            observed_players = set()
            names_cache = {}
            for report in selected:
                if time.monotonic() >= deadline:
                    raise BattleQueryTimeout("对手名单查询超过时间预算，请按账号缩小范围。")
                owner_uid = _owner(report["namespace"])
                if owner_uid is None:
                    coverage["unknown_owner_reports"] += 1
                    continue
                original_sides = sides_by_snapshot[report["snapshot_id"]]
                # Two aliases for the defending side must not create a second
                # opponent. Conflicting aliases remain unresolved instead.
                canonical = defaultdict(list)
                for side in original_sides:
                    canonical[side["canonical_side"]].append(side)
                sides = []
                alias_conflict = False
                for values in canonical.values():
                    identities = {(row["player_identity"], row["army_uid"], row["union_uid"]) for row in values}
                    if len(values) > 1:
                        compositions = {tuple(tuple(item) for item in connection.execute(
                            "SELECT ordinal,hero_uid,position,json_extract(raw_json,'$.evolutionNum') "
                            "FROM report_heroes WHERE side_id=? ORDER BY ordinal", (side["id"],))) for side in values}
                        if len(compositions) != 1:
                            alias_conflict = True
                            break
                    if len(identities) != 1:
                        alias_conflict = True
                        break
                    sides.append(values[0])
                if alias_conflict:
                    coverage["unresolved_side_reports"] += 1
                    continue
                if len(sides) != 2:
                    coverage["missing_side_reports"] += 1
                    continue
                own = [side for side in sides if side["player_uid"] == owner_uid]
                basis = "owner_in_battle"
                if len(own) > 1:
                    coverage["unresolved_side_reports"] += 1
                    continue
                if not own:
                    unions = owner_unions[report["namespace"]]
                    own = [side for side in sides if side["union_uid"] in unions] if len(unions) == 1 else []
                    basis = "same_day_unique_owner_union"
                if len(own) != 1:
                    coverage["unresolved_side_reports"] += 1
                    continue
                opposing = next(side for side in sides if side is not own[0])
                if opposing["player_kind"] == "npc":
                    coverage["npc_opponent_reports"] += 1
                    continue
                if opposing["player_kind"] == "unknown":
                    coverage["unknown_opponent_id_reports"] += 1
                    continue
                coverage["reports_with_player_opponents"] += 1
                observed_players.add((report["namespace"], opposing["player_uid"]))
                team = self._team(connection, report, opposing, names_cache, resolve_names=False)
                team["opponent_basis"] = basis
                # A complete composition identifies a repeated team; unknown
                # composition cannot collapse unrelated incomplete observations.
                composition = tuple((hero["hero_uid"], hero["position"] if hero["position"] is not None else hero["ordinal"])
                                    for hero in team["heroes"])
                identity = composition if composition and all(hero["hero_uid"] for hero in team["heroes"]) else ("unknown", report["snapshot_id"], opposing["id"])
                key = (report["namespace"], opposing["player_uid"], identity)
                current = latest_teams.get(key)
                if current is not None:
                    coverage["deduplicated_team_observations"] += 1
                rank = (team["last_seen"], team["completeness"] == "complete", team["snapshot_id"])
                previous_rank = (current["last_seen"], current["completeness"] == "complete", current["snapshot_id"]) if current else None
                if previous_rank is None or rank > previous_rank:
                    latest_teams[key] = team
            players = {}
            for team in latest_teams.values():
                coverage["incomplete_teams"] += int(not team["team_complete"])
                coverage["unknown_evolution_heroes"] += team["unknown_evolution_heroes"]
                if not team["tags"]:
                    coverage["filtered_low_or_unknown_teams"] += 1
                    continue
                names = self._names(connection, team["snapshot_id"], names_cache, deadline=deadline)
                for hero in team["heroes"]:
                    hero["name_zh"] = names.get(("hero", str(hero["hero_uid"]))) or hero["name_zh"]
                    for skill in hero["skills"]:
                        skill["name_zh"] = names.get(("skill", str(skill["skill_uid"])))
                key = (team["namespace"], team["player_uid"])
                player = players.setdefault(key, {"namespace": key[0], "player_uid": key[1],
                    "player_name": None, "tags": [], "teams": [], "last_seen": 0})
                player["teams"].append(team)
                if team["last_seen"] >= player["last_seen"]:
                    player["last_seen"], player["player_name"] = team["last_seen"], team["player_name"]
                player["tags"] = sorted(set(player["tags"]) | set(team["tags"]))
            ordered = sorted(players.values(), key=lambda item: (-item["last_seen"], item["namespace"], item["player_uid"]))
            for player in ordered:
                player["teams"].sort(key=lambda item: (-item["last_seen"], item["battle_uid"]))
                player["team_count"] = len(player["teams"])
            all_teams = [team for player in ordered for team in player["teams"]]
            result.update(available=True, day=latest, players=ordered, coverage=coverage)
            result["counts"].update(latest_day_reports=len(selected), players=len(ordered), teams=len(all_teams),
                opponents_observed=len(observed_players),
                single_full_red_players=sum("单将满红" in player["tags"] for player in ordered),
                whole_team_full_red_teams=sum("整队满红" in team["tags"] for team in all_teams),
                high_grade_teams=sum("高品（进阶≥3）" in team["tags"] for team in all_teams))
            result["coverage"]["owner_union_ids"] = {key: sorted(values) for key, values in owner_unions.items()}
        result["elapsed_seconds"] = round(time.monotonic() - started, 4)
        return result
