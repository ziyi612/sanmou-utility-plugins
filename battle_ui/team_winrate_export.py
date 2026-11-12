"""Export an already computed lineup result without opening the game or database."""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from parser.report_archive import _write_path
from .winrate_sorting import MIN_KNOWN_RESULTS, win_rate_sort_key


_ROOT = Path(__file__).resolve().parents[1]
_TIER_LABELS = {"low": "低红", "medium": "中红", "high": "高满", "unknown": "属性未知", "unclassified": "未设置门槛"}
_COUNTS = ("samples", "wins", "draws", "losses", "unknown_results")


def _text(value):
    """Keep text literal and make invalid XML code points visible, never drop them."""
    if value is None:
        return ""
    text = str(value)
    text = "".join(ch if ch in "\t\n\r" or 32 <= ord(ch) <= 0xD7FF or 0xE000 <= ord(ch) <= 0xFFFD
                   or 0x10000 <= ord(ch) <= 0x10FFFF else f"\\u{ord(ch):04x}" for ch in text)
    if len(text) > 32766:
        raise ValueError("一个导出单元格超过 Excel 长度上限，请缩小日期范围后导出。")
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def _count(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"统计缓存中的{name}不是有效计数，请重新统计。")
    return value


def _stats(row):
    values = [_count(row.get(key), key) for key in _COUNTS]
    samples, wins, draws, losses, unknown = values
    known = wins + draws + losses
    if samples != known + unknown or row.get("known_results", known) != known:
        raise ValueError("统计缓存中的胜平负与样本数不一致，请重新统计。")
    for key, expected in (("win_rate", wins / known if known else None), ("draw_rate", draws / known if known else None)):
        value = row.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or expected is None or abs(value - expected) > 1e-12):
            raise ValueError("统计缓存中的比例与胜平负不一致，请重新统计。")
    return [*values, known]


def _attribute(row, key, bound):
    value = row.get(key + "_" + bound, row.get(key))
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 15:
        raise ValueError("统计缓存中的总红或总品无效，请重新统计。")
    return value


def _heroes(row):
    heroes = row.get("heroes", [])
    if not isinstance(heroes, list) or any(not isinstance(hero, dict) for hero in heroes):
        raise ValueError("统计缓存中的武将列表无效，请重新统计。")
    return sorted(heroes, key=lambda hero: str(hero.get("hero_uid") or ""))


def _identity(row):
    heroes = _heroes(row)
    ids = [str(hero.get("hero_uid") or "") for hero in heroes]
    if len(ids) == 3 and len(set(ids)) == 3 and all(uid.isascii() and uid.isdigit() and int(uid) > 0 for uid in ids):
        return (row.get("tier", "unknown"), tuple(ids))
    # Unidentified lineups must not be collapsed into a fictitious common team.
    return ("unknown", _text(row.get("key")))


def _sources(row, field):
    return _text("\n".join(sorted({str(source[field]) for source in row.get("sources", [])
                                  if isinstance(source, dict) and source.get(field) is not None})))


@lru_cache(maxsize=1)
def _catalog_names():
    """Reuse a small retained translation catalog only for missing display names."""
    path = _ROOT / "data/dictionaries/display_names.json"
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return {kind: {str(uid): row["name"] for uid, row in value.get(kind, {}).items()
                       if isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"]}
                for kind in ("heroes", "skills")}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _display_name(value, kind):
    uid = value.get("hero_uid" if kind == "heroes" else "skill_uid")
    return (value.get("name_zh") or _catalog_names().get(kind, {}).get(str(uid))
            or f"{'武将' if kind == 'heroes' else '战法'} ID {uid or '未知'}")


def _name(hero):
    return _text(_display_name(hero, "heroes"))


def _prepare(result, context):
    if not isinstance(result, dict) or result.get("available") is not True:
        raise ValueError("没有可导出的统计缓存，请先完成队伍统计。")
    context = dict(context or {})
    if context.get("minimum_known_results", MIN_KNOWN_RESULTS) != MIN_KNOWN_RESULTS:
        raise ValueError("统计筛选的最低有效场次已改变，请重新统计后导出。")
    for key in ("namespace", "day", "battle_type"):
        if key in context and context[key] != result.get(key):
            raise ValueError("统计缓存与当前筛选不一致，请重新统计后导出。")
    thresholds = result.get("rule", {}).get("thresholds", {})
    for key in ("high_red", "high_quality", "medium_red", "medium_quality"):
        if key in context and context[key] != thresholds.get(key):
            raise ValueError("统计缓存与当前门槛不一致，请应用门槛后导出。")
    main = result.get("without_skills", [])
    skilled = result.get("with_skills", [])
    if not isinstance(main, list) or not isinstance(skilled, list):
        raise ValueError("统计缓存格式无效，请重新统计。")
    detail_by_team = {}
    for row in skilled:
        identity = _identity(row)
        if identity in detail_by_team:
            raise ValueError("缓存中同一档队伍有重复主行，请重新统计后导出。")
        detail_by_team[identity] = row
    if set(detail_by_team) != {_identity(row) for row in main}:
        raise ValueError("主表与战法视图的队伍不一致，请重新统计后导出。")
    rows, variants, seen = [], [], set()
    ordered = sorted(main, key=win_rate_sort_key)
    for row in ordered:
        identity = _identity(row)
        if identity in seen:
            raise ValueError("缓存中同一档队伍有重复主行，请重新统计后导出。")
        seen.add(identity)
        heroes = _heroes(row)
        skilled_row = detail_by_team.get(identity)
        configs = skilled_row.get("skill_variants", []) if skilled_row else []
        if not isinstance(configs, list):
            raise ValueError("统计缓存中的战法配置无效，请重新统计。")
        configs = sorted(configs, key=win_rate_sort_key)
        stats = _stats(row)
        if stats[0] and not configs:
            raise ValueError("有参战样本但缺少战法配置明细，请重新统计后导出。")
        if skilled_row and _stats(skilled_row) != stats:
            raise ValueError("两个统计视图的队伍总计不一致，请重新统计。")
        if any(sum(_count(config.get(key), key) for config in configs) != row[key] for key in _COUNTS):
            raise ValueError("战法配置合计与主队伍不一致，请重新统计。")
        tier = identity[0]
        label = _text(" / ".join(str(_display_name(hero, "heroes")) for hero in heroes))
        key = _text(row.get("key"))
        rows.append([_TIER_LABELS.get(tier, "属性未知"), label,
            *[_attribute(row, field, bound) for field in ("total_red", "total_quality") for bound in ("min", "max")],
            *stats, None, None, _count(row.get("full_red_full_quality_samples", 0), "全红全品样本"),
            len(configs), sum(config.get("skills_complete") is not True for config in configs),
            _text(" / ".join(str(hero.get("hero_uid") or "未知") for hero in heroes)), key,
            _sources(row, "battle_uid"), _sources(row, "snapshot_id")])
        config_keys = set()
        for number, config in enumerate(configs, 1):
            config_key = _text(config.get("key"))
            if config_key in config_keys:
                raise ValueError("同一队伍中出现重复战法配置，请重新统计。")
            config_keys.add(config_key)
            hero_cells = []
            for hero in _heroes(config)[:3]:
                skills = hero.get("skills", [])
                skill_names = "、".join(str(_display_name(skill, "skills"))
                                       for skill in skills if isinstance(skill, dict))
                hero_cells.extend([_name(hero), _text(skill_names or "战法未取得")])
            hero_cells += [None] * (6 - len(hero_cells))
            variants.append([_TIER_LABELS.get(tier, "属性未知"), label, number, *hero_cells,
                *_stats(config), None, None, "已确认" if config.get("skills_complete") is True else "未知",
                *[_attribute(config, field, bound) for field in ("total_red", "total_quality") for bound in ("min", "max")],
                key, config_key, _sources(config, "battle_uid"), _sources(config, "snapshot_id")])
    if set(detail_by_team) - seen:
        raise ValueError("战法视图包含主表未覆盖的队伍，请重新统计。")
    # Validate the entire original cache above before selecting export rows.
    # Parent totals retain every configuration, including small configurations
    # omitted from the separate detail page.
    eligible_keys = {row[18] for row in rows if row[11] >= MIN_KNOWN_RESULTS}
    small_configs = [row for row in variants if row[14] < MIN_KNOWN_RESULTS]
    selection = {
        "minimum_known_results": MIN_KNOWN_RESULTS,
        "original_teams": len(rows), "excluded_small_teams": len(rows) - len(eligible_keys),
        "original_configurations": len(variants), "excluded_small_configurations": len(small_configs),
        "excluded_configuration_known_results": sum(row[14] for row in small_configs),
        "excluded_configuration_samples": sum(row[9] for row in small_configs),
    }
    rows = [row for row in rows if row[18] in eligible_keys]
    variants = [row for row in variants if row[22] in eligible_keys and row[14] >= MIN_KNOWN_RESULTS]
    counts, unknown_counts = {}, {}
    for variant in variants:
        key = variant[22]
        counts[key] = counts.get(key, 0) + 1
        unknown_counts[key] = unknown_counts.get(key, 0) + int(variant[17] != "已确认")
        variant[2] = counts[key]
    for row in rows:
        row[15], row[16] = counts.get(row[18], 0), unknown_counts.get(row[18], 0)
    selection.update(eligible_teams=len(rows), eligible_configurations=len(variants))
    notes = [
        ["队伍胜率", ""],
        ["战斗日期", _text(result.get("day") or "全部日期"), "战斗类型", "仅 PVP" if result.get("battle_type") == "pvp" else "全部类型"],
        ["账号范围", _text(result.get("namespace") or "全部已采集账号")],
        ["分档门槛", _text(f"高满：总红≥{thresholds.get('high_red')}且总品≥{thresholds.get('high_quality')}；中红：总红≥{thresholds.get('medium_red')}且总品≥{thresholds.get('medium_quality')}；其余已知为低红")],
        ["统计口径", f"主队及每种配置均至少 {MIN_KNOWN_RESULTS} 场有效胜平负。胜率=胜÷(胜+平+负)，未知结果不进入分母，未知红品单列。"],
        ["导出范围", f"主表按胜率降序，队伍整体统计全部场次；配置仅列≥{MIN_KNOWN_RESULTS}有效场次。小样本排除{selection['excluded_small_teams']}队、{selection['excluded_small_configurations']}配置/{selection['excluded_configuration_known_results']}有效场次。两页合计可能不同。"],
        ["数据来源", "本地已采集战报的当前统计缓存", "导出时间", datetime.now().isoformat(timespec="seconds")],
    ]
    return {"notes": notes, "teams": rows, "variants": variants, "selection": selection}


def _runtime():
    """Load the publicly installable XLSX dependency only when export is used."""
    try:
        from openpyxl import Workbook
    except ImportError as error:
        raise RuntimeError("Excel 导出需要 openpyxl，请先安装项目 requirements.txt。") from error
    return Workbook


_COLUMNS = {
    "teams": ["档位", "队伍", "总红最小", "总红最大", "总品最小", "总品最大", "参战样本", "胜", "平", "负", "未知结果", "已知场次", "胜率", "平局率", "满红满品样本", "战法配置数", "未知配置数", "武将 ID", "队伍标识", "来源战报 ID", "来源快照 ID"],
    "variants": ["档位", "队伍", "配置序号", "武将一", "战法一", "武将二", "战法二", "武将三", "战法三", "参战样本", "胜", "平", "负", "未知结果", "已知场次", "胜率", "平局率", "战法信息", "总红最小", "总红最大", "总品最小", "总品最大", "队伍标识", "配置标识", "来源战报 ID", "来源快照 ID"],
}


def _cache_formula_results(path, values):
    """Set numeric OOXML formula caches for viewers which do not calculate.

    openpyxl writes formulas without their cached results. All formulas here are
    fixed count/rate expressions over counts validated by _prepare; writing the
    matching cached numbers also lets read-only/data_only readers show results.
    Excel still recalculates normally when the workbook is opened or edited.
    """
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ET.register_namespace("", namespace)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".team-winrates-cache-", suffix=".xlsx",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
        with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(temporary, "w") as output:
            for item in source.infolist():
                if item.filename in values:
                    root = ET.fromstring(source.read(item.filename))
                    for cell in root.iter("{" + namespace + "}c"):
                        cached = values[item.filename].get(cell.get("r"))
                        if cached is None:
                            continue
                        value = cell.find("{" + namespace + "}v")
                        if value is None:
                            value = ET.SubElement(cell, "{" + namespace + "}v")
                        value.text = str(cached)
                    output.writestr(item, ET.tostring(root, encoding="utf-8", xml_declaration=True))
                else:
                    with source.open(item, "r") as incoming, output.open(item, "w") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_workbook(document, path):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    workbook = _runtime()()
    workbook.remove(workbook.active)
    formula_caches = {}
    for number, (key, name, table_name) in enumerate((
            ("teams", "队伍胜率", "TeamWinrates"), ("variants", "战法配置", "SkillVariants")), 1):
        headers, rows = _COLUMNS[key], document[key]
        if len(rows) + 9 > 1048576:
            raise ValueError("导出结果超过 Excel 单页行数上限，请缩小日期范围。")
        sheet = workbook.create_sheet(name)
        sheet.sheet_view.showGridLines = False
        sheet.sheet_properties.tabColor = "24466B" if key == "teams" else "708BA5"
        for index, note in enumerate(document["notes"], 2):
            note = list(note)
            if index == 2:
                note[0] = name
            for column, value in enumerate(note, 1):
                sheet.cell(index, column, value)
        for cell in sheet[2][:4]:
            cell.font = Font(name="Arial", size=15, bold=True, color="20354D")
        for column, value in enumerate(headers, 1):
            cell = sheet.cell(9, column, value)
            cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="24466B")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        sheet.row_dimensions[9].height = 30
        cache = {}
        for row_number, values in enumerate(rows, 10):
            for column, value in enumerate(values, 1):
                cell = sheet.cell(row_number, column, value)
                cell.font = Font(name="Arial", size=10, color="253747")
                cell.alignment = Alignment(vertical="center", wrap_text=key == "variants" and 4 <= column <= 9)
            sheet.row_dimensions[row_number].height = 26 if key == "teams" else 42
            if key == "teams":
                denominator, wins, draws, known_column, rate_column, draw_column = "L", "H", "I", 12, 13, 14
                known, win_count, draw_count = values[11], values[7], values[8]
                known_formula = "=SUM(H{0}:J{0})".format(row_number)
                integer_columns = list(range(3, 13)) + list(range(15, 18))
            else:
                denominator, wins, draws, known_column, rate_column, draw_column = "O", "K", "L", 15, 16, 17
                known, win_count, draw_count = values[14], values[10], values[11]
                known_formula = "=SUM(K{0}:M{0})".format(row_number)
                integer_columns = list(range(10, 16)) + list(range(19, 23))
            sheet.cell(row_number, known_column, known_formula)
            sheet.cell(row_number, rate_column, '=IF({0}{1}=0,"",{2}{1}/{0}{1})'.format(denominator, row_number, wins))
            sheet.cell(row_number, draw_column, '=IF({0}{1}=0,"",{2}{1}/{0}{1})'.format(denominator, row_number, draws))
            cache[get_column_letter(known_column) + str(row_number)] = known
            if known:
                cache[get_column_letter(rate_column) + str(row_number)] = win_count / known
                cache[get_column_letter(draw_column) + str(row_number)] = draw_count / known
            for column in integer_columns:
                sheet.cell(row_number, column).number_format = "#,##0"
            sheet.cell(row_number, rate_column).number_format = "0.0%"
            sheet.cell(row_number, draw_column).number_format = "0.0%"
        if rows:
            reference = "A9:{0}{1}".format(get_column_letter(len(headers)), len(rows) + 9)
            table = Table(displayName=table_name, ref=reference)
            table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
            sheet.add_table(table)
        for index in range(1, len(headers) + 1):
            width = 32 if index == 2 else 14 if key == "variants" and index in (4, 6, 8) else (
                35 if key == "variants" and index in (5, 7, 9) else 28 if index >= len(headers) - 2 else 12)
            sheet.column_dimensions[get_column_letter(index)].width = width
        sheet.freeze_panes = "A10"
        formula_caches["xl/worksheets/sheet{}.xml".format(number)] = cache
    workbook.save(path)
    workbook.close()
    _cache_formula_results(path, formula_caches)


def export_team_winrates(db_path, target, result, context=None):
    """Export every selected result on the GUI's existing background worker."""
    target = _write_path(target)
    database = Path(db_path).expanduser().resolve()
    protected = {database, *(Path(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
    if target in protected:
        raise ValueError("不能覆盖数据库及其 SQLite 辅助文件。")
    if target.suffix.lower() != ".xlsx":
        raise ValueError("Excel 导出文件须使用 .xlsx 扩展名。")
    if target.exists() or target.is_symlink():
        raise FileExistsError("导出位置已有文件，请使用新文件名，避免覆盖原件。")
    document = _prepare(result, context)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # Both XLSX serialization and formula-cache staging stay beside the
        # requested output, without relying on a personal profile or runtime.
        with tempfile.NamedTemporaryFile(prefix=".team-winrates-", suffix=".xlsx",
                                         dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
        _write_workbook(document, temporary)
        with temporary.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link publication is atomic and refuses a competing file/symlink.
        os.link(temporary, target)
        return {"path": str(target), "team_rows": len(document["teams"]), "variant_rows": len(document["variants"]),
                "sheets": ["队伍胜率", "战法配置"], "selection": document["selection"]}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
