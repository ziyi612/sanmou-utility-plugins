"""Compatibility parser for old captures, with lossless routing of runtime exports.

Runtime JSON is carried as original bytes in DataFrame.attrs['runtime_sources'];
BattleDBWriter sends it to ReportArchive rather than the old summary schema.
"""
import argparse
import json
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from loguru import logger


def _first(data: dict, *keys: str, default=None):
    for key in keys:
        if key in data:
            return data[key]
    return default


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _candidate(raw: dict) -> bool:
    verification = _object(raw.get("verification"))
    kind = str(raw.get("source_kind", raw.get("schema", ""))).lower()
    return (raw.get("method") == "CE_MEMORY_READ" or str(raw.get("url", "")).startswith("ce://")
            or "candidate" in kind or "unverified" in kind or "ce_numeric" in kind
            or verification.get("do_not_treat_as_full_battle_report") is True
            or verification.get("battle_uid_verified_by_ce") is False)


def _runtime_document(value: Any) -> bool:
    if isinstance(value, list):
        return any(_runtime_document(item) for item in value)
    return isinstance(value, dict) and (
        "captureVersion" in value or "components" in value
        or ("battleID" in value and isinstance(value.get("data"), dict)
            and (value.get("source") in ("all", "detail", "basic") or "eventCount" in value)))


def _battles(value: Any) -> list[dict]:
    """Unwrap known historical envelopes without discarding batch members."""
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        raise ValueError("战报内容应为对象或对象列表")
    for key in ("response_body", "battleReport", "battleReports", "data"):
        if key in value:
            return _battles(value[key])
    return [value]


class BattleParser:
    def __init__(self, input_dir: str = "capture/output/raw"):
        self.input_dir = Path(input_dir)
        self.records: list[dict] = []
        self.errors: list[dict] = []
        self.runtime_sources: list[dict] = []

    def parse_all(self) -> pd.DataFrame:
        # A watch loop reuses this instance. Each scan must start cleanly.
        self.records, self.errors, self.runtime_sources = [], [], []
        files = sorted(p for p in self.input_dir.glob("battle_*.json")
                       if not p.name.endswith("_readable.json") and not p.name.startswith("battle_reports_"))
        for path in files:
            try:
                self.records.extend(self._parse_file(path))
            except Exception as exc:
                self.errors.append({"file": str(path), "error": str(exc)})
                logger.error(f"解析失败，原文件保留: {path.name} — {exc}")
        # Object dtype prevents large IDs/timestamps being coerced through float64.
        frame = pd.DataFrame(self.records, dtype=object)
        frame.attrs["runtime_sources"] = list(self.runtime_sources)
        frame.attrs["parse_errors"] = list(self.errors)
        logger.info(f"解析完成: 旧格式 {len(frame)} 条；运行时原文 {len(self.runtime_sources)} 个待归档；失败 {len(self.errors)} 项")
        return frame

    def _parse_file(self, path: Path) -> list[dict]:
        original = path.read_bytes()
        try:
            raw = json.loads(original)
        except (ValueError, UnicodeError):
            if path.name.endswith("_decrypted.json"):
                self.runtime_sources.append({"path": str(path.resolve()), "raw_bytes": original})
                return []
            raise
        metadata = _object(raw)
        if _candidate(metadata):
            raise ValueError("CE 内存候选或未证实数据不能写入正式战报表；原始证据保留")
        if path.name.endswith("_decrypted.json") or _runtime_document(raw):
            self.runtime_sources.append({"path": str(path.resolve()), "raw_bytes": original})
            return []
        records = []
        for index, battle in enumerate(_battles(raw)):
            try:
                if not isinstance(battle, dict):
                    raise ValueError("批量战报中的条目不是对象")
                if _candidate(battle):
                    raise ValueError("候选证据不能当作已验证战报")
                records.append(self._parse_battle(battle, path, metadata, index))
            except Exception as exc:
                self.errors.append({"file": str(path), "document_index": index, "error": str(exc)})
                logger.error(f"解析失败，原文件保留: {path.name}[{index}] — {exc}")
        return records

    def _parse_one(self, fpath: Path) -> Optional[dict]:
        """Compatibility helper: batches must use parse_all, never first-only."""
        records = self._parse_file(fpath)
        if len(records) > 1:
            raise ValueError("文件包含多封战报，请使用 parse_all() 处理全部条目")
        return records[0] if records else None

    def _parse_battle(self, battle: dict, path: Path, metadata: dict, index: int) -> dict:
        uid = _first(battle, "battleId", "battle_id", "battle_uid", "battleID")
        if isinstance(uid, bool) or not isinstance(uid, (str, int)) or not str(uid).strip():
            raise ValueError("缺少有效战报 ID，不能生成合成 ID 或合并空 ID")
        record = {
            "battle_uid": str(uid), "raw_filename": path.name,
            "raw_url": metadata.get("url"), "captured_at": metadata.get("timestamp_iso"),
            "source_index": index,
            "timestamp": _first(battle, "timestamp", "time"),
            "result": _first(battle, "result", "outcome"),
            "total_rounds": _first(battle, "rounds", "totalRounds", "total_rounds"),
            "battle_type": _first(battle, "type", "battleType", "battle_type"),
            # Python/pickle keep the original object. Old SQL columns are only
            # compatibility summaries, not a claim of full-field coverage.
            "raw_data": battle,
        }
        for side in ("attacker", "defender"):
            army = _object(battle.get(side))
            record[side + "_id"] = _first(army, "playerId", "uid", "player_id")
            record[side + "_name"] = _first(army, "name", "playerName", "player_name")
            record[side + "_heroes"] = self._parse_heroes(army.get("heroes"))
            formation = army.get("formation", army.get("formation_name"))
            record[side + "_formation"] = {"name": formation} if isinstance(formation, str) else formation
            for total_key, hero_key, aliases in (
                ("troops_total", "troops", ("troops_total", "troopsTotal")),
                ("damage_total", "damage_dealt", ("damage_total", "damageTotal")),
            ):
                values = [hero.get(hero_key) for hero in record[side + "_heroes"]]
                # Missing constituents imply an unknown sum, not zero.
                derived = sum(values) if values and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values) else None
                record[side + "_" + total_key] = _first(army, *aliases, default=derived)
        return record

    def _parse_heroes(self, heroes_data: Any) -> list[dict]:
        if heroes_data is None:
            return []
        if not isinstance(heroes_data, list):
            raise ValueError("武将明细不是列表")
        result = []
        for hero in heroes_data:
            if not isinstance(hero, dict):
                raise ValueError("武将明细条目不是对象")
            result.append({
                "hero_id": _first(hero, "heroId", "hero_id"),
                "hero_name": _first(hero, "name", "heroName", "hero_name"),
                "rarity": hero.get("rarity"), "troop_type": _first(hero, "troopType", "troop_type"),
                "level": hero.get("level"), "star": _first(hero, "star", "starLevel"),
                "troops": _first(hero, "troops", "soldiers"),
                "troops_lost": _first(hero, "lost", "troopsLost", "troops_lost"),
                "position": _first(hero, "position", "pos"),
                "damage_dealt": _first(hero, "damageDealt", "damage", "damage_dealt"),
                "damage_taken": _first(hero, "damageTaken", "damageReceived", "damage_taken"),
                "damage_healed": _first(hero, "heal", "healed", "damage_healed"),
                "kills": hero.get("kills"), "survived": _first(hero, "survived", "alive"),
                "skills": hero.get("skills") or [],
            })
        return result

    def clean(self, df: pd.DataFrame) -> pd.DataFrame:
        """Retain zero/unknown rounds, large damage and separate observations.

        Database uniqueness handles duplicate IDs. Analysis filters belong later.
        """
        return df.copy()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="解析旧战报，运行时导出转交无损归档")
    parser.add_argument("--input", default="capture/output/raw")
    parser.add_argument("--output", default="data/processed")
    parser.add_argument("--db", default="data/database/battle_reports.db")
    parser.add_argument("--save-db", action="store_true")
    parser.add_argument("--namespace", default="default", help="运行时归档的账号/服务器标识")
    args = parser.parse_args(argv)
    bp = BattleParser(args.input)
    df = bp.clean(bp.parse_all())
    failed = bool(bp.errors)
    if not df.empty:
        from parser.report_archive import _write_path
        out_dir = _write_path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        df.to_pickle(out_dir / "battles.pkl")
        df.to_csv(out_dir / "battles.csv", index=False, encoding="utf-8-sig")
        logger.info(f"旧格式摘要已保存: {out_dir}，{len(df)} 条")
    if args.save_db:
        from parser.db_writer import BattleDBWriter
        writer = BattleDBWriter(args.db)
        try:
            count = writer.insert_battles(df, namespace=args.namespace)
            failed = failed or bool(writer.last_result["errors"])
            logger.info(f"已提交新记录 {count} 条；详情: {writer.last_result}")
        finally:
            writer.engine.dispose()
    elif bp.runtime_sources:
        logger.warning(f"发现 {len(bp.runtime_sources)} 个运行时原始文件；加 --save-db 无损归档，未转换成旧格式摘要")
    if df.empty and not bp.runtime_sources and not bp.errors:
        logger.warning("没有找到可处理的战报文件")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
