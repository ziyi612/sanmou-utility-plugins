"""Collector control through pipes, with runtime status held only in RAM."""
from __future__ import annotations

from collections import deque
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import psutil

from capture.game_assets.collection_window import TIME_RANGE_HOURS


class MemoryCollectorController:
    memory_only = True

    def __init__(self, root, db_path=None, out_dir=None, python_executable=None):
        self.root = Path(root).resolve()
        # These legacy arguments identify the project; they are never opened.
        self.db_path = Path(db_path or self.root / "data/database/battle_reports.db").resolve()
        self.out_dir = Path(out_dir or self.root / "data/incoming/runtime").resolve()
        executable = Path(python_executable or sys.executable).resolve()
        if executable.name.casefold() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        self.python_executable = executable
        self._process = None
        self._lock = threading.RLock()
        self._events = deque(maxlen=500)
        self._raw = {}
        self._options = None
        self._last_error = None
        self._stopping = False
        self._closed = False

    def _other_collectors(self):
        found = []
        for process in psutil.process_iter(["name", "cmdline"]):
            try:
                if not str(process.info.get("name") or "").casefold().startswith(("python", "pypy")):
                    continue
                command = process.info.get("cmdline") or []
                for index, value in enumerate(command[:-1]):
                    if Path(value).name.casefold() == "battle_reports.py" and command[index + 1] == "collect" \
                            and Path(value).resolve() == self.root / "battle_reports.py":
                        if self._process is None or process.pid != self._process.pid:
                            found.append(process.pid)
                        break
            except (psutil.Error, OSError, ValueError):
                continue
        return found

    def _event(self, kind, text):
        from parser.quark_cloud import public_result
        with self._lock:
            self._events.append({"type": kind, "text": str(public_result(str(text)))[:1500]})

    def _consume(self, line, *, stderr=False):
        from parser.quark_cloud import public_result
        if len(line) > 65536:
            self._event("error", "采集器输出超过单条状态上限，未保留该条内容。")
            return
        try:
            item = json.loads(line)
        except ValueError:
            # Keep a bounded diagnostic only; raw report data is never echoed.
            if stderr and line.strip():
                self._event("error", line.strip())
            return
        if not isinstance(item, dict):
            return
        with self._lock:
            status = item.get("status") if isinstance(item.get("status"), dict) else item
            for key in ("phase", "current_battle_id", "last_progress_at", "last_capture", "game_pid",
                        "identity", "namespace", "current_scope", "cycle", "filter", "result"):
                if key in status:
                    self._raw[key] = public_result(status[key])
            error = item.get("error") or status.get("last_error")
            if error:
                self._last_error = str(public_result(str(error)))[:1500]
                self._events.append({"type": "error", "text": self._last_error})
            else:
                message = item.get("message") or item.get("phase")
                if isinstance(message, str) and message:
                    self._events.append({"type": "log", "text": str(public_result(message))[:1000]})

    def _read_pipe(self, process, pipe, stderr=False):
        try:
            while True:
                line = pipe.readline(65537)
                if not line:
                    break
                if len(line) > 65536 and not line.endswith("\n"):
                    while line and not line.endswith("\n"):
                        line = pipe.readline(65537)
                    self._event("error", "采集器输出超过单条状态上限，未保留该条内容。")
                    continue
                self._consume(line, stderr=stderr)
        finally:
            pipe.close()

    def snapshot(self):
        with self._lock:
            process = self._process
            owned = process is not None and process.poll() is None
            others = self._other_collectors() if not owned else []
            external = bool(others)
            running = owned or external
            if process is not None and not owned and process.returncode and not self._last_error:
                self._last_error = f"云端采集程序已退出（代码 {process.returncode}）。"
            stopping = owned and self._stopping
            phase = "external" if external else "stopping" if stopping else str(self._raw.get("phase") or "starting") if running else "failed" if self._last_error else "stopped"
            result = {"running": running, "owned": owned, "external": external,
                      "pid": others[0] if external else process.pid if owned else None, "phase": phase,
                      "status": "其他窗口的采集仍在运行，请在原窗口停止" if external else "等待当前云端校验完成后停止" if stopping else "云端采集中" if running else "采集失败" if self._last_error else "已停止",
                      "last_error": self._last_error, "state_read_error": None,
                      "can_start": not self._closed and not running, "can_stop": owned and not stopping,
                      "stopping": stopping, "status_stale": False, "other_collectors": len(others),
                      "raw_status": dict(self._raw), "collection_options": dict(self._options) if self._options else None,
                      "log_path": None, "diagnostic_path": None, "storage_backend": "quark-memory"}
            for key in ("current_battle_id", "last_progress_at", "last_capture", "game_pid", "identity", "namespace", "cycle"):
                result[key] = self._raw.get(key)
            result["scope"] = self._raw.get("current_scope")
            return result

    def start(self, options=None):
        options = dict(options or {})
        scopes = options.get("scopes", ["personal", "union", "star"])
        if not isinstance(scopes, (list, tuple)) or not scopes or any(item not in {"personal", "union", "star"} for item in scopes):
            raise ValueError("请选择个人、同盟或收藏范围。")
        interval = options.get("interval", 60)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 1 <= interval <= 3600:
            raise ValueError("采集间隔必须为 1 至 3600 秒。")
        if isinstance(options.get("parallel", 1), bool) or options.get("parallel", 1) != 1:
            raise ValueError("严格云端模式逐封上传和校验，并行数固定为 1。")
        pid = options.get("pid")
        if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
            raise ValueError("游戏进程 ID 必须为正整数。")
        time_range = options.get("time_range", "24h")
        if time_range not in TIME_RANGE_HOURS:
            raise ValueError("下载时段无效。")
        if not isinstance(options.get("pvp_only", True), bool):
            raise ValueError("PVP 选项必须为布尔值。")
        namespace = options.get("namespace", "auto")
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("账号命名空间不能为空。")
        with self._lock:
            if self._closed:
                raise RuntimeError("界面控制器已关闭。")
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("当前云端采集仍在运行。")
            if self._other_collectors():
                raise RuntimeError("当前项目已有采集器，已阻止重复启动。")
            command = [str(self.python_executable), "-B", "-u", str(self.root / "battle_reports.py"),
                       "collect", "--console", "--db", str(self.db_path), "--out-dir", str(self.out_dir),
                       "--namespace", namespace, "--scope", ",".join(dict.fromkeys(scopes)),
                       "--interval", str(interval), "--parallel", "1", "--time-range", time_range]
            if options.get("pvp_only", True):
                command.append("--pvp-only")
            if pid is not None:
                command.extend(["--pid", str(pid)])
            environment = os.environ.copy()
            temporary = str(Path(os.environ.get("SANMOU_TEMP_DIR", self.root / ".runtime/temp")).resolve())
            environment.update(TEMP=temporary, TMP=temporary, TMPDIR=temporary,
                               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
            process = subprocess.Popen(command, cwd=str(self.root), env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._process, self._options = process, {**options, "parallel": 1, "scopes": list(scopes),
                                                     "time_range": time_range, "interval": interval}
            self._raw, self._last_error, self._stopping = {}, None, False
            # Readers keep the hidden parent alive until normal cloud shutdown,
            # even after the visible Tk window has closed.
            for pipe, is_error in ((process.stdout, False), (process.stderr, True)):
                threading.Thread(target=self._read_pipe, args=(process, pipe, is_error),
                                 name="cloud-collector-pipe", daemon=False).start()
            self._events.append({"type": "log", "text": "云端采集已启动；战报和续采状态仅存运行内存及夸克。"})
            return self.snapshot()

    def request_stop(self):
        with self._lock:
            if self._process is None or self._process.poll() is not None or self._stopping:
                return
            try:
                self._process.stdin.write('{"command":"stop"}\n')
                self._process.stdin.flush()
            except (BrokenPipeError, OSError):
                # The collector can finish between poll() and the pipe write.
                if self._process.poll() is None:
                    raise RuntimeError("停止管道不可用，采集进程尚未退出；请保留当前窗口。")
                return
            self._stopping = True
            self._events.append({"type": "log", "text": "已请求正常停止；等待当前云端上传和校验完成。"})

    def drain_events(self, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._lock:
            result = []
            while self._events and len(result) < limit:
                result.append(self._events.popleft())
            return result

    def close(self):
        self.request_stop()
        with self._lock:
            self._closed = True
