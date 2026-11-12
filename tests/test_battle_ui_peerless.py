"""Each occurrence links only its recorded player and exact report snapshot."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

from battle_ui.app import BattleApp, account_label

NS = "server:760057/scenario:18/avatar:123456789"
OTHER = "server:760058/scenario:18/avatar:222"


def result(namespace=NS):
    rows = []
    for snapshot, battle, day, uid, name in ((8, "123", "2026-11-07", "100", "同一玩家"),
            (9, "124", "2026-11-08", "100", "同一玩家"), (10, "125", "2026-11-08", "200", "另一玩家")):
        rows.append({"namespace": namespace, "battle_uid": battle, "snapshot_id": snapshot,
            "player_uid": uid, "player_name": name, "side": "attacker" if uid == "100" else "defensive",
            "day": day, "timestamp": 1794140000 + snapshot, "union_name": "当次同盟", "union_uid": "66",
            "avatar_data": {"avatar_id": uid, "avatar_name": name, "avatar_union_name": "当次同盟"},
            "evidence": [{"pointer": "/components/basic/marker", "value": True, "kind": "明确标记"}],
            "completeness": "complete" if snapshot != 10 else "partial"})
    return {"available": True, "namespace": namespace, "dates": ["2026-11-07", "2026-11-08"],
        "records": rows, "counts": {"occurrences": 3, "players": 2, "reports": 3},
        "coverage": {}, "note": "只依据已捕获的无双标记；无双按发生次数记录。"}


class FakePeerless:
    def __init__(self):
        self.calls, self.delay, self.failure = [], None, None

    def summary(self, namespace=None):
        self.calls.append(namespace)
        if self.delay is not None:
            self.delay.wait(5)
        if self.failure:
            raise RuntimeError(self.failure)
        return copy.deepcopy(result(namespace))


def make_view():
    base = runpy.run_path(str(Path(__file__).with_name("test_battle_ui_app.py")))
    controller, data = base["FakeController"](), base["FakeData"]()
    data.detail_calls = []
    detail = data.detail
    def exact_detail(snapshot_id, **kwargs):
        data.detail_calls.append((snapshot_id, kwargs))
        payload = detail(snapshot_id, **kwargs)
        payload["report"]["battle_uid"] = str(115 + snapshot_id)
        return payload
    data.detail = exact_detail
    service = FakePeerless()
    app = BattleApp(Path("."), controller, data, peerless_service=service)
    app.withdraw()
    app.after_cancel(app._initial_refresh_id)
    app._next_overview = time.monotonic() + 3600
    app._account_map.update({account_label(NS): NS, account_label(OTHER): OTHER})
    app.account_var.set(account_label(NS))
    return app, service, data, base["pump"]


def case_all_dates_occurrences_and_player_filters(view):
    app, _, _, _ = view
    app.show_page("peerless", load=False)
    app._render_peerless(result())
    assert app.peerless_day_var.get() == "全部日期"
    assert [var.get() for var in app.peerless_metrics.values()] == ["3", "2", "3"]
    assert len(app.peerless_tree.get_children()) == len(app._peerless_export_rows) == 3
    app.peerless_day_var.set("2026-11-08")
    app._filter_peerless()
    assert app.peerless_metrics["occurrences"].get() == "2"
    app.peerless_search_var.set("100")
    app._filter_peerless()
    assert len(app.peerless_tree.get_children()) == 1 and app._peerless_export_rows[0][7] == "124"
    app.peerless_day_var.set("全部日期")
    app.peerless_search_var.set("同一玩家")
    app._filter_peerless()
    assert len(app._peerless_export_rows) == 2 and app.peerless_metrics["players"].get() == "1"
    assert {row[7] for row in app._peerless_export_rows} == {"123", "124"}


def case_player_information_evidence_and_exact_snapshot_navigation(view):
    app, _, data, pump = view
    app._render_peerless(result())
    app.peerless_tree.selection_set("1")
    app._peerless_selected()
    text = app.peerless_text.get("1.0", "end")
    assert "同一玩家" in text and "当次同盟" in text
    assert "这次战报：124" in text and "/components/basic/marker" in text
    assert "这次战报：123" not in text and "另一玩家" not in text
    assert str(app.peerless_view_button.cget("state")) == "normal"
    app.view_peerless_report()
    pump(app, lambda: app._detail is not None)
    assert app._page == "archive" and app._selected["snapshot_id"] == 9
    assert app._selected["namespace"] == NS and app._detail["report"]["battle_uid"] == "124"
    assert [sid for sid, _ in data.detail_calls] == [9]
    assert data.calls == []
    assert app.page_var.get() == "当前只显示这次无双关联的战报"


def case_account_change_clears_cache_buttons_and_late_old_result(view):
    app, service, _, pump = view
    app.show_page("peerless", load=False)
    app._render_peerless(result())
    app.peerless_tree.selection_set("0")
    app._peerless_selected()
    service.delay = threading.Event()
    try:
        app.account_var.set(account_label(OTHER))
        app._account_changed()
        app._filter_peerless()
        assert app._peerless_data == {} and not app._peerless_export_rows
        assert not app.peerless_tree.get_children()
        assert str(app.peerless_view_button.cget("state")) == "disabled"
        assert str(app.peerless_export_button.cget("state")) == "disabled"
        assert "同一玩家" not in app.peerless_text.get("1.0", "end")
        app._render_peerless(result(NS))
        assert app._peerless_data == {} and not app._peerless_export_rows
    finally:
        service.delay.set()
    pump(app, lambda: "peerless" not in app._inflight)
    assert service.calls[-1] == OTHER
    assert all(row[2] == OTHER for row in app._peerless_export_rows)


def case_empty_unknown_date_and_export_only_filtered_occurrences(view):
    app, _, _, _ = view
    payload = result()
    payload["records"][0].update(day=None, snapshot_id=None)
    app._render_peerless(payload)
    assert "日期未知" in app.peerless_day_box.cget("values")
    app.peerless_day_var.set("日期未知")
    app._filter_peerless()
    assert len(app.peerless_tree.get_children()) == 1
    app.peerless_tree.selection_set("0")
    app._peerless_selected()
    assert str(app.peerless_view_button.cget("state")) == "disabled"
    with patch.object(app, "_export_csv") as export:
        app.export_peerless()
    _, _, headers, rows = export.call_args.args
    assert len(rows) == 1 and rows[0][7] == "123"
    assert "该次玩家信息" in headers and "判定依据" in headers
    empty = result()
    empty["records"], empty["dates"] = [], []
    app._render_peerless(empty)
    assert not app.peerless_tree.get_children() and not app._peerless_export_rows
    assert app.peerless_metrics["occurrences"].get() == "0"
    assert "尚未发现可确认" in app.peerless_text.get("1.0", "end")
    assert str(app.peerless_view_button.cget("state")) == str(app.peerless_export_button.cget("state")) == "disabled"


def case_old_archive_query_cannot_overwrite_opened_occurrence(view):
    app, _, data, pump = view
    old_ready, release = threading.Event(), threading.Event()
    def slow_old_list():
        old_ready.set()
        release.wait(5)
        return {"items": [], "offset": 0, "has_more": False}
    app._submit("reports", slow_old_list, app._render_reports, replace=True)
    pump(app, old_ready.is_set)
    try:
        app._render_peerless(result())
        app.peerless_tree.selection_set("1")
        app._peerless_selected()
        app.view_peerless_report()
        pump(app, lambda: app._detail is not None)
    finally:
        release.set()
    pump(app, lambda: not any(key[0] == "reports" for key in app._callbacks))
    assert app._selected["snapshot_id"] == 9 and app._detail["report"]["battle_uid"] == "124"
    assert len(app.report_tree.get_children()) == 1 and [sid for sid, _ in data.detail_calls] == [9]


def case_failed_account_load_keeps_no_old_occurrences(view):
    app, service, _, pump = view
    app._render_peerless(result())
    service.failure = "数据读取失败"
    app.load_peerless()
    pump(app, lambda: "peerless" not in app._inflight)
    assert "数据读取失败" in app.message_var.get()
    assert app._peerless_data == {} and not app._peerless_export_rows
    assert not app.peerless_tree.get_children()


@pytest.mark.parametrize("case", [
    "all_dates_occurrences_and_player_filters", "player_information_evidence_and_exact_snapshot_navigation",
    "account_change_clears_cache_buttons_and_late_old_result", "empty_unknown_date_and_export_only_filtered_occurrences",
    "old_archive_query_cannot_overwrite_opened_occurrence", "failed_account_load_keeps_no_old_occurrences"])
def test_peerless_native_scenario(case):
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
print('PEERLESS_WINDOW_SCENARIO_OK')
"""
    env = dict(os.environ, TEMP=".runtime/test-temp", TMP=".runtime/test-temp",
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    completed = subprocess.run([sys.executable, "-B", "-c", script, str(Path(__file__).resolve()), case],
        cwd=Path(__file__).resolve().parents[1], env=env, text=True, encoding="utf-8", errors="replace",
        capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert "PEERLESS_WINDOW_SCENARIO_OK" in completed.stdout
    assert "Tcl_AsyncDelete" not in completed.stderr
    assert "Variable.__del__" not in completed.stderr
