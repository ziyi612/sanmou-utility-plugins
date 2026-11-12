"""Execute queued Lua snippets on an observed NSLG client Lua thread.

Uses exported lua_pcall and its actual game-provided Lua state, with no build
specific offsets. Jobs run after the original call on the same native thread.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import math
import sys
import threading
import time
from pathlib import Path
from typing import Any
import frida
import psutil

try:
    from .runtime_errors import RuntimeBridgeUnavailable, is_bridge_exception
except ImportError:  # Preserve direct-script entry points.
    from runtime_errors import RuntimeBridgeUnavailable, is_bridge_exception


def _visible_game_windows(candidate_pids: set[int]) -> dict[int, list[dict[str, Any]]]:
    """Read visible, unowned top-level game windows without activating them."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    windows: dict[int, list[dict[str, Any]]] = {}

    @callback_type
    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x80:  # GWL_EXSTYLE / WS_EX_TOOLWINDOW
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in candidate_pids:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        if not user32.GetWindowTextW(hwnd, title, len(title)) or not title.value.strip():
            return True
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        windows.setdefault(int(pid.value), []).append({
            'hwnd': int(hwnd), 'title': title.value, 'class_name': class_name.value})
        return True

    if not user32.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return windows


def find_game_process() -> dict[str, Any] | None:
    candidates = []
    for proc in psutil.process_iter(['pid', 'name', 'memory_info', 'create_time']):
        try:
            info = proc.info
            if info.get('name') not in {'com.bilibili.nslg.exe', 'com.bilibili.nslg', 'PCGamePlatform.exe'}:
                continue
            candidates.append({'pid': int(info['pid']), 'name': info['name'],
                'rss': int(getattr(info.get('memory_info'), 'rss', 0)),
                'create_time': float(info.get('create_time') or 0)})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    games = [item for item in candidates if item['name'].startswith('com.bilibili.nslg')]
    pool = games or candidates
    if len(pool) <= 1:
        return pool[0] if pool else None
    if sys.platform != 'win32':
        return max(pool, key=lambda item: item['rss'])
    pids = {item['pid'] for item in pool}
    try:
        windows = _visible_game_windows(pids)
    except OSError as exc:
        raise RuntimeError('Unable to inspect visible game windows; choose a process explicitly with --pid. '
                           f'Candidate PIDs: {sorted(pids)}') from exc
    visible = [item for item in pool if windows.get(item['pid'])]
    if len(visible) == 1:
        return {**visible[0], 'selection': 'visible_main_window', 'windows': windows[visible[0]['pid']]}
    details = [{'pid': item['pid'], 'windows': windows.get(item['pid'], [])} for item in (visible or pool)]
    raise RuntimeError('Ambiguous game processes: no unique visible main game window. '
                       f'Choose the intended game process with --pid. Candidates: {details}')


def build_agent_source() -> str:
    return r'''
const xlua = Process.getModuleByName('xlua.dll');
const address = name => xlua.getExportByName(name);
let lastNativeOperation = null;
const native = (name, result, args) => {
  const fn = new NativeFunction(address(name), result, args);
  return (...values) => { lastNativeOperation = name; return fn(...values); };
};
const load = native('luaL_loadbuffer', 'int', ['pointer', 'pointer', Process.pointerSize === 8 ? 'uint64' : 'uint32', 'pointer']);
const call = native('lua_pcall', 'int', ['pointer', 'int', 'int', 'int']);
const tolstring = native('lua_tolstring', 'pointer', ['pointer', 'int', 'pointer']);
const settop = native('lua_settop', 'void', ['pointer', 'int']);
const gettop = native('lua_gettop', 'int', ['pointer']);
const luatype = native('lua_type', 'int', ['pointer', 'int']);
const jobs = new Map(), queue = [], observed = new Map();
let nextId = 1, running = false, stopped = false;
let selected = null, lastObserved = null, lastTop = null, lastError = null;
let bridgeFailure = null;
function poison(error, L, threadId) {
  if (bridgeFailure !== null) return;
  lastError = String(error);
  bridgeFailure = {ok: false, stage: 'lua_hook', error: lastError,
    runtime_unavailable: true, nativeOperation: lastNativeOperation,
    rawL: L ? L.toString() : null, threadId};
  queue.length = 0;
  for (const [id, job] of jobs) if (!job.done)
    jobs.set(id, Object.assign({}, bridgeFailure, {id, done: true}));
}
function utf8Length(text) {
  let n = 0;
  for (const ch of text) {
    const cp = ch.codePointAt(0);
    n += cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
  }
  return n;
}
function readString(L, index) {
  const len = Memory.alloc(Process.pointerSize), p = tolstring(L, index, len);
  if (p.isNull()) return null;
  lastNativeOperation = 'readUtf8String';
  return p.readUtf8String(Process.pointerSize === 8 ? len.readU64().toNumber() : len.readU32());
}
function runLua(L, code) {
  const before = gettop(L);
  // A native exception can leave Lua's own call frames inconsistent. Restore
  // the stack only after an ordinary return, including normal Lua error codes.
  let returnedNormally = false;
  const done = result => { returnedNormally = true; return result; };
  const wrapped = ['local __codex_fn = function()', code, 'end',
    'local __codex_ok, __codex_ret = pcall(__codex_fn)',
    "if not __codex_ok then return '__LUA_ERROR__' .. tostring(__codex_ret) end",
    "if __codex_ret == nil then return '' end", 'return tostring(__codex_ret)'].join('\n');
  try {
    const text = Memory.allocUtf8String(wrapped), name = Memory.allocUtf8String('@codex_runtime');
    const loaded = load(L, text, utf8Length(wrapped), name);
    if (loaded !== 0) return done({ok: false, stage: 'loadbuffer', code: loaded, error: readString(L, -1)});
    const called = call(L, 0, 1, 0);
    if (called !== 0) return done({ok: false, stage: 'pcall', code: called, error: readString(L, -1)});
    const resultType = luatype(L, -1), result = readString(L, -1) || '';
    if (result.startsWith('__LUA_ERROR__')) return done({ok: false, stage: 'lua', error: result.slice(13)});
    return done({ok: true, result, resultType, rawL: L.toString()});
  } finally { if (returnedNormally) settop(L, before); }
}
const listener = Interceptor.attach(address('lua_pcall'), {
  onEnter(args) {
    this.skip = running || stopped || bridgeFailure !== null;
    if (!this.skip) { this.L = args[0]; this.threadId = Process.getCurrentThreadId(); }
  },
  onLeave() {
    if (this.skip || running || stopped || bridgeFailure !== null) return;
    const L = this.L;
    running = true;
    try {
      const key = L.toString() + ':' + this.threadId;
      let state = observed.get(key);
      if (!state) {
        state = {rawL: L.toString(), threadId: this.threadId, calls: 0, eligible: null, lastProbe: 0};
        observed.set(key, state);
      }
      state.calls++; lastObserved = state.rawL;
      if (queue.length === 0 || (selected !== null && selected !== key)) return;
      // Even diagnostic reads can fault; keep them in the poison boundary.
      // All Lua C API calls stay on the game's observed native thread.
      lastTop = gettop(L); state.luaTop = lastTop;
      if (selected === null) {
        if (Date.now() - state.lastProbe < 1000) return;
        state.lastProbe = Date.now();
        const p = runLua(L, "return (type(ClientProxy) == 'table' or type(ClientProxy) == 'userdata') and 'client' or 'other'");
        state.eligible = p.ok && p.result === 'client';
        if (!state.eligible) return;
        selected = key;
      }
      const id = queue.shift(), job = jobs.get(id);
      if (!job) return;
      job.stage = 'running';
      jobs.set(id, Object.assign(runLua(L, job.code), {id, done: true, threadId: this.threadId}));
    } catch (e) {
      poison(e, L, this.threadId);
    } finally { running = false; }
  }
});
rpc.exports = {
  info() {
    const chosen = selected === null ? null : observed.get(selected);
    return {backend: 'observed_lua_pcall', xluaBase: xlua.base.toString(),
      luaPcall: address('lua_pcall').toString(), rawL: chosen ? chosen.rawL : lastObserved,
      luaTop: chosen ? chosen.luaTop : lastTop, selectedThreadId: chosen ? chosen.threadId : null,
      selectedState: selected, states: Array.from(observed.values()), queuedJobs: queue.length, lastError,
      runtimeUnavailable: bridgeFailure !== null, bridgeFailure};
  },
  schedule(code) {
    if (stopped) throw new Error('runner stopped');
    const id = nextId++;
    if (bridgeFailure !== null) {
      jobs.set(id, Object.assign({}, bridgeFailure, {id, done: true})); return id;
    }
    jobs.set(id, {id, done: false, ok: false, stage: 'queued', code});
    queue.push(id); return id;
  },
  poll(id) {
    const job = jobs.get(id);
    if (!job) return {id, done: true, ok: false, stage: 'poll', error: 'job not found'};
    if (!job.done) return {id, done: false, ok: false, stage: job.stage};
    jobs.delete(id); return job;
  },
  cancel(id) {
    const job = jobs.get(id);
    if (job && job.done) { jobs.delete(id); return job; }
    const i = queue.indexOf(id); if (i >= 0) queue.splice(i, 1);
    jobs.delete(id);
    return {id, done: true, ok: false, stage: 'timeout', cancelled: true, error: 'queued job cancelled before execution'};
  },
  shutdown() { stopped = true; queue.length = 0; jobs.clear(); listener.detach(); return true; }
};
'''


@contextmanager
def frida_deadline(stage: str, timeout: float):
    """Cancel a blocked native operation on the calling thread's Frida context."""
    if timeout <= 0:
        raise TimeoutError(f'Game runtime {stage} timed out; check the running game and retry.')
    cancellable, expired = frida.Cancellable(), threading.Event()
    def cancel():
        expired.set()
        cancellable.cancel()
    timer = threading.Timer(timeout, cancel)
    timer.daemon = True
    timer.start()
    try:
        with cancellable:
            try:
                yield
            except frida.OperationCancelledError as exc:
                if expired.is_set():
                    raise TimeoutError(f'Game runtime {stage} timed out after {timeout:.2f}s; check the running game and retry.') from exc
                raise
        if expired.is_set():
            raise TimeoutError(f'Game runtime {stage} timed out after {timeout:.2f}s; check the running game and retry.')
    finally:
        timer.cancel()


class GameLuaRunner:
    def __init__(self, pid: int | None = None, startup_timeout: float = 15.0,
                 cleanup_timeout: float = 5.0) -> None:
        if any(not math.isfinite(value) or value <= 0 for value in (startup_timeout, cleanup_timeout)):
            raise ValueError('startup_timeout and cleanup_timeout must be finite and positive')
        self.startup_timeout, self.cleanup_timeout = startup_timeout, cleanup_timeout
        if pid is None:
            proc = find_game_process()
            if proc is None:
                raise RuntimeError('Game process not found. Start the NSLG client first.')
            self.process, pid = proc, int(proc['pid'])
        else:
            self.process = {'pid': pid, 'name': 'manual', 'rss': 0}
        self.pid, self.session, self.script = pid, None, None
        self._loaded = False
        self._unavailable_reason: str | None = None
        self._unavailable_result: dict[str, Any] | None = None

    def __enter__(self) -> 'GameLuaRunner':
        deadline = time.monotonic() + self.startup_timeout
        try:
            with frida_deadline('attach', deadline - time.monotonic()):
                self.session = frida.attach(self.pid)
            with frida_deadline('script creation', deadline - time.monotonic()):
                self.script = self.session.create_script(build_agent_source())
            with frida_deadline('script load', deadline - time.monotonic()):
                self.script.load()
                self._loaded = True
                self._unavailable_reason = None
                self._unavailable_result = None
        except BaseException:
            # Cleanup receives a fresh cancellation context; the startup context
            # has already unwound, and must not prevent detaching a created session.
            try:
                self.close()
            except Exception:
                pass
            raise
        return self

    def close(self) -> None:
        if self.script is not None:
            try:
                if self._loaded:
                    with frida_deadline('script shutdown', self.cleanup_timeout):
                        self.script.exports_sync.shutdown()
            except Exception:
                pass
            self.script = None
            self._loaded = False
        if self.session is not None:
            try:
                with frida_deadline('session detach', self.cleanup_timeout):
                    self.session.detach()
            finally:
                self.session = None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def info(self) -> dict[str, Any]:
        if self.script is None:
            raise RuntimeError('runner is not attached')
        try:
            result = dict(self.script.exports_sync.info())
        except Exception as exc:
            if not is_bridge_exception(exc):
                raise
            self._mark_bridge_error(exc, 'info')
            result = {'pid': self.pid, 'infoError': str(exc)}
        result.setdefault('pid', self.pid)
        result['sessionAvailable'] = self._unavailable_reason is None
        if self._unavailable_reason is not None:
            result['sessionUnavailableReason'] = self._unavailable_reason
            result['unavailableResult'] = dict(self._unavailable_result or {})
            if (self._unavailable_result or {}).get('cancelled'):
                result['cancelledJob'] = dict(self._unavailable_result or {})
        return result

    def _mark_cancelled_job(self, result: dict[str, Any]) -> None:
        if result.get('cancelled') and not result.get('ok') and self._unavailable_reason is None:
            self._unavailable_reason = (f'Game PID {self.pid}: '
                + str(result.get('error') or 'Queued Lua job was cancelled')
                + '; this runtime session is unavailable and must be reconnected')
            self._unavailable_result = {**result, 'pid': self.pid}

    def _mark_bridge_error(self, error: Exception, stage: str) -> None:
        if self._unavailable_reason is None:
            self._unavailable_reason = (f'Game PID {self.pid}: runtime bridge {stage} failed '
                f'({type(error).__name__}: {error}); this session must be reconnected')
            self._unavailable_result = {'pid': self.pid, 'stage': 'runtime_bridge',
                'operation': stage, 'error_type': type(error).__name__, 'error': str(error)}

    def _mark_hook_failure(self, result: dict[str, Any]) -> None:
        if result.get('stage') == 'lua_hook' and not result.get('ok') and self._unavailable_reason is None:
            self._unavailable_reason = (f'Game PID {self.pid}: native Lua hook failed '
                f'({result.get("error") or "unknown native error"}); '
                'this runtime session is unavailable and must be reconnected')
            self._unavailable_result = {**result, 'pid': self.pid, 'runtime_unavailable': True}

    def _require_available(self) -> None:
        if self._unavailable_reason is not None:
            error = RuntimeBridgeUnavailable(self._unavailable_reason)
            error.result = dict(self._unavailable_result or {})
            raise error

    def execute(self, code: str) -> dict[str, Any]:
        """Compatibility alias: always use the observed game thread, never Frida's thread."""
        return self.execute_on_update(code)

    def execute_on_update(self, code: str, timeout: float = 5.0, poll: float = .05) -> dict[str, Any]:
        self._require_available()
        if self.script is None:
            raise RuntimeError('runner is not attached')
        if not math.isfinite(timeout) or not math.isfinite(poll) or timeout <= 0 or poll <= 0:
            raise ValueError('timeout and poll must be positive')
        code.encode('utf-8')
        if '\0' in code:
            raise ValueError('Lua source must not contain NUL bytes')
        api = self.script.exports_sync
        try:
            job_id = int(api.schedule(code))
        except Exception as exc:
            if not is_bridge_exception(exc):
                raise
            self._mark_bridge_error(exc, 'schedule')
            self._require_available()
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                result = dict(api.poll(job_id))
                if result.get('done'):
                    self._mark_hook_failure(result)
                    self._mark_cancelled_job(result)
                    self._require_available()
                    return result
                time.sleep(min(poll, max(0, deadline - time.monotonic())))
            result = dict(api.cancel(job_id))
            self._mark_hook_failure(result)
            if result.get('cancelled'):
                result['error'] = f'Lua job timed out after {timeout:.2f}s; pending execution was cancelled'
            self._mark_cancelled_job(result)
            self._require_available()
            return result
        except RuntimeBridgeUnavailable:
            raise
        except BaseException as exc:
            if is_bridge_exception(exc):
                self._mark_bridge_error(exc, 'execute')
                self._require_available()
            try:
                self._mark_cancelled_job(dict(api.cancel(job_id)))
            except Exception:
                pass
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--code')
    parser.add_argument('--file', type=Path)
    parser.add_argument('--info', action='store_true')
    parser.add_argument('--main-thread', action='store_true', help='Compatibility flag; every job uses the game Lua thread.')
    args = parser.parse_args()
    if args.code is None and args.file is None and not args.info:
        parser.error('Pass --code, --file, or --info.')
    if args.code is not None and args.file is not None:
        parser.error('Use only one of --code or --file.')
    code = args.file.read_text(encoding='utf-8') if args.file else args.code
    with GameLuaRunner(args.pid) as runner:
        if code is not None:
            print(runner.execute_on_update(code))
        if args.info:
            print(runner.info())


if __name__ == '__main__':
    main()
