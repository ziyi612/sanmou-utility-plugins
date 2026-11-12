"""Nonblocking desktop control for the existing, independent collector process.

This module never opens the game or database. The collector keeps owning its
original locks, validation, resume state and SQLite transactions. Its output
goes directly to a file, so closing the GUI does not close a pipe underneath it.
"""
from __future__ import annotations

import argparse
import codecs
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

import psutil

if __package__:
    from .capture_timing import LastCaptureTimingReader
else:
    # The independent stop helper invokes this file directly, outside a package.
    from capture_timing import LastCaptureTimingReader


WINDOWS = sys.platform == "win32"
SCOPES = ("personal", "union", "star")
PHASE_LABELS = {
    "connecting": "正在连接游戏", "collecting": "采集中", "waiting": "等待下一轮",
    "stopping": "正在保存并停止", "stopped": "已停止", "finished": "本轮已结束",
    "failed": "采集失败", "starting": "正在启动", "external": "已有采集正在运行",
}


def _path_key(value: str | Path, base: Path | None = None) -> str:
    path = Path(value)
    if not path.is_absolute() and base is not None:
        path = base / path
    return os.path.normcase(str(path.resolve()))


def _write_path(value: str | Path) -> Path:
    path = Path(value).resolve()
    return path


def _flag(args: list[str], name: str, default: Path, cwd: Path) -> Path:
    for index, item in enumerate(args):
        if item == name:
            if index + 1 == len(args):
                raise ValueError(f"Missing argument for {name}")
            return Path(_path_key(args[index + 1], cwd))
        if item.startswith(name + "="):
            return Path(_path_key(item.split("=", 1)[1], cwd))
    return default


def _collection_options(args: list[str]) -> dict[str, Any] | None:
    """Read active CLI settings for display, mirroring argparse's last value."""
    def value(name: str, default: str) -> str:
        result = default
        for index, item in enumerate(args):
            if item == name:
                if index + 1 == len(args):
                    raise ValueError(f"Missing argument for {name}")
                result = args[index + 1]
            elif item.startswith(name + "="):
                result = item.split("=", 1)[1]
        return result
    try:
        scopes = list(dict.fromkeys(item.strip() for item in value("--scope", ",".join(SCOPES)).split(",") if item.strip()))
        interval = float(value("--interval", "60"))
        parallel = int(value("--parallel", "1"))
        if not scopes or any(item not in SCOPES for item in scopes) or not math.isfinite(interval) or interval <= 0:
            return None
        if not 1 <= parallel <= 4:
            return None
        result = {"scopes": scopes, "interval": int(interval) if interval.is_integer() else interval,
                  "pvp_only": "--pvp-only" in args, "parallel": parallel}
        manual_pid = value("--pid", "")
        if manual_pid:
            pid = int(manual_pid)
            if pid <= 0:
                return None
            result["pid"] = pid
        return result
    except (ValueError, TypeError):
        return None


def collector_signature(process: Any, root: Path, db_path: Path) -> dict[str, Any] | None:
    """Verify the real entry point, collect command, DB, output and PID age."""
    try:
        args = list(process.cmdline())
        cwd = Path(process.cwd()).resolve()
        entry = root / "battle_reports.py"
        tail = None
        index = 1
        while index < len(args):
            arg = args[index]
            if arg in {"-B", "-u", "-E", "-I", "-s", "-S", "-q", "-v", "-O", "-OO", "-x", "-i"}:
                index += 1
                continue
            if arg in {"-X", "-W", "--check-hash-based-pycs"}:
                index += 2
                continue
            if arg.startswith(("-X", "-W")):
                index += 1
                continue
            if arg == "-m":
                if args[index + 1:index + 2] == ["battle_reports"] and cwd == root:
                    tail = args[index + 2:]
                break
            # Only the interpreter's actual entry point counts. A different
            # script containing our path as a data argument is not a collector.
            if not arg.startswith("-") and Path(arg).name.casefold() == "battle_reports.py" and _path_key(arg, cwd) == _path_key(entry):
                tail = args[index + 1:]
            break
        if not tail or tail[0] != "collect":
            return None
        db = _flag(tail, "--db", root / "data/database/battle_reports.db", cwd)
        if _path_key(db) != _path_key(db_path):
            return None
        out_dir = _flag(tail, "--out-dir", root / "data/incoming/runtime", cwd)
        created = float(process.create_time())
        if not process.is_running():
            return None
        return {"pid": int(process.pid), "created_at": created, "entry": str(entry),
                "db_path": str(db), "out_dir": str(out_dir), "cmdline": args,
                "collection_options": _collection_options(tail)}
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError, IndexError):
        return None


def _read_shared(path: Path, limit: int = 1024 * 1024) -> bytes:
    """A short Windows reader must permit the collector's atomic replacement."""
    if not WINDOWS:
        with path.open("rb") as handle:
            return handle.read(limit + 1)
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        return stream.read(limit + 1)


def read_status(path: Path) -> dict[str, Any]:
    try:
        content = _read_shared(path)
        if len(content) > 1024 * 1024:
            raise ValueError("运行状态文件过大，暂时无法显示。")
        value = json.loads(content)
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}


def _timestamp(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _validated_last_capture(value: Any, namespace: str, created_at: float, status_at: Any) -> dict | None:
    if not isinstance(value, dict) or value.get("namespace") not in (None, namespace):
        return None
    duration, battle_id = value.get("elapsed_seconds"), value.get("battleID")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return None
    try:
        duration = float(duration)
    except OverflowError:
        return None
    outcome, source = value.get("outcome"), value.get("source", "collector")
    if not isinstance(outcome, str) or not isinstance(source, str):
        return None
    finished = _timestamp(value.get("finished_at"))
    state_time = _timestamp(status_at)
    if (not math.isfinite(duration) or duration < 0
            or isinstance(battle_id, bool) or not isinstance(battle_id, (str, int))
            or not str(battle_id).strip() or outcome not in {"complete", "partial", "failed"}
            or source not in {"collector", "recovered_log"}
            or finished is None or finished + 2 < created_at
            or finished - duration + 2 < created_at
            or (state_time is not None and finished > state_time + 2)):
        return None
    return {**value, "battleID": str(battle_id).strip(), "elapsed_seconds": float(duration),
            "namespace": namespace, "source": value.get("source", "collector")}


def _console_python(value: str | Path) -> Path:
    path = Path(value).resolve()
    if path.name.casefold() == "pythonw.exe":
        console = path.with_name("python.exe")
        if console.exists():
            return console
    return path


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    temporary = _write_path(Path(os.environ.get("SANMOU_TEMP_DIR", str(Path(__file__).resolve().parents[1] / ".runtime" / "temp"))))
    temporary.mkdir(parents=True, exist_ok=True)
    env.update(TEMP=str(temporary), TMP=str(temporary), PYTHONDONTWRITEBYTECODE="1",
               PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return env


def _hidden_console() -> dict[str, Any]:
    if not WINDOWS:
        return {"start_new_session": True}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {"creationflags": subprocess.CREATE_NEW_CONSOLE,
            "startupinfo": startup}


@contextmanager
def _start_lock(path: Path):
    """Serialize GUI starts; the CLI retains its own authoritative DB lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if WINDOWS:
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("另一个窗口正在启动采集，请稍后刷新。") from exc
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if WINDOWS:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Controller:
    """Small GUI-facing process manager; all real collection stays in the CLI."""
    def __init__(self, root: str | Path, db_path: str | Path | None = None,
                 out_dir: str | Path | None = None, python_executable: str | Path | None = None):
        self.root = Path(root).resolve()
        self.db_path = _write_path(db_path or self.root / "data/database/battle_reports.db")
        self.out_dir = _write_path(out_dir or self.root / "data/incoming/runtime")
        self.python_executable = _console_python(python_executable or sys.executable)
        self.entry = self.root / "battle_reports.py"
        self.status_path = self.out_dir / "collector_status.json"
        self.log_dir = _write_path(self.root / "logs/gui")
        self._process: subprocess.Popen | None = None
        self._owned: dict[str, Any] | None = None
        self._log_path: Path | None = None
        self._diagnostic_offset: int | None = None
        self._log_offset = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._line_buffer = ""
        self._events: queue.SimpleQueue = queue.SimpleQueue()
        self._last_error: str | None = None
        self._stopping: dict[str, Any] | None = None
        self._stop_thread: threading.Thread | None = None
        self._last_state_key: str | None = None
        self._capture_timing_reader = LastCaptureTimingReader()
        self._last_capture_session: tuple[int, float, str] | None = None
        self._last_capture_value: dict[str, Any] | None = None
        self._closed = False

    def _find_collectors(self) -> list[dict[str, Any]]:
        found = []
        for process in psutil.process_iter(["name"]):
            name = (process.info.get("name") or "").casefold()
            if not name.startswith(("python", "pypy")):
                continue
            signature = collector_signature(process, self.root, self.db_path)
            if signature is not None:
                found.append(signature)
        return sorted(found, key=lambda item: item["created_at"])

    def _is_owned(self, signature: dict[str, Any]) -> bool:
        return bool(self._owned and self._owned["pid"] == signature["pid"] and
                    abs(self._owned["created_at"] - signature["created_at"]) < .01)

    def _error(self, message: str) -> None:
        self._last_error = message
        self._events.put({"type": "error", "text": message})

    def _capture_for_status(self, raw: dict, process: dict | None, matches: bool) -> dict | None:
        """Keep a completed attempt only within a verified account/session."""
        previous = self._last_capture_session
        if process and previous and (process["pid"] != previous[0]
                or abs(process["created_at"] - previous[1]) >= .01):
            self._last_capture_session, self._last_capture_value = None, None
        namespace = raw.get("namespace")
        if matches and isinstance(namespace, str) and namespace:
            session = (process["pid"], process["created_at"], namespace)
            if session != self._last_capture_session:
                self._last_capture_session, self._last_capture_value = session, None
        elif (process is not None or self._last_capture_session is None
                or raw.get("pid") != self._last_capture_session[0]
                or namespace != self._last_capture_session[2]
                or (_timestamp(raw.get("at")) or 0) + 2 < self._last_capture_session[1]):
            return None
        session = self._last_capture_session
        if session is None:
            return None
        if raw.get("last_capture") is not None:
            # An explicit invalid/mismatched receipt is unknown, rather than
            # an invitation to reuse another account's older diagnostic.
            self._last_capture_value = _validated_last_capture(
                raw["last_capture"], session[2], session[1], raw.get("at"))
        else:
            try:
                recovered = self._capture_timing_reader.read(self.out_dir / "collector_diagnostics.jsonl",
                    namespace=session[2], pid=session[0], process_created_at=session[1])
            except (OSError, ValueError, UnicodeError):
                recovered = None
            recovered = _validated_last_capture(recovered, session[2], session[1], raw.get("at"))
            if recovered is not None:
                self._last_capture_value = recovered
        return dict(self._last_capture_value) if self._last_capture_value is not None else None

    def snapshot(self) -> dict[str, Any]:
        candidates = self._find_collectors()
        process = next((item for item in candidates if self._is_owned(item)), candidates[0] if candidates else None)
        state_error = None
        try:
            raw = read_status(self.status_path)
        except (OSError, ValueError, UnicodeError) as exc:
            raw, state_error = {}, str(exc)
        owned = bool(process and self._is_owned(process))
        running = process is not None
        if self._stopping and not any(item["pid"] == self._stopping["pid"] and
                abs(item["created_at"] - self._stopping["created_at"]) < .01 for item in candidates):
            self._stopping = None
            self._events.put({"type": "log", "text": "采集已停止，已保存的数据和续采进度保留。"})
        state_at = _timestamp(raw.get("at"))
        matches = bool(process and raw.get("pid") == process["pid"] and state_at is not None
                       and state_at + 2 >= process["created_at"]
                       and _path_key(process["out_dir"]) == _path_key(self.out_dir))
        status_stale = bool(raw.get("running") and not matches)
        if not running:
            phase = "stopped"
        elif self._stopping:
            phase = "stopping"
        elif matches:
            phase = str(raw.get("phase") or "collecting")
        else:
            phase = "starting" if owned else "external"
        last_error = self._last_error or (raw.get("last_error") or raw.get("error") if matches or not running else None)
        if self._process is not None and self._process.poll() is not None and self._is_owned(self._owned or {}):
            code = self._process.returncode
            if code and not running:
                phase = "failed"
                last_error = last_error or f"采集程序已退出（代码 {code}），请查看运行日志。"
        same_output = bool(process and _path_key(process["out_dir"]) == _path_key(self.out_dir))
        last_capture = self._capture_for_status(raw, process, matches)
        result = {
            "running": running, "owned": owned, "external": bool(running and not owned),
            "pid": process["pid"] if process else None, "phase": phase,
            "status": PHASE_LABELS.get(phase, "采集中" if running else "已停止"),
            "last_error": last_error, "state_read_error": state_error,
            "current_battle_id": raw.get("current_battle_id") if matches else None,
            "last_progress_at": raw.get("last_progress_at") if matches else None,
            "last_capture": last_capture,
            "game_pid": raw.get("game_pid") if matches else None,
            "identity": raw.get("identity") if matches else None,
            "namespace": raw.get("namespace") if matches else None,
            "scope": raw.get("current_scope") if matches else None,
            "cycle": raw.get("cycle") if matches else None,
            "log_path": str(self._log_path) if self._log_path else None,
            "diagnostic_path": str(self.out_dir / "collector_diagnostics.jsonl"),
            "can_start": not running and not self._stopping,
            "can_stop": bool(running and same_output and len(candidates) == 1 and not self._stopping),
            "stopping": self._stopping is not None,
            "status_stale": status_stale, "other_collectors": len(candidates),
            "raw_status": raw if matches else {},
            "collection_options": process.get("collection_options") if process else None,
        }
        key = json.dumps({name: result[name] for name in ("running", "pid", "phase", "last_error", "current_battle_id", "last_progress_at", "namespace", "last_capture")}, sort_keys=True)
        if key != self._last_state_key:
            self._last_state_key = key
            self._events.put({"type": "state", "text": result["status"], "snapshot": result})
        return result

    def start(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("界面控制器已关闭，请重新打开。")
        options = dict(options or {})
        scopes = options.get("scopes", SCOPES)
        if isinstance(scopes, str):
            scopes = scopes.split(",")
        if not isinstance(scopes, (tuple, list)) or not scopes or any(scope not in SCOPES for scope in scopes):
            raise ValueError("至少选择个人、同盟、收藏中的一个采集范围。")
        scopes = tuple(dict.fromkeys(scopes))
        interval = options.get("interval", 60)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval < 1:
            raise ValueError("轮询间隔必须是至少 1 秒的有限数字。")
        pid = options.get("pid")
        if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
            raise ValueError("游戏进程 ID 必须为正整数，留空时自动选择。")
        parallel = options.get("parallel", 1)
        if isinstance(parallel, bool) or not isinstance(parallel, int) or not 1 <= parallel <= 4:
            raise ValueError("并行数必须为 1 到 4；1 为逐封采集。")
        pvp_only = options.get("pvp_only", False)
        if not isinstance(pvp_only, bool):
            raise ValueError("仅采集 PVP 选项必须为勾选或未勾选。")
        namespace = options.get("namespace", "auto")
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("账号命名空间不能为空。")
        if not self.entry.is_file() or not self.python_executable.is_file():
            raise FileNotFoundError("采集入口或 Python 运行环境不存在，请检查程序目录。")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        with _start_lock(self.out_dir / ".gui_start.lock"):
            if self._find_collectors():
                raise RuntimeError("已有采集程序正在使用这个数据库，已阻止重复启动。")
            self.log_dir.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime("collector_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8] + ".log"
            self._log_path = self.log_dir / name
            command = [str(self.python_executable), "-B", "-u", str(self.entry), "collect", "--console",
                       "--db", str(self.db_path), "--out-dir", str(self.out_dir), "--namespace", namespace,
                       "--scope", ",".join(scopes), "--interval", str(interval), "--parallel", str(parallel)]
            if pvp_only:
                command.append("--pvp-only")
            if pid is not None:
                command.extend(("--pid", str(pid)))
            with self._log_path.open("ab", buffering=0) as output:
                self._process = subprocess.Popen(command, cwd=str(self.root), env=_child_env(),
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, **_hidden_console())
            try:
                created = psutil.Process(self._process.pid).create_time()
            except psutil.NoSuchProcess:
                created = time.time()
            self._owned = {"pid": self._process.pid, "created_at": created}
            self._last_error = None
            self._log_offset = 0
            self._line_buffer = ""
            self._decoder.reset()
            self._events.put({"type": "log", "text": "开始采集；继续使用现有数据库和断点进度。"})
        return self.snapshot()

    def request_stop(self) -> None:
        """Ask exactly one verified collector to run its normal Ctrl+C cleanup."""
        if self._closed or self._stopping:
            return
        candidates = self._find_collectors()
        if not candidates:
            self._events.put({"type": "log", "text": "当前没有正在运行的采集程序。"})
            return
        if len(candidates) != 1 or _path_key(candidates[0]["out_dir"]) != _path_key(self.out_dir):
            raise RuntimeError("无法唯一确认目标采集程序，未发送停止信号。")
        signature = candidates[0]
        self._stopping = signature
        self._events.put({"type": "log", "text": "正在请求安全停止，等待当前数据保存与连接清理……"})
        def stop_worker():
            try:
                if WINDOWS:
                    command = [str(self.python_executable), "-B", str(Path(__file__).resolve()), "--signal-stop",
                               str(signature["pid"]), "--created-at", repr(signature["created_at"]),
                               "--root", str(self.root), "--db", str(self.db_path), "--out-dir", str(self.out_dir)]
                    completed = subprocess.run(command, cwd=str(self.root), env=_child_env(),
                        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
                        creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                    if completed.returncode:
                        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "停止请求失败")
                else:
                    process = psutil.Process(signature["pid"])
                    current = collector_signature(process, self.root, self.db_path)
                    if current is None or abs(current["created_at"] - signature["created_at"]) >= .01:
                        raise RuntimeError("采集进程已变化，未发送停止信号。")
                    process.send_signal(signal.SIGINT)
                self._events.put({"type": "log", "text": "停止请求已发送；正在等待采集器完成保存。"})
            except Exception as exc:
                self._stopping = None
                self._error(str(exc))
        self._stop_thread = threading.Thread(target=stop_worker, name="battle-stop-request", daemon=True)
        self._stop_thread.start()

    def _tail_log(self) -> None:
        if self._log_path is None or not self._log_path.exists():
            return
        if self._log_path.stat().st_size < self._log_offset:
            self._log_offset = 0
            self._decoder.reset()
            self._line_buffer = ""
        with self._log_path.open("rb") as handle:
            handle.seek(self._log_offset)
            data = handle.read(64 * 1024)
            self._log_offset = handle.tell()
        self._line_buffer += self._decoder.decode(data)
        lines = self._line_buffer.split("\n")
        self._line_buffer = lines.pop()
        for line in lines:
            self._events.put({"type": "log", "text": line.rstrip("\r")})
        if len(self._line_buffer) > 64 * 1024:
            self._events.put({"type": "log", "text": self._line_buffer})
            self._line_buffer = ""

    def _tail_external_diagnostics(self) -> None:
        path = self.out_dir / "collector_diagnostics.jsonl"
        if not path.is_file():
            return
        size = path.stat().st_size
        initial = self._diagnostic_offset is None
        if initial:
            self._diagnostic_offset = max(0, size - 16 * 1024)
        if size < self._diagnostic_offset:
            self._diagnostic_offset = 0
        with path.open("rb") as handle:
            handle.seek(self._diagnostic_offset)
            if initial and self._diagnostic_offset:
                handle.readline()  # Discard an incomplete first line at tail start.
            count = 0
            while count < 100:
                begin = handle.tell()
                line = handle.readline(64 * 1024 + 1)
                if not line or not line.endswith(b"\n"):
                    handle.seek(begin)
                    break
                count += 1
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if item.get("phase") in {"capture_failed", "cycle_failed"}:
                    self._events.put({"type": "error", "text": str(item.get("error") or "采集失败")})
                elif item.get("phase") == "capture_imported":
                    quality = "完整" if item.get("completeness") == "complete" else "待补齐"
                    self._events.put({"type": "log", "text": f"战报 {item.get('battleID', '')} 已保存（{quality}）"})
            self._diagnostic_offset = handle.tell()

    def drain_events(self, limit: int = 100) -> list[dict[str, Any]]:
        if self._closed:
            return []
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        try:
            self._tail_log()
            # Existing collectors have no owned stdout log; their structured
            # diagnostics still provide useful live progress and errors.
            if self._log_path is None:
                self._tail_external_diagnostics()
        except OSError as exc:
            self._error(f"暂时无法读取运行日志：{exc}")
        result = []
        while len(result) < limit:
            try:
                result.append(self._events.get_nowait())
            except queue.Empty:
                break
        return result

    def close(self) -> None:
        """Close GUI-side state only; an independent collection keeps running."""
        self._closed = True


def signal_verified_collector(pid: int, created_at: float, root: Path, db_path: Path, out_dir: Path) -> None:
    process = psutil.Process(pid)
    signature = collector_signature(process, root.resolve(), db_path.resolve())
    if signature is None or abs(signature["created_at"] - created_at) >= .01 or \
            _path_key(signature["out_dir"]) != _path_key(out_dir):
        raise RuntimeError("采集进程校验失败，未发送停止信号。")
    if not WINDOWS:
        process.send_signal(signal.SIGINT)
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.AttachConsole.argtypes = [wintypes.DWORD]
    kernel.AttachConsole.restype = wintypes.BOOL
    kernel.SetConsoleCtrlHandler.argtypes = [wintypes.LPVOID, wintypes.BOOL]
    kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    kernel.GetConsoleProcessList.restype = wintypes.DWORD
    kernel.FreeConsole()
    if not kernel.AttachConsole(pid):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel.SetConsoleCtrlHandler(None, True):
            raise ctypes.WinError(ctypes.get_last_error())
        # Revalidate after attaching, before broadcasting only to this console.
        current = collector_signature(psutil.Process(pid), root.resolve(), db_path.resolve())
        if current is None or abs(current["created_at"] - created_at) >= .01:
            raise RuntimeError("采集进程已变化，未发送停止信号。")
        capacity = 128
        while True:
            members = (wintypes.DWORD * capacity)()
            count = kernel.GetConsoleProcessList(members, capacity)
            if not count:
                raise ctypes.WinError(ctypes.get_last_error())
            if count <= capacity:
                break
            if count > 16384:
                raise RuntimeError("无法可靠确认控制台进程，未发送停止信号。")
            capacity = count
        allowed = {pid, os.getpid()} | _verified_launcher_ancestors(process, root)
        _validate_console_members(set(members[:count]), allowed)
        if not kernel.GenerateConsoleCtrlEvent(0, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        time.sleep(.2)
    finally:
        kernel.FreeConsole()


def _validate_console_members(members: set[int], allowed: set[int]) -> None:
    if not members or members - allowed:
        raise RuntimeError("该采集器与其他程序共享控制台，无法安全单独停止。请在原采集窗口按 Ctrl+C。")


def _verified_launcher_ancestors(process: Any, root: Path) -> set[int]:
    """Only explicit project launcher shells may share the Ctrl+C broadcast."""
    allowed = set()
    known = [str(root / name).replace("/", "\\").casefold() for name in
             ("start_battle_archive.cmd", "start_battle_archive.ps1", "battle_reports.py")]
    try:
        ancestors = process.parents()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return allowed
    for ancestor in ancestors:
        try:
            if ancestor.name().casefold() not in {"cmd.exe", "powershell.exe", "pwsh.exe"}:
                break
            args = ancestor.cmdline()
            command = " ".join(args).replace("/", "\\").casefold()
            verified = any(item in command for item in known)
            if not verified:
                # The existing desktop file is a tiny, marked forwarder. Reading
                # it is allowed; this helper never creates or updates it.
                for arg in args:
                    candidate = Path(arg.strip('" '))
                    if candidate.name.casefold() != "战报采集.cmd" or not candidate.is_file():
                        continue
                    content = candidate.read_text(encoding="utf-8-sig").casefold()
                    if "sanmou_battle_archive_desktop_forwarder_v1" in content and known[0] in content:
                        verified = True
                        break
            if not verified:
                break
            allowed.add(ancestor.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, UnicodeError):
            break
    return allowed


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-stop", type=int, required=True)
    parser.add_argument("--created-at", type=float, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        signal_verified_collector(args.signal_stop, args.created_at, args.root, args.db, args.out_dir)
        return 0
    except psutil.NoSuchProcess:
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
