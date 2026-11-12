"""Read saved XLSX independently; never access the production database or game."""
import copy
import json
from pathlib import Path
from unittest.mock import patch
import zipfile

from openpyxl import load_workbook
import pytest

from battle_ui import team_winrate_export as export


def group(tier="low", *, wins=10, draws=10, losses=0, unknown=1, base=1000, quality=0):
    known = wins + draws + losses
    row = {"key": json.dumps([tier, [str(base + index) for index in range(3)], None]),
           "tier": tier, "heroes": [{"hero_uid": str(base + index), "name_zh": "武将" + str(index),
                "skills": [{"skill_uid": "900719925474099312345", "name_zh": "同心协力"}]} for index in range(3)],
           "samples": known + unknown, "wins": wins, "draws": draws, "losses": losses,
           "unknown_results": unknown, "known_results": known,
           "win_rate": wins / known if known else None, "draw_rate": draws / known if known else None,
           "total_red": 0, "total_red_min": 0, "total_red_max": 0,
           "total_quality": quality, "total_quality_min": quality, "total_quality_max": quality,
           "full_red_full_quality_samples": 0, "skills_complete": True,
           "sources": [{"namespace": "server:1/scenario:18/avatar:100", "battle_uid": "900719925474099312345",
                        "snapshot_id": 900719925474099312346, "side": "attacker", "outcome": "win"}]}
    return row


def cached():
    rows = [group(), group("medium", wins=0, draws=20, unknown=0, base=2000),
            group("high", wins=0, draws=0, losses=20, unknown=1, base=3000),
            group("unknown", wins=0, draws=20, unknown=0, base=4000, quality=None)]
    skilled = []
    for row in rows:
        parent = copy.deepcopy(row)
        config = copy.deepcopy(row)
        config["key"] = "配置" + row["key"]
        parent["skill_variants"] = [config]
        skilled.append(parent)
    return {"available": True, "namespace": "server:1/scenario:18/avatar:100", "day": None,
            "battle_type": "pvp", "without_skills": rows, "with_skills": skilled,
            "rule": {"thresholds": {"high_red": 12, "high_quality": 12, "medium_red": 6, "medium_quality": 6}}}


def test_prepare_sorts_main_table_globally_by_unrounded_win_rate_then_known_and_samples_without_authoring():
    low_many = group(wins=5, draws=0, losses=45, unknown=0, base=1000)
    low_best = group(wins=20, draws=0, unknown=0, base=1100)
    low_near_half = group(wins=500001, draws=0, losses=499999, unknown=0, base=1200)
    low_half = group(wins=20, draws=0, losses=20, unknown=0, base=1300)
    low_half_more_samples = group(wins=20, draws=0, losses=20, unknown=3, base=1400)
    low_half_fewer_known = group(wins=10, draws=0, losses=10, unknown=100, base=1500)
    low_zero = group(wins=0, draws=0, losses=20, unknown=0, base=1600)
    low_unknown = group(wins=0, draws=0, losses=20, unknown=50, base=1700)
    low_unknown["win_rate"] = None
    middle = group("medium", wins=20, draws=0, unknown=0, base=2000)
    high = group("high", wins=20, draws=0, unknown=0, base=3000)
    unknown_attributes = group("unknown", wins=20, draws=0, unknown=0, base=4000, quality=None)
    source = cached()
    source["without_skills"] = [low_many, low_unknown, low_half_fewer_known, high,
        unknown_attributes, low_half, low_best, middle, low_half_more_samples, low_zero, low_near_half]
    source["with_skills"] = []
    for row in source["without_skills"]:
        parent = copy.deepcopy(row)
        config = copy.deepcopy(row)
        config["key"] = "配置" + row["key"]
        parent["skill_variants"] = [config]
        source["with_skills"].append(parent)
    before = copy.deepcopy(source)
    with patch.object(export, "_runtime") as runtime:
        prepared = export._prepare(source, {})
    runtime.assert_not_called()
    expected = [high, unknown_attributes, low_best, middle, low_near_half, low_half_more_samples,
                low_half, low_half_fewer_known, low_many, low_zero, low_unknown]
    assert [row[18] for row in prepared["teams"]] == [row["key"] for row in expected]
    assert source == before


def test_prepare_sorts_variants_within_each_team_by_same_winrate_rule_without_authoring():
    configs = [group(wins=5, draws=0, losses=45, unknown=0),
               group(wins=20, draws=0, unknown=0),
               group(wins=20, draws=0, losses=20, unknown=0),
               group(wins=20, draws=0, losses=20, unknown=3),
               group(wins=10, draws=0, losses=10, unknown=100),
               group(wins=0, draws=0, losses=20, unknown=0),
               group(wins=0, draws=0, losses=20, unknown=200)]
    configs[-1]["win_rate"] = None
    for index, config in enumerate(configs):
        config["key"] = f"variant-{index}"
    parent = group(wins=sum(row["wins"] for row in configs), draws=0,
                   losses=sum(row["losses"] for row in configs), unknown=sum(row["unknown_results"] for row in configs))
    detailed = copy.deepcopy(parent)
    detailed["skill_variants"] = configs
    source = cached()
    source["without_skills"], source["with_skills"] = [parent], [detailed]
    before = copy.deepcopy(source)
    with patch.object(export, "_runtime") as runtime:
        prepared = export._prepare(source, {})
    runtime.assert_not_called()
    assert [row[23] for row in prepared["variants"]] == [f"variant-{index}" for index in (1, 3, 2, 4, 0, 5, 6)]
    assert [row[2] for row in prepared["variants"]] == list(range(1, 8))
    assert sum(row[9] for row in prepared["variants"]) == prepared["teams"][0][6]
    assert source == before


def test_prepare_20_known_boundary_excludes_19_even_with_many_unknown_samples():
    source = cached()
    enough = group(wins=10, draws=5, losses=5, unknown=0, base=1000)
    small = group(wins=19, draws=0, losses=0, unknown=100000, base=2000)
    source["without_skills"], source["with_skills"] = [small, enough], []
    for row in source["without_skills"]:
        detailed = copy.deepcopy(row)
        config = copy.deepcopy(row)
        config["key"] = "variant" + row["key"]
        detailed["skill_variants"] = [config]
        source["with_skills"].append(detailed)
    prepared = export._prepare(source, {})
    assert len(prepared["teams"]) == len(prepared["variants"]) == 1
    assert prepared["teams"][0][18] == enough["key"]
    assert prepared["teams"][0][11] == 20
    assert prepared["selection"]["excluded_small_teams"] == 1
    assert prepared["selection"]["excluded_small_configurations"] == 1
    assert prepared["selection"]["excluded_configuration_known_results"] == 19


def test_prepare_small_variants_are_omitted_without_changing_whole_team_totals():
    configs = [group(wins=20, draws=0, unknown=0), group(wins=19, draws=0, unknown=100000)]
    for index, row in enumerate(configs):
        row["key"] = "variant-" + str(index)
    parent = group(wins=39, draws=0, unknown=100000)
    detailed = copy.deepcopy(parent)
    detailed["skill_variants"] = configs
    source = cached()
    source["without_skills"], source["with_skills"] = [parent], [detailed]
    before = copy.deepcopy(source)
    prepared = export._prepare(source, {})
    assert len(prepared["teams"]) == len(prepared["variants"]) == 1
    assert prepared["teams"][0][6] == 100039
    assert prepared["teams"][0][11] == 39
    assert prepared["teams"][0][15] == 1
    assert prepared["variants"][0][9] == prepared["variants"][0][14] == 20
    assert prepared["selection"]["original_configurations"] == 2
    assert prepared["selection"]["excluded_small_configurations"] == 1
    assert prepared["selection"]["excluded_configuration_known_results"] == 19
    assert "两页合计可能不同" in prepared["notes"][5][1]
    assert source == before


def test_prepare_eligible_team_can_have_no_eligible_configuration():
    configs = [group(wins=10, draws=0, unknown=0), group(wins=0, draws=0, losses=10, unknown=0)]
    for index, row in enumerate(configs):
        row["key"] = "variant-" + str(index)
    parent = group(wins=10, draws=0, losses=10, unknown=0)
    detailed = copy.deepcopy(parent)
    detailed["skill_variants"] = configs
    source = cached()
    source["without_skills"], source["with_skills"] = [parent], [detailed]
    prepared = export._prepare(source, {})
    assert len(prepared["teams"]) == 1 and prepared["variants"] == []
    assert prepared["teams"][0][6] == prepared["teams"][0][11] == 20
    assert prepared["teams"][0][15] == 0
    assert prepared["selection"]["excluded_small_configurations"] == 2
    assert prepared["selection"]["excluded_configuration_known_results"] == 20


def test_prepare_validates_small_original_rows_before_threshold_filtering():
    small = group(wins=19, draws=0, unknown=100000)
    detailed = copy.deepcopy(small)
    malformed = copy.deepcopy(small)
    malformed["samples"] += 1
    detailed["skill_variants"] = [malformed]
    source = cached()
    source["without_skills"], source["with_skills"] = [small], [detailed]
    with pytest.raises(ValueError, match="合计与主队伍不一致"):
        export._prepare(source, {})


@pytest.mark.parametrize("minimum", (19, True, "20"))
def test_prepare_rejects_a_stale_minimum_known_result_setting(minimum):
    with pytest.raises(ValueError, match="最低有效场次已改变"):
        export._prepare(cached(), {"minimum_known_results": minimum})


@pytest.fixture(scope="module")
def workbook_files(tmp_path_factory):
    folder = tmp_path_factory.mktemp("xlsx_saved")
    source = cached()
    source["without_skills"][0]["heroes"][0]["name_zh"] = '=HYPERLINK("https://invalid.example","名称")'
    source["with_skills"][0]["heroes"][0]["name_zh"] = source["without_skills"][0]["heroes"][0]["name_zh"]
    source["with_skills"][0]["skill_variants"][0]["heroes"][0]["name_zh"] = source["without_skills"][0]["heroes"][0]["name_zh"]
    original = copy.deepcopy(source)
    target = folder / "队伍胜率.xlsx"
    response = export.export_team_winrates(folder / "never_created.db", target, source,
        {"namespace": source["namespace"], "day": None, "battle_type": "pvp", "selected_tier": "low", "current_mode": "with_skills"})
    assert source == original and not (folder / "never_created.db").exists()
    yield target, response


def test_saved_workbook_has_all_tiers_one_team_each_and_variant_detail(workbook_files):
    target, response = workbook_files
    workbook = load_workbook(target, data_only=False)
    assert workbook.sheetnames == ["队伍胜率", "战法配置"]
    main, detail = workbook.worksheets
    assert response["team_rows"] == response["variant_rows"] == 4
    assert [main.cell(row, 1).value for row in range(10, 14)] == ["低红", "高满", "中红", "属性未知"]
    assert len({main.cell(row, 19).value for row in range(10, 14)}) == 4
    assert len(list(main.tables.values())) == len(list(detail.tables.values())) == 1
    assert main.freeze_panes == detail.freeze_panes == "A10"
    assert main.tables["TeamWinrates"].autoFilter.ref == "A9:U13"
    assert detail.tables["SkillVariants"].autoFilter.ref == "A9:Z13"
    assert main.column_dimensions["B"].width >= 30


def test_numeric_counts_percentages_zeros_and_missing_stay_distinct(workbook_files):
    target, _ = workbook_files
    formulas = load_workbook(target, data_only=False)
    values = load_workbook(target, data_only=True)
    main = values["队伍胜率"]
    assert main["G10"].value == 21 and main["G10"].data_type == "n"
    assert main["C10"].value == 0 and main["E10"].value == 0
    assert main["E13"].value is None and main["F13"].value is None
    assert main["M10"].value == main["N10"].value == .5
    assert main["M11"].value == 0 and main["N11"].value == 0
    assert main["M12"].value == 0 and main["N12"].value == 1
    assert formulas["队伍胜率"]["M10"].number_format == "0.0%"
    assert formulas["队伍胜率"]["M10"].value == '=IF(L10=0,"",H10/L10)'
    assert formulas["队伍胜率"]["L10"].value == '=SUM(H10:J10)'
    assert formulas["战法配置"]["O10"].value == '=SUM(K10:M10)'


def test_identifiers_are_exact_text_and_names_never_become_formulas(workbook_files):
    target, _ = workbook_files
    workbook = load_workbook(target, data_only=False)
    assert workbook["队伍胜率"]["T10"].value == "900719925474099312345"
    assert workbook["队伍胜率"]["U10"].value == "900719925474099312346"
    assert workbook["队伍胜率"]["T10"].data_type == "s"
    assert workbook["队伍胜率"]["B10"].data_type == "s"
    assert workbook["战法配置"]["D10"].data_type == "s"
    with zipfile.ZipFile(target) as archive:
        for name in archive.namelist():
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                assert '<f>HYPERLINK' not in archive.read(name).decode("utf-8")


def test_missing_display_names_can_use_retained_translation_without_changing_ids(monkeypatch):
    monkeypatch.setattr(export, "_catalog_names", lambda: {"skills": {"600013": "盾甲同心"}})
    skill = {"skill_uid": "600013", "name_zh": None}
    assert export._display_name(skill, "skills") == "盾甲同心"
    assert skill == {"skill_uid": "600013", "name_zh": None}
    assert export._display_name({"skill_uid": "12345678901234567890"}, "skills") == "战法 ID 12345678901234567890"


@pytest.mark.parametrize("change", ("duplicate", "counts", "view_counts", "variant_counts", "missing_team", "missing_variants", "context"))
def test_inconsistent_cached_results_are_rejected_before_authoring(tmp_path, change):
    value = cached()
    context = {}
    if change == "duplicate":
        value["without_skills"].append(copy.deepcopy(value["without_skills"][0]))
    elif change == "counts":
        value["without_skills"][0]["samples"] += 1
    elif change == "view_counts":
        value["with_skills"][0]["unknown_results"] += 1
        value["with_skills"][0]["samples"] += 1
    elif change == "variant_counts":
        value["with_skills"][0]["skill_variants"][0]["samples"] += 1
    elif change == "missing_team":
        value["with_skills"].pop()
    elif change == "missing_variants":
        value["with_skills"][0]["skill_variants"] = []
    else:
        context["high_red"] = 14
    target = tmp_path / "invalid.xlsx"
    with patch.object(export, "_runtime") as runtime, pytest.raises(ValueError):
        export.export_team_winrates(tmp_path / "main.db", target, value, context)
    runtime.assert_not_called()
    assert not target.exists()


def test_db_sidecars_and_existing_sources_are_never_overwritten(tmp_path):
    for target in (tmp_path / "main.xlsx", tmp_path / "main.xlsx-wal", tmp_path / "main.xlsx-shm"):
        with pytest.raises(ValueError):
            export.export_team_winrates(tmp_path / "main.xlsx", target, cached())
    source = tmp_path / "original.xlsx"
    source.write_bytes(b"preserve exact bytes")
    with pytest.raises(FileExistsError):
        export.export_team_winrates(tmp_path / "main.db", source, cached())
    assert source.read_bytes() == b"preserve exact bytes"


def test_publish_race_keeps_other_file_and_cleans_target_directory_temporary_files(tmp_path, monkeypatch):
    def build(_document, path):
        path.write_bytes(b"ready workbook")
    def race(_source, target):
        Path(target).write_bytes(b"another user's file")
        raise FileExistsError("raced")
    monkeypatch.setattr(export, "_write_workbook", build)
    monkeypatch.setattr(export.os, "link", race)
    target = tmp_path / "raced.xlsx"
    with pytest.raises(FileExistsError):
        export.export_team_winrates(tmp_path / "main.db", target, cached())
    assert target.read_bytes() == b"another user's file"
    assert not list(tmp_path.glob(".team-winrates-*"))


def test_builder_failure_does_not_publish_a_partial_export(tmp_path, monkeypatch):
    def build(_document, path):
        path.write_bytes(b"partial workbook")
        raise RuntimeError("workbook build failed")
    monkeypatch.setattr(export, "_write_workbook", build)
    target = tmp_path / "failed.xlsx"
    with pytest.raises(RuntimeError, match="workbook build failed"):
        export.export_team_winrates(tmp_path / "main.db", target, cached())
    assert not target.exists()
    assert not list(tmp_path.glob(".team-winrates-*"))


def test_empty_result_keeps_both_sheets_and_explanation(tmp_path):
    source = cached()
    source["without_skills"], source["with_skills"] = [], []
    target = tmp_path / "empty.xlsx"
    result = export.export_team_winrates(tmp_path / "never-created.db", target, source)
    assert result["team_rows"] == result["variant_rows"] == 0
    workbook = load_workbook(target, data_only=True)
    assert workbook.sheetnames == ["队伍胜率", "战法配置"]
    assert all(sheet["A9"].value == "档位" and sheet["B7"].value for sheet in workbook)
    assert all(len(sheet.tables) == 0 for sheet in workbook)
    workbook.close()
    assert not list(tmp_path.glob(".team-winrates-*"))


def test_excel_export_uses_public_openpyxl_dependency_and_no_database(tmp_path, monkeypatch):
    from openpyxl import Workbook
    assert export._runtime() is Workbook
    target = tmp_path / "portable.xlsx"
    result = export.export_team_winrates(tmp_path / "never-created.db", target, cached())
    assert result["sheets"] == ["队伍胜率", "战法配置"]
    assert not (tmp_path / "never-created.db").exists()
    assert not list(tmp_path.glob(".team-winrates-*"))
