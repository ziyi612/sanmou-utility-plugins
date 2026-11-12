"""One observed client-confirmed peerless occurrence and its own participant.

The historical game's thresholds are deliberately not guessed here. The list
collector evaluates the loaded client's IsLegend function alongside the raw
continuousInfo and retains its inputs. Missing or conflicting evidence stays
unverified. This view never fetches a person's unrelated battles or writes DB.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import time

from .data import BattleData, BattleQueryTimeout
from .daily import CHINA, META_PATHS, _battle_date
from .current_metadata import read_current_metadata, read_indexed_subtree


EVALUATOR = "client_BattleReportHelper.IsLegend"
PREFIXES = ("/data", "/components/basic", "/components/all", "/components/detail")
_DATE_POINTERS = tuple(path.removeprefix("$").replace(".", "/") for path in META_PATHS)
_IDENTITY_FIELDS = ("serverId", "scenarioId", "avatarId")
_POINTERS = tuple(dict.fromkeys((*_DATE_POINTERS, "/runtimeIdentity", "/discovery",
    *("/runtimeIdentity/" + field for field in _IDENTITY_FIELDS),
    *(prefix + "/" + field for prefix in PREFIXES
      for field in ("otherInfo/continuousInfo", "blockHash", "battleID", "endTimeStampTime")))))


def _point_values(connection, snapshot_id, pointers):
    """Read only the explicitly named leaves/container markers of one version."""
    placeholders = ",".join("?" for _ in pointers)
    return {row["pointer"]: json.loads(row["value_json"]) for row in connection.execute(
        "SELECT pointer,value_json FROM report_fields INDEXED BY sqlite_autoindex_report_fields_1 "
        "WHERE snapshot_id=? AND pointer IN (" + placeholders + ")", (snapshot_id, *pointers))}


def _subtree(connection, snapshot_id, pointer, deadline):
    value, truncated = read_indexed_subtree(connection, snapshot_id, pointer,
                                           max_nodes=65536, deadline=deadline)
    if truncated:
        # Do not confirm a client evaluation using a partly read evidence tree.
        raise BattleQueryTimeout("无双记录的单项元数据过大，本次未返回不完整的判定依据。")
    return value


def _observed_metadata(connection, source, observation, deadline):
    prefix = observation["pointer"]
    ci_path = prefix + "/row/otherInfo/continuousInfo"
    combat_ci_path = prefix + "/row/combatInfo/otherInfo/continuousInfo"
    evaluation_path = prefix + "/clientPeerless"
    pointers = (ci_path, combat_ci_path, evaluation_path, prefix + "/row/blockHash",
                prefix + "/row/combatInfo/blockHash", prefix + "/row/combatInfo/battleID",
                prefix + "/row/combatInfo/endTimeStampTime")
    values = _point_values(connection, source["snapshot_id"], pointers)
    if not isinstance(values.get(ci_path), dict) and not isinstance(values.get(combat_ci_path), dict):
        return None
    block = values.get(prefix + "/row/blockHash")
    if block is None:
        block = values.get(prefix + "/row/combatInfo/blockHash")
    index = observation["index"]
    if isinstance(source["metadata"].get("/discovery"), list):
        index = int(index)
    return {"index": index, "parent": observation.get("parent"), "block": block,
            "battle": values.get(prefix + "/row/combatInfo/battleID"),
            "end_time": values.get(prefix + "/row/combatInfo/endTimeStampTime"),
            "ci": _subtree(connection, source["snapshot_id"], ci_path, deadline)
                  if isinstance(values.get(ci_path), dict) else None,
            "combat_ci": _subtree(connection, source["snapshot_id"], combat_ci_path, deadline)
                         if isinstance(values.get(combat_ci_path), dict) else None,
            "evaluations": _subtree(connection, source["snapshot_id"], evaluation_path, deadline)
                           if isinstance(values.get(evaluation_path), list) else None,
            "observation_pointer": prefix}


def _uid(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return str(int(text)) if len(text) <= 64 and text.isascii() and text.isdigit() and int(text) > 0 else None


def _number(value, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if (isinstance(value, float) and not math.isfinite(value)) or value < 0 or (integer and int(value) != value):
        return None
    return value


def _hash(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text if text and text != "0" else None


def _when(value):
    if _number(value) is None or value <= 0:
        return None, None
    try:
        return value, datetime.fromtimestamp(value, CHINA).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None, None


def _account_matches(namespace, identity):
    if identity is None:
        return True
    if not isinstance(identity, dict):
        return False
    parts = dict(part.split(":", 1) for part in namespace.split("/") if ":" in part)
    if not all(key in parts for key in ("server", "scenario", "avatar")):
        return True
    for part, field in (("server", "serverId"), ("scenario", "scenarioId"), ("avatar", "avatarId")):
        value = identity.get(field)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return False
        text = str(value)
        if not text.isascii() or not text.isdigit() or not parts[part].isascii() or not parts[part].isdigit():
            return False
        if int(text) != int(parts[part]):
            return False
    return True


def _matches(evaluation, candidate, namespace, battle_uid, category="normal"):
    """Bind a real evaluation to this exact observed row, never another player."""
    ci = candidate["ci"]
    if not isinstance(evaluation, dict) or type(evaluation.get("version")) is not int or evaluation["version"] != 1:
        return False
    if evaluation.get("evaluator") != EVALUATOR or evaluation.get("status") != ("evaluated" if category == "normal" else "excluded"):
        return False
    if category == "normal" and type(evaluation.get("legend")) is not bool:
        return False
    if evaluation.get("namespace", namespace) != namespace or _uid(evaluation.get("battleID")) != _uid(battle_uid):
        return False
    if _uid(evaluation.get("avatarId")) != _uid(ci.get("avatarId")):
        return False
    if ci.get("isMuti") is not True or evaluation.get("isMuti") is not True:
        return False
    rule = evaluation.get("rule")
    if not isinstance(rule, dict) or evaluation.get("category") != category or evaluation.get("category_state") != "runtime_enum":
        return False
    normal = _number(rule.get("normalType"), integer=True)
    required_type = normal if category == "normal" else _number(rule.get("pvBarbarianArmyType"), integer=True)
    actual_type = ci.get("continuousInfoType", normal)
    if required_type is None or _number(actual_type, integer=True) != required_type or _number(evaluation.get("continuousInfoType"), integer=True) != required_type:
        return False
    for field, integer in (("zgNum", False), ("winBattleNum", True)):
        actual = _number(ci.get(field), integer=integer)
        if actual is None or _number(evaluation.get(field), integer=integer) != actual:
            return False
    # The UI passes this exact combatInfo field, not the import date or a sum
    # reconstructed from other reports. A missing timestamp remains explicit.
    expected_time = candidate.get("end_time")
    if expected_time is not None and (_number(expected_time) is None or expected_time <= 0):
        return False
    if expected_time != evaluation.get("endTimeStamp"):
        return False
    return True


def _assessment_key(item):
    value = item["evaluation"]
    # A running streak can legitimately progress from false to true. Only
    # incompatible answers for the same bound inputs and rule are conflicts.
    inputs = {key: value.get(key) for key in ("battleID", "avatarId", "isMuti", "continuousInfoType",
        "zgNum", "winBattleNum", "endTimeStamp", "rule", "helper_bytecode_sha256")}
    inputs["battleID"], inputs["avatarId"] = _uid(inputs["battleID"]), _uid(inputs["avatarId"])
    return json.dumps(inputs, sort_keys=True, ensure_ascii=False)


class PeerlessRecords:
    def __init__(self, db_path, timeout_seconds=10):
        self.data = BattleData(db_path, timeout_seconds=timeout_seconds)

    def summary(self, namespace=None):
        if namespace is not None and (not isinstance(namespace, str) or not namespace or len(namespace) > 500):
            raise ValueError("账号范围无效")
        started = time.monotonic()
        deadline = started + self.data.timeout_seconds
        coverage = {key: 0 for key in ("total_reports", "reports_with_continuous_info", "missing_continuous_info",
            "unverified_occurrences", "invalid_metadata", "excluded_not_multi", "excluded_barbarian",
            "unknown_player_ids", "conflicting_evaluations", "missing_player_details", "fallback_battle_id_groups",
            "unknown_date_records", "identity_conflicts")}
        result = {"available": False, "namespace": namespace, "dates": [], "records": [],
            "counts": {"occurrences": 0, "players": 0, "reports": 0}, "coverage": coverage,
            "note": "仅记录当前客户端明确确认的天下无双；一条记录只关联那次连战和该人。缺少连战汇总或判定依据，不等于未打出无双。",
            "rule": {"evaluator": EVALUATOR, "historical_thresholds_assumed": False,
                "deduplication": "namespace + blockHash + continuousInfo.avatarId；无blockHash时退回战报ID",
                "scope": "已采集的全部日期；双方真人均可，不限于对手或高品名单"}}
        if not self.data.db_path.is_file():
            return result

        def check_time():
            if time.monotonic() >= deadline:
                raise BattleQueryTimeout("无双记录查询超过时间预算，请按账号缩小范围。")

        groups, root_blocks = {}, {}
        with self.data._connection() as connection:
            for source in read_current_metadata(connection, _POINTERS, namespace,
                    include_discovery=True, discovery_limit=None, deadline=deadline):
                check_time()
                source = dict(source)
                metadata = source["metadata"]
                coverage["total_reports"] += 1
                identity = metadata.get("/runtimeIdentity")
                if isinstance(identity, dict):
                    identity = {field: metadata["/runtimeIdentity/" + field] for field in _IDENTITY_FIELDS
                                if "/runtimeIdentity/" + field in metadata}
                if not _account_matches(source["namespace"], identity):
                    coverage["identity_conflicts"] += 1
                    continue
                default_day, default_stamp, default_pointer = _battle_date([metadata.get(pointer) for pointer in _DATE_POINTERS])
                candidates = []
                for prefix in PREFIXES:
                    ci_pointer = prefix + "/otherInfo/continuousInfo"
                    if isinstance(metadata.get(ci_pointer), dict):
                        candidates.append({"ci": _subtree(connection, source["snapshot_id"], ci_pointer, deadline),
                            "block": metadata.get(prefix + "/blockHash"), "battle": metadata.get(prefix + "/battleID"),
                            "end_time": metadata.get(prefix + "/endTimeStampTime"), "pointer": ci_pointer,
                            "time_pointer": prefix + "/endTimeStampTime", "evaluations": []})
                for observed in source["discovery"]:
                    # Child combat rows have no continuous block of their own.
                    if observed.get("parent") is not None:
                        continue
                    observation = _observed_metadata(connection, source, observed, deadline)
                    if observation is None:
                        continue
                    prefix = observation["observation_pointer"] + "/row"
                    for field, suffix in (("ci", "/otherInfo/continuousInfo"), ("combat_ci", "/combatInfo/otherInfo/continuousInfo")):
                        if isinstance(observation.get(field), dict):
                            candidates.append({**observation, "ci": observation[field], "pointer": prefix + suffix,
                                "time_pointer": prefix + "/combatInfo/endTimeStampTime",
                                "observation_index": observation["index"], "evaluation_pointer": observation["observation_pointer"] + "/clientPeerless"})
                coverage["reports_with_continuous_info"] += int(bool(candidates))
                coverage["missing_continuous_info"] += int(not candidates)
                for candidate in candidates:
                    check_time()
                    ci = candidate["ci"]
                    uid = _uid(ci.get("avatarId"))
                    if uid is None:
                        coverage["unknown_player_ids"] += 1
                        continue
                    if ci.get("isMuti") is not True:
                        coverage["excluded_not_multi"] += 1
                        continue
                    if _uid(candidate.get("battle")) != _uid(source["battle_uid"]):
                        coverage["invalid_metadata"] += 1
                        continue
                    block = _hash(candidate.get("block"))
                    if block:
                        root_blocks.setdefault((source["namespace"], source["battle_uid"], uid), set()).add(block)
                    group_key = (source["namespace"], block or source["battle_uid"], uid, bool(block))
                    group = groups.setdefault(group_key, {"true": [], "false": [], "unverified": False,
                                                          "fallback": not bool(block), "barbarian": False})
                    evaluations = candidate.get("evaluations")
                    matched = []
                    for evaluation_index, evaluation in enumerate(evaluations if isinstance(evaluations, list) else []):
                        if _matches(evaluation, candidate, source["namespace"], source["battle_uid"], category="barbarian"):
                            group["barbarian"] = True
                        if _matches(evaluation, candidate, source["namespace"], source["battle_uid"]):
                            matched.append((evaluation_index, evaluation))
                    if not matched:
                        group["unverified"] = True
                        continue
                    stamp, day = _when(candidate.get("end_time"))
                    stamp, day = (stamp, day) if stamp is not None else (default_stamp, default_day)
                    for evaluation_index, evaluation in matched:
                        item = {"source": source, "candidate": candidate, "evaluation": evaluation,
                                "evaluation_pointer": candidate["evaluation_pointer"] + f"/{evaluation_index}",
                                "timestamp": stamp, "day": day,
                                "date_pointer": candidate["time_pointer"] if candidate.get("end_time") else default_pointer}
                        group["true" if evaluation["legend"] else "false"].append(item)

            # A later list observation can supply the blockHash missing from an
            # older observation of this exact root/player. Resolve that alias
            # instead of counting the fallback and its known block twice.
            for key, group in list(groups.items()):
                ns, root_id, uid, has_block = key
                if has_block:
                    continue
                blocks = root_blocks.get((ns, root_id, uid), set())
                if len(blocks) == 1:
                    resolved_key = (ns, next(iter(blocks)), uid, True)
                    target = groups.setdefault(resolved_key, {"true": [], "false": [], "unverified": False,
                                                              "fallback": False, "barbarian": False})
                    target["true"].extend(group["true"])
                    target["false"].extend(group["false"])
                    target["unverified"] |= group["unverified"]
                    target["barbarian"] |= group["barbarian"]
                    del groups[key]
                elif len(blocks) > 1:
                    coverage["unverified_occurrences"] += 1
                    del groups[key]

            for (ns, group_id, uid, has_block), group in groups.items():
                check_time()
                if group["barbarian"]:
                    coverage["excluded_barbarian"] += 1
                    continue
                true_keys = {_assessment_key(item) for item in group["true"]}
                false_keys = {_assessment_key(item) for item in group["false"]}
                conflicts = true_keys & false_keys
                coverage["conflicting_evaluations"] += len(conflicts)
                confirmed = [item for item in group["true"] if _assessment_key(item) not in conflicts]
                if not confirmed:
                    coverage["unverified_occurrences"] += int(bool(conflicts) or (group["unverified"] and not group["false"]))
                    continue
                selected = max(confirmed, key=lambda item: (item["timestamp"] or 0,
                    item["source"]["snapshot_id"], item["candidate"].get("observation_index", -1)))
                source, candidate = selected["source"], selected["candidate"]
                avatar, side = self._participant(connection, source["snapshot_id"], uid, candidate.get("observation_index"),
                    observation_pointer=candidate.get("observation_pointer"), deadline=deadline)
                if not avatar:
                    coverage["missing_player_details"] += 1
                coverage["fallback_battle_id_groups"] += int(not has_block)
                coverage["unknown_date_records"] += int(not selected["day"])
                evidence, retained = [], set()
                for item in confirmed:
                    ci_pointer = item["candidate"]["pointer"]
                    for kind, pointer, value in (("continuous_info", ci_pointer, item["candidate"]["ci"]),
                            ("client_evaluation", item["evaluation_pointer"], item["evaluation"])):
                        key = (item["source"]["snapshot_id"], pointer, json.dumps(value, sort_keys=True, ensure_ascii=False))
                        if key not in retained:
                            retained.add(key)
                            evidence.append({"kind": kind, "pointer": pointer, "value": value,
                                "snapshot_id": item["source"]["snapshot_id"], "battle_uid": item["source"]["battle_uid"]})
                result["records"].append({"namespace": ns, "battle_uid": source["battle_uid"],
                    "snapshot_id": source["snapshot_id"], "player_uid": uid,
                    "player_name": avatar.get("avatar_name") or None, "side": side,
                    "day": selected["day"], "timestamp": selected["timestamp"],
                    "union_name": avatar.get("avatar_union_name") or None,
                    "union_uid": _uid(avatar.get("avatar_union_id")), "avatar_data": avatar,
                    "evidence": evidence, "completeness": source["completeness"],
                    "block_hash": group_id if has_block else None,
                    "win_battle_num": candidate["ci"].get("winBattleNum"), "merit": candidate["ci"].get("zgNum"),
                    "date_source_pointer": selected["date_pointer"], "deduplication_fallback": not has_block})
        result["records"].sort(key=lambda row: (row["timestamp"] or 0, row["namespace"], row["battle_uid"], row["player_uid"]), reverse=True)
        result["dates"] = sorted({row["day"] for row in result["records"] if row["day"]}, reverse=True)
        result["counts"] = {"occurrences": len(result["records"]),
            "players": len({(row["namespace"], row["player_uid"]) for row in result["records"]}),
            "reports": len({(row["namespace"], row["battle_uid"]) for row in result["records"]})}
        result.update(available=True, elapsed_seconds=round(time.monotonic() - started, 4))
        result["coverage_note"] = (f"已查 {coverage['total_reports']} 封当前战报；"
            f"有连战汇总 {coverage['reports_with_continuous_info']} 封，无连战汇总 {coverage['missing_continuous_info']} 封；"
            f"尚待核实的连战 {coverage['unverified_occurrences']} 次，判定冲突 {coverage['conflicting_evaluations']} 次。")
        check_time()
        return result

    @staticmethod
    def _participant(connection, snapshot_id, uid, observation_index, *, observation_pointer=None, deadline=None):
        matches = []
        for row in connection.execute("SELECT side,raw_json FROM report_sides WHERE snapshot_id=?", (snapshot_id,)):
            raw = json.loads(row["raw_json"])
            avatar = raw.get("avatar_data") if isinstance(raw, dict) else None
            if isinstance(avatar, dict) and _uid(avatar.get("avatar_id")) == uid:
                matches.append((avatar, row["side"]))
        if not matches and observation_index is not None:
            prefix = observation_pointer or f"/discovery/{int(observation_index)}"
            for side in ("attacker", "defensive", "defender"):
                avatar = _subtree(connection, snapshot_id,
                    prefix + "/row/combatInfo/battleSettlement/" + side + "/avatar_data", deadline)
                if isinstance(avatar, dict) and _uid(avatar.get("avatar_id")) == uid:
                    matches.append((avatar, side))
        if not matches:
            return {}, None
        # Ambiguous aliases cannot silently attach another side's profile.
        profiles = {json.dumps(value[0], sort_keys=True, ensure_ascii=False) for value in matches}
        return matches[0] if len(profiles) == 1 else ({}, None)
