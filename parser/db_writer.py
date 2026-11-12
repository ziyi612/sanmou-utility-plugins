"""Write historical summary rows atomically; route runtime bytes to ReportArchive."""
import json
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from parser.schema import Base, Hero, BattleRecord, BattleHero, BattleFormation, SkillUsage


def _null(value):
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if not isinstance(value, (dict, list, tuple, str, bytes)) and pd.isna(value):
        return None
    return value


def _integer(value):
    value = _null(value)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean cannot be used as an integer field")
    parsed = int(value)
    if isinstance(value, float) and value != parsed:
        raise ValueError(f"Fractional value in integer field: {value}")
    return parsed


def _string(value):
    value = _null(value)
    return None if value is None else str(value)


def _boolean(value):
    value = _null(value)
    if value is None:
        return None
    if value in (True, 1, "true", "True", "1"):
        return True
    if value in (False, 0, "false", "False", "0"):
        return False
    raise ValueError(f"Invalid boolean field: {value!r}")


def _first(data, *keys):
    for key in keys:
        if key in data:
            return data[key]
    return None


class BattleDBWriter:
    def __init__(self, db_path: str = "data/database/battle_reports.db"):
        from parser.report_archive import _write_path
        path = _write_path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        self.engine = create_engine(f"sqlite:///{path.as_posix()}", echo=False)

        @event.listens_for(self.engine, "connect")
        def _settings(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA busy_timeout=30000")

        Base.metadata.create_all(self.engine)
        self.last_result = {"inserted_legacy": 0, "inserted_archived": 0, "duplicates": 0, "errors": []}

    def insert_battles(self, df: pd.DataFrame, namespace: str = "default") -> int:
        """Return committed new records, excluding duplicates and failed rows."""
        self.last_result = {"inserted_legacy": 0, "inserted_archived": 0, "duplicates": 0,
                            "errors": list(df.attrs.get("parse_errors", []))}
        sources = df.attrs.get("runtime_sources", [])
        if sources:
            from parser.report_archive import ReportArchive
            with ReportArchive(self.db_path) as archive:
                for source in sources:
                    result = archive.import_bytes(source["raw_bytes"], source_path=source["path"], namespace=namespace)
                    if result["status"] == "imported":
                        self.last_result["inserted_archived"] += len(result["snapshot_ids"])
                    elif result["status"] == "duplicate":
                        self.last_result["duplicates"] += len(result["snapshot_ids"])
                    else:
                        self.last_result["errors"].append(result)
        for _, row in df.iterrows():
            try:
                # Each row commits independently; a later failure cannot undo
                # earlier battles or inflate the successful result count.
                with Session(self.engine) as session, session.begin():
                    inserted = self._insert_one(session, row)
                self.last_result["inserted_legacy" if inserted else "duplicates"] += 1
            except Exception as exc:
                issue = {"battle_uid": _string(row.get("battle_uid")), "error": str(exc)}
                self.last_result["errors"].append(issue)
                logger.error(f"插入失败，其他已提交记录保留: {issue}")
        count = self.last_result["inserted_legacy"] + self.last_result["inserted_archived"]
        logger.info(f"数据库已提交 {count} 条新记录；重复 {self.last_result['duplicates']}；错误 {len(self.last_result['errors'])}")
        return count

    def _insert_one(self, session: Session, row) -> bool:
        uid = _string(row.get("battle_uid"))
        if not uid or not uid.strip():
            raise ValueError("Missing battle_uid")
        if str(row.get("raw_url", "")).startswith("ce://") or "unverified" in str(row.get("source_kind", "")):
            raise ValueError("Unverified CE candidates cannot enter battle_records")
        if session.query(BattleRecord.id).filter_by(battle_uid=uid).first():
            return False
        timestamp = _integer(row.get("timestamp"))
        converted = pd.to_datetime(timestamp, unit="ms", errors="coerce") if timestamp is not None else None
        time_iso = None if converted is None or pd.isna(converted) else converted.to_pydatetime()
        battle = BattleRecord(
            battle_uid=uid, timestamp=timestamp, time_iso=time_iso,
            attacker_id=_string(row.get("attacker_id")), attacker_name=_string(row.get("attacker_name")),
            defender_id=_string(row.get("defender_id")), defender_name=_string(row.get("defender_name")),
            result=_string(row.get("result")), total_rounds=_integer(row.get("total_rounds")),
            battle_type=_string(row.get("battle_type")),
            attacker_troops_total=_integer(row.get("attacker_troops_total")),
            defender_troops_total=_integer(row.get("defender_troops_total")),
            attacker_damage_total=_integer(row.get("attacker_damage_total")),
            defender_damage_total=_integer(row.get("defender_damage_total")),
            raw_filename=_string(row.get("raw_filename")), raw_url=_string(row.get("raw_url")),
        )
        session.add(battle)
        session.flush()
        for side in ("attacker", "defender"):
            heroes = _null(row.get(side + "_heroes"))
            if isinstance(heroes, str):
                heroes = json.loads(heroes) if heroes else []
            if heroes is None:
                heroes = []
            if not isinstance(heroes, list):
                raise ValueError("Hero details must be a list")
            for hero in heroes:
                if not isinstance(hero, dict):
                    raise ValueError("Hero detail must be an object")
                hero_id = _integer(hero.get("hero_id"))
                hero_id = hero_id if hero_id else None
                self._ensure_hero(session, hero_id, hero.get("hero_name"))
                bh = BattleHero(
                    battle_id=battle.id, hero_id=hero_id, side=side,
                    position=_integer(hero.get("position")), level=_integer(hero.get("level")),
                    star=_integer(hero.get("star")), troops=_integer(hero.get("troops")),
                    troops_lost=_integer(hero.get("troops_lost")), damage_dealt=_integer(hero.get("damage_dealt")),
                    damage_taken=_integer(hero.get("damage_taken")), damage_healed=_integer(hero.get("damage_healed")),
                    kills=_integer(hero.get("kills")), survived=_boolean(hero.get("survived")),
                )
                session.add(bh)
                session.flush()
                for skill in hero.get("skills") or []:
                    if isinstance(skill, str):
                        skill = {"name": skill}
                    if not isinstance(skill, dict):
                        raise ValueError("Skill detail must be a string or object")
                    session.add(SkillUsage(
                        battle_id=battle.id, battle_hero_id=bh.id,
                        skill_name=_string(_first(skill, "name", "skillName")),
                        skill_type=_string(_first(skill, "type", "skill_type")),
                        casts=_integer(skill.get("casts")),
                        total_damage=_integer(_first(skill, "totalDamage", "total_damage")),
                        total_heal=_integer(_first(skill, "totalHeal", "total_heal")),
                        targets_hit=_integer(_first(skill, "targetsHit", "targets_hit")),
                    ))
            formation = _null(row.get(side + "_formation"))
            if isinstance(formation, str):
                formation = json.loads(formation) if formation.lstrip().startswith("{") else {"name": formation}
            if formation:
                if not isinstance(formation, dict):
                    raise ValueError("Formation must be an object or name")
                session.add(BattleFormation(
                    battle_id=battle.id, side=side,
                    formation_name=_string(_first(formation, "name", "formationName")),
                    hero_ids=json.dumps(formation.get("heroIds"), ensure_ascii=False),
                    hero_positions=json.dumps(formation.get("positions"), ensure_ascii=False),
                ))
        session.flush()
        return True

    def _ensure_hero(self, session: Session, hero_id: Optional[int], hero_name):
        if hero_id is None:
            return
        if session.query(Hero.id).filter_by(hero_id=hero_id).first() is None:
            session.add(Hero(hero_id=hero_id, name=_string(hero_name) or ""))
            session.flush()

    def query_battles(self, limit: int = 10) -> pd.DataFrame:
        sql = text("""SELECT battle_uid,result,total_rounds,attacker_name,defender_name,
                            attacker_damage_total,defender_damage_total
                       FROM battle_records ORDER BY id DESC LIMIT :limit""")
        with self.engine.connect() as connection:
            return pd.read_sql(sql, connection, params={"limit": max(0, int(limit))})


if __name__ == "__main__":
    writer = BattleDBWriter()
    try:
        print(writer.query_battles())
    finally:
        writer.engine.dispose()
