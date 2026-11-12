"""One entry point for lossless battle-report collection, storage and inspection."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid

from capture.game_assets.atomic_files import replace_with_retry

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data/database/battle_reports.db"
DEFAULT_REPORTS = ROOT / "capture/output/processed/battle_reports"
DEFAULT_CAPTURE = ROOT / "data/incoming/runtime"
DEFAULT_MIN_FREE_GIB = 5.0


class CollectionStorageLimit(KeyboardInterrupt):
    """Controlled stop which must escape collectors' ordinary retry handlers."""

    def __init__(self, path: Path, free_bytes: int, minimum_bytes: int):
        self.storage = {"path": str(path), "free_bytes": free_bytes,
                        "minimum_free_bytes": minimum_bytes}
        super().__init__(
            f"磁盘可用空间不足：{path} 仅余 {free_bytes / 1024 ** 3:.2f} GiB，"
            f"采集至少保留 {minimum_bytes / 1024 ** 3:g} GiB。"
            "请释放空间后手动启动采集；已保存的原文和续采状态保留。")


class CollectionStorageGuard:
    """Reserve room for in-flight originals, SQLite WAL and final checkpoints."""

    def __init__(self, paths, minimum_gib: float = DEFAULT_MIN_FREE_GIB):
        if (isinstance(minimum_gib, bool) or not math.isfinite(minimum_gib)
                or minimum_gib <= 0 or not math.isfinite(minimum_gib * 1024 ** 3)):
            raise ValueError("--min-free-gib must be finite and positive")
        self.paths = tuple(Path(path).resolve() for path in paths)
        self.minimum_bytes = math.ceil(minimum_gib * 1024 ** 3)
        self.failure = None

    def check(self):
        if self.failure is not None:
            raise self.failure
        for destination in self.paths:
            existing = destination
            while not existing.exists():
                if existing.parent == existing:
                    raise FileNotFoundError(destination)
                existing = existing.parent
            free_bytes = shutil.disk_usage(existing).free
            if free_bytes < self.minimum_bytes:
                self.failure = CollectionStorageLimit(destination, free_bytes, self.minimum_bytes)
                raise self.failure


def output_path(value: str | Path) -> Path:
    from parser.report_archive import _write_path
    return _write_path(value)


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str), flush=True)


def atomic_json(path: Path, value: object) -> None:
    path = output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    replace_with_retry(temporary, path)


def new_json(path: Path, value: object) -> None:
    """Publish a report without replacing a database, source file or prior report."""
    path = output_path(path)
    content = json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    if path.exists():
        if path.is_file() and path.read_bytes() == content:
            return
        raise FileExistsError(f"Output already exists; choose a new file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".battle-report-", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def collector_lock(path: Path):
    """OS lock releases on crashes; a stale filename never blocks recovery."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another collector already owns this database.") from exc
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="战报全字段归档：采集、入库、查询和完整性核验")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in [("import", "导入原始战报 JSON"), ("collect", "持续采集个人、同盟和收藏战报"),
                            ("status", "查看数据库计数和战报列表"), ("verify", "逐字段核对数据库与原文件"),
                            ("export", "按快照 ID 导出原始文件"), ("query", "只读 SQL 查询"),
                            ("enrich", "为已归档战报补充中文解释"), ("explain", "查看一封战报的中文解释")]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--db", type=Path, default=DEFAULT_DB)
        if name in {"import", "collect"}:
            p.add_argument("--namespace", default="auto" if name == "collect" else "default",
                           help="账号/服务器标识；采集默认按当前角色、服务器和赛季自动隔离")
        if name == "import":
            p.add_argument("paths", nargs="*", type=Path, default=[DEFAULT_REPORTS])
            p.add_argument("--pattern", default="battle_*_decrypted.json")
            p.add_argument("--recursive", action="store_true")
        elif name == "collect":
            p.add_argument("--pid", type=int)
            p.add_argument("--out-dir", type=Path, default=DEFAULT_CAPTURE)
            p.add_argument("--scope", default="personal,union,star")
            p.add_argument("--interval", type=float, default=60)
            p.add_argument("--min-free-gib", type=float, default=DEFAULT_MIN_FREE_GIB,
                           help="采集所需的最小可用空间GiB；不足时安全停止，释放空间后手动启动")
            p.add_argument("--wait", type=float, default=20)
            p.add_argument("--request-delay", type=float, default=0.5)
            p.add_argument("--parallel", type=int, choices=(1, 2, 3, 4), default=1,
                           help="同时预取的战报上限；Lua调用仍在同一游戏线程执行")
            p.add_argument("--pvp-only", action="store_true",
                           help="只请求当前客户端明确标为PVP的战报正文；跳过PVE和未知类型")
            p.add_argument("--max-pages", type=int, default=10000)
            p.add_argument("--max-cycles", type=int)
            p.add_argument("--once", action="store_true")
            p.add_argument("--console", action="store_true", help="显示简明采集进度，完整状态仍写入文件")
            p.add_argument("--battle-ids", nargs="+", help="只采集明确列出的ID；完整度仅针对这些ID")
        elif name == "status":
            p.add_argument("--limit", type=int, default=20)
        elif name == "verify":
            p.add_argument("--out", type=Path)
        elif name == "export":
            p.add_argument("snapshot_id", type=int)
            p.add_argument("output", type=Path)
        elif name == "query":
            p.add_argument("sql", help="只读 SELECT / WITH / PRAGMA 查询")
            p.add_argument("--limit", type=int, default=100)
        elif name == "enrich":
            p.add_argument("--current-only", action="store_true")
            p.add_argument("--limit", type=int)
            p.add_argument("--force", action="store_true")
        elif name == "explain":
            p.add_argument("battle_id")
            p.add_argument("--namespace")
            p.add_argument("--limit", type=int, default=20, help="最多展示多少条事件")
            p.add_argument("--out", type=Path)
    return parser


def normalize_identity(value: object) -> dict[str, str]:
    """Canonicalize client numbers and retained strings before account comparisons."""
    if not isinstance(value, dict):
        raise RuntimeError("Current account identity must be an object")
    result = {}
    for name in ("avatarId", "serverId", "scenarioId"):
        item = value.get(name)
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise RuntimeError(f"Current account identity is incomplete: {name}")
        text = str(item)
        if not text.isascii() or not text.isdigit():
            raise RuntimeError(f"Current account identity is invalid: {name}")
        number = int(text)
        if name in {"avatarId", "serverId"} and number == 0:
            raise RuntimeError(f"Current account identity is not ready: {name}")
        result[name] = str(number)
    return result


def read_identity(runner) -> dict:
    from export_battle_report_runtime import LUA_JSON, decode_envelope, run_checked
    code = LUA_JSON + """
local cp = ClientProxy
if not cp or cp.avatarId == nil or type(cp.GetServerId) ~= 'function' then
  return envelope({error='Game not logged in or unavailable; current avatar/server identity is missing.'})
end
return envelope({avatarId=cp.avatarId,serverId=cp:GetServerId(),scenarioId=G_SCENARIO_ID})
"""
    result = decode_envelope(run_checked(runner, code))["payload"]
    if result.get("error"):
        raise RuntimeError(result["error"])
    return normalize_identity(result)


def recover_captures(archive, out_dir: Path, namespace: str, expected_identity: dict | None = None,
                     before_import=None) -> dict:
    """Replay durable files left between file rename and database commit."""
    if before_import is not None:
        before_import()
    if expected_identity is not None:
        expected_identity = normalize_identity(expected_identity)
    paths = set(out_dir.glob("battle_*_decrypted.json"))
    paths.update(out_dir.glob("captures/*/*.json"))
    result_summary = {"recovered": 0, "errors": []}
    for path in sorted(paths):
        try:
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            stored = archive.connection.execute(
                "SELECT parse_status FROM report_files WHERE namespace=? AND sha256=?", (namespace, digest)
            ).fetchone()
            if stored and stored[0] == "parsed":
                continue
            # Check any identity explicitly declared by the file before assigning
            # it to the directory's account. Old exports may lack these fields.
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                payload = None  # The archive retains malformed original bytes.
            if isinstance(payload, dict):
                if "namespace" in payload and payload["namespace"] != namespace:
                    raise ValueError("Capture namespace conflicts with account directory; file retained without misattribution")
                if (expected_identity is not None and "runtimeIdentity" in payload
                        and normalize_identity(payload["runtimeIdentity"]) != expected_identity):
                    raise ValueError("Capture runtimeIdentity conflicts with account binding; file retained without misattribution")
            if before_import is not None:
                before_import()
            result = archive.import_bytes(raw, source_path=str(path), namespace=namespace)
            if result.get("status") == "failed":
                result_summary["errors"].append({"path": str(path), "error": result.get("error")})
            else:
                result_summary["recovered"] += result.get("status") == "imported"
        except Exception as exc:
            result_summary["errors"].append({"path": str(path), "error": str(exc)})
    return result_summary


def read_only_sql(action, arg1, arg2, database, trigger):
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE}
    if action in allowed:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() not in {"load_extension", "writefile"}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        simple = {"table_list", "database_list", "compile_options", "integrity_check", "quick_check", "foreign_key_check", "user_version", "schema_version"}
        named = {"table_info", "table_xinfo", "index_info", "index_xinfo", "index_list", "foreign_key_list"}
        if str(arg1).lower() in named or (str(arg1).lower() in simple and arg2 is None):
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def run_collect(args, archive) -> int:
    sys.path.insert(0, str(ROOT / "capture/game_assets"))
    from battle_collector import BattleCollector
    from run_lua_in_game import GameLuaRunner
    from export_battle_report_runtime import capture_report, validate_battle_id
    from runtime_errors import RuntimeBridgeUnavailable

    scopes = tuple(dict.fromkeys(s.strip() for s in args.scope.split(",") if s.strip()))
    if not scopes or set(scopes) - {"personal", "union", "star"}:
        raise ValueError("--scope must use personal,union,star")
    if any(not math.isfinite(value) or value <= 0 for value in (args.interval, args.wait, args.request_delay)) or args.max_pages < 1:
        raise ValueError("Intervals, wait and max-pages must be positive.")
    if args.max_cycles is not None and args.max_cycles < 1:
        raise ValueError("--max-cycles must be positive")
    if getattr(args, "pvp_only", False) and getattr(args, "battle_ids", None):
        raise ValueError("--pvp-only cannot be combined with --battle-ids: explicit IDs lack verified list matchType")
    out_dir = output_path(args.out_dir)
    storage = CollectionStorageGuard((output_path(args.db).parent, out_dir),
                                     getattr(args, "min_free_gib", DEFAULT_MIN_FREE_GIB))
    storage.check()
    out_dir.mkdir(parents=True, exist_ok=True)
    cycle_limit = 1 if args.once else args.max_cycles
    cycle = 0
    last_failed = False
    active_namespace = args.namespace
    recovered_accounts = set()
    recover_again = set()
    recovery_issues = {}
    binding_errors = []
    active_identity = None
    runtime_context = {}
    status_context = {}
    progress_state = {"current_battle_id": None, "last_error": None}
    last_progress_write = -math.inf
    status_path = out_dir / "collector_status.json"
    diagnostic_path = out_dir / "collector_diagnostics.jsonl"

    def diagnostic(event):
        record = {"at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
                  "cycle": cycle, "namespace": active_namespace, **runtime_context, **event}
        with diagnostic_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
            handle.flush()
            if event.get("error"):
                os.fsync(handle.fileno())

    def collection_database_status():
        # Per-cycle UI status must not count the archive's entire field/event
        # history. Those scans belong to the explicit status/verify commands.
        began = time.monotonic()
        try:
            diagnostic({"phase": "cycle_statistics_started"})
        except OSError:
            pass
        summary = archive.collection_stats()
        try:
            diagnostic({"phase": "cycle_statistics_finished",
                        "elapsed_seconds": max(0.0, time.monotonic() - began),
                        "statistics_mode": summary.get("statistics_mode")})
        except OSError:
            pass
        return summary

    def write_status(value):
        status_context.clear()
        status_context.update(value)
        # Progress is event-driven, not inferred from a process merely existing.
        known = {"namespace": active_namespace, **runtime_context,
                 "pvp_only": bool(getattr(args, "pvp_only", False))}
        result_filter = value.get("result", {}).get("filter") if isinstance(value.get("result"), dict) else None
        known["filter"] = result_filter if isinstance(result_filter, dict) else {
            "mode": "pvp_only" if known["pvp_only"] else "all"}
        if active_identity is not None:
            known["identity"] = active_identity
        atomic_json(status_path, {**known, **progress_state, **status_context, "pid": os.getpid()})

    def record_progress(event):
        nonlocal last_progress_write
        stage = event.get("phase")
        if stage not in {"page_received", "capture_started", "capture_failed", "capture_imported", "capture_finished"}:
            return
        now = datetime.now(timezone.utc).isoformat()
        if stage == "capture_finished":
            elapsed, outcome = event.get("elapsed_seconds"), event.get("outcome")
            if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
                return
            try:
                elapsed = float(elapsed)
            except (OverflowError, ValueError):
                return
            if (not math.isfinite(elapsed) or elapsed < 0
                    or outcome not in {"complete", "partial", "failed"}
                    or event.get("battleID") is None):
                return
            progress_state["last_capture"] = {
                "namespace": active_namespace, "battleID": str(event["battleID"]),
                "elapsed_seconds": float(elapsed), "outcome": outcome,
                "finished_at": event.get("finished_at") or now,
                "source": "collector", "approximate": False,
            }
        previous_stage = progress_state.get("current_stage")
        progress_state.update(current_stage=stage, last_progress_at=now)
        if event.get("battleID") is not None:
            progress_state["current_battle_id"] = str(event["battleID"])
        elif stage == "page_received":
            progress_state["current_battle_id"] = None
        if event.get("scope") is not None:
            progress_state["current_scope"] = event["scope"]
        if event.get("index") is not None:
            progress_state["current_page"] = event["index"]
        error = event.get("error")
        if stage == "capture_failed" or (stage == "capture_finished" and event.get("outcome") == "failed"):
            progress_state.update(last_error=str(error or progress_state.get("last_error") or "Unknown capture failure"), last_error_at=now,
                                  last_error_type=event.get("error_type") or progress_state.get("last_error_type"),
                                  reconnecting=bool(event.get("runtime_unavailable") or event.get("bridge_unavailable")
                                                    or event.get("error_type") == "RuntimeBridgeUnavailable"
                                                    or (stage == "capture_finished" and progress_state.get("reconnecting"))))
        else:
            progress_state.update(last_error=None, last_error_type=None, reconnecting=False)
            if stage in {"page_received", "capture_imported", "capture_finished"}:
                progress_state["last_success_at"] = now
        tick = time.monotonic()
        # Page boundaries, capture starts and failures are useful landmarks.
        # Rapid repeated import notifications need at most one write a second.
        if stage == "capture_imported" and previous_stage == stage and tick - last_progress_write < 1:
            return
        last_progress_write = tick
        try:
            diagnostic(event)
        except OSError as exc:
            progress_state["diagnostic_log_error"] = str(exc)
        value = {**status_context, "cycle": cycle, "at": now, "running": True,
                 "phase": "collecting", "complete": False, "namespace": active_namespace}
        if active_identity is not None:
            value["identity"] = active_identity
        try:
            write_status(value)
        except OSError as exc:
            # Optional progress reporting must not turn a committed raw import
            # into a database failure; a later checkpoint exposes this error.
            progress_state["progress_write_error"] = str(exc)

    def ingest(path):
        storage.check()
        try:
            result = archive.import_file(path, namespace=active_namespace)
            if result.get("status") in {"failed", "error", "invalid"} or result.get("error"):
                raise RuntimeError(f"Database rejected {path}: {result}")
        except Exception:
            recover_again.add(active_namespace)
            raise
        if result.get("status") in {"imported", "duplicate"}:
            reports = result.get("reports", [])
            report = reports[-1] if reports else {}
            record_progress({"phase": "capture_imported", "battleID": report.get("battle_uid"),
                             "path": str(path), "import_status": result["status"],
                             "completeness": report.get("completeness")})
        if args.console:
            if result.get("semantics", {}).get("failed"):
                print("战报原文已保存；中文解释有待重试项，可查看 enrich/status。", flush=True)
            if result.get("status") == "imported":
                component_names = {"basic": "基础信息", "detail": "详情", "all": "完整事件", "statics": "统计", "snapshots": "武将快照"}
                for report in result.get("reports", []):
                    if report.get("capture_kind") in {"list_discovery", "cache_at_dispatch", "response_before_handler"}:
                        # Intermediate preservation is archived normally, but
                        # only the final capture should describe this battle's
                        # completeness to the person watching the console.
                        continue
                    uid = report.get("battle_uid", "")
                    if report.get("completeness") == "complete":
                        print(f"完整战报已入库：{uid}（五组件齐全，{report.get('event_count', 0)} 条事件）", flush=True)
                    else:
                        statuses = report.get("component_status", {})
                        missing = [component_names[name] for name in component_names
                                   if not statuses.get(name, {}).get("complete") or statuses.get(name, {}).get("error")]
                        reason = "、".join(missing) or "需核查完整性问题"
                        print(f"战报 {uid} 尚未采齐：{reason}；已取得的数据保留，继续补齐。", flush=True)
        else:
            emit({"database_import": result})
    def progress(event):
        if event.get("phase") == "capture_started":
            storage.check()
        record_progress(event)
        if not args.console:
            return
        if event.get("phase") == "capture_started":
            print(f"正在补齐完整战报：{event['battleID']}……", flush=True)
        elif event.get("phase") == "page_received":
            label = {"personal": "个人", "union": "同盟", "star": "收藏"}.get(event.get("scope"), event.get("scope"))
            print(f"已读取{label}战报一页（{event.get('count', 0)} 条），开始处理本页完整数据。", flush=True)
        elif event.get("phase") == "capture_failed":
            reason = str(event.get("error") or "未知错误")
            uid = event.get("battleID", "未知ID")
            if event.get("runtime_unavailable") or event.get("bridge_unavailable") or event.get("error_type") == "RuntimeBridgeUnavailable":
                following = ("将重新连接游戏后继续。" if cycle_limit is None or cycle < cycle_limit
                             else "保持游戏登录后可重新启动采集。")
                print(f"战报 {uid} 暂未采齐：游戏连接已失效，原因：{reason}。已保留数据，{following}", flush=True)
            else:
                print(f"战报 {uid} 采集失败：{reason}；已保留数据，后续重试。", flush=True)
    class StorageCheckedCollector(BattleCollector):
        def page(self, *args, **kwargs):
            storage.check()
            return super().page(*args, **kwargs)

        def _replay_pending_capture(self, entry):
            if storage.failure is not None:
                # Stop-time harvesting still saves already prefetched originals.
                # Keep the durable pending queue intact until a manual restart;
                # do not acknowledge or index it while storage is paused.
                return entry["pendingImport"]["result"]
            return super()._replay_pending_capture(entry)

    with collector_lock(output_path(args.db).with_suffix(".collector.lock")), collector_lock(out_dir / ".collector.lock"):
        owner_id = uuid.uuid4().hex
        try:
            recovery_errors = []
            progress_state.update(current_stage="recovering_local_files", last_progress_at=datetime.now(timezone.utc).isoformat())
            write_status({"cycle": 0, "at": datetime.now(timezone.utc).isoformat(),
                          "running": True, "phase": "connecting", "complete": False,
                          "namespace": active_namespace})
            # Recovery works even when maintenance prevents login. Only account
            # directories written by this entry point carry this small binding.
            for binding in out_dir.glob("*/account.json"):
                try:
                    account = json.loads(binding.read_text(encoding="utf-8"))
                    namespace = account["namespace"]
                    if not isinstance(namespace, str) or not namespace.strip() or binding.parent.name != hashlib.sha256(namespace.encode()).hexdigest()[:20]:
                        raise ValueError(f"Account directory binding mismatch: {binding}")
                    recovered = recover_captures(archive, binding.parent, namespace,
                                                 expected_identity=account.get("identity"), before_import=storage.check)
                    recovered_accounts.add(namespace)
                    recovery_issues[namespace] = recovered["errors"]
                    if recovered["recovered"] or recovered["errors"]:
                        emit({"recovery": recovered, "namespace": namespace})
                except Exception as exc:
                    binding_errors.append({"path": str(binding), "error": str(exc)})
            while cycle_limit is None or cycle < cycle_limit:
                cycle += 1
                recovery_errors = binding_errors + [issue for issues in recovery_issues.values() for issue in issues]
                progress_state.update(current_stage="connecting", current_battle_id=None,
                                      current_scope=None, current_page=None,
                                      last_progress_at=datetime.now(timezone.utc).isoformat())
                runtime_context.clear()
                write_status({
                    "cycle": cycle, "at": datetime.now(timezone.utc).isoformat(),
                    "running": True, "phase": "connecting", "complete": False,
                })
                try:
                    if args.console:
                        print("正在连接游戏并确认当前角色……", flush=True)
                    storage.check()
                    with GameLuaRunner(args.pid) as runner:
                        process = getattr(runner, "process", None)
                        runtime_context["game_pid"] = getattr(runner, "pid", None)
                        runtime_context["game_selection"] = (
                            process.get("selection") if isinstance(process, dict) else None
                        ) or ("explicit_pid" if args.pid is not None else "automatic")
                        if isinstance(process, dict):
                            runtime_context["game_process"] = dict(process)
                        write_status(dict(status_context))
                        identity = read_identity(runner)
                        active_identity = identity
                        account_key = "server:{serverId}/scenario:{scenarioId}/avatar:{avatarId}".format(**identity)
                        namespace = account_key if args.namespace == "auto" else args.namespace
                        if namespace != active_namespace:
                            progress_state.pop("last_capture", None)
                            progress_state["current_battle_id"] = None
                        active_namespace = namespace
                        account_dir = out_dir / hashlib.sha256(active_namespace.encode()).hexdigest()[:20]
                        binding = account_dir / "account.json"
                        if binding.exists():
                            previous = json.loads(binding.read_text(encoding="utf-8"))
                            if normalize_identity(previous.get("identity")) != identity:
                                raise RuntimeError("This namespace belongs to another account/server/season; use --namespace auto.")
                        atomic_json(binding, {"namespace": active_namespace, "identity": identity})
                        # Historical immutable files need a full recovery scan
                        # on startup/account entry or after a failed commit,
                        # not on every normal polling cycle.
                        if active_namespace not in recovered_accounts or active_namespace in recover_again:
                            recovered = recover_captures(archive, account_dir, active_namespace,
                                                         expected_identity=identity, before_import=storage.check)
                            recovered_accounts.add(active_namespace)
                            recover_again.discard(active_namespace)
                            recovery_issues[active_namespace] = recovered["errors"]
                        recovery_errors = binding_errors + [issue for issues in recovery_issues.values() for issue in issues]
                        progress_state.update(current_stage="collecting", last_progress_at=datetime.now(timezone.utc).isoformat(),
                                              last_error=None, last_error_type=None, reconnecting=False)
                        write_status({
                            "cycle": cycle, "at": datetime.now(timezone.utc).isoformat(),
                            "running": True, "phase": "collecting", "complete": False,
                            "namespace": active_namespace, "identity": identity,
                            "recovery_errors": recovery_errors,
                        })
                        if args.battle_ids:
                            records = []
                            for battle_id in dict.fromkeys(validate_battle_id(value) for value in args.battle_ids):
                                storage.check()
                                capture_began = time.monotonic()
                                capture_started_at = datetime.now(timezone.utc).isoformat()
                                capture_outcome, capture_error, capture_error_type = "failed", None, None
                                progress({"phase": "capture_started", "battleID": battle_id})
                                try:
                                    record = capture_report(runner, battle_id, account_dir,
                                                            wait_seconds=args.wait, expected_identity=identity)
                                    # A failed final request may still have preserved
                                    # real pages before the client cleared its cache.
                                    # Commit those bytes in this run as well; recovery
                                    # on restart remains a fallback, not the only path.
                                    capture_paths = list(record.get("cacheCheckpointPaths", []))
                                    if record.get("exported"):
                                        capture_paths.append(record["path"])
                                    for capture_path in dict.fromkeys(capture_paths):
                                        ingest(Path(capture_path))
                                    capture_outcome = ("complete" if record.get("complete") is True
                                                       else "partial" if record.get("exported") else "failed")
                                    if not record.get("exported"):
                                        capture_error = record.get("error", "No battle data was exported")
                                        capture_error_type = "CaptureUnavailable"
                                        progress({"phase": "capture_failed", "battleID": battle_id,
                                                  "error": capture_error,
                                                  "error_type": "CaptureUnavailable"})
                                except RuntimeBridgeUnavailable as exc:
                                    capture_error, capture_error_type = str(exc), type(exc).__name__
                                    progress({"phase": "capture_failed", "battleID": battle_id,
                                              "error": str(exc), "error_type": type(exc).__name__,
                                              "runtime_unavailable": True})
                                    raise
                                except Exception as exc:
                                    capture_error, capture_error_type = str(exc), type(exc).__name__
                                    progress({"phase": "capture_failed", "battleID": battle_id,
                                              "error": str(exc), "error_type": type(exc).__name__})
                                    record = {"battleID": battle_id, "exported": False, "complete": False, "error": str(exc)}
                                finally:
                                    progress({"phase": "capture_finished", "battleID": battle_id,
                                              "elapsed_seconds": max(0.0, time.monotonic() - capture_began),
                                              "outcome": capture_outcome, "started_at": capture_started_at,
                                              "finished_at": datetime.now(timezone.utc).isoformat(),
                                              "error": capture_error, "error_type": capture_error_type})
                                records.append(record)
                                atomic_json(account_dir / "explicit_ids_manifest.json", records)
                            result = {"scope": "explicit_ids", "reports": records,
                                      "complete": bool(records) and all(r.get("complete") is True for r in records),
                                      "errors": [r for r in records if r.get("error")]}
                        else:
                            collector = StorageCheckedCollector(
                                runner, account_dir, scopes=scopes, namespace=active_namespace,
                                wait_seconds=args.wait, request_delay=args.request_delay,
                                max_pages=args.max_pages, on_export=ingest, on_progress=progress,
                                parallelism=getattr(args, "parallel", 1),
                                owner_id=owner_id,
                                pvp_only=bool(getattr(args, "pvp_only", False)),
                            )
                            result = collector.run_cycle()
                    last_failed = bool(result.get("errors")) or result.get("complete") is not True or bool(recovery_errors)
                    status = {"cycle": cycle, "at": datetime.now(timezone.utc).isoformat(),
                              "namespace": active_namespace, "identity": identity,
                              "complete": False,
                              "result": result, "recovery_errors": recovery_errors,
                              "database": collection_database_status()}
                except (Exception, SystemExit) as exc:
                    last_failed = True
                    if active_namespace in recovered_accounts:
                        recover_again.add(active_namespace)
                    status = {"cycle": cycle, "at": datetime.now(timezone.utc).isoformat(),
                              "error": str(exc), "complete": False, "recovery_errors": recovery_errors,
                              "error_type": type(exc).__name__,
                              "retrying": cycle_limit is None or cycle < cycle_limit}
                    bridge_error = getattr(exc, "result", None)
                    if isinstance(exc, RuntimeBridgeUnavailable) and isinstance(bridge_error, dict):
                        status["runtime_error"] = dict(bridge_error)
                    progress_state.update(current_stage="cycle_failed", last_error=str(exc),
                                          last_error_at=status["at"], last_error_type=type(exc).__name__,
                                          last_progress_at=status["at"],
                                          reconnecting=type(exc).__name__ == "RuntimeBridgeUnavailable")
                    try:
                        diagnostic({"phase": "cycle_failed", "error": str(exc), "error_type": type(exc).__name__,
                                    **({"runtime_error": dict(bridge_error)} if isinstance(bridge_error, dict) else {})})
                    except OSError as log_error:
                        progress_state["diagnostic_log_error"] = str(log_error)
                continuing = cycle_limit is None or cycle < cycle_limit
                status["running"] = continuing
                status["phase"] = "waiting" if continuing else ("failed" if status.get("error") else "finished")
                write_status(status)
                if args.console:
                    if status.get("error"):
                        if status.get("error_type") == "RuntimeBridgeUnavailable":
                            following = "等待下一轮重新连接游戏。" if continuing else "保持游戏登录后可重新启动采集。"
                            print(f"游戏连接已失效：{status['error']}；已保存的数据保留，{following}", flush=True)
                        else:
                            print(f"当前无法采集：{status['error']}", flush=True)
                    else:
                        summary = status["result"]
                        if summary.get("scope") == "explicit_ids":
                            visible = len(summary["reports"])
                            complete = sum(r.get("complete") is True for r in summary["reports"])
                        else:
                            visible = summary.get("visibleReports", summary.get("discoveredReports", 0))
                            complete = summary.get("completeVisibleReports", summary.get("completeReports", 0))
                        print(f"本轮发现 {visible} 封战报，组件完整 {complete} 封。", flush=True)
                        if summary.get("errors") or status.get("recovery_errors"):
                            print(f"部分步骤需要重试，详细原因已保存：{out_dir / 'collector_status.json'}", flush=True)
                    if cycle_limit is None or cycle < cycle_limit:
                        print(f"{args.interval:g} 秒后继续检查；按 Ctrl+C 停止。", flush=True)
                else:
                    emit(status)
                recovery_errors = []
                if cycle_limit is None or cycle < cycle_limit:
                    time.sleep(args.interval)
        except CollectionStorageLimit as exc:
            now = datetime.now(timezone.utc).isoformat()
            progress_state.update(current_stage="storage_paused", last_error=str(exc),
                                  last_error_type=type(exc).__name__, last_error_at=now,
                                  last_progress_at=now, reconnecting=False)
            status = {"cycle": cycle, "at": now, "running": False,
                      "phase": "storage_paused", "complete": False, "retrying": False,
                      "error": str(exc), "error_type": type(exc).__name__, "storage": exc.storage}
            try:
                diagnostic({"phase": "storage_paused", "error": str(exc), "storage": exc.storage})
            except OSError as error:
                status["diagnostic_log_error"] = str(error)
            try:
                write_status(status)
            except OSError as error:
                status["status_write_error"] = str(error)
            if args.console:
                print(str(exc), flush=True)
            else:
                emit(status)
            return 2
        except KeyboardInterrupt:
            progress_state.update(current_stage="stopped", last_progress_at=datetime.now(timezone.utc).isoformat(), reconnecting=False)
            write_status({
                "cycle": cycle, "at": datetime.now(timezone.utc).isoformat(),
                "running": False, "phase": "stopped", "complete": False,
            })
            if args.console:
                print("已停止；已入库数据和重试状态保留。", flush=True)
            else:
                emit({"stopped": True, "database": collection_database_status(), "message": "已停止；已入库数据和重试状态保留。"})
            return 0
    return 2 if last_failed else 0


def explain_report(archive, battle_id: str, namespace: str | None = None, limit: int = 20) -> dict:
    from parser.report_semantics import semantic_stats
    if limit < 1:
        raise ValueError("--limit must be positive")
    state = semantic_stats(archive.connection)
    if not state.get("active_version"):
        raise ValueError("中文解释尚未生成，请先运行 battle_reports.py enrich")
    params = [str(battle_id)]
    where = "battle_uid=?"
    if namespace is not None:
        where += " AND namespace=?"
        params.append(namespace)
    rows = archive.connection.execute(
        f"SELECT namespace,battle_uid,id,completeness,event_count FROM report_current_snapshots WHERE {where}", params).fetchall()
    if not rows:
        raise ValueError("数据库中没有这封战报")
    if len(rows) > 1:
        raise ValueError("该战报在多个账号中出现，请用 --namespace 指定账号")
    report = dict(rows[0])
    snapshot_id = report["id"]
    ids, seen = [], set()
    for row in archive.connection.execute(
            "SELECT pointer,kind,raw_value_json,name_zh,mapped,status FROM report_current_id_meanings "
            "WHERE snapshot_id=? ORDER BY pointer", (snapshot_id,)):
        item = dict(row)
        key = (item["kind"], item["raw_value_json"], item["name_zh"])
        if key in seen:
            continue
        seen.add(key)
        item["raw_value"] = json.loads(item.pop("raw_value_json"))
        ids.append(item)
    events = []
    for row in archive.connection.execute(
            "SELECT sequence,event_id,event_name_zh,summary_zh,parameters_json,status FROM report_current_event_meanings "
            "WHERE snapshot_id=? ORDER BY sequence LIMIT ?", (snapshot_id, limit)):
        item = dict(row)
        item["parameters"] = json.loads(item.pop("parameters_json"))
        events.append(item)
    run = archive.connection.execute(
        "SELECT status,error FROM report_semantic_runs WHERE snapshot_id=? AND version=?",
        (snapshot_id, state["active_version"])).fetchone()
    return {"report": report, "mapping_version": state["active_version"],
            "mapping_run": dict(run) if run else {"status": "pending"},
            "ids": ids, "events": events, "events_limit": limit,
            "note": "保留原始ID和数字参数；mapped表示有来源的解释，未核实项明确保留。"}


def main(argv=None) -> int:
    # Set these before importing libraries that create temporary/cache files.
    temp = output_path(os.environ.get("SANMOU_TEMP_DIR", str(ROOT / ".runtime" / "temp")))
    temp.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = str(temp)
    sys.dont_write_bytecode = True
    args = build_parser().parse_args(argv)
    try:
        if args.command == "collect":
            for name in ("interval", "wait", "request_delay"):
                value = getattr(args, name)
                if not math.isfinite(value) or value <= 0:
                    raise ValueError(f"--{name.replace('_', '-')} must be finite and positive")
            if args.max_pages < 1 or (args.max_cycles is not None and args.max_cycles < 1):
                raise ValueError("Page and cycle limits must be positive")
            if not args.namespace.strip():
                raise ValueError("Namespace must not be empty")
        db_path = output_path(args.db)
        if args.command == "collect":
            # Refuse before opening a writable database or attaching to the game.
            CollectionStorageGuard((db_path.parent, output_path(args.out_dir)), args.min_free_gib).check()
        if args.command == "query":
            if args.limit < 1:
                raise ValueError("--limit must be positive")
            with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.execute("PRAGMA temp_store=MEMORY")
                connection.execute("PRAGMA query_only=ON")
                connection.set_authorizer(read_only_sql)
                cursor = connection.execute(args.sql)
                rows = cursor.fetchmany(args.limit + 1)
                emit({"columns": [c[0] for c in cursor.description or []], "rows": rows[:args.limit],
                      "truncated": len(rows) > args.limit})
            return 0
        if args.command in {"status", "verify", "export", "enrich", "explain"} and not db_path.exists():
            raise FileNotFoundError(f"Database does not exist: {db_path}")
        from parser.report_archive import ReportArchive
        mapper = None
        semantic_warning = None
        if args.command in {"import", "collect", "enrich"}:
            try:
                from parser.semantic_catalog import build_default_mapper
                mapper = build_default_mapper()
            except Exception as exc:
                if args.command == "enrich":
                    raise
                semantic_warning = str(exc)
        if semantic_warning:
            emit({"semantic_warning": semantic_warning, "message": "原文入库继续；中文解释可稍后重试。"})
        with ReportArchive(db_path, read_only=args.command in {"status", "verify", "export", "explain"},
                           semantic_mapper=mapper) as archive:
            if args.command == "import":
                paths = set()
                for path in args.paths:
                    if not path.exists():
                        raise FileNotFoundError(path)
                    if path.is_dir():
                        paths.update(path.rglob(args.pattern) if args.recursive else path.glob(args.pattern))
                    else:
                        paths.add(path)
                if not paths:
                    raise FileNotFoundError("No matching report files; nothing imported.")
                results = [archive.import_file(path, namespace=args.namespace) for path in sorted(paths)]
                emit({"files": len(results), "results": results, "database": archive.stats()})
                return 2 if any(r.get("error") or r.get("status") in {"failed", "error", "invalid"} for r in results) else 0
            if args.command == "collect":
                return run_collect(args, archive)
            if args.command == "status":
                from parser.report_semantics import semantic_stats
                emit({"database": archive.stats(), "semantics": semantic_stats(archive.connection),
                      "reports": archive.list_reports(limit=args.limit)})
            elif args.command == "enrich":
                result = archive.semantic_archive().backfill(only_current=args.current_only, limit=args.limit, force=args.force)
                emit(result)
                return 2 if result["failed"] else 0
            elif args.command == "explain":
                result = explain_report(archive, args.battle_id, args.namespace, args.limit)
                if args.out:
                    new_json(archive.protect_output_path(args.out), result)
                emit(result)
            elif args.command == "verify":
                result = archive.verify()
                if args.out:
                    new_json(archive.protect_output_path(args.out), result)
                emit(result)
                return 0 if result.get("ok") else 2
            elif args.command == "export":
                result = archive.export_original(args.snapshot_id, output_path(args.output))
                emit({"exported": str(args.output.resolve()), "result": result})
        return 0
    except CollectionStorageLimit as exc:
        emit({"error": str(exc), "error_type": type(exc).__name__, "phase": "storage_paused",
              "running": False, "complete": False, "retrying": False, "storage": exc.storage})
        return 2
    except Exception as exc:
        emit({"error": str(exc), "complete": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
