"""Atomic state publishing keeps the old file intact under Windows readers."""
import ctypes
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import Mock

import pytest

import battle_reports
from capture.game_assets import atomic_files
from capture.game_assets import export_battle_report_runtime as exporter


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing semantics")
@pytest.mark.parametrize("writer", [battle_reports.atomic_json, exporter.atomic_json])
def test_atomic_json_waits_for_windows_reader_to_release_delete_sharing(tmp_path, writer):
    path = tmp_path / "collector_state.json"
    old = b'{"previous":true}'
    path.write_bytes(old)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    released = threading.Event()
    def release():
        assert path.read_bytes() == old
        assert kernel.CloseHandle(handle)
        released.set()
    timer = threading.Timer(0.12, release)
    timer.start()
    try:
        writer(path, {"updated": True})
        assert released.is_set()
        assert json.loads(path.read_text(encoding="utf-8")) == {"updated": True}
        assert not path.with_name(path.name + ".tmp").exists()
    finally:
        timer.join()
        if not released.is_set():
            kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows retry error codes")
@pytest.mark.parametrize("writer", [battle_reports.atomic_json, exporter.atomic_json])
@pytest.mark.parametrize("code", [5, 32, 33])
def test_permanent_windows_failure_is_bounded_and_preserves_both_files(tmp_path, monkeypatch, writer, code):
    path = tmp_path / "collector_status.json"
    old = b'{"previous":true}'
    path.write_bytes(old)
    clock = [0.0]
    sleeps = []
    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    error = OSError("persistent access/sharing failure")
    error.winerror = code
    replace = Mock(side_effect=error)
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(atomic_files.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(atomic_files.time, "sleep", sleep)
    with pytest.raises(OSError, match="persistent access/sharing failure"):
        writer(path, {"updated": True})
    assert len(sleeps) > 1
    assert clock[0] == pytest.approx(2.0)
    assert replace.call_count == len(sleeps) + 1
    assert path.read_bytes() == old
    temporary = path.with_name(path.name + ".tmp")
    assert json.loads(temporary.read_text(encoding="utf-8")) == {"updated": True}


@pytest.mark.parametrize("writer", [battle_reports.atomic_json, exporter.atomic_json])
def test_nonsharing_error_is_not_retried_and_keeps_the_previous_file(tmp_path, monkeypatch, writer):
    path = tmp_path / "collector_state.json"
    old = b'{"previous":true}'
    path.write_bytes(old)
    error = OSError("disk input/output failure")
    error.winerror = 1117
    replace = Mock(side_effect=error)
    sleep = Mock()
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(atomic_files.time, "sleep", sleep)
    with pytest.raises(OSError, match="disk input/output failure"):
        writer(path, {"updated": True})
    replace.assert_called_once()
    sleep.assert_not_called()
    assert path.read_bytes() == old
    assert path.with_name(path.name + ".tmp").exists()
