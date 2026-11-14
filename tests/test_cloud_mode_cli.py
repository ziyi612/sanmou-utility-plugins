"""Fail-closed cloud CLI, RAM-backlog lifecycle and settings activation."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import threading
import zipfile

import pytest

import battle_reports as cli
from capture.game_assets import cloud_collect
from capture.game_assets import battle_collector as collector_module
from capture.game_assets import export_battle_report_runtime as export_module
from parser import cloud_store, quark_cloud, report_archive


@pytest.fixture
def isolated_program(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    monkeypatch.setattr(cli, "ROOT", project)
    monkeypatch.setattr(quark_cloud, "DEFAULT_CONFIG", project / "config/quark.local.json")
    monkeypatch.delenv("SANMOU_QUARK_CONFIG", raising=False)
    return project


def settings(project, *, launcher=None, quark=None):
    for name, value in (("launcher.json", launcher), ("quark.local.json", quark)):
        if value is not None:
            (project / "config" / name).write_text(json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize("launcher,quark", [({"storage_backend": "quark-memory"}, {}),
    ({}, {"storage_backend": "quark-memory"}), (None, {"storage_backend": "quark-memory"}),
    ({"storage_backend": "quark-memory"}, None)])
def test_either_persistent_marker_prevents_local_fallback(isolated_program, launcher, quark):
    settings(isolated_program, launcher=launcher, quark=quark)
    assert cli.memory_mode_enabled() is True


def test_legacy_launcher_is_still_explicitly_supported(isolated_program):
    settings(isolated_program, launcher={"collector_parallelism": 1})
    assert cli.memory_mode_enabled() is False


def test_missing_both_settings_fails_closed(isolated_program):
    with pytest.raises(ValueError, match="setup_quark.py --memory-only"):
        cli.memory_mode_enabled()


@pytest.mark.parametrize("bad_name", ["launcher.json", "quark.local.json"])
def test_corrupt_marker_cannot_fall_back_even_when_other_marker_is_valid(isolated_program, bad_name):
    settings(isolated_program, launcher={"storage_backend": "quark-memory"}, quark={"storage_backend": "quark-memory"})
    (isolated_program / "config" / bad_name).write_text('{"incomplete":', encoding="utf-8")
    with pytest.raises(ValueError):
        cli.memory_mode_enabled()


def test_explicit_config_cannot_override_default_strict_marker(isolated_program, tmp_path):
    settings(isolated_program, launcher={}, quark={"storage_backend": "quark-memory"})
    alternate = tmp_path / "alternate.json"
    alternate.write_text('{}', encoding="utf-8")
    assert cli.memory_mode_enabled(alternate) is True


@pytest.mark.parametrize("value", [[], None, "text", 1])
@pytest.mark.parametrize("name", ["launcher.json", "quark.local.json"])
def test_valid_json_nonobject_cannot_disable_strict_mode(isolated_program, value, name):
    settings(isolated_program, launcher={"storage_backend": "quark-memory"}, quark={"storage_backend": "quark-memory"})
    (isolated_program / "config" / name).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="配置格式无效"):
        cli.memory_mode_enabled()


def forbid_disk(*_args, **_kwargs):
    pytest.fail("Strict command attempted a local archive, SQLite or legacy collection path")


def forbid_local_archive(monkeypatch):
    monkeypatch.setattr(report_archive, "ReportArchive", forbid_disk)
    monkeypatch.setattr(cli.sqlite3, "connect", forbid_disk)
    monkeypatch.setattr(cli.CollectionStorageGuard, "check", forbid_disk)


def test_strict_collect_branches_before_archive_guard_or_sqlite(isolated_program, monkeypatch):
    settings(isolated_program, launcher={"storage_backend": "quark-memory"})
    forbid_local_archive(monkeypatch)
    calls = []
    monkeypatch.setattr(cloud_collect, "run_memory_collect", lambda args: calls.append(args) or 73)
    db = isolated_program / "never.db"
    output = isolated_program / "no-reports"
    assert cli.main(["collect", "--db", str(db), "--out-dir", str(output), "--once"]) == 73
    assert len(calls) == 1 and calls[0].once
    assert not db.exists() and not output.exists()


@pytest.mark.parametrize("arguments", [["query", "SELECT 1"], ["import"], ["enrich"],
    ["export", "1", "ignored.json"], ["explain", "1001"]])
def test_legacy_data_commands_fail_before_any_cloud_or_local_io(isolated_program, monkeypatch, capsys, arguments):
    settings(isolated_program, launcher={"storage_backend": "quark-memory"})
    forbid_local_archive(monkeypatch)
    monkeypatch.setattr(cloud_store, "CloudStore", forbid_disk)
    assert cli.main([*arguments, "--db", str(isolated_program / "never.db")]) == 2
    assert "停用" in json.loads(capsys.readouterr().out)["error"]
    assert not (isolated_program / "never.db").exists()


@pytest.mark.parametrize("arguments", [["status"], ["verify"], ["cloud", "status"]])
def test_cloud_status_and_verify_use_only_cloud_store(isolated_program, monkeypatch, capsys, arguments):
    settings(isolated_program, launcher={"storage_backend": "quark-memory"})
    forbid_local_archive(monkeypatch)
    calls = []
    class Store:
        def __init__(self, *_a, **_k):
            self.catalog = {"files": {"first": {}, "second": {}}}
        def refresh(self): calls.append("refresh")
        def status(self): return {"backend": "quark-memory", "files": 2}
        def iter_file(self, key):
            calls.append(key)
            yield b"in-memory fixture"
    monkeypatch.setattr(cloud_store, "CloudStore", Store)
    assert cli.main([*arguments, "--db", str(isolated_program / "never.db")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["backend"] == "quark-memory"
    assert calls == (["refresh", "first", "second"] if arguments == ["verify"] else ["refresh"])
    assert not (isolated_program / "never.db").exists()


def test_verify_rejects_output_file_before_cloud_download(isolated_program, monkeypatch, capsys):
    settings(isolated_program, launcher={"storage_backend": "quark-memory"})
    forbid_local_archive(monkeypatch)
    monkeypatch.setattr(cloud_store, "CloudStore", forbid_disk)
    output = isolated_program / "no-report.json"
    assert cli.main(["verify", "--out", str(output)]) == 2
    assert "写入本地" in json.loads(capsys.readouterr().out)["error"]
    assert not output.exists()


@pytest.mark.parametrize("delete_local,explicit_sources", [(False, False), (True, False), (True, True)])
def test_migration_cli_defaults_to_retaining_local_files_and_delegates_safe_scope(isolated_program, tmp_path, monkeypatch, capsys, delete_local, explicit_sources):
    from parser import cloud_migration
    settings(isolated_program, launcher={"storage_backend": "quark-memory"})
    forbid_local_archive(monkeypatch)
    store = object()
    construction, migrations = [], []
    monkeypatch.setattr(cloud_store, "CloudStore", lambda config: construction.append(config) or store)
    def migrated(supplied_store, root, **kwargs):
        migrations.append((supplied_store, root, kwargs))
        kwargs["progress"]({"phase": "verification_complete", "verified_files": 2})
        return {"phase": "complete" if kwargs["delete_after_verify"] else "verified_local_retained", "verified_files": 2}
    monkeypatch.setattr(cloud_migration, "migrate_local", migrated)
    sources = [isolated_program / "data/incoming", isolated_program / "data/archive/backup.db"]
    arguments = ["cloud", "migrate", "--db", str(tmp_path / "never.db")]
    if delete_local:
        arguments.append("--delete-local")
    if explicit_sources:
        for source in sources:
            arguments.extend(["--source", str(source)])
    assert cli.main(arguments) == 0
    assert construction == [None] and len(migrations) == 1
    supplied_store, root, options = migrations[0]
    assert supplied_store is store and root == isolated_program
    assert options["delete_after_verify"] is delete_local
    assert options["roots"] == (sources if explicit_sources else None)
    stream, messages = capsys.readouterr().out.strip(), []
    while stream:
        message, end = json.JSONDecoder().raw_decode(stream)
        messages.append(message)
        stream = stream[end:].lstrip()
    assert messages[-1]["verified_files"] == 2
    assert not (tmp_path / "never.db").exists()


def test_identity_reader_imports_package_module_without_legacy_sys_path():
    class Runner:
        def execute_on_update(self, code, timeout=10):
            assert "ClientProxy" in code
            return {"ok": True, "result": json.dumps({"payload": {"avatarId": 123, "serverId": 4, "scenarioId": 0}})}
    assert cli.read_identity(Runner()) == {"avatarId": "123", "serverId": "4", "scenarioId": "0"}


class MemoryStore:
    def __init__(self):
        self.journal_mode = False
        self.catalog = {"files": {}}
        self.files = {}
        self.events = []
        self.failed_writes = 0
        self.failed_checkpoints = 0
        self.interrupt_write = False
        self.leased = False
    @contextmanager
    def writer_lease(self):
        assert not self.leased
        self.leased = True
        try:
            yield
        finally:
            self.leased = False
    def refresh(self): self.events.append("refresh")
    def load_state(self, _namespace): return None
    def _ensure_loaded(self): pass
    def put_file(self, key, raw, namespace=None):
        self.events.append("write")
        if self.interrupt_write:
            self.interrupt_write = False
            raise KeyboardInterrupt()
        if self.failed_writes:
            self.failed_writes -= 1
            raise RuntimeError("synthetic cloud transfer failure")
        self.files[key] = raw
        self.catalog["files"][key] = {"byte_count": len(raw)}
        return {"verified": True}
    def checkpoint(self):
        self.events.append("checkpoint")
        if self.failed_checkpoints:
            self.failed_checkpoints -= 1
            raise RuntimeError("synthetic checkpoint failure")
        return {"verified": True}
    def status(self): return {"files": len(self.files), "backend": "quark-memory"}


def runtime_fixture(tmp_path, monkeypatch, *, once=True, ids=None):
    args = cli.build_parser().parse_args(["collect", "--out-dir", str(tmp_path / "must-stay-absent"),
        *(["--once"] if once else []), *(["--battle-ids", *ids] if ids else [])])
    events, emitted = [], []
    class Runner:
        def __init__(self, pid): events.append(("runner", pid))
        def __enter__(self): return self
        def __exit__(self, *_args): events.append(("closed", None))
    monkeypatch.setattr(cli, "read_identity", lambda _runner: {"avatarId": "123", "serverId": "4", "scenarioId": "5"})
    monkeypatch.setattr(cloud_collect.sys, "stdin", io.StringIO(""))
    return args, Runner, events, emitted


def test_once_artifact_failure_retries_exact_ram_bytes_without_new_game_cycle(tmp_path, monkeypatch):
    args, runner, events, emitted = runtime_fixture(tmp_path, monkeypatch)
    store = MemoryStore();store.failed_writes = 2
    class Collector:
        def __init__(self, _runner, logical_dir, **kwargs):
            self.store, self.path = kwargs["artifact_store"], logical_dir / "response.json"
        def run_cycle(self):
            events.append(("rpc", None))
            self.store.write_bytes(self.path, b"already-received-original")
    monkeypatch.setattr(collector_module, "BattleCollector", Collector)
    waits = []
    def waited(value):
        assert store.leased, "Cloud writer lease was released while RAM bytes were pending"
        waits.append(value)
    assert cloud_collect.run_memory_collect(args, store=store, runner_factory=runner, emit=emitted.append,
        retry_wait=waited) == 0
    assert sum(event[0] == "runner" for event in events) == sum(event[0] == "rpc" for event in events) == 1
    assert list(store.files.values()) == [b"already-received-original"]
    assert waits == [10]
    assert store.leased is False
    assert emitted[-1]["phase"] == "stopped"
    assert not Path(args.out_dir).exists()


def test_stdin_stop_during_upload_waits_for_verification_before_exit(tmp_path, monkeypatch):
    args, runner, events, emitted = runtime_fixture(tmp_path, monkeypatch, once=False)
    store = MemoryStore();store.failed_writes = 1
    stop = threading.Event()
    class Stdin:
        def __iter__(self):
            while not any(event[0] == "rpc" for event in events):
                stop.wait(0.01)
            yield '{"command":"stop"}\n'
    monkeypatch.setattr(cloud_collect.sys, "stdin", Stdin())
    class Collector:
        def __init__(self, _runner, logical_dir, **kwargs): self.store, self.path = kwargs["artifact_store"], logical_dir / "response.json"
        def run_cycle(self):
            events.append(("rpc", None))
            assert stop.wait(2), "stdin stop command was not received"
            self.store.write_bytes(self.path, b"pending-on-stop")
    monkeypatch.setattr(collector_module, "BattleCollector", Collector)
    assert cloud_collect.run_memory_collect(args, store=store, runner_factory=runner, emit=emitted.append,
        stop_event=stop, retry_wait=lambda _: None) == 0
    assert list(store.files.values()) == [b"pending-on-stop"]
    assert len([event for event in events if event[0] == "runner"]) == 1
    assert emitted[-1]["phase"] == "stopped"


def test_ctrl_c_during_upload_drains_pending_before_stop(tmp_path, monkeypatch):
    args, runner, events, emitted = runtime_fixture(tmp_path, monkeypatch)
    store = MemoryStore();store.interrupt_write = True
    class Collector:
        def __init__(self, _runner, logical_dir, **kwargs): self.store, self.path = kwargs["artifact_store"], logical_dir / "response.json"
        def run_cycle(self):
            events.append(("rpc", None))
            self.store.write_bytes(self.path, b"received-before-control-c")
    monkeypatch.setattr(collector_module, "BattleCollector", Collector)
    assert cloud_collect.run_memory_collect(args, store=store, runner_factory=runner, emit=emitted.append,
        retry_wait=lambda _: None) == 0
    assert list(store.files.values()) == [b"received-before-control-c"]
    assert emitted[-1]["phase"] == "stopped"


def test_checkpoint_failure_only_retries_cloud_without_reopening_game(tmp_path, monkeypatch):
    args, runner, events, emitted = runtime_fixture(tmp_path, monkeypatch)
    store = MemoryStore();store.failed_checkpoints = 2
    class Collector:
        def __init__(self, *_a, **_k): pass
        def run_cycle(self): events.append(("rpc", None))
    monkeypatch.setattr(collector_module, "BattleCollector", Collector)
    waits = []
    assert cloud_collect.run_memory_collect(args, store=store, runner_factory=runner, emit=emitted.append,
        retry_wait=waits.append) == 0
    assert len([event for event in events if event[0] == "runner"]) == 1
    assert len([event for event in events if event[0] == "rpc"]) == 1
    assert store.events.count("checkpoint") == 3
    assert waits == [10]


def test_explicit_battle_once_keeps_pending_until_the_receipt_is_verified(tmp_path, monkeypatch):
    args, runner, events, emitted = runtime_fixture(tmp_path, monkeypatch, ids=["1001"])
    store = MemoryStore()
    writes = []
    original_put = store.put_file
    def initially_unconfirmed(key, raw, namespace=None):
        writes.append(raw)
        if len(writes) == 1:
            return {"verified": False}
        return original_put(key, raw, namespace)
    store.put_file = initially_unconfirmed
    class Collector:
        def __init__(self, *_args, **_kwargs): pass
        def run_cycle(self): pytest.fail("Explicit IDs must not enumerate game scopes")
    monkeypatch.setattr(collector_module, "BattleCollector", Collector)
    def capture(_runner, uid, directory, **kwargs):
        events.append(("explicit-rpc", uid))
        kwargs["artifact_store"].write_bytes(directory / "battle_1001_decrypted.json", b"explicit received original")
        return {"complete": True}
    monkeypatch.setattr(export_module, "capture_report", capture)
    assert cloud_collect.run_memory_collect(args, store=store, runner_factory=runner, emit=emitted.append,
        retry_wait=lambda _: None) == 0
    assert writes == [b"explicit received original", b"explicit received original"]
    assert list(store.files.values()) == [b"explicit received original"]
    assert len([event for event in events if event[0] == "explicit-rpc"]) == 1
    assert emitted[-1]["phase"] == "stopped"


def test_ctrl_c_inside_retry_wait_still_preserves_pending_bytes(tmp_path, monkeypatch):
    args, runner, events, emitted = runtime_fixture(tmp_path, monkeypatch)
    store = MemoryStore();store.failed_writes = 2
    stop = threading.Event()
    class Collector:
        def __init__(self, _runner, logical_dir, **kwargs): self.store, self.path = kwargs["artifact_store"], logical_dir / "response.json"
        def run_cycle(self):
            events.append(("rpc", None))
            self.store.write_bytes(self.path, b"pending-through-interrupt")
    monkeypatch.setattr(collector_module, "BattleCollector", Collector)
    def interrupted_wait(_seconds): raise KeyboardInterrupt()
    assert cloud_collect.run_memory_collect(args, store=store, runner_factory=runner, emit=emitted.append,
        stop_event=stop, retry_wait=interrupted_wait) == 0
    assert stop.is_set()
    assert list(store.files.values()) == [b"pending-through-interrupt"]
    assert len([event for event in events if event[0] == "runner"]) == 1


def test_closed_gui_pipe_cannot_discard_an_upload_backlog(tmp_path, monkeypatch):
    args, runner, events, _emitted = runtime_fixture(tmp_path, monkeypatch)
    store = MemoryStore();store.failed_writes = 2
    stop = threading.Event()
    class Collector:
        def __init__(self, _runner, logical_dir, **kwargs): self.store, self.path = kwargs["artifact_store"], logical_dir / "response.json"
        def run_cycle(self):
            events.append(("rpc", None))
            self.store.write_bytes(self.path, b"pending-after-window-close")
    monkeypatch.setattr(collector_module, "BattleCollector", Collector)
    def gui_pipe(value):
        if value.get("phase") == "paused_upload":
            raise BrokenPipeError("synthetic desktop pipe closed")
    assert cloud_collect.run_memory_collect(args, store=store, runner_factory=runner, emit=gui_pipe,
        stop_event=stop, retry_wait=lambda _: None) == 0
    assert stop.is_set()
    assert list(store.files.values()) == [b"pending-after-window-close"]
    assert len([event for event in events if event[0] == "runner"]) == 1
    assert store.leased is False


def test_runtime_progress_uses_gui_stage_fields_without_changing_collection_phase(tmp_path, monkeypatch):
    args, runner, events, emitted = runtime_fixture(tmp_path, monkeypatch)
    store = MemoryStore()
    class Collector:
        def __init__(self, _runner, _directory, **kwargs): self.progress = kwargs["on_progress"]
        def run_cycle(self): self.progress({"phase": "capture_started", "battleID": "1001", "scope": "union"})
    monkeypatch.setattr(collector_module, "BattleCollector", Collector)
    assert cloud_collect.run_memory_collect(args, store=store, runner_factory=runner, emit=emitted.append) == 0
    progress = next(item for item in emitted if item["type"] == "progress")
    assert progress["phase"] == "collecting" and progress["current_stage"] == "capture_started"
    assert progress["current_battle_id"] == "1001" and progress["current_scope"] == "union"


def test_cloud_refresh_failure_does_not_connect_to_game(tmp_path, monkeypatch):
    args, _runner, _events, emitted = runtime_fixture(tmp_path, monkeypatch)
    store = MemoryStore()
    def offline(): raise RuntimeError("synthetic cloud unavailable")
    store.refresh = offline
    with pytest.raises(RuntimeError, match="cloud unavailable"):
        cloud_collect.run_memory_collect(args, store=store, runner_factory=forbid_disk, emit=emitted.append)


def setup_fixture(tmp_path, monkeypatch):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/setup_quark.py"))
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as package:
        package.writestr("scripts/quark-drive.cjs", "// synthetic official-runtime fixture\n")
    archive = tmp_path / "runtime.zip";archive.write_bytes(payload.getvalue())
    install = module["install"]
    monkeypatch.setitem(install.__globals__, "SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    node = tmp_path / "node/node.exe";node.parent.mkdir();node.write_bytes(b"synthetic executable, never run")
    monkeypatch.setattr(module["shutil"], "which", lambda _name: str(node))
    project = tmp_path / "project";project.mkdir()
    return module, install, archive, project, node


def test_memory_setup_preserves_existing_configuration_and_activates_only_after_dependencies(tmp_path, monkeypatch):
    module, install, archive, project, _node = setup_fixture(tmp_path, monkeypatch)
    (project / "config").mkdir()
    old = {"enabled": True, "folder_id": "existing-parent", "cli_command": ["preserved-node", "preserved-sdk"],
           "cli_env": {"CUSTOM_OPTION": "kept"}, "custom": {"preserved": True}}
    (project / "config/quark.local.json").write_text(json.dumps(old), encoding="utf-8")
    (project / "config/launcher.json").write_text('{"python_executable":"preserved-python","collector_parallelism":2}', encoding="utf-8")
    def installed(root):
        assert "storage_backend" not in json.loads((root / "config/quark.local.json").read_text())
        assert "storage_backend" not in json.loads((root / "config/launcher.json").read_text())
    monkeypatch.setitem(install.__globals__, "install_memory_dependencies", installed)
    install(project / ".runtime/sdk", root=project, archive=archive, memory_only=True)
    config = json.loads((project / "config/quark.local.json").read_text())
    launcher = json.loads((project / "config/launcher.json").read_text())
    assert config["storage_backend"] == launcher["storage_backend"] == "quark-memory"
    assert config["folder_id"] == old["folder_id"] and config["cli_command"] == old["cli_command"]
    assert config["custom"] == old["custom"] and config["cli_env"]["CUSTOM_OPTION"] == "kept"
    assert launcher["python_executable"] == "preserved-python" and launcher["collector_parallelism"] == 1


def test_memory_dependency_failure_does_not_publish_fresh_config_or_markers(tmp_path, monkeypatch):
    _module, install, archive, project, _node = setup_fixture(tmp_path, monkeypatch)
    def unavailable(_root): raise subprocess.CalledProcessError(1, ["synthetic npm"])
    monkeypatch.setitem(install.__globals__, "install_memory_dependencies", unavailable)
    with pytest.raises(subprocess.CalledProcessError):
        install(project / ".runtime/sdk", root=project, archive=archive, memory_only=True)
    assert not (project / "config/quark.local.json").exists()
    assert not (project / "config/launcher.json").exists()


def test_failed_second_marker_write_restores_both_original_settings(tmp_path, monkeypatch):
    _module, install, archive, project, _node = setup_fixture(tmp_path, monkeypatch)
    (project / "config").mkdir()
    quark = project / "config/quark.local.json";launcher = project / "config/launcher.json"
    old_quark, old_launcher = b'{"cli_env":{},"custom":true}\n', b'{"collector_parallelism":2}\n'
    quark.write_bytes(old_quark);launcher.write_bytes(old_launcher)
    monkeypatch.setitem(install.__globals__, "install_memory_dependencies", lambda _root: None)
    original = install.__globals__["_atomic"]
    calls = []
    def failed_once(path, raw):
        calls.append(path)
        if len(calls) == 2: raise PermissionError("synthetic settings publication failure")
        return original(path, raw)
    monkeypatch.setitem(install.__globals__, "_atomic", failed_once)
    with pytest.raises(PermissionError):
        install(project / ".runtime/sdk", root=project, archive=archive, memory_only=True)
    assert quark.read_bytes() == old_quark and launcher.read_bytes() == old_launcher


def test_npm_install_is_pinned_and_uses_only_project_scratch_without_home_changes(tmp_path, monkeypatch):
    module, _install, _archive, project, node = setup_fixture(tmp_path, monkeypatch)
    npm = node.parent / "node_modules/npm/bin/npm-cli.js";npm.parent.mkdir(parents=True);npm.write_text('// fixture')
    calls = []
    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        for package, version in (("memfs", "4.80.1"), ("fs-monkey", "1.1.0")):
            manifest = project / ".runtime/quark-memory/node_modules" / package / "package.json"
            manifest.parent.mkdir(parents=True, exist_ok=True);manifest.write_text(json.dumps({"version": version}))
    monkeypatch.setattr(module["subprocess"], "run", fake_run)
    module["install_memory_dependencies"](project)
    command, kwargs = calls[0]
    assert command[:2] == [str(node), str(npm)]
    assert all(value in command for value in ["--ignore-scripts", "--no-audit", "--no-fund", "memfs@4.80.1", "fs-monkey@1.1.0"])
    for name in ["TEMP", "TMP", "TMPDIR", "npm_config_cache", "npm_config_prefix"]:
        assert Path(kwargs["env"][name]).resolve().is_relative_to(project)
    for name in ["HOME", "USERPROFILE", "CODEX_HOME"]:
        assert kwargs["env"].get(name) == os.environ.get(name)
    assert kwargs["check"] is True
