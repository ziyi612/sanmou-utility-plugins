"""Cloud-only collector: no local archive, checkpoint, status or report log."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import threading


def run_memory_collect(args, *, store=None, runner_factory=None, emit=None, stop_event=None, retry_wait=None):
    from parser.cloud_store import CloudStore
    if store is None:
        store = CloudStore(journal_mode=True)
    # Hold the same folder lease throughout capture and RAM-backlog recovery.
    # It is a named kernel mutex; no local report or lock file is created.
    with store.writer_lease():
        return _run_memory_collect(args, store=store, runner_factory=runner_factory,
            emit=emit, stop_event=stop_event, retry_wait=retry_wait)


def _run_memory_collect(args, *, store, runner_factory=None, emit=None, stop_event=None, retry_wait=None):
    from parser.cloud_store import CloudArtifacts
    from parser.quark_cloud import public_result
    from .battle_collector import BattleCollector
    from .export_battle_report_runtime import capture_report, validate_battle_id
    from .memory_storage import ArtifactStoreFailure, verified_store
    from battle_reports import read_identity

    if runner_factory is None:
        from .run_lua_in_game import GameLuaRunner
        runner_factory = GameLuaRunner
    if emit is None:
        emit = lambda value: print(json.dumps(public_result(value), ensure_ascii=False), flush=True)
    stop_event = stop_event or threading.Event()
    store.journal_mode = True
    retry_wait = retry_wait or (lambda seconds: threading.Event().wait(seconds))
    output_available = True

    def deliver(value):
        nonlocal output_available
        if not output_available:
            return
        try:
            emit(value)
        except (OSError, UnicodeError):
            # Closing the desktop window closes its pipe. Stop new game work,
            # but do not let a progress-write failure discard a RAM backlog.
            output_available = False
            stop_event.set()

    def receive_commands():
        try:
            for line in sys.stdin:
                try:
                    command = json.loads(line)
                except ValueError:
                    continue
                if isinstance(command, dict) and command.get("command") == "stop":
                    stop_event.set()
                    break
        except (OSError, ValueError):
            pass

    threading.Thread(target=receive_commands, name="memory-collector-control", daemon=True).start()
    store.refresh()  # Prove cloud access before requesting anything from the game.
    cycles, last_error = 0, None

    def status(phase, **values):
        deliver({"type": "status", "phase": phase, "running": phase not in ("stopped", "failed"),
              "backend": "quark-memory", "disk_cache_bytes": 0, **values})

    def progress(value):
        # Only control/metadata cross the GUI pipe; originals never become logs.
        metadata = public_result(value)
        payload = {"type": "progress", **metadata, "phase": "collecting",
                   "current_stage": metadata.get("phase")}
        for source, target in (("battleID", "current_battle_id"), ("scope", "current_scope")):
            if source in metadata:
                payload[target] = metadata[source]
        deliver(payload)

    def drain_pending(pending, error):
        """A stop request cannot discard bytes awaiting verified cloud commit."""
        while True:
            status("paused_upload", error=str(public_result(str(error))),
                   stopping=stop_event.is_set(),
                   message="云端未确认，已暂停新请求；待上传原文仅保留在运行内存中。")
            try:
                if pending is not None:
                    receipt = pending.retry_pending()
                    if not isinstance(receipt, dict) or receipt.get("verified") is not True:
                        raise RuntimeError("待上传原文尚未获得完整云端确认。")
                    if pending.pending_artifacts or pending.pending_states:
                        raise RuntimeError("仍有尚未确认的内存原文或续采状态。")
                store.checkpoint()
                return
            except KeyboardInterrupt:
                stop_event.set()
            except (ArtifactStoreFailure, Exception) as retry_error:
                error = retry_error
                if isinstance(retry_error, ArtifactStoreFailure):
                    pending = retry_error.artifact_store
            try:
                retry_wait(10)
            except KeyboardInterrupt:
                stop_event.set()

    active_artifacts = None

    try:
        while not stop_event.is_set():
            cycles += 1
            active_artifacts = None
            try:
                status("connecting", cycle=cycles)
                with runner_factory(args.pid) as runner:
                    identity = read_identity(runner)
                    account = "server:{serverId}/scenario:{scenarioId}/avatar:{avatarId}".format(**identity)
                    namespace = account if args.namespace == "auto" else args.namespace
                    logical_dir = Path(args.out_dir) / hashlib.sha256(namespace.encode()).hexdigest()[:20]
                    artifacts = verified_store(CloudArtifacts(store, namespace, logical_root=logical_dir))
                    active_artifacts = artifacts
                    previous = store.load_state(namespace)
                    if previous and previous.get("runtimeIdentity") not in (None, identity):
                        raise RuntimeError("云端续采目录属于其他角色、服务器或赛季。")
                    collector = BattleCollector(runner, logical_dir, namespace=namespace,
                        scopes=tuple(value.strip() for value in args.scope.split(",") if value.strip()),
                        wait_seconds=args.wait, request_delay=args.request_delay, max_pages=args.max_pages,
                        parallelism=1, pvp_only=args.pvp_only, time_range=args.time_range,
                        artifact_store=artifacts, on_progress=progress)
                    collector._stop_event = stop_event
                    status("collecting", namespace=namespace, cycle=cycles)
                    if args.battle_ids:
                        for uid in dict.fromkeys(args.battle_ids):
                            if stop_event.is_set():
                                break
                            record = capture_report(runner, validate_battle_id(uid), logical_dir,
                                wait_seconds=args.wait, expected_identity=identity, artifact_store=artifacts)
                            progress({"phase": "capture_complete", "complete": record.get("complete", False)})
                    else:
                        collector.run_cycle()
                    try:
                        store.checkpoint()
                    except Exception as error:
                        drain_pending(artifacts, error)
                    last_error = None
                    status("waiting", cycle=cycles, cloud=store.status())
            except ArtifactStoreFailure as error:
                # The original stays in the adapter's RAM. Do not reconnect or
                # dispatch any new game request until every pending byte is safe.
                drain_pending(error.artifact_store, error)
                last_error = None
                if stop_event.is_set():
                    break
            except Exception as error:
                last_error = str(public_result(str(error)))
                if args.once or args.max_cycles is not None and cycles >= args.max_cycles:
                    status("failed", error=last_error)
                    return 2
                status("waiting", error=last_error, retrying=True)
            if args.once or args.max_cycles is not None and cycles >= args.max_cycles:
                break
            stop_event.wait(args.interval)
    except KeyboardInterrupt:
        stop_event.set()
        if active_artifacts is not None and (active_artifacts.pending_artifacts or active_artifacts.pending_states):
            drain_pending(active_artifacts, "停止前必须确认已收到的原文。")
    finally:
        status("stopped", error=last_error)
    return 0 if last_error is None else 2
