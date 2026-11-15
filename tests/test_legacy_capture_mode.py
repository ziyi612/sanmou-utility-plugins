"""Both published standalone capture CLIs honor persistent RAM-only settings.

The private start_capture/proxy/addon tools are outside this distribution; no
proxy cases are silently skipped or included in the claimed public coverage.
"""
import builtins
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from capture.game_assets import capture_mode


ROOT = Path(__file__).resolve().parents[1]
ENTRIES = (
    ("capture.game_assets.battle_collector", ()),
    ("capture.game_assets.export_battle_report_runtime", ("1",)),
)


@pytest.fixture(params=ENTRIES, ids=("collector", "exporter"))
def entry(request, monkeypatch, tmp_path):
    module_name, arguments = request.param
    monkeypatch.setattr(capture_mode, "ROOT", tmp_path)
    monkeypatch.delenv("SANMOU_QUARK_CONFIG", raising=False)
    module = importlib.import_module(module_name)
    monkeypatch.setattr(sys, "argv", [module.__file__, *arguments])
    return module


def configure(root, *, launcher=None, default=None, selected=None, monkeypatch=None):
    directory = root / "config"
    directory.mkdir(exist_ok=True)
    for name, value in (("launcher.json", launcher), ("quark.local.json", default)):
        if value is not None:
            (directory / name).write_text(json.dumps(value), encoding="utf-8")
    if selected is not None:
        path = root / "selected-provider.json"
        path.write_text(json.dumps(selected), encoding="utf-8")
        monkeypatch.setenv("SANMOU_QUARK_CONFIG", str(path))


def forbid_capture_side_effects(module, monkeypatch):
    attempts = []

    def refused(*_args, **_kwargs):
        attempts.append(True)
        raise AssertionError("A blocked legacy command attempted a capture side effect")

    original_open = builtins.open
    original_path_open = Path.open

    def checked_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            return refused()
        return original_open(file, mode, *args, **kwargs)

    def checked_path_open(path, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            return refused()
        return original_path_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", checked_open)
    monkeypatch.setattr(Path, "open", checked_path_open)
    for name in ("mkdir", "write_text", "write_bytes"):
        monkeypatch.setattr(Path, name, refused)
    monkeypatch.setitem(sys.modules, "capture.game_assets.run_lua_in_game",
                        SimpleNamespace(GameLuaRunner=refused))
    if hasattr(module, "BattleCollector"):
        monkeypatch.setattr(module, "BattleCollector", refused)
    else:
        monkeypatch.setattr(module, "capture_report", refused)
        monkeypatch.setattr(module, "atomic_json", refused)
    return attempts


@pytest.mark.parametrize("source", ("launcher", "default", "selected", "hidden-default"))
def test_memory_marker_blocks_each_published_legacy_main_before_capture(entry, monkeypatch, tmp_path, capsys, source):
    local, memory = {"storage_backend": "local"}, {"storage_backend": "quark-memory"}
    configure(tmp_path, launcher=memory if source == "launcher" else local,
              default=memory if source in {"default", "hidden-default"} else local,
              selected=memory if source == "selected" else local if source == "hidden-default" else None,
              monkeypatch=monkeypatch)
    attempts = forbid_capture_side_effects(entry, monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == 2
    error = capsys.readouterr().err
    assert "start_battle_archive.cmd" in error and "battle_reports.py collect" in error
    assert "纯云端" in error
    assert attempts == []


@pytest.mark.parametrize("invalid", ("json", "list", "backend", "unknown-backend", "unreadable", "missing-link",
                                     "missing-selected", "empty-selected"))
def test_invalid_mode_never_falls_back_to_local(entry, monkeypatch, tmp_path, capsys, invalid):
    configure(tmp_path, launcher={"storage_backend": "local"})
    path = tmp_path / "config/quark.local.json"
    if invalid == "empty-selected":
        monkeypatch.setenv("SANMOU_QUARK_CONFIG", " ")
    elif invalid == "missing-selected":
        monkeypatch.setenv("SANMOU_QUARK_CONFIG", str(tmp_path / "missing.json"))
    elif invalid in {"unreadable", "missing-link"}:
        path.write_text("{}", encoding="utf-8")
        original = Path.read_text

        def unreadable(current, *args, **kwargs):
            if current == path:
                raise (FileNotFoundError if invalid == "missing-link" else PermissionError)("synthetic unreadable config")
            return original(current, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", unreadable)
    else:
        path.write_text({"json": "{broken", "list": "[]", "backend": '{"storage_backend":null}',
                         "unknown-backend": '{"storage_backend":"quark-memroy"}'}[invalid],
                        encoding="utf-8")
    attempts = forbid_capture_side_effects(entry, monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == 2
    assert "已阻止" in capsys.readouterr().err
    assert attempts == []


@pytest.mark.parametrize("backend", (None, "local", "sqlite", "quark", "old-settings"),
                         ids=("unconfigured", "explicit-local", "sqlite", "quark", "old-settings"))
def test_legacy_local_compatibility_still_reaches_only_fake_capture(entry, monkeypatch, tmp_path, backend):
    if backend == "old-settings":
        configure(tmp_path, launcher={"collector_parallelism": 1}, default={"enabled": False})
    elif backend is not None:
        configure(tmp_path, launcher={"storage_backend": backend}, default={"storage_backend": "quark"})
    calls = []

    class Runner:
        def __init__(self, *_args):
            calls.append("runner")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    class Collector:
        def __init__(self, *_args, **kwargs):
            assert "artifact_store" not in kwargs
            calls.append("collector")

        def run_cycle(self):
            calls.append("cycle")
            return {"coverageComplete": True}

    class Output:
        def mkdir(self, **_kwargs):
            calls.append("fake-mkdir")

        def __truediv__(self, _name):
            return self

    monkeypatch.setitem(sys.modules, "capture.game_assets.run_lua_in_game", SimpleNamespace(GameLuaRunner=Runner))
    monkeypatch.setattr(entry, "safe_output_path", lambda _path: Output())
    if hasattr(entry, "BattleCollector"):
        monkeypatch.setattr(entry, "BattleCollector", Collector)
    else:
        monkeypatch.setattr(entry, "capture_report", lambda *_args: calls.append("capture") or {
            "exported": True, "complete": True})
        monkeypatch.setattr(entry, "atomic_json", lambda *_args: calls.append("fake-manifest"))
    entry.main()
    if hasattr(entry, "BattleCollector"):
        assert calls == ["runner", "collector", "cycle"]
    else:
        assert calls == ["fake-mkdir", "runner", "capture", "fake-manifest"]
    assert not (tmp_path / "capture").exists()


def test_help_bypasses_even_invalid_config_without_capture(entry, monkeypatch, tmp_path, capsys):
    configure(tmp_path, launcher=[])
    attempts = forbid_capture_side_effects(entry, monkeypatch)
    monkeypatch.setattr(sys, "argv", [entry.__file__, "--help"])
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == 0 and "usage:" in capsys.readouterr().out.lower()
    assert attempts == []


@pytest.mark.parametrize("script", ("capture/game_assets/battle_collector.py",
                                     "capture/game_assets/export_battle_report_runtime.py"))
def test_real_standalone_help_from_another_directory_needs_no_game(script, tmp_path):
    environment = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path), TMPDIR=str(tmp_path),
                       PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                       SANMOU_QUARK_CONFIG=str(tmp_path / "not-configured.json"))
    result = subprocess.run([sys.executable, "-B", str(ROOT / script), "--help"], cwd=tmp_path,
                            env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0 and "usage:" in result.stdout.lower(), result.stderr
