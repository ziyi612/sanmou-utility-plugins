"""Historical player lineups for screen recognition, from one read-only account.

Both player sides of the selected account's visible reports are included. The
screen recognizer supplies the red-name observation; these rows alone do not
prove allegiance or the lineup currently deployed in the game.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re

from .data import BattleData


DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "data/database/battle_reports.db"
_ACCOUNT = re.compile(r"^server:[0-9]+/scenario:[0-9]+/avatar:([0-9]+)$")
_CHINA = timezone(timedelta(hours=8))
_TIME_POINTERS = tuple(prefix + key for key in ("endTimeStampTime", "timeStamp")
    for prefix in ("/data/", "/components/all/", "/components/detail/", "/components/basic/"))


def _uid(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    value = str(value).strip()
    return str(int(value)) if value.isascii() and value.isdigit() and int(value) > 0 else None


def _signature(path):
    """Include WAL commits and file replacement; never create missing files."""
    result = []
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            stat = candidate.stat()
            result.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


class EnemyDataLoader:
    """Old recognizer interface with explicit SQLite/JSON source selection.

    A missing namespace auto-selects only a single fully identified account;
    unscoped legacy rows are never combined with it. Ambiguous player names
    are excluded from automatic name matching rather than combining identities.
    """

    def __init__(self, json_path=None, *, db_path=DEFAULT_DATABASE, namespace=None,
                 source="sqlite", max_teams=4, timeout_seconds=15):
        if source not in ("sqlite", "json"):
            raise ValueError("数据源必须为 sqlite 或 json")
        if not isinstance(max_teams, int) or isinstance(max_teams, bool) or not 1 <= max_teams <= 100:
            raise ValueError("max_teams 必须为 1 到 100")
        self.source = source
        self.data = BattleData(db_path, timeout_seconds=timeout_seconds)
        self.json_path = Path(json_path).resolve() if json_path is not None else None
        if source == "json" and self.json_path is None:
            raise ValueError("JSON 模式需要指定数据文件")
        self.requested_namespace = namespace
        self.namespace = None
        self.max_teams = max_teams
        self.player_teams_map = {}
        self.known_player_names = []
        self.ambiguous_names = []
        self.stats = {}
        self._signature = None
        self.load_data()

    def get_teams(self, player_name):
        return self.player_teams_map.get(player_name, [])

    def _choose_namespace(self, connection):
        values = [row[0] for row in connection.execute(
            "SELECT DISTINCT namespace FROM report_records ORDER BY namespace")]
        if self.requested_namespace is not None:
            match = _ACCOUNT.fullmatch(self.requested_namespace)
            if not match or _uid(match.group(1)) is None:
                raise ValueError("必须指定完整 server/scenario/avatar 账号，不能使用 default 或合并账号")
            if self.requested_namespace not in values:
                raise ValueError("所选账号在数据库中没有战报：" + self.requested_namespace)
            return self.requested_namespace
        identified = [value for value in values if _ACCOUNT.fullmatch(value)
                      and _uid(_ACCOUNT.fullmatch(value).group(1))]
        if len(identified) != 1:
            raise ValueError("无法唯一确定账号，请用 --namespace 指定：" + ", ".join(identified))
        # Pin the account after first selection; a later second account cannot
        # silently switch the running recognizer to a different player.
        self.requested_namespace = identified[0]
        return identified[0]

    @staticmethod
    def _names(connection, namespace):
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"report_id_meanings", "report_semantic_settings", "report_semantic_runs"} <= tables:
            return {}
        names = defaultdict(set)
        pointers = ",".join("'" + prefix + "' || sd.side || '/army_member/' || h.ordinal || '/heros_id'"
            for prefix in ("/data/battleSettlement/", "/components/all/battleSettlement/",
                           "/components/detail/battleSettlement/", "/components/basic/battleSettlement/",
                           "/battleSettlement/"))
        for row in connection.execute(
            "SELECT m.snapshot_id,m.raw_value_json,m.name_zh FROM report_records r "
            "JOIN report_sides sd ON sd.snapshot_id=r.current_snapshot_id "
            "JOIN report_heroes h ON h.side_id=sd.id "
            "CROSS JOIN report_id_meanings AS m INDEXED BY sqlite_autoindex_report_id_meanings_1 "
            f"ON m.snapshot_id=r.current_snapshot_id AND m.pointer IN ({pointers}) "
            "JOIN report_semantic_settings a ON a.singleton=1 AND a.active_version=m.version "
            "JOIN report_semantic_runs x ON x.snapshot_id=m.snapshot_id AND x.version=m.version AND x.status='ready' "
            "WHERE r.namespace=? AND m.kind='hero' AND m.mapped=1 AND m.name_zh IS NOT NULL", (namespace,)):
            try:
                uid = _uid(json.loads(row["raw_value_json"]))
            except (ValueError, TypeError):
                continue
            if uid:
                names[(row["snapshot_id"], uid)].add(row["name_zh"])
        return {key: next(iter(values)) for key, values in names.items() if len(values) == 1}

    def _sqlite(self):
        with self.data._connection() as connection:
            namespace = self._choose_namespace(connection)
            owner = _uid(_ACCOUNT.fullmatch(namespace).group(1))
            names = self._names(connection, namespace)
            timestamps = {}
            placeholders = ",".join("?" for _ in _TIME_POINTERS)
            for row in connection.execute(
                "SELECT f.snapshot_id,f.pointer,f.value_json FROM report_records r "
                "JOIN report_fields f ON f.snapshot_id=r.current_snapshot_id "
                f"WHERE r.namespace=? AND f.pointer IN ({placeholders})", (namespace, *_TIME_POINTERS)):
                try:
                    stamp = float(json.loads(row["value_json"]))
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(stamp) and stamp > 0:
                    priority = _TIME_POINTERS.index(row["pointer"])
                    previous = timestamps.get(row["snapshot_id"])
                    if previous is None or priority < previous[0]:
                        timestamps[row["snapshot_id"]] = (priority, stamp)
            query = (
                "SELECT r.battle_uid,r.current_snapshot_id snapshot_id,sd.id side_id,sd.player_uid,sd.player_name,"
                "h.ordinal,h.hero_uid,h.position,h.hero_name "
                "FROM report_records r "
                "JOIN report_sides sd ON sd.snapshot_id=r.current_snapshot_id "
                "JOIN report_heroes h ON h.side_id=sd.id "
                "WHERE r.namespace=? AND sd.side IN ('attacker','defensive','defender') "
                "ORDER BY r.id,sd.id,h.ordinal")
            sides = {}
            for row in connection.execute(query, (namespace,)):
                uid = _uid(row["player_uid"])
                name = (row["player_name"] or "").strip()
                if uid is None or uid == owner or not name:
                    continue
                side = sides.setdefault(row["side_id"], {"uid": uid, "name": name,
                    "battle": row["battle_uid"], "timestamp": timestamps.get(row["snapshot_id"], (0, 0))[1],
                    "heroes": []})
                hero_uid = _uid(row["hero_uid"])
                hero_name = names.get((row["snapshot_id"], hero_uid)) or row["hero_name"]
                label = hero_name or (f"武将#{hero_uid}" if hero_uid else "未知武将")
                side["heroes"].append((hero_uid, row["position"], row["ordinal"], label))
            report_count = connection.execute(
                "SELECT count(*) FROM report_records WHERE namespace=?", (namespace,)).fetchone()[0]
        aliases, teams = defaultdict(set), {}
        # Defender/defensive aliases must not double-count the same battle.
        seen = set()
        for side_id, side in sides.items():
            uid = side["uid"]
            aliases[side["name"]].add(uid)
            heroes = side["heroes"]
            composition = tuple((hero[0], hero[1] if hero[1] is not None else hero[2]) for hero in heroes)
            if not all(hero[0] for hero in heroes):
                composition = ("unknown", side["battle"], side_id)
            key = (uid, composition)
            observation = (side["battle"], key)
            if observation in seen:
                continue
            seen.add(observation)
            try:
                stamp = float(side["timestamp"])
                if not math.isfinite(stamp) or stamp <= 0:
                    stamp = 0
            except (TypeError, ValueError, OverflowError):
                stamp = 0
            team = teams.setdefault(key, {"count": 0, "last_seen": -1, "label": ""})
            team["count"] += 1
            if stamp >= team["last_seen"]:
                team.update(last_seen=stamp, label=" + ".join(hero[3] for hero in heroes))
        by_player = defaultdict(list)
        for (uid, _), team in teams.items():
            by_player[uid].append(team)
        ambiguous = sorted(name for name, ids in aliases.items() if len(ids) != 1)
        mapping = {}
        for name, ids in aliases.items():
            if len(ids) != 1:
                continue
            uid = next(iter(ids))
            ranked = sorted(by_player[uid], key=lambda x: (-x["count"], -x["last_seen"], x["label"]))
            labels = []
            for team in ranked[:self.max_teams]:
                try:
                    last = datetime.fromtimestamp(team["last_seen"], _CHINA).strftime("%m-%d %H:%M")
                except (ValueError, OverflowError, OSError):
                    last = "时间未知"
                if team["last_seen"] == 0:
                    last = "时间未知"
                labels.append(f"{team['label']}  [历史{team['count']}次，最近{last}]")
            mapping[name] = labels
        return mapping, ambiguous, {"source": "sqlite", "namespace": namespace,
            "reports": report_count, "players": len(by_player), "names": len(mapping),
            "teams": len(teams), "ambiguous_names": len(ambiguous)}

    def _json(self):
        records = json.loads(self.json_path.read_text(encoding="utf-8-sig"))
        if not isinstance(records, list):
            raise ValueError("旧版 JSON 数据必须是战报记录数组")
        counters = defaultdict(Counter)
        for record in records:
            if not isinstance(record, dict):
                continue
            name, team = record.get("enemy_player"), record.get("enemy_teams")
            if isinstance(name, str) and name.strip() and isinstance(team, str) and team.strip():
                counters[name.strip()][team.strip()] += 1
        mapping = {name: [team for team, _ in counter.most_common(self.max_teams)]
                   for name, counter in counters.items()}
        return mapping, [], {"source": "json", "namespace": None, "reports": len(records),
            "players": len(mapping), "names": len(mapping), "teams": sum(map(len, mapping.values())),
            "ambiguous_names": 0}

    def load_data(self, *, force=True):
        path = self.data.db_path if self.source == "sqlite" else self.json_path
        signature = _signature(path)
        if not force and signature == self._signature:
            return False
        # Publish only a complete successful read. Keep the previous map on a
        # transient database error, and retry on the following recognition pass.
        mapping, ambiguous, stats = self._sqlite() if self.source == "sqlite" else self._json()
        self.player_teams_map = mapping
        self.known_player_names = sorted(mapping)
        self.ambiguous_names = ambiguous
        self.stats = stats
        self.namespace = stats["namespace"]
        self._signature = signature
        return True

    def reload(self, *, force=False):
        return self.load_data(force=force)
