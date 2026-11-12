"""Read-only daily report and captured battle-merit statistics.

The game renders settlement zgNum as earned merit. originZgNum is a different
source value; barbarianPoint is never substituted. Known sums are bounded by
the reports actually captured and are not presented as complete account totals.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Any

from .data import BattleData, BattleQueryTimeout
from .current_metadata import json_path_pointer, read_current_metadata


CHINA = timezone(timedelta(hours=8))
SCOPES = ("personal", "union", "star")
PREFIXES = ("$.data", "$.components.all", "$.components.detail", "$.components.basic")
META_PATHS = tuple(prefix + "." + key for prefix in PREFIXES
                   for key in ("endTimeStampTime", "timeStamp", "childNum"))
SETTLEMENT_PATHS = tuple(prefix + "." + key for prefix in PREFIXES
                         for key in ("battleID", "battleJobId", "battleSettlement.winer"))
MAX_UNION_CHECKPOINT_AGE = 600


def _uid(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


def _player_uid(value: Any) -> str | None:
    text = _uid(value)
    if text is None:
        return None
    try:
        return str(int(text)) if int(text) > 0 else None
    except ValueError:
        return None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if isinstance(value, int) or math.isfinite(value) else None


def _battle_date(values: list) -> tuple[str | None, float | None, str | None]:
    # End time has priority even if another component contains a start time.
    for offset in (0, 1):
        for index, prefix in enumerate(PREFIXES):
            value = values[index * 3 + offset]
            if isinstance(value, bool) or value in (None, ""):
                continue
            try:
                stamp = float(value)
                if not math.isfinite(stamp) or stamp <= 0:
                    continue
                day = datetime.fromtimestamp(stamp, timezone.utc).astimezone(CHINA).date().isoformat()
                field = "endTimeStampTime" if offset == 0 else "timeStamp"
                return day, stamp, prefix.replace("$", "").replace(".", "/") + "/" + field
            except (ValueError, TypeError, OverflowError, OSError):
                continue
    return None, None, None


def _valid_day(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("日期必须为 YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日期必须为 YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("日期必须为 YYYY-MM-DD")
    return value


def _independent_settlement(node: dict, armies: set[str]) -> bool:
    """A continuous-list root can itself contain one real fight.

    The client displays childNum + 1 fights and keeps the rolling total in
    otherInfo.continuousInfo.zgNum, separately from each side's settlement
    zgNum. A list-only header is insufficient proof: require retained events,
    a modern all/detail capture and consistent independent battle headers.
    """
    if (node["source"] not in ("all", "detail") or node["event_count"] <= 0
            or not isinstance(node["capture_version"], int) or node["capture_version"] < 2
            or len(armies - {"0"}) < 2):
        return False
    headers = set()
    values = node["settlement_headers"]
    for offset in range(0, len(values), 3):
        battle_id, job_id, winner = values[offset:offset + 3]
        if battle_id is None and job_id is None and winner is None:
            continue
        if ((battle_id is not None and _uid(battle_id) != node["battle_uid"])
                or (job_id is not None and (isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0))
                or (winner is not None and (isinstance(winner, bool) or not isinstance(winner, int)
                    or winner in (-1, -2) or (winner != 0 and str(winner) not in armies)))):
            return False
        if battle_id is not None and job_id is not None and winner is not None:
            headers.add((str(battle_id), job_id, winner))
    return len(headers) == 1


class DailyStats:
    """Bounded reads of current snapshots, their discovery links and sides."""

    def __init__(self, db_path, timeout_seconds: float = 10, *, own_union_only: bool = False,
                 runtime_dir: str | Path | None = None):
        if not isinstance(own_union_only, bool):
            raise ValueError("own_union_only must be a boolean")
        self.data = BattleData(db_path, timeout_seconds=timeout_seconds)
        self.own_union_only = own_union_only
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else None

    def _own_unions(self, nodes: dict, sides: list[dict]) -> dict:
        """Use captured numeric identity, never list ownership or player names."""
        contexts = {}
        for namespace in {key[0] for key in nodes}:
            match = re.fullmatch(r"server:(\d+)/scenario:(\d+)/avatar:(\d+)", namespace)
            context = {"status": "unknown", "union_id": None, "union_name": "",
                       "source": None, "player_uid": match[3] if match else None,
                       "reason": "runtime_union_unconfirmed"}
            contexts[namespace] = context
            if match is None:
                continue
            if self.runtime_dir is not None:
                folder = hashlib.sha256(namespace.encode()).hexdigest()[:20]
                path = self.runtime_dir / folder / "collector_state.json"
                try:
                    # This is an existing local checkpoint, not a game read.
                    if path.stat().st_size > 64 * 1024 * 1024:
                        raise ValueError("Runtime checkpoint exceeds the read limit")
                    state = json.loads(path.read_text(encoding="utf-8"))
                    identity = state.get("runtimeIdentity", {})
                    actual = tuple(_uid(identity.get(key)) for key in ("serverId", "scenarioId", "avatarId"))
                    union = state.get("runtimeCapabilities", {}).get("unionId")
                    checked_at = _number(state.get("runtimeCapabilitiesObservedAt"))
                    if checked_at is None:
                        last_cycle = state.get("lastCycle")
                        checked_at = _number(last_cycle.get("startedAt")) if isinstance(last_cycle, dict) else None
                    if (state.get("namespace") == namespace and actual == match.groups()
                            and isinstance(union, int) and not isinstance(union, bool) and union >= 0
                            and checked_at is not None and -60 <= time.time() - checked_at <= MAX_UNION_CHECKPOINT_AGE):
                        context.update(status="identified" if union else "no_union", union_id=str(union),
                                       source="collector_runtime_unionId", observed_at=checked_at,
                                       reason="captured_current_union" if union else "account_has_no_union")
                        continue
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
            # Historical membership is not proof of the account's current
            # union. Missing runtime identity remains explicitly unresolved.
        for row in sides:
            context = contexts.get(row["namespace"], {})
            avatar = row["_avatar"]
            union = avatar.get("avatar_union_id")
            if (context.get("status") == "identified" and not isinstance(union, bool)
                    and str(union) == context["union_id"] and isinstance(avatar.get("avatar_union_name"), str)):
                context["union_name"] = avatar["avatar_union_name"]
        return contexts

    def summary(self, day: str | None = None, namespace: str | None = None,
                player_search: str = "") -> dict:
        day = _valid_day(day)
        if namespace is not None and (not isinstance(namespace, str) or len(namespace) > 500):
            raise ValueError("账号范围无效")
        if not isinstance(player_search, str) or len(player_search) > 200:
            raise ValueError("玩家筛选过长")
        started = time.monotonic()
        deadline = started + self.data.timeout_seconds
        if not self.data.db_path.exists():
            return self._empty(day, namespace, player_search)
        pointers = tuple(json_path_pointer(path) for path in (*META_PATHS, *SETTLEMENT_PATHS))
        params = []
        if namespace:
            params.append(namespace)
        nodes = {}
        with self.data._connection() as connection:
            for row in read_current_metadata(connection, pointers, namespace or None,
                                             include_discovery=True, deadline=deadline):
                if time.monotonic() >= deadline:
                    raise BattleQueryTimeout("每日统计超过时间预算，请按账号缩小范围。")
                metadata = [row["metadata"].get(pointer) for pointer in pointers]
                values = metadata[:len(META_PATHS)]
                battle_day, stamp, pointer = _battle_date(values)
                observations = row["discovery"]
                key = (row["namespace"], row["battle_uid"])
                child_counts = [value for value in values[2::3]
                                if isinstance(value, int) and not isinstance(value, bool) and value >= 0]
                scopes, parents = set(), set()
                for observation in observations[:128]:
                    if observation.get("scope") in SCOPES:
                        scopes.add(observation["scope"])
                    parent = _uid(observation.get("parent"))
                    if parent is not None:
                        parents.add((key[0], parent))
                    child_counts.extend(value for value in (observation.get("children"), observation.get("combat_children"))
                                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0)
                nodes[key] = {"namespace": key[0], "battle_uid": key[1], "snapshot_id": row["snapshot_id"],
                              "completeness": row["completeness"], "date": battle_day, "timestamp": stamp,
                              "source": row["source"], "event_count": row["event_count"],
                              "capture_version": row["capture_version"],
                              "settlement_headers": metadata[len(META_PATHS):],
                              "date_source_pointer": pointer, "scopes": scopes, "parents": parents,
                              "children": set(), "expected_children": max(child_counts) if child_counts else None,
                              "observations_truncated": row["discovery_truncated"] or len(observations) > 128}
            side_query = (
                "SELECT r.namespace,r.battle_uid,q.side,q.army_uid,q.player_uid,q.player_name,q.raw_json "
                "FROM report_records r JOIN report_sides q ON q.snapshot_id=r.current_snapshot_id"
            )
            if namespace:
                side_query += " WHERE r.namespace=?"
            side_rows = [dict(row) for row in connection.execute(side_query, params)]
        for row in side_rows:
            try:
                raw = json.loads(row["raw_json"])
            except (ValueError, TypeError):
                raw = {}
            row["_raw"] = raw if isinstance(raw, dict) else {}
            avatar = row["_raw"].get("avatar_data")
            row["_avatar"] = avatar if isinstance(avatar, dict) else {}
        union_contexts = self._own_unions(nodes, side_rows) if self.own_union_only else {}
        armies_by_report = defaultdict(set)
        for row in side_rows:
            army = _uid(row["_raw"].get("army_id", row["army_uid"]))
            try:
                numeric_army = int(army) if army is not None else 0
            except ValueError:
                continue
            if numeric_army:
                armies_by_report[(row["namespace"], row["battle_uid"])].add(str(numeric_army))
        for key, node in nodes.items():
            for parent in node["parents"]:
                if parent in nodes:
                    nodes[parent]["children"].add(key)

        warnings = []
        def ancestors(key):
            found, pending = set(), list(nodes[key]["parents"])
            for _ in range(1000):
                if not pending:
                    return found, False
                parent = pending.pop()
                if parent == key:
                    return found | {parent}, True
                if parent in found:
                    continue
                found.add(parent)
                if parent in nodes:
                    pending.extend(nodes[parent]["parents"])
            return found, True

        families = []
        for key, node in nodes.items():
            family_ids, cycle = ancestors(key)
            aggregate = bool(node["children"]) or (node["expected_children"] or 0) > 0
            parent_summary = aggregate and not _independent_settlement(node, armies_by_report[key])
            unknown_family = node["expected_children"] is None or node["observations_truncated"] or cycle
            node.update(family_ids=family_ids | ({key} if aggregate else set()), aggregate=aggregate,
                        parent_summary=parent_summary, unknown_family=unknown_family)
            if aggregate:
                expected = node["expected_children"]
                missing = max(0, expected - len(node["children"])) if expected is not None and expected > 0 else None
                families.append({"namespace": key[0], "battle_uid": key[1], "date": node["date"],
                                 "expected_children": expected if expected and expected > 0 else None,
                                 "known_children": len(node["children"]), "missing_children": missing,
                                 "child_battle_ids": sorted(child[1] for child in node["children"]),
                                 "complete": missing == 0 and not cycle and not node["observations_truncated"]})
            if cycle:
                warnings.append({"battle_uid": key[1], "namespace": key[0], "reason": "family_cycle_or_depth_limit"})
        family_lookup = {(row["namespace"], row["battle_uid"]): row for row in families}
        selected = {key: value for key, value in nodes.items() if value["date"] and (day is None or value["date"] == day)}
        participants = defaultdict(list)
        npc_sides = unidentified_sides = 0
        enemy_sides = unknown_union_sides = 0
        eligible_reports = set()
        memberships = defaultdict(set)
        if self.own_union_only:
            for row in side_rows:
                avatar = row["_avatar"]
                uid = _player_uid(avatar.get("avatar_id", row["player_uid"]))
                union = avatar.get("avatar_union_id")
                value = str(union) if isinstance(union, int) and not isinstance(union, bool) and union >= 0 else None
                memberships[(row["namespace"], row["battle_uid"], uid)].add(value)
        for row in side_rows:
            key = (row["namespace"], row["battle_uid"])
            if key not in selected:
                continue
            raw, avatar = row["_raw"], row["_avatar"]
            raw_uid = avatar.get("avatar_id", row["player_uid"])
            uid = _player_uid(raw_uid)
            if uid is None:
                try:
                    is_npc = int(str(raw_uid)) <= 0
                except (ValueError, TypeError):
                    is_npc = False
                npc_sides += int(is_npc)
                unidentified_sides += int(not is_npc)
                continue
            if self.own_union_only:
                context = union_contexts.get(key[0], {})
                union = avatar.get("avatar_union_id")
                valid_union = isinstance(union, int) and not isinstance(union, bool) and union >= 0
                if (context.get("status") != "identified" or not valid_union
                        or len(memberships[(key[0], key[1], uid)]) != 1):
                    unknown_union_sides += 1
                    continue
                if str(union) != context["union_id"]:
                    enemy_sides += 1
                    continue
            eligible_reports.add(key)
            name = avatar.get("avatar_name")
            if not isinstance(name, str) or not name:
                name = row["player_name"] or ""
            participants[(key[0], key[1], uid)].append({"army_uid": _uid(raw.get("army_id", row["army_uid"])),
                "side": row["side"], "player_name": name, "merit": _number(raw.get("zgNum")),
                "merit_present": "zgNum" in raw, "snapshot_id": selected[key]["snapshot_id"]})

        union_excluded_reports = len(selected) - len(eligible_reports) if self.own_union_only else 0
        if self.own_union_only:
            selected = {key: node for key, node in selected.items() if key in eligible_reports}
        days = {}
        for node in selected.values():
            item = days.setdefault(node["date"], {"date": node["date"], "total": 0, "reports": 0,
                "complete": 0, "partial": 0, "legacy": 0, "personal": 0, "union": 0, "star": 0,
                "unknown": 0, "parent_summaries_excluded": 0, "settlement_reports": 0})
            item["total"] += 1
            item["reports"] += 1
            item[node["completeness"]] = item.get(node["completeness"], 0) + 1
            for scope in node["scopes"]:
                item[scope] += 1
            if not node["scopes"] or node["observations_truncated"]:
                item["unknown"] += 1
            item["parent_summaries_excluded"] += int(node["parent_summary"])
            item["settlement_reports"] += int(not node["parent_summary"] and not node["unknown_family"])

        players = {}
        records = []
        for (ns, battle_uid, uid), variants in participants.items():
            node = selected[(ns, battle_uid)]
            player_key = (node["date"], ns, uid)
            item = players.setdefault(player_key, {"date": node["date"], "namespace": ns, "player_uid": uid,
                "player_name": "", "aliases": set(), "battles": 0, "known_merit": 0,
                "known_merit_reports": 0, "unknown_merit_reports": 0, "complete_reports": 0,
                "partial_reports": 0, "legacy_reports": 0, "excluded_parent_reports": 0,
                "unknown_family_reports": 0, "family_ids": set(), "last_name_time": -1})
            for variant in variants:
                if variant["player_name"]:
                    item["aliases"].add(variant["player_name"])
                    if (node["timestamp"] or 0) >= item["last_name_time"]:
                        item["player_name"] = variant["player_name"]
                        item["last_name_time"] = node["timestamp"] or 0
            item["family_ids"].update(node["family_ids"])
            record = {"date": node["date"], "namespace": ns, "battle_uid": battle_uid, "player_uid": uid,
                      "snapshot_id": node["snapshot_id"], "date_source_pointer": node["date_source_pointer"],
                      "counted": False, "known_merit": None}
            if node["parent_summary"]:
                item["excluded_parent_reports"] += 1
                record["reason"] = "parent_summary_excluded"
            elif node["unknown_family"]:
                item["unknown_family_reports"] += 1
                record["reason"] = "family_metadata_unknown"
            else:
                item["battles"] += 1
                status_key = {"complete": "complete_reports", "partial": "partial_reports", "legacy": "legacy_reports"}.get(node["completeness"], "partial_reports")
                item[status_key] += 1
                armies = {variant["army_uid"] for variant in variants}
                merits = {variant["merit"] for variant in variants}
                # Alias sides of the same army are one settlement. Distinct
                # armies for one player in the same report need interpretation,
                # so they do not silently get added or counted as extra battles.
                same_identified_army = len(armies) == 1 and next(iter(armies)) not in (None, "0")
                merit = next(iter(merits)) if len(merits) == 1 and (len(variants) == 1 or same_identified_army) else None
                if merit is None:
                    item["unknown_merit_reports"] += 1
                    record["reason"] = "merit_missing_or_conflicting_sides"
                else:
                    item["known_merit"] += merit
                    item["known_merit_reports"] += 1
                    record.update(known_merit=merit, reason="captured_zgNum")
                record["counted"] = True
            records.append(record)

        query = player_search.strip().casefold()
        visible_players = []
        for item in players.values():
            item["aliases"] = sorted(item["aliases"])
            if query and query not in item["player_uid"].casefold() and not any(query in name.casefold() for name in item["aliases"]):
                continue
            family_ids = item.pop("family_ids")
            unresolved = [family_lookup[key] for key in family_ids
                          if key in family_lookup and not family_lookup[key]["complete"]]
            item["missing_children"] = sum(row["missing_children"] or 0 for row in unresolved)
            item["missing_parent_reports"] = sum(key not in nodes for key in family_ids)
            item["unresolved_families"] = len(unresolved) + item["missing_parent_reports"]
            item["data_complete"] = not (item["unknown_merit_reports"] or item["unknown_family_reports"]
                or item["partial_reports"] or item["legacy_reports"] or item["unresolved_families"] or not item["battles"])
            if not item["known_merit_reports"]:
                item["known_merit"] = None
            item.pop("last_name_time", None)
            visible_players.append(item)
        visible_players.sort(key=lambda row: (row["date"], row["known_merit"] if row["known_merit"] is not None else -math.inf,
                                             row["battles"], row["player_uid"]), reverse=True)
        relevant_keys = set(selected)
        for node in selected.values():
            relevant_keys.update(node["family_ids"])
        relevant_families = [row for key, row in family_lookup.items() if key in relevant_keys]
        known_values = [row["known_merit"] for row in visible_players if row["known_merit"] is not None]
        totals = {"reports": len(selected), "total": len(selected), "complete": sum(row["completeness"] == "complete" for row in selected.values()),
                  "partial": sum(row["completeness"] == "partial" for row in selected.values()),
                  "legacy": sum(row["completeness"] == "legacy" for row in selected.values()),
                  "players": len({(row["namespace"], row["player_uid"]) for row in visible_players}),
                  "player_days": len(visible_players), "settlement_reports": sum(row["settlement_reports"] for row in days.values()),
                  "battles": sum(row["battles"] for row in visible_players),
                  "known_merit": sum(known_values) if known_values else None,
                  "unknown_merit_reports": sum(row["unknown_merit_reports"] for row in visible_players),
                  "known_merit_reports": sum(row["known_merit_reports"] for row in visible_players)}
        coverage = {"account_wide_complete": False,
                    "unknown_date_reports": sum(node["date"] is None for node in nodes.values()),
                    "partial_reports": totals["partial"], "unknown_family_reports": sum(node["unknown_family"] for node in selected.values()),
                    "excluded_parent_reports": sum(node["parent_summary"] for node in selected.values()),
                    "families": relevant_families,
                    "incomplete_families": sum(not row["complete"] for row in relevant_families),
                    "missing_children": sum(row["missing_children"] or 0 for row in relevant_families),
                    "unknown_expected_children_families": sum(row["expected_children"] is None for row in relevant_families),
                    "npc_sides_excluded": npc_sides, "unidentified_sides_excluded": unidentified_sides,
                    "observations_truncated_reports": sum(node["observations_truncated"] for node in selected.values())}
        coverage["missing_parent_reports"] = len({parent for node in selected.values() for parent in node["family_ids"] if parent not in nodes})
        if time.monotonic() >= deadline:
            raise BattleQueryTimeout("每日统计超过时间预算，请按账号缩小范围。")
        return {"available": True, "database_path": str(self.data.db_path), "day": day, "namespace": namespace,
                "player_search": player_search, "dates": sorted({node["date"] for node in nodes.values() if node["date"]}, reverse=True),
                "days": sorted(days.values(), key=lambda row: row["date"], reverse=True), "players": visible_players,
                "totals": totals, "coverage": coverage, "records": records, "warnings": warnings,
                "union_filter": {"enabled": self.own_union_only, "contexts": union_contexts,
                    "excluded_reports": union_excluded_reports, "enemy_sides_excluded": enemy_sides,
                    "unknown_union_sides_excluded": unknown_union_sides,
                    "policy": "captured_avatar_union_id_equals_recent_runtime_unionId",
                    "max_checkpoint_age_seconds": MAX_UNION_CHECKPOINT_AGE},
                "elapsed_seconds": round(time.monotonic() - started, 4),
                "merit_source": {"field": "zgNum", "name_zh": "战报结算武勋（战功）", "originZgNum_used": False,
                                 "barbarianPoint_used": False},
                "family_policy": "按唯一战报逐场累计；连战根报告有独立结算和事件时计入自身一场，纯列表父摘要不计入。连续汇总武勋不参与累计，子项缺失与未知结构单列。",
                "note": "只统计当前最佳版本、已捕获的zgNum。已知累计不等于完整实际总额；分类可能重叠，不能相加。玩家筛选只影响玩家汇总，不改变日期战报分类。"}

    def _empty(self, day, namespace, player_search):
        return {"available": False, "database_path": str(self.data.db_path), "day": day, "namespace": namespace,
                "player_search": player_search, "dates": [], "days": [], "players": [], "records": [], "warnings": [],
                "union_filter": {"enabled": self.own_union_only, "contexts": {}, "excluded_reports": 0,
                                 "enemy_sides_excluded": 0, "unknown_union_sides_excluded": 0,
                                 "policy": "captured_avatar_union_id_equals_recent_runtime_unionId",
                                 "max_checkpoint_age_seconds": MAX_UNION_CHECKPOINT_AGE},
                "totals": {"reports": 0, "total": 0, "complete": 0, "partial": 0, "legacy": 0,
                           "players": 0, "player_days": 0, "settlement_reports": 0, "battles": 0,
                           "known_merit": None, "unknown_merit_reports": 0, "known_merit_reports": 0},
                "coverage": {"account_wide_complete": False, "unknown_date_reports": 0, "missing_children": 0,
                             "incomplete_families": 0, "families": [], "partial_reports": 0, "unknown_family_reports": 0,
                             "excluded_parent_reports": 0, "unknown_expected_children_families": 0,
                             "npc_sides_excluded": 0, "unidentified_sides_excluded": 0,
                             "observations_truncated_reports": 0, "missing_parent_reports": 0},
                "merit_source": {"field": "zgNum", "name_zh": "战报结算武勋（战功）",
                                 "originZgNum_used": False, "barbarianPoint_used": False},
                "family_policy": "按唯一战报逐场累计；有独立结算的连战根报告计入自身一场，纯列表父摘要与连续汇总不累计，缺子项单独显示。", "elapsed_seconds": 0,
                "note": "数据库尚未创建；没有将缺失数据当作零。"}
