"""The memory-only GUI cannot create the legacy DB, logs or collector files."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import battle_reports_gui as entry
from battle_ui import memory_backend


class Process:
    pid = 876543
    returncode = None

    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO('{"phase":"uploading","current_battle_id":"1001","namespace":"fixture"}\n')
        self.stderr = io.StringIO("")

    def poll(self):
        return self.returncode


def test_memory_collector_has_pipes_and_ram_state_but_no_disk_files(tmp_path, monkeypatch):
    calls = []
    process = Process()

    def start(command, **kwargs):
        calls.append((command, kwargs))
        return process

    controller = memory_backend.MemoryCollectorController(tmp_path)
    monkeypatch.setattr(controller, "_other_collectors", lambda: [])
    monkeypatch.setattr(memory_backend.subprocess, "Popen", start)

    def forbidden(*_args, **_kwargs):
        pytest.fail("The memory collector attempted a local directory or file write")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    snapshot = controller.start({"scopes": ["personal"], "parallel": 1, "interval": 60,
                                 "pvp_only": True, "time_range": "24h"})
    command, options = calls[0]
    assert "collect" in command and "--parallel" in command
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.PIPE
    assert not hasattr(controller, "status_path") and not hasattr(controller, "log_dir")
    assert snapshot["running"] and snapshot["log_path"] is None
    with pytest.raises(RuntimeError, match="仍在运行"):
        controller.start()
    controller.request_stop()
    assert process.stdin.getvalue() == '{"command":"stop"}\n'
    assert controller.snapshot()["stopping"]
    controller.close()
    assert process.stdin.getvalue().count("stop") == 1
    assert not (tmp_path / "data").exists() and not (tmp_path / "logs").exists()


def test_memory_controller_refuses_another_live_collector_and_parallelism(tmp_path, monkeypatch):
    controller = memory_backend.MemoryCollectorController(tmp_path)
    monkeypatch.setattr(controller, "_other_collectors", lambda: [123456])
    status = controller.snapshot()
    assert status["running"] and status["external"] and not status["can_start"] and not status["can_stop"]
    with pytest.raises(RuntimeError, match="已有采集器"):
        controller.start()
    for value in (2, True):
        with pytest.raises(ValueError, match="固定为 1"):
            controller.start({"parallel": value})


def test_memory_status_queue_is_bounded_and_redacts_credentials(tmp_path):
    controller = memory_backend.MemoryCollectorController(tmp_path)
    for _ in range(600):
        controller._consume('{"phase":"collecting","error":"access_token=synthetic-credential"}')
    values = controller.drain_events(1000)
    assert len(values) == 500
    assert "synthetic-credential" not in json.dumps(values)


def test_memory_stop_tolerates_collector_finishing_during_pipe_write(tmp_path):
    process = Process()

    class FinishingPipe:
        def write(self, _line):
            process.returncode = 0
            raise BrokenPipeError("fixture child just finished")

    process.stdin = FinishingPipe()
    controller = memory_backend.MemoryCollectorController(tmp_path)
    controller._process = process
    controller.request_stop()
    controller.close()


def write_config(root, *, launcher=None, quark=None):
    directory = root / "config"
    directory.mkdir(parents=True)
    if launcher is not None:
        (directory / "launcher.json").write_text(json.dumps(launcher), encoding="utf-8")
    if quark is not None:
        (directory / "quark.local.json").write_text(json.dumps(quark), encoding="utf-8")


def test_gui_memory_mode_is_fail_closed_if_provider_config_disappears_or_is_corrupt(tmp_path, monkeypatch):
    monkeypatch.delenv("SANMOU_QUARK_CONFIG", raising=False)
    write_config(tmp_path, launcher={"storage_backend": "quark-memory"})
    with pytest.raises(ValueError, match="配置缺失"):
        entry.memory_storage_enabled(tmp_path)
    (tmp_path / "config/quark.local.json").write_text("invalid JSON", encoding="utf-8")
    with pytest.raises(ValueError):
        entry.memory_storage_enabled(tmp_path)


@pytest.mark.parametrize("quark", [None, {}, {"enabled": True}])
def test_gui_without_launcher_requires_memory_setup_instead_of_defaulting_local(tmp_path, monkeypatch, quark):
    monkeypatch.delenv("SANMOU_QUARK_CONFIG", raising=False)
    write_config(tmp_path, quark=quark)
    with pytest.raises(ValueError, match="setup_quark.py --memory-only"):
        entry.memory_storage_enabled(tmp_path)
    assert not (tmp_path / "data").exists() and not (tmp_path / "logs").exists()


@pytest.mark.parametrize("filename,value", [("launcher.json", []), ("launcher.json", None),
                                            ("quark.local.json", []), ("quark.local.json", None)])
def test_gui_rejects_every_existing_nonobject_configuration(tmp_path, monkeypatch, filename, value):
    monkeypatch.delenv("SANMOU_QUARK_CONFIG", raising=False)
    write_config(tmp_path, launcher={})
    (tmp_path / "config" / filename).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON 对象"):
        entry.memory_storage_enabled(tmp_path)


def test_gui_explicit_legacy_launcher_remains_compatible_and_override_cannot_hide_strict(tmp_path, monkeypatch):
    monkeypatch.delenv("SANMOU_QUARK_CONFIG", raising=False)
    write_config(tmp_path, launcher={"collector_parallelism": 2})
    assert entry.memory_storage_enabled(tmp_path) is False
    (tmp_path / "config/quark.local.json").write_text('{"storage_backend":"quark-memory"}', encoding="utf-8")
    selected = tmp_path / "ordinary-provider.json"
    selected.write_text('{"enabled":true}', encoding="utf-8")
    monkeypatch.setenv("SANMOU_QUARK_CONFIG", str(selected))
    assert entry.memory_storage_enabled(tmp_path) is True


@pytest.mark.parametrize("quark", [None, {"enabled": True}])
def test_real_gui_entry_missing_launcher_exits_without_legacy_logs_or_database(tmp_path, quark):
    write_config(tmp_path, quark=quark)
    executable = tmp_path / "battle_reports_gui.py"
    executable.write_bytes(Path(entry.__file__).read_bytes())
    script = r'''
import os, pathlib, runpy, sys
from unittest.mock import patch
from contextlib import nullcontext
scope = patch('ctypes.windll.user32.MessageBoxW', return_value=0) if os.name == 'nt' else nullcontext()
sys.argv = [sys.argv[1]]
with scope:
    runpy.run_path(sys.argv[0], run_name='__main__')
'''
    environment = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path), TMPDIR=str(tmp_path),
                       PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    environment.pop("SANMOU_QUARK_CONFIG", None)
    result = subprocess.run([sys.executable, "-B", "-c", script, str(executable)],
        cwd=Path(entry.__file__).parent, env=environment, capture_output=True, text=True,
        encoding="utf-8", timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 1
    assert "setup_quark.py --memory-only" in result.stderr
    assert not (tmp_path / "data").exists() and not (tmp_path / "logs").exists()


def test_gui_entry_uses_memory_classes_and_never_constructs_legacy_storage(tmp_path, monkeypatch):
    monkeypatch.delenv("SANMOU_QUARK_CONFIG", raising=False)
    write_config(tmp_path, launcher={"storage_backend": "quark-memory", "collector_parallelism": 2},
                 quark={"storage_backend": "quark-memory"})
    monkeypatch.setattr(entry, "ROOT", tmp_path)
    from battle_ui import app, backend, cloud_data, data
    calls = {}

    def forbidden(*_args, **_kwargs):
        pytest.fail("GUI constructed the legacy disk-backed data/controller")

    class App:
        def __init__(self, _root, controller, view, **kwargs):
            calls.update(controller=controller, data=view, options=kwargs)

        def mainloop(self):
            calls["mainloop"] = True

    memory_data, memory_controller = object(), object()
    monkeypatch.setattr(backend, "Controller", forbidden)
    monkeypatch.setattr(data, "BattleData", forbidden)
    monkeypatch.setattr(cloud_data, "CloudBattleData", lambda: memory_data)
    monkeypatch.setattr(memory_backend, "MemoryCollectorController", lambda *_args, **_kwargs: memory_controller)
    monkeypatch.setattr(app, "BattleApp", App)
    for name in ("TEMP", "TMP", "PYTHONDONTWRITEBYTECODE", "PYTHONIOENCODING"):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    assert entry.main([]) == 0
    assert calls["controller"] is memory_controller and calls["data"] is memory_data
    assert calls["options"]["parallelism"] == 1 and calls["mainloop"]
    assert not (tmp_path / "data").exists() and not (tmp_path / "logs").exists()


def scenario_actual_memory_gui(tmp_path):
    import gc
    from battle_ui.app import BattleApp
    from battle_ui.cloud_data import CloudBattleData
    from unittest.mock import patch
    import runpy
    fixtures = runpy.run_path(str(Path(__file__).with_name("test_cloud_memory_data.py")))
    data = CloudBattleData(fixtures["Store"]())

    class Controller:
        def __init__(self):
            self.closed = False

        def snapshot(self):
            return {"running": False, "status": "已停止", "can_start": True, "can_stop": False}

        def drain_events(self, _limit=100):
            return []

        def close(self):
            self.closed = True

    controller = Controller()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Memory GUI attempted a local file or directory write")

    from parser import semantic_catalog
    with patch.object(semantic_catalog, "build_default_mapper", lambda: None), \
            patch.object(Path, "mkdir", forbidden), patch.object(Path, "write_bytes", forbidden), \
            patch.object(Path, "write_text", forbidden):
        app = BattleApp(tmp_path, controller, data)
        app.withdraw()

        def pump(predicate):
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                app.update()
                if predicate():
                    return
                time.sleep(.01)
            raise AssertionError("Memory Tk did not reach expected state")

        pump(lambda: app.metric_vars["total"].get() == "1")
        assert app.daily_service is app.opponent_service is app.peerless_service is app.team_winrate_service is None
        assert str(app.nav_buttons["daily"]["state"]) == "disabled"
        assert app.parallel_var.get() == "1" and str(app.parallel_box["state"]) == "disabled"
        app.show_page("daily")
        assert app._page == "archive"
        pump(lambda: len(app._rows) == 1)
        item = next(iter(app._rows))
        app.report_tree.selection_set(item)
        app._select_report()
        pump(lambda: app._detail is not None)
        assert str(app.export_button["state"]) == "disabled"
        app.export_selected()
        app.open_logs()
        app.show_page("cloud")
        pump(lambda: app.cloud_panel.note.get().startswith("已读取云端状态。")
             and "运行内存" in app.cloud_panel.note.get())
        app.close()
        assert controller.closed
        del app
        gc.collect()
    assert not (tmp_path / "data").exists() and not (tmp_path / "logs").exists()


def test_actual_memory_gui_pages_details_and_export_do_not_write_local_files(tmp_path):
    script = "import pathlib,runpy,sys; runpy.run_path(sys.argv[1])['scenario_actual_memory_gui'](pathlib.Path(sys.argv[2])); print('MEMORY_GUI_OK')"
    environment = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path), TMPDIR=str(tmp_path),
                       PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-B", "-c", script, str(Path(__file__).resolve()), str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True,
        encoding="utf-8", timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "MEMORY_GUI_OK" in result.stdout
