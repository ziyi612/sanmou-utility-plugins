"""Actual hidden Tk panel checks with local status and bounded fake actions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tkinter as tk
from unittest.mock import patch

import pytest


def scenario_local_status(folder):
    from battle_ui.cloud_panel import CloudPanel
    from parser import quark_cloud as cloud

    root = tk.Tk()
    root.withdraw()
    submitted = []

    def submit(key, operation, callback, **options):
        submitted.append((key, options))
        callback(operation())

    def forbid_provider(*_args, **_kwargs):
        raise AssertionError("Viewing cloud status must not start login, upload or an external command")

    try:
        with patch.object(cloud, "QuarkClient", side_effect=forbid_provider):
            panel = CloudPanel(root, root_path=folder, db_path=folder / "missing.db", submit=submit)
            panel.pack()
            assert submitted == []
            titles = {child.cget("text") for child in panel.winfo_children()[0].winfo_children()}
            assert {"查看状态", "登录夸克", "启用战报归档", "同步接下来10份原文"} <= titles
            panel.refresh()
            root.update_idletasks()
            assert submitted == [("cloud-status", {"replace": False})]
            result = json.loads(panel.output.get("1.0", "end"))
            assert result["configured"] is False and result["enabled"] is False
            assert "尚未配置" in panel.note.get()
            assert panel.auth_link.get() == ""
            assert not (folder / "missing.db").exists()
    finally:
        root.destroy()


def scenario_sync_failure(folder):
    from battle_ui.cloud_panel import CloudPanel
    from parser import quark_cloud as cloud

    root = tk.Tk()
    root.withdraw()
    submitted, calls = [], []

    def submit(key, operation, callback, **options):
        submitted.append((key, options))
        callback(operation())

    def failed_sync(db_path, **options):
        calls.append((db_path, options))
        return {"verified": 0, "failed": 1, "released_bytes": 0,
                "errors": [{"file_id": 5, "error": "fixture cloud copy failed validation"}]}

    try:
        with patch.object(cloud, "sync_archive", side_effect=failed_sync), \
             patch.object(cloud, "QuarkClient", side_effect=AssertionError("Unexpected provider/network operation")):
            panel = CloudPanel(root, root_path=folder, db_path=folder / "fixture.db", submit=submit)
            panel.pack()
            panel.action("sync")
            root.update_idletasks()
            assert submitted == [("cloud-action", {"replace": False})]
            assert len(calls) == 1 and calls[0][1]["limit"] == 10
            assert calls[0][1]["config_path"] == folder / "config/quark.local.json"
            result = json.loads(panel.output.get("1.0", "end"))
            assert result["failed"] == 1 and result["verified"] == result["released_bytes"] == 0
            assert "同步未完成" in panel.note.get() and "本地未验证原件继续保留" in panel.note.get()
            assert "操作已结束" not in panel.note.get()
    finally:
        root.destroy()


def scenario_login_handoff_signature(folder):
    import time
    from battle_ui.cloud_panel import CloudPanel
    from parser import quark_cloud as cloud

    root = tk.Tk()
    root.withdraw()
    handoff = folder / "authorization-url.json"
    url = "https://pan.quark.cn/open/v1/oauth/agent?fixture=panel-consent"
    handoff.write_text(json.dumps({"url": url}), encoding="utf-8")
    old = time.time() - 60
    os.utime(handoff, (old, old))
    config_path = folder / "config/quark.local.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"cli_command": ["fixture-cli"], "work_dir": str(folder / "runtime"),
        "cli_env": {"SANMOU_QUARK_AUTH_URL_FILE": str(handoff)}}), encoding="utf-8")
    opened = []
    panel = None

    def submit(_key, operation, callback, **_options):
        callback(operation())

    class Provider:
        def run(self, *arguments, **_kwargs):
            assert arguments == ("login",)
            assert opened == [], "Unchanged expired handoff opened before login"
            assert panel.auth_link.get() == ""
            # A freshly rewritten handoff may still have an mtime earlier than
            # the login's wall clock on Windows. Only its baseline signature matters.
            handoff.write_text(json.dumps({"url": url}), encoding="utf-8")
            os.utime(handoff, (old + 1, old + 1))
            panel._watch_auth_url()
            assert opened == [url]
            assert panel.auth_link.get() == url
            panel._watch_auth_url()
            assert opened == [url], "One consent URL must open only once"
            return {"type": "result", "code": 0}

    try:
        with patch.object(cloud, "QuarkClient", side_effect=lambda _config: Provider()), \
             patch.object(cloud, "open_authorization_page", side_effect=lambda target, _config: opened.append(target)):
            panel = CloudPanel(root, root_path=folder, db_path=folder / "absent.db", submit=submit)
            panel.pack()
            panel.action("login")
            assert not panel._watching_auth
            assert opened == [url]
            assert not (folder / "absent.db").exists()
    finally:
        if panel is not None:
            panel.destroy()
        root.destroy()


@pytest.mark.parametrize("scenario", ["local_status", "sync_failure", "login_handoff_signature"])
def test_actual_cloud_panel_scenario(tmp_path, scenario):
    script = r"""
import gc, pathlib, runpy, sys
namespace = runpy.run_path(sys.argv[1])
namespace['scenario_' + sys.argv[2]](pathlib.Path(sys.argv[3]))
gc.collect()
print('CLOUD_PANEL_SCENARIO_OK')
"""
    environment = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path),
                       PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    completed = subprocess.run([sys.executable, "-B", "-c", script, str(Path(__file__).resolve()), scenario, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], env=environment, text=True, encoding="utf-8", errors="replace",
        capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert "CLOUD_PANEL_SCENARIO_OK" in completed.stdout
    assert "Tcl_AsyncDelete" not in completed.stderr
