"""Desktop download choices persist and remain separate from an old live worker."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from battle_ui.collection_settings import read_launcher_settings, save_time_range, saved_time_range
from battle_ui.app import format_cycle_coverage


def test_launcher_range_defaults_and_preserves_other_settings(tmp_path):
    settings_path = tmp_path / "config/launcher.json"
    assert saved_time_range(read_launcher_settings(settings_path)) == "24h"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({"python_executable": sys.executable, "collector_parallelism": 2}))
    for choice in ("all", "24h", "72h", "7d"):
        save_time_range(settings_path, choice)
        settings = read_launcher_settings(settings_path)
        assert saved_time_range(settings) == choice
        assert settings["collector_parallelism"] == 2
        assert settings["python_executable"] == sys.executable
    assert not list(settings_path.parent.glob("*.tmp"))


@pytest.mark.parametrize("value", [None, True, "", "25h", [], 24])
def test_invalid_saved_window_is_rejected(value):
    with pytest.raises(ValueError):
        saved_time_range({"collector_time_range": value})


def test_window_coverage_requires_same_range():
    summary = {"visibleReports": 3, "completeVisibleReports": 2,
               "enumerationComplete": True, "finishedAt": 1794474000,
               "filter": {"mode": "pvp_only", "timeRange": "24h", "timeUnknownReports": 2}}
    assert "完整 2 / 3 封" in format_cycle_coverage(summary, True, "24h")
    assert "时间待确认 2 封" in format_cycle_coverage(summary, True, "24h")
    assert format_cycle_coverage(summary, True, "72h") is None
    assert format_cycle_coverage(summary, True, "all") is None


def test_native_window_selects_remembers_and_disables_all_ranges(tmp_path):
    project = Path(__file__).resolve().parents[1]
    script = r'''
import gc, json, runpy, sys
from pathlib import Path
from battle_ui.collection_settings import saved_time_range, read_launcher_settings
from capture.game_assets.collection_window import TIME_RANGE_LABELS
namespace = runpy.run_path(sys.argv[1])
app, controller, data = namespace['make_view']()
pump = namespace['pump']
settings_path = Path(sys.argv[2]) / 'launcher.json'
settings_path.write_text(json.dumps({'collector_parallelism': 2}))
app.settings_path = settings_path
try:
    assert app._selected_time_range() == '24h'
    assert tuple(app.time_range_box.cget('values')) == tuple(TIME_RANGE_LABELS.values())
    for choice, label in TIME_RANGE_LABELS.items():
        app.time_range_var.set(label)
        app.time_range_box.event_generate('<<ComboboxSelected>>')
        app.update()
        assert app._selected_time_range() == choice
        assert saved_time_range(read_launcher_settings(settings_path)) == choice
    assert read_launcher_settings(settings_path)['collector_parallelism'] == 2
    app.time_range_var.set(TIME_RANGE_LABELS['72h'])
    app.time_range_box.event_generate('<<ComboboxSelected>>')
    app.update()
    state = {**controller.snapshot(), 'running': True, 'pid': 777,
             'collection_options': {'pvp_only': True}}
    app._render_runtime((state, []))
    assert app._selected_time_range() == 'all'
    assert str(app.time_range_box.cget('state')) == 'disabled'
    assert saved_time_range(read_launcher_settings(settings_path)) == '72h'
    # Matching status can refine the default after discovery, without a PID change.
    app._render_runtime(({**state,
                          'collection_options': {'pvp_only': True, 'time_range': '24h'}}, []))
    assert app._selected_time_range() == '24h'
    assert saved_time_range(read_launcher_settings(settings_path)) == '72h'
    app._render_runtime(({**state, 'running': False}, []))
    assert app._selected_time_range() == '72h'
    assert str(app.time_range_box.cget('state')) == 'readonly'
    summary = {'visibleReports': 3, 'completeVisibleReports': 2,
               'enumerationComplete': True, 'finishedAt': 1794474000,
               'filter': {'mode': 'pvp_only', 'timeRange': '24h'}}
    state = {**state, 'pid': 778, 'collection_options': {'pvp_only': True, 'time_range': '24h'},
             'raw_status': {'namespace': namespace['NS'], 'result': summary}}
    app._render_runtime((state, []))
    assert '完整 2 / 3 封' in app.scope_coverage_var.get()
    app._render_runtime(({**state, 'pid': 779,
                          'collection_options': {'pvp_only': True, 'time_range': '72h'}}, []))
    assert '等待' in app.scope_coverage_var.get()
    app._render_runtime(({**state, 'running': False}, []))
    app.start_button.invoke()
    pump(app, lambda: bool(controller.started))
    assert controller.started[-1]['time_range'] == '72h'
finally:
    app.close()
del app
gc.collect()
print('NATIVE_TIME_RANGE_OK')
'''
    env = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path),
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    completed = subprocess.run([sys.executable, "-B", "-c", script,
        str(project / "tests/test_battle_ui_app.py"), str(tmp_path)], cwd=project,
        env=env, text=True, encoding="utf-8", errors="replace", capture_output=True,
        timeout=25, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert "NATIVE_TIME_RANGE_OK" in completed.stdout
    assert "Tcl_AsyncDelete" not in completed.stderr
