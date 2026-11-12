"""Exercise the actual Tk view with bounded data/process substitutes."""
from pathlib import Path
import threading
import time
import os
import subprocess
import sys

import pytest

from battle_ui.app import BattleApp, account_label, format_cycle_coverage, format_last_capture, format_time


NS = "server:760057/scenario:18/avatar:123456789"


class FakeController:
    def __init__(self):
        self.running = False
        self.closed = False
        self.started = []
        self.stops = 0
        self.events = []

    def snapshot(self):
        return {"running": self.running, "status": "采集中" if self.running else "已停止",
                "can_start": not self.running, "can_stop": self.running,
                "namespace": NS, "current_battle_id": "123" if self.running else None,
                "last_progress_at": "2026-11-09T03:00:00Z", "last_error": None}

    def drain_events(self, limit=100):
        result, self.events = self.events[:limit], self.events[limit:]
        return result

    def start(self, options):
        self.started.append(options)
        self.running = True

    def request_stop(self):
        self.stops += 1
        self.running = False

    def close(self):
        self.closed = True


class FakeData:
    def __init__(self):
        self.calls = []
        self.delay_detail = None

    def overview(self):
        return {"available": True, "total": 2, "complete": 1, "partial": 1, "legacy": 0,
                "accounts": [{"namespace": NS, "total": 2, "complete": 1, "partial": 1, "legacy": 0}]}

    def list_reports(self, **kwargs):
        self.calls.append(kwargs)
        offset = kwargs.get("offset", 0)
        return {"items": [{"snapshot_id": 8 + offset, "battle_uid": str(123 + offset), "namespace": NS,
                           "completeness": "complete", "event_count": 105, "imported_at": "2026-11-09T03:00:00Z",
                           "attacker_name": "甲", "defender_name": "乙"}],
                "offset": offset, "has_more": offset == 0, "limit": 100}

    def detail(self, snapshot_id, event_limit=100, event_offset=0):
        if self.delay_detail is not None:
            self.delay_detail.wait(4)
        return {"report": {"snapshot_id": snapshot_id, "battle_uid": "123", "namespace": NS,
                           "completeness": "complete", "event_count": 105},
                "components": {key: {"complete": True} for key in ("basic", "detail", "all", "statics", "snapshots")},
                "missing_components": [], "sides": [{"side": "defensive", "player_name": "乙",
                    "heroes": [{"hero_uid": 999999, "name_zh": None, "level": 0, "troops_initial": 0,
                                "troops_remaining": 0, "skills": [{"skill_uid": 888888, "name_zh": None}]}]}],
                "events": [{"sequence": event_offset + 1, "event_id": 200, "event_name_zh": "测试事件",
                            "summary_zh": "数字保留为原值", "parameters": [{"raw_value": 0}]}],
                "event_total": 105, "event_has_more": event_offset == 0,
                "semantics": {"available": True, "status": "pending"}, "metadata": {"unknown": None}}


def make_view():
    controller, data = FakeController(), FakeData()
    app = BattleApp(Path("."), controller, data)
    app.withdraw()
    return app, controller, data


def pump(app, predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.update()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Tk did not reach the expected state before its deadline")


def case_window_opens_without_starting_collection_and_loads_current_counts(view):
    app, controller, _ = view
    pump(app, lambda: app.metric_vars["total"].get() == "2")
    assert not controller.started
    assert app.metric_vars["ratio"].get() == "50.0%"
    assert app._page == "overview"


def case_start_stop_buttons_control_backend_without_blocking_tk(view):
    app, controller, _ = view
    app.parallel_var.set("2")
    app.start_button.invoke()
    pump(app, lambda: controller.running)
    assert controller.started == [{"scopes": ["personal", "union", "star"], "interval": 60, "pid": None,
                                   "pvp_only": True, "parallel": 2}]
    pump(app, lambda: str(app.stop_button.cget("state")) == "normal")
    app.stop_button.invoke()
    pump(app, lambda: controller.stops == 1)


def case_invalid_options_do_not_start_collector(view):
    app, controller, _ = view
    for var in app.scopes.values():
        var.set(False)
    app.start_collection()
    assert not controller.started
    assert "至少选择" in app.message_var.get()


def case_pvp_checkbox_controls_start_and_reflects_running_worker(view):
    app, controller, _ = view
    assert app.pvp_only_var.get()
    app.pvp_only_check.invoke()
    assert not app.pvp_only_var.get() and "包含 PVP 和 PVE" in app.collection_mode_var.get()
    app.start_button.invoke()
    pump(app, lambda: bool(controller.started))
    assert controller.started[0]["pvp_only"] is False
    state = {**controller.snapshot(), "pid": 777,
             "collection_options": {"pvp_only": True},
             "raw_status": {"filter": {"pveReports": 12, "unknownReports": 3}}}
    app._render_runtime((state, []))
    assert app.pvp_only_var.get() and str(app.pvp_only_check.cget("state")) == "disabled"
    assert "非 PVP 12 封" in app.collection_mode_var.get() and "待确认 3 封" in app.collection_mode_var.get()
    app._render_runtime(({**state, "running": False}, []))
    assert str(app.pvp_only_check.cget("state")) == "normal"


def case_cycle_coverage_remains_scoped_during_next_cycle(view):
    app, controller, _ = view
    summary = {"visibleReports": 54, "completeVisibleReports": 54,
               "enumerationComplete": True, "finishedAt": 1794474000,
               "filter": {"mode": "pvp_only", "unknownReports": 0}}
    state = {**controller.snapshot(), "running": True, "pid": 777,
             "collection_options": {"pvp_only": True},
             "raw_status": {"namespace": NS, "result": summary}}
    app._render_runtime((state, []))
    text = app.scope_coverage_var.get()
    assert "完整 54 / 54 封" in text and "最近一轮" in text
    app._render_runtime(({**state, "raw_status": {"namespace": NS}}, []))
    assert app.scope_coverage_var.get() == text
    # Another account's result and another selection cannot inherit 54/54.
    app._render_runtime(({**state, "namespace": "another-account"}, []))
    assert "等待" in app.scope_coverage_var.get()
    app._render_runtime(({**state, "pid": 778,
                          "collection_options": {"pvp_only": False}}, []))
    assert "等待" in app.scope_coverage_var.get()


def case_browser_reads_detail_unknown_ids_zero_values_and_event_pages(view):
    app, _, _ = view
    app.show_page("archive")
    pump(app, lambda: "8" in app._rows)
    app.report_tree.selection_set("8")
    app.report_tree.event_generate("<<TreeviewSelect>>")
    pump(app, lambda: app._detail is not None)
    text = app.info_text.get("1.0", "end")
    assert "999999" in text and "888888" in text and "初始兵力 0" in text
    assert "防守方" in text and "解释尚未就绪" in text
    app.change_events(1)
    pump(app, lambda: app._detail["events"][0]["sequence"] == 101)
    assert app._event_offset == 100


def case_changed_filters_invalidate_slow_old_detail(view):
    app, _, data = view
    app.show_page("archive")
    pump(app, lambda: "8" in app._rows)
    data.delay_detail = threading.Event()
    app.report_tree.selection_set("8")
    app.report_tree.event_generate("<<TreeviewSelect>>")
    pump(app, lambda: "detail" in app._inflight)
    app.search_var.set("456")
    app.load_reports(reset=True)
    data.delay_detail.set()
    pump(app, lambda: "reports" not in app._inflight)
    for _ in range(10):
        app.update()
        time.sleep(.01)
    assert app._selected is None and app._detail is None
    assert str(app.export_button.cget("state")) == "disabled"


def case_existing_background_collector_prevents_second_start(view):
    app, controller, _ = view
    controller.running = True
    original = controller.snapshot
    controller.snapshot = lambda: {**original(), "pid": 444,
        "collection_options": {"scopes": ["personal"], "interval": 180, "pid": 12345}}
    pump(app, lambda: app.phase_var.get() == "采集中")
    assert str(app.start_button.cget("state")) == "disabled"
    assert str(app.stop_button.cget("state")) == "normal"
    assert app.scopes["personal"].get() and not app.scopes["union"].get()
    assert app.interval_var.get() == "180" and app.pid_var.get() == "12345"
    assert not app.pvp_only_var.get()
    assert app.parallel_var.get() == "1" and str(app.parallel_box.cget("state")) == "disabled"


def case_refresh_preserves_account_when_two_accounts_share_battle_id(view):
    app, _, data = view
    original = data.list_reports
    other = "server:760058/scenario:18/avatar:222"
    def two_accounts(**kwargs):
        result = original(**kwargs)
        result["items"].append({**result["items"][0], "snapshot_id": 9, "namespace": other})
        return result
    data.list_reports = two_accounts
    app.show_page("archive")
    pump(app, lambda: "9" in app._rows)
    app.report_tree.selection_set("9")
    app.report_tree.event_generate("<<TreeviewSelect>>")
    pump(app, lambda: app._selected is not None and app._selected["namespace"] == other)
    app.load_reports(reset=False)
    pump(app, lambda: "reports" not in app._inflight)
    pump(app, lambda: app._selected is not None)
    assert app._selected["namespace"] == other
    assert app._selected["snapshot_id"] == 9


def case_close_leaves_existing_collection_running(view):
    app, controller, _ = view
    controller.running = True
    app.close()
    assert controller.closed and controller.running and controller.stops == 0


def case_errors_are_visible_and_logs_are_bounded(view):
    app, controller, _ = view
    controller.events.append({"type": "error", "text": "游戏连接失效"})
    pump(app, lambda: "游戏连接失效" in app.log_text.get("1.0", "end"))
    app._append_log("line\n" * 2300)
    assert int(app.log_text.index("end-1c").split(".")[0]) < 2200


def case_last_capture_stays_visible_during_next_report_and_after_stop(view):
    app, controller, _ = view
    controller.running = True
    receipt = {"battleID": "10071018760057", "elapsed_seconds": 7.42, "outcome": "complete",
               "source": "collector", "namespace": NS}
    state = {**controller.snapshot(), "pid": 444, "last_capture": receipt}
    app._render_runtime((state, []))
    assert app.last_capture_var.get() == "7.42 秒 · 完整 · 战报 10071018760057"
    app._render_runtime(({**state, "current_battle_id": "10071014660057"}, []))
    assert app.current_var.get() == "10071014660057" and "10071018760057" in app.last_capture_var.get()
    app._render_runtime(({**state, "running": False, "namespace": None}, []))
    assert "7.42 秒" in app.last_capture_var.get() and app.current_var.get() == "—"
    app._render_runtime(({**state, "namespace": "another-account"}, []))
    assert app.last_capture_var.get() == "—"
    app._render_runtime(({**state, "last_capture": None}, []))
    assert app.last_capture_var.get() == "—"


def test_times_and_accounts_have_plain_chinese_labels():
    assert format_time("2026-11-09T03:00:00Z") == "11-09 11:00:00"
    assert "760057 服" in account_label(NS)
    assert account_label("default") == "历史导入档案"


def test_cycle_coverage_never_uses_archive_counts_or_unproved_totals():
    summary = {"visibleReports": 4, "completeVisibleReports": 3,
               "discoveredReports": 1334, "completeReports": 914,
               "finishedAt": 1794474000, "enumerationComplete": False,
               "filter": {"mode": "pvp_only", "unknownReports": 2}}
    text = format_cycle_coverage(summary, True)
    assert "完整 3 / 4 封" in text and "列表未读全" in text and "类型待确认 2 封" in text
    assert "1,334" not in text and "914" not in text
    for invalid in (None, {}, {**summary, "visibleReports": True},
                    {**summary, "completeVisibleReports": 5},
                    {**summary, "finishedAt": float("nan")},
                    {**summary, "filter": {"mode": "all"}}):
        assert format_cycle_coverage(invalid, True) is None


def test_capture_duration_formats_real_and_recovered_outcomes_without_invalid_values():
    receipt = {"battleID": "123", "elapsed_seconds": 61.25, "outcome": "partial", "source": "collector"}
    assert format_last_capture(receipt) == "1 分 01.25 秒 · 待补齐 · 战报 123"
    assert format_last_capture({**receipt, "elapsed_seconds": 0, "outcome": "failed"}) == "0.00 秒 · 失败 · 战报 123"
    assert format_last_capture({**receipt, "source": "recovered_log"}).startswith("约 1 分 01.25 秒")
    assert format_last_capture({**receipt, "elapsed_seconds": 59.999}).startswith("1 分 00.00 秒")
    for invalid in (None, {}, {**receipt, "elapsed_seconds": True}, {**receipt, "elapsed_seconds": float("inf")},
                    {**receipt, "elapsed_seconds": -1}, {**receipt, "elapsed_seconds": 10**1000},
                    {**receipt, "outcome": "unknown"}, {**receipt, "outcome": {}},
                    {**receipt, "source": []}, {**receipt, "battleID": ""}):
        assert format_last_capture(invalid) == "—"


# The shipped desktop uses one Tcl interpreter per process. Give each UI case
# the same lifecycle; pytest's fd-capture swaps must not become Tcl channels.
@pytest.mark.parametrize("case", ['window_opens_without_starting_collection_and_loads_current_counts', 'start_stop_buttons_control_backend_without_blocking_tk', 'invalid_options_do_not_start_collector', 'pvp_checkbox_controls_start_and_reflects_running_worker', 'cycle_coverage_remains_scoped_during_next_cycle', 'browser_reads_detail_unknown_ids_zero_values_and_event_pages', 'changed_filters_invalidate_slow_old_detail', 'existing_background_collector_prevents_second_start', 'refresh_preserves_account_when_two_accounts_share_battle_id', 'close_leaves_existing_collection_running', 'errors_are_visible_and_logs_are_bounded', 'last_capture_stays_visible_during_next_report_and_after_stop'])
def test_native_window_scenario(case):
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
print('NATIVE_WINDOW_SCENARIO_OK')
"""
    env = dict(os.environ, TEMP=".runtime/test-temp", TMP=".runtime/test-temp",
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    completed = subprocess.run([sys.executable, "-B", "-c", script, str(Path(__file__).resolve()), case],
        cwd=Path(__file__).resolve().parents[1], env=env, text=True, encoding="utf-8", errors="replace",
        capture_output=True, timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert "NATIVE_WINDOW_SCENARIO_OK" in completed.stdout
    assert "Tcl_AsyncDelete" not in completed.stderr
    assert "Variable.__del__" not in completed.stderr
