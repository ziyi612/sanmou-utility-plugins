"""Statistics scenarios give each real Tcl interpreter a separate process."""
from __future__ import annotations

import copy
import csv
import hashlib
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import time

import pytest

from battle_ui.app import BattleApp, account_label, write_csv
from parser.report_archive import ReportArchive


NS = "server:760057/scenario:18/avatar:123456789"
OTHER = "server:760058/scenario:18/avatar:222"


def daily_result(namespace=NS):
    return {"available": True, "namespace": namespace,
        "dates": ["2026-11-07", "2026-11-08"],
        "days": [{"date": "2026-11-08", "reports": 2, "complete": 1, "partial": 1,
                  "personal": 1, "union": 2, "star": 0},
                 {"date": "2026-11-07", "reports": 1, "complete": 1, "partial": 0,
                  "personal": 1, "union": 0, "star": 0}],
        "players": [
            {"date": "2026-11-08", "namespace": namespace, "player_uid": "100", "player_name": "零值玩家",
             "battles": 1, "known_merit": 0, "unknown_merit_reports": 0, "data_complete": True},
            {"date": "2026-11-08", "namespace": namespace, "player_uid": "200", "player_name": "缺值玩家",
             "battles": 1, "known_merit": None, "unknown_merit_reports": 1, "data_complete": False},
            {"date": "2026-11-07", "namespace": namespace, "player_uid": "100", "player_name": "零值玩家",
             "battles": 1, "known_merit": 9, "unknown_merit_reports": 0, "data_complete": True}],
        "coverage": {"incomplete_families": 1, "missing_children": 3}}


def roster_result(namespace=NS):
    heroes = [{"hero_uid": str(index), "name_zh": "武将" + str(index), "evolution_num": 5,
               "level": 50, "skills": [{"skill_uid": "88", "name_zh": "战法甲"}]}
              for index in (1, 2, 3)]
    return {"available": True, "namespace": namespace, "day": "2026-11-08",
        "players": [{"namespace": namespace, "player_uid": "900", "player_name": "对手甲",
            "tags": ["高品（进阶≥3）", "单将满红", "整队满红"], "last_seen": 1794140000,
            "teams": [{"namespace": namespace, "battle_uid": "report-123", "snapshot_id": 7,
                "tags": ["单将满红", "整队满红"], "last_seen": 1794140000, "heroes": heroes}]}],
        "counts": {"latest_day_reports": 2, "opponents_observed": 1},
        "coverage": {"npc_opponent_reports": 1, "unresolved_side_reports": 0,
                     "missing_side_reports": 0},
        "note": "只保留最近有战报数据的一天，不删除历史。"}


class FakeService:
    def __init__(self, factory):
        self.factory = factory
        self.calls = []
        self.delay = None

    def summary(self, namespace=None):
        self.calls.append(namespace)
        if self.delay is not None:
            self.delay.wait(5)
        return copy.deepcopy(self.factory(namespace))


def make_view():
    base = runpy.run_path(str(Path(__file__).with_name("test_battle_ui_app.py")))
    controller, data = base["FakeController"](), base["FakeData"]()
    daily, roster = FakeService(daily_result), FakeService(roster_result)
    app = BattleApp(Path("."), controller, data,
                    daily_service=daily, opponent_service=roster)
    app.withdraw()
    app.after_cancel(app._initial_refresh_id)
    app._next_overview = time.monotonic() + 3600
    app._account_map.update({account_label(NS): NS, account_label(OTHER): OTHER})
    app.account_var.set(account_label(NS))
    return app, daily, roster, base["pump"]


def case_daily_zero_unknown_date_and_search_filters(view):
    app, _, _, _ = view
    app._render_daily(daily_result())
    assert app.day_var.get() == "2026-11-08"
    assert app.daily_metrics["reports"].get() == "2"
    assert app.daily_metrics["players"].get() == "2"
    assert app.daily_metrics["merit"].get() == "0"
    assert app.daily_metrics["unknown"].get() == "1"
    values = [app.players_tree.item(key, "values") for key in app.players_tree.get_children()]
    assert values[0][4] == "0" and values[1][4] == "未取得"
    assert app._daily_export_rows[0][5] == 0 and app._daily_export_rows[1][5] is None
    app.day_var.set("全部日期")
    app._filter_daily()
    assert len(app.players_tree.get_children()) == 3
    assert app.daily_metrics["merit"].get() == "9"
    assert app.daily_metrics["players"].get() == "2"
    app.player_search_var.set("200")
    app._filter_daily()
    assert len(app.players_tree.get_children()) == 1
    assert app.daily_metrics["merit"].get() == "未取得"
    assert app._daily_export_rows[0][2] == "200"


def case_daily_own_union_identity_and_exclusions_are_visible(view):
    app, _, _, _ = view
    value = daily_result()
    value["union_filter"] = {"enabled": True, "contexts": {NS: {
        "status": "identified", "union_id": "2", "union_name": "将进酒", "observed_at": 1794470000}},
        "enemy_sides_excluded": 3, "unknown_union_sides_excluded": 1}
    app._render_daily(value)
    note = app.daily_note_var.get()
    assert "仅统计我盟" in note and "将进酒" in note and "ID 2" in note and "确认于" in note
    assert "其他同盟 3" in note and "身份未确认 1" in note
    value["union_filter"]["contexts"][NS]["status"] = "unknown"
    app._render_daily(value)
    assert "请采集一轮确认所属同盟" in app.daily_note_var.get()


def case_new_account_cannot_reexport_old_daily_result_while_loading(view):
    app, daily, _, pump = view
    app.show_page("daily", load=False)
    app._render_daily(daily_result())
    daily.delay = threading.Event()
    try:
        app.account_var.set(account_label(OTHER))
        app._account_changed()
        app._filter_daily()
        assert not app._daily_export_rows
        assert not app.players_tree.get_children()
        assert str(app.daily_export_button.cget("state")) == "disabled"
    finally:
        daily.delay.set()
    pump(app, lambda: "daily" not in app._inflight)
    assert daily.calls[-1] == OTHER
    assert all(row[1] == OTHER for row in app._daily_export_rows)


def case_opponent_details_and_latest_empty_day_replace_old_roster(view):
    app, _, _, _ = view
    app._render_opponents(roster_result())
    assert app.roster_metrics["players"].get() == "1"
    assert app.roster_metrics["teams"].get() == "1"
    assert app.roster_metrics["full"].get() == "1"
    app.opponents_tree.selection_set("0")
    app._opponent_selected()
    detail = app.opponent_text.get("1.0", "end")
    assert "对手甲" in detail and "report-123" in detail
    assert all("武将" + str(index) + "（5阶）" in detail for index in (1, 2, 3))
    assert "战法甲" in detail and "整队满红" in detail
    empty = roster_result()
    empty.update(day="2026-11-09", players=[])
    empty["counts"] = {"latest_day_reports": 82, "opponents_observed": 0}
    empty["coverage"]["npc_opponent_reports"] = 82
    app._render_opponents(empty)
    assert "2026-11-09" in app.roster_day_var.get()
    assert not app.opponents_tree.get_children() and not app._roster_export_rows
    assert app.roster_metrics["players"].get() == "0"
    assert str(app.roster_export_button.cget("state")) == "disabled"
    assert "report-123" not in app.opponent_text.get("1.0", "end")
    assert "82" in app.roster_note_var.get()


def case_new_account_clears_old_opponents_while_loading(view):
    app, _, roster, pump = view
    app.show_page("opponents", load=False)
    app._render_opponents(roster_result())
    roster.delay = threading.Event()
    try:
        app.account_var.set(account_label(OTHER))
        app._account_changed()
        assert not app._roster_export_rows
        assert not app.opponents_tree.get_children()
        assert "对手甲" not in app.opponent_text.get("1.0", "end")
        assert str(app.roster_export_button.cget("state")) == "disabled"
    finally:
        roster.delay.set()
    pump(app, lambda: "opponents" not in app._inflight)
    assert roster.calls[-1] == OTHER
    assert all(row[1] == OTHER for row in app._roster_export_rows)


def case_late_previous_account_result_is_discarded(view):
    app, _, _, _ = view
    # This can occur when the first overview selects the runtime account
    # while a statistics query for the initial all-account choice is running.
    app.account_var.set(account_label(OTHER))
    app._render_daily(daily_result(NS))
    app._render_opponents(roster_result(NS))
    assert app._daily_data == {} and app._roster_data == {}
    assert not app.players_tree.get_children() and not app.opponents_tree.get_children()
    assert not app._daily_export_rows and not app._roster_export_rows


@pytest.mark.parametrize("case", ["daily_zero_unknown_date_and_search_filters",
    "daily_own_union_identity_and_exclusions_are_visible",
    "new_account_cannot_reexport_old_daily_result_while_loading",
    "opponent_details_and_latest_empty_day_replace_old_roster",
    "new_account_clears_old_opponents_while_loading",
    "late_previous_account_result_is_discarded"])
def test_statistics_native_scenario(case):
    script = r"""
import gc, runpy, sys
namespace = runpy.run_path(sys.argv[1])
view = namespace['make_view']()
try:
    namespace['case_' + sys.argv[2]](view)
finally:
    if not view[0]._closed:
        view[0].close()
del view
gc.collect()
print('STATISTICS_WINDOW_SCENARIO_OK')
"""
    env = dict(os.environ, TEMP=".runtime/test-temp", TMP=".runtime/test-temp",
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    completed = subprocess.run([sys.executable, "-B", "-c", script, str(Path(__file__).resolve()), case],
        cwd=Path(__file__).resolve().parents[1], env=env, text=True, encoding="utf-8", errors="replace",
        capture_output=True, timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert "STATISTICS_WINDOW_SCENARIO_OK" in completed.stdout
    assert "Tcl_AsyncDelete" not in completed.stderr
    assert "Variable.__del__" not in completed.stderr


@pytest.fixture
def export_db(tmp_path):
    path = tmp_path / "archive.db"
    with ReportArchive(path):
        pass
    return path


def test_csv_preserves_unicode_zero_unknown_and_neutralizes_formulas(export_db, tmp_path):
    target = tmp_path / "武勋.csv"
    digest = hashlib.sha256(export_db.read_bytes()).hexdigest()
    result = write_csv(export_db, target, ["玩家", "已记录武勋", "未记录"],
                       [["甲", 0, None], [" =1+1", 4, "\t@SUM(A1:A2)"], ["-昵称", -2, "普通文字"]])
    assert result == {"path": str(target), "rows": 3}
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")
    with target.open(encoding="utf-8-sig", newline="") as handle:
        values = list(csv.reader(handle))
    assert values[1] == ["甲", "0", ""]
    assert values[2] == ["' =1+1", "4", "'\t@SUM(A1:A2)"]
    assert values[3] == ["'-昵称", "-2", "普通文字"]
    assert hashlib.sha256(export_db.read_bytes()).hexdigest() == digest
    assert not list(tmp_path.glob(".battle-stats-*.tmp"))


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_csv_rejects_database_and_sidecar_paths(export_db, suffix):
    with pytest.raises(ValueError, match="archive database"):
        write_csv(export_db, Path(str(export_db) + suffix), ["a"], [[1]])


def test_csv_rejects_database_hardlink_and_existing_output(export_db, tmp_path):
    alias = tmp_path / "alias.csv"
    os.link(export_db, alias)
    with pytest.raises(ValueError, match="archive database"):
        write_csv(export_db, alias, ["a"], [[1]])
    existing = tmp_path / "existing.csv"
    existing.write_bytes(b"preserve original")
    with pytest.raises(FileExistsError):
        write_csv(export_db, existing, ["a"], [[1]])
    assert existing.read_bytes() == b"preserve original"
    assert not list(tmp_path.glob(".battle-stats-*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows storage-policy path")
def test_csv_rejects_c_drive_before_any_write(export_db):
    with pytest.raises(ValueError, match="Writing to C:"):
        write_csv(export_db, Path("C:/__codex_forbidden_statistics_export__.csv"), ["a"], [[1]])
