"""Exercise real Windows launcher processes, using project-local substitutes for desktop."""
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows launcher integration")


@pytest.fixture
def launch_area():
    with tempfile.TemporaryDirectory(dir=".runtime/test-temp", prefix="sanmou launcher review ") as folder:
        yield Path(folder)


def execute(arguments, cwd=None):
    environment = dict(os.environ, TEMP=".runtime/test-temp", TMP=".runtime/test-temp",
                       PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", BATTLE_ARCHIVE_NO_PAUSE="1")
    return subprocess.run(arguments, cwd=cwd or ROOT, env=environment, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=45)


def install(project, desktop):
    return execute(["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(ROOT / "tools/install_desktop_launcher.ps1"),
                    "-ProjectRoot", str(project), "-DesktopDirectory", str(desktop)])


def run_cmd(path, *args):
    command = '""' + str(path) + '"' + ''.join(' "' + a + '"' for a in args) + '"'
    # cmd.exe uses its own /c quoting rules, not list2cmdline's CRT escaping.
    return execute("cmd.exe /d /s /c " + command)


def test_forwarder_uses_updated_project_without_desktop_reinstall(launch_area):
    project, desktop = launch_area / "project with spaces", launch_area / "desktop"
    project.mkdir(); desktop.mkdir()
    entry = project / "start_battle_archive.cmd"
    entry.write_text('@echo off\necho version-one\nexit /b 0\n', encoding="ascii")
    installed = install(project, desktop)
    assert installed.returncode == 0, installed.stderr
    forwarder = desktop / "战报采集.cmd"
    assert forwarder.is_file()
    original = forwarder.read_bytes()
    first = run_cmd(forwarder)
    assert first.returncode == 0 and "version-one" in first.stdout, (first.stdout, first.stderr)
    entry.write_text('@echo off\necho version-two\nexit /b 7\n', encoding="ascii")
    second = run_cmd(forwarder)
    assert second.returncode == 7 and "version-two" in second.stdout, (second.stdout, second.stderr)
    assert forwarder.read_bytes() == original


def test_install_does_not_overwrite_an_unrelated_desktop_file(launch_area):
    desktop = launch_area / "desktop"
    desktop.mkdir()
    path = desktop / "战报采集.cmd"
    path.write_bytes(b"user-owned-launcher")
    result = install(ROOT, desktop)
    assert result.returncode != 0
    assert path.read_bytes() == b"user-owned-launcher"


def test_real_project_launcher_preflight_from_another_working_directory(launch_area):
    result = execute(["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-File", str(ROOT / "start_battle_archive.ps1"), "-Check"], cwd=launch_area)
    assert result.returncode == 0, result.stderr
    assert '"ready": true' in result.stdout
    assert "check_only_no_game_connection" in result.stdout
    assert '"launch_mode": "desktop"' in result.stdout
    cmd_result = run_cmd(ROOT / "start_battle_archive.cmd", "-Check")
    assert cmd_result.returncode == 0, (cmd_result.stdout, cmd_result.stderr)
    assert "check_only_no_game_connection" in cmd_result.stdout


def launcher_project(area):
    """A real launcher around harmless scripts, never the production collector."""
    project = area / "desktop project with spaces"
    project.mkdir()
    for name in ("start_battle_archive.ps1", "start_battle_archive.cmd"):
        shutil.copyfile(ROOT / name, project / name)
    (project / "config").mkdir()
    (project / "config/launcher.json").write_text(
        json.dumps({"python_executable": sys.executable}), encoding="utf-8")
    stub = ("import json,os,pathlib,sys\n"
            "root=pathlib.Path(__file__).parent\n"
            "(root / (pathlib.Path(__file__).stem+'.invocation.json')).write_text(\n"
            " json.dumps({'args':sys.argv[1:],'cwd':os.getcwd(),'temp':os.environ['TEMP']}), encoding='utf-8')\n")
    for name in ("battle_reports_gui.py", "battle_reports.py"):
        (project / name).write_text(stub, encoding="utf-8")
    return project


def project_ps(project, *args):
    return execute(["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(project / "start_battle_archive.ps1"), *args], cwd=project.parent)


def test_default_launcher_opens_gui_with_spaced_path_without_collecting(launch_area):
    project = launcher_project(launch_area)
    result = project_ps(project, "-Interval", "17", "-GamePid", "4321")
    assert result.returncode == 0, (result.stdout, result.stderr)
    captured = json.loads((project / "battle_reports_gui.invocation.json").read_text("utf-8"))
    assert captured["args"] == ["--interval", "17", "--pid", "4321"]
    assert Path(captured["cwd"]) == project
    assert Path(captured["temp"]) == project / ".runtime" / "temp"
    assert not (project / "battle_reports.invocation.json").exists()
    assert len(list((project / "logs").glob("desktop_*.stderr.log"))) == 1


def test_default_cmd_success_returns_without_waiting_for_a_key(launch_area):
    project = launcher_project(launch_area)
    result = run_cmd(project / "start_battle_archive.cmd")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (project / "battle_reports_gui.invocation.json").is_file()
    assert not (project / "battle_reports.invocation.json").exists()


@pytest.mark.parametrize("args,tail", [
    (("-Console",), []),
    (("-Once",), ["--once"]),
    (("-Once", "-PvpOnly"), ["--once", "--pvp-only"]),
    (("-Namespace", "server:1/scenario:2/avatar:3", "-Parallel", "2"), []),
])
def test_explicit_console_modes_preserve_collection_arguments(launch_area, args, tail):
    project = launcher_project(launch_area)
    result = project_ps(project, *args)
    assert result.returncode == 0, (result.stdout, result.stderr)
    captured = json.loads((project / "battle_reports.invocation.json").read_text("utf-8"))
    expected_namespace = "server:1/scenario:2/avatar:3" if "-Namespace" in args else "auto"
    expected_parallel = "2" if "-Parallel" in args else "1"
    assert captured["args"] == ["collect", "--console", "--namespace", expected_namespace,
                                "--interval", "60", "--parallel", expected_parallel, *tail]
    assert not (project / "battle_reports_gui.invocation.json").exists()


def test_preflight_checks_gui_without_running_any_entry(launch_area):
    project = launcher_project(launch_area)
    result = project_ps(project, "-Check")
    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["launch_mode"] == "desktop"
    assert Path(payload["entry"]) == project / "battle_reports_gui.py"
    assert not list(project.glob("*.invocation.json"))
    assert not (project / "logs").exists()


def test_gui_immediate_failure_is_nonzero_and_retains_stderr(launch_area):
    project = launcher_project(launch_area)
    (project / "battle_reports_gui.py").write_text(
        "import sys\nprint('fixture startup failure',file=sys.stderr)\nraise SystemExit(7)\n", encoding="utf-8")
    result = project_ps(project)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "Desktopstartupfailed" in "".join(result.stderr.split())
    errors = list((project / "logs").glob("desktop_*.stderr.log"))
    assert len(errors) == 1 and "fixture startup failure" in errors[0].read_text("utf-8")


def test_gui_logs_remain_writable_after_launcher_exits(launch_area):
    project = launcher_project(launch_area)
    (project / "battle_reports_gui.py").write_text(
        "import pathlib,sys,time\n"
        "time.sleep(2.2)\n"
        "print('late desktop stdout',flush=True)\n"
        "print('late desktop stderr',file=sys.stderr,flush=True)\n"
        "(pathlib.Path(__file__).parent/'late.finished').write_text('done')\n", encoding="utf-8")
    result = project_ps(project)
    assert result.returncode == 0, (result.stdout, result.stderr)
    deadline = time.monotonic() + 6
    while not (project / "late.finished").exists() and time.monotonic() < deadline:
        time.sleep(.05)
    assert (project / "late.finished").is_file()
    output = next((project / "logs").glob("desktop_*.stdout.log"))
    error = next((project / "logs").glob("desktop_*.stderr.log"))
    assert "late desktop stdout" in output.read_text("utf-8")
    assert "late desktop stderr" in error.read_text("utf-8")


def test_missing_project_is_a_visible_nonzero_error(launch_area):
    desktop = launch_area / "desktop"
    desktop.mkdir()
    missing = launch_area / "missing"
    missing.mkdir()
    result = install(missing, desktop)
    assert result.returncode != 0
    assert not list(desktop.iterdir())
