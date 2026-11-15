"""Real Tk exercises all restored Quark statistics without local report files."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def scenario_cloud_statistics_gui(root_path):
    import builtins
    import csv
    import gc
    import io
    import runpy
    import sqlite3
    import threading
    import time
    from unittest.mock import patch

    from battle_ui.app import BattleApp
    from battle_ui.cloud_data import CloudBattleData
    from parser import semantic_catalog

    fixtures = runpy.run_path(str(Path(__file__).with_name("test_cloud_stats.py")))
    namespace = fixtures["NS"]
    values = [fixtures["payload"](str(1001 + index)) for index in range(21)]
    for index, value in enumerate(values):
        # Keep one recent-day report, plus twenty older-day samples that meet
        # the unchanged minimum known-result rule for the win-rate table.
        shift = 86400 if index == 20 else 0
        for source in (value["data"], *(value["components"][name] for name in ("basic", "detail", "all"))):
            source["timeStamp"] += shift
            source["endTimeStampTime"] += shift
            for hero in source["battleSettlement"]["defensive"]["army_member"]:
                hero["evolutionNum"] = 5
        observation = value["discovery"][0]
        observation["row"]["combatInfo"]["endTimeStampTime"] += shift
        observation["clientPeerless"][0]["endTimeStamp"] += shift

    store = fixtures["Store"](*values)
    store.catalog["states"][namespace] = fixtures["state"]()

    class Client:
        def __init__(self):
            self.uploads = []
            self.downloads = []
            self.cloud = {}

        def upload_bytes(self, raw, name, *, parent_fid):
            assert parent_fid == "fixture-cloud-folder"
            fid = "csv-" + str(len(self.uploads))
            self.uploads.append((name, bytes(raw)))
            self.cloud[fid] = bytes(raw)
            return {"fid": fid}

        def download_bytes(self, fid, *, max_bytes):
            raw = self.cloud[fid]
            assert max_bytes == len(raw)
            self.downloads.append(fid)
            return raw

    store.client, store.folder_id = Client(), "fixture-cloud-folder"
    data = CloudBattleData(store)
    closed = []
    real_close = data.close

    def close_data():
        closed.append(True)
        real_close()

    data.close = close_data

    class Controller:
        closed = False

        def snapshot(self):
            return {"running": False, "status": "已停止", "can_start": True, "can_stop": False}

        def drain_events(self, _limit=100):
            return []

        def close(self):
            self.closed = True

    controller = Controller()
    connections = []
    real_connect = sqlite3.connect
    real_open, real_io_open = builtins.open, io.open
    main_thread = threading.get_ident()

    def memory_connect(database, *args, **kwargs):
        assert database == ":memory:", "The cloud GUI opened a disk-backed SQLite database"
        connection = real_connect(database, *args, **kwargs)
        connections.append(connection)
        return connection

    def forbidden(*_args, **_kwargs):
        raise AssertionError("The cloud GUI attempted a local report write or file dialog")

    def guarded_open(original, path, mode="r", *args, **kwargs):
        if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
            forbidden()
        return original(path, mode, *args, **kwargs)

    with patch.object(semantic_catalog, "build_default_mapper", lambda: None), \
            patch.object(sqlite3, "connect", memory_connect), \
            patch.object(Path, "mkdir", forbidden), patch.object(Path, "write_bytes", forbidden), \
            patch.object(Path, "write_text", forbidden), \
            patch.object(builtins, "open", lambda *a, **k: guarded_open(real_open, *a, **k)), \
            patch.object(io, "open", lambda *a, **k: guarded_open(real_io_open, *a, **k)), \
            patch("battle_ui.app.filedialog.asksaveasfilename", forbidden):
        app = BattleApp(root_path, controller, data)
        app.withdraw()
        callback_errors = []
        app.report_callback_exception = lambda *error: callback_errors.append(error)
        real_message = app._message

        def message(text, error=False):
            assert threading.get_ident() == main_thread, "A worker touched the Tk GUI"
            return real_message(text, error)

        app._message = message

        def pump(predicate):
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                app.update()
                assert not callback_errors, callback_errors
                assert "界面更新失败" not in app.error_var.get(), app.error_var.get()
                if predicate():
                    return
                time.sleep(.01)
            raise AssertionError("Cloud Tk did not reach expected state: " + app.error_var.get())

        def export(action, count):
            action()
            pump(lambda: len(store.client.uploads) == count and "csv-export" not in app._inflight)
            assert len(store.client.downloads) == count
            assert "已上传夸克并校验" in app.message_var.get()

        try:
            pump(lambda: app.metric_vars["total"].get() == "21")
            account = next(label for label, ns in app._account_map.items() if ns == namespace)
            app.account_var.set(account)
            app._account_changed()
            for page, service in (("daily", app.daily_service), ("team_winrates", app.team_winrate_service),
                                  ("opponents", app.opponent_service), ("peerless", app.peerless_service)):
                assert service is not None and str(app.nav_buttons[page]["state"]) == "normal"

            app.nav_buttons["daily"].invoke()
            pump(lambda: app._daily_data.get("available"))
            assert app._page == "daily" and app._daily_data["namespace"] == namespace
            assert len(app.days_tree.get_children()) == 2
            old_day, new_day = sorted(app._daily_data["dates"])
            app.day_var.set(old_day)
            app.player_search_var.set("200")
            app._filter_daily()
            assert len(app.players_tree.get_children()) == 1
            assert app._daily_export_rows[0][2] == "200" and app._daily_export_rows[0][5] == 300
            app.player_search_var.set("absent-player")
            app._filter_daily()
            assert not app.players_tree.get_children()
            app.player_search_var.set("200")
            app._filter_daily()
            export(app.export_daily, 1)

            # Progress is an informational queue event, not an operation result.
            # It must not pop callbacks, futures or in-flight state.
            key = "cloud-stats-progress"
            marker = object()
            app._active_generations[key], app._generation[key] = 0, 0
            app._futures[key] = marker
            app._inflight.add(key)
            app._events.put((key, 0, {"phase": "reports", "completed": 7, "total": 21}, None))
            app.after_cancel(app._poll_id)
            app._poll()
            assert app._active_generations[key] == 0 and app._futures[key] is marker
            assert key in app._inflight and "7/21" in app.daily_note_var.get()
            app._active_generations.pop(key)
            app._generation.pop(key)
            app._futures.pop(key)
            app._inflight.remove(key)

            app.nav_buttons["team_winrates"].invoke()
            pump(lambda: app._team_winrate_data.get("available"))
            app.team_tier_var.set("high")
            app._filter_team_winrate_tier()
            assert len(app.team_winrate_trees["without_skills"].get_children()) == 2
            assert app._team_winrate_data["totals"]["reports"] == 21
            app.team_day_var.set(old_day)
            app.load_team_winrates()
            pump(lambda: app._team_winrate_data.get("day") == old_day)
            assert app._team_winrate_data["totals"]["reports"] == 20
            assert len(app.team_winrate_trees["without_skills"].get_children()) == 2
            export(app.export_team_winrates, 2)
            assert app.team_winrate_export_menu.entrycget(0, "state") == "disabled"
            app.export_team_winrates_excel()
            assert len(store.client.uploads) == 2
            app.team_day_var.set(new_day)
            app.load_team_winrates()
            pump(lambda: app._team_winrate_data.get("day") == new_day)
            assert app._team_winrate_data["totals"]["reports"] == 1
            assert not app.team_winrate_trees["without_skills"].get_children()

            app.nav_buttons["opponents"].invoke()
            pump(lambda: app._roster_data.get("available"))
            assert app._roster_data["day"] == new_day
            assert len(app.opponents_tree.get_children()) == 1
            player_key = app.opponents_tree.get_children()[0]
            app.opponents_tree.selection_set(player_key)
            app._opponent_selected()
            assert "200" in app.opponent_text.get("1.0", "end")
            assert "1021" in app.opponent_text.get("1.0", "end")
            export(app.export_roster, 3)

            app.nav_buttons["peerless"].invoke()
            pump(lambda: app._peerless_data.get("available"))
            assert len(app.peerless_tree.get_children()) == 21
            app.peerless_day_var.set(new_day)
            app.peerless_search_var.set("200")
            app._filter_peerless()
            assert len(app.peerless_tree.get_children()) == 1
            app.peerless_search_var.set("absent-player")
            app._filter_peerless()
            assert not app.peerless_tree.get_children()
            app.peerless_search_var.set("200")
            app._filter_peerless()
            export(app.export_peerless, 4)
            occurrence_key = app.peerless_tree.get_children()[0]
            app.peerless_tree.selection_set(occurrence_key)
            app._peerless_selected()
            assert str(app.peerless_view_button["state"]) == "normal"
            assert "client_BattleReportHelper.IsLegend" in app.peerless_text.get("1.0", "end")
            app.peerless_view_button.invoke()
            pump(lambda: app._detail is not None)
            assert app._page == "archive" and app._detail["report"]["battle_uid"] == "1021"
            assert len(app.report_tree.get_children()) == 1
            assert str(app.export_button["state"]) == "disabled"
            app.export_selected()
            assert len(store.client.uploads) == 4

            for name, raw in store.client.uploads:
                assert name.endswith(".csv") and raw.startswith(b"\xef\xbb\xbf")
                assert len(list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))) >= 2
            projection = data._statistics().connection
            assert projection in connections
            assert all(not row[2] for row in projection.execute("PRAGMA database_list"))
        finally:
            app.close()
        assert controller.closed and closed == [True]
        assert data.statistics_status()["phase"] in {"closed", "idle"}
        try:
            projection.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            pass
        else:
            raise AssertionError("Closing the GUI did not close its RAM projection")
        del app
        gc.collect()
    assert not list(root_path.rglob("*")), "The cloud statistics GUI left files in its runtime directory"


def test_actual_cloud_statistics_gui_filters_source_jump_and_quark_exports_stay_in_ram(tmp_path):
    script = "import pathlib,runpy,sys; runpy.run_path(sys.argv[1])['scenario_cloud_statistics_gui'](pathlib.Path(sys.argv[2])); print('CLOUD_STATS_GUI_OK')"
    environment = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path), TMPDIR=str(tmp_path),
                       PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-B", "-c", script, str(Path(__file__).resolve()), str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True,
        encoding="utf-8", timeout=65, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "CLOUD_STATS_GUI_OK" in result.stdout
