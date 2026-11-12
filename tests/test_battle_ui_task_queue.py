"""Exercise background coalescing without opening Tk, SQLite or the game."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import queue
from types import SimpleNamespace
import threading
import time

from battle_ui.app import BattleApp


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Harness:
    _submit = BattleApp._submit
    _start_operation = BattleApp._start_operation
    _poll = BattleApp._poll
    _account_changed = BattleApp._account_changed
    _select_initial_runtime_account = BattleApp._select_initial_runtime_account
    _render_overview = BattleApp._render_overview
    close = BattleApp.close

    def __init__(self, workers=3):
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=workers)
        self._events = queue.Queue()
        self._inflight, self._generation = set(), {}
        self._callbacks, self._futures = {}, {}
        self._active_generations, self._pending_operations = {}, {}
        self._next_overview = time.monotonic() + 3600
        self._poll_id, self._initial_refresh_id = None, None
        self.messages, self.logs, self.results, self.load_calls = [], [], [], []
        self._runtime = {}
        self._selected = None
        self.controller = SimpleNamespace(snapshot=lambda: {}, drain_events=lambda _: [], close=lambda: None)
        self._render_runtime = lambda _: None
        self.after = lambda *_: None
        self.after_cancel = lambda *_: None
        self.withdraw = lambda: None
        self.destroy = lambda: None
        self._page = "daily"
        self._account_initialized = False
        self._overview, self._account_map = {}, {"全部账号": None}
        self.account_var = Value("全部账号")
        self.account_box = SimpleNamespace(configure=lambda **_: None)
        self._render_metrics = lambda: None
        self.scope_checks = []
        for name in ("pages", "nav_buttons", "component_labels", "metric_vars", "scopes", "daily_metrics",
                     "roster_metrics", "peerless_metrics", "team_threshold_vars", "team_winrate_trees",
                     "team_winrate_texts", "team_winrate_page_vars", "team_winrate_previous",
                     "team_winrate_next", "team_tier_buttons"):
            setattr(self, name, {})

    def _message(self, text, error=False):
        self.messages.append((text, error))

    def _append_log(self, text):
        self.logs.append(text)

    def _namespace(self):
        return self._account_map.get(self.account_var.get())

    def load_daily(self):
        namespace = self._namespace()
        self.load_calls.append(namespace)
        self._submit("daily", lambda: namespace, self.results.append, replace=True)

    def wait_idle(self, key, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._poll()
            if key not in self._inflight and key not in self._active_generations:
                return
            time.sleep(.002)
        raise AssertionError(f"Background task {key} did not finish")


def test_repeated_refresh_runs_only_old_and_latest_and_keeps_runtime_available():
    view = Harness()
    release, started, runtime_ran = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def old():
        calls.append("old-account")
        started.set()
        release.wait(2)
        raise ValueError("obsolete timeout")
    try:
        view._submit("daily", old, view.results.append, replace=True)
        assert started.wait(1)
        for namespace in ("second-account", "third-account", "latest-account"):
            view._submit("daily", lambda value=namespace: calls.append(value) or value,
                         view.results.append, replace=True)
        assert len(view._active_generations) == len(view._pending_operations) == 1
        assert view._pending_operations["daily"][0] == 4
        view._submit("runtime", lambda: runtime_ran.set(), lambda _: None)
        assert runtime_ran.wait(1)
        assert calls == ["old-account"]
        release.set()
        view.wait_idle("daily")
        assert calls == ["old-account", "latest-account"]
        assert view.results == ["latest-account"]
        assert not any("obsolete timeout" in str(message) for message in view.messages + view.logs)
    finally:
        release.set()
        view.close()


def test_unstarted_executor_request_is_replaced_before_it_can_run():
    view = Harness(workers=1)
    release, started = threading.Event(), threading.Event()
    calls = []
    try:
        view._submit("blocking-other-page", lambda: started.set() or release.wait(2), lambda _: None)
        assert started.wait(1)
        view._submit("daily", lambda: calls.append("obsolete"), view.results.append, replace=True)
        old_future = view._futures["daily"]
        assert not old_future.running()
        view._submit("daily", lambda: calls.append("latest") or "latest", view.results.append, replace=True)
        assert old_future.cancelled()
        release.set()
        view.wait_idle("daily")
        assert calls == ["latest"] and view.results == ["latest"]
    finally:
        release.set()
        view.close()


def test_nonreplace_preserves_first_request_and_does_not_queue_a_duplicate():
    view = Harness()
    release, started = threading.Event(), threading.Event()
    def first():
        started.set()
        release.wait(2)
        return "first"
    try:
        view._submit("daily", first, view.results.append)
        assert started.wait(1)
        view._submit("daily", lambda: "duplicate", view.results.append)
        assert view._generation["daily"] == 1 and not view._pending_operations
        release.set()
        view.wait_idle("daily")
        assert view.results == ["first"]
    finally:
        release.set()
        view.close()


def test_generation_invalidation_discards_a_queued_refresh_without_starting_it():
    view = Harness()
    release, started = threading.Event(), threading.Event()
    calls = []
    try:
        view._submit("detail", lambda: started.set() or release.wait(2), view.results.append, replace=True)
        assert started.wait(1)
        view._submit("detail", lambda: calls.append("invalidated-page"), view.results.append, replace=True)
        view._generation["detail"] += 1
        view._inflight.discard("detail")
        release.set()
        view.wait_idle("detail")
        assert not calls and not view.results and not view._pending_operations
    finally:
        release.set()
        view.close()


def test_close_clears_latest_pending_refresh_and_never_starts_it():
    view = Harness()
    release, started = threading.Event(), threading.Event()
    calls = []
    timer = None
    try:
        view._submit("daily", lambda: started.set() or release.wait(2), view.results.append, replace=True)
        assert started.wait(1)
        view._submit("daily", lambda: calls.append("must-not-start"), view.results.append, replace=True)
        timer = threading.Timer(.03, release.set)
        timer.start()
        view.close()
        view._poll()
        assert view._closed and not calls
        assert not view._pending_operations and not view._callbacks and not view._futures
    finally:
        release.set()
        if timer is not None:
            timer.join()
        view.close()


def test_automatic_account_selection_reloads_after_an_old_all_account_failure():
    view = Harness()
    release, started = threading.Event(), threading.Event()
    namespace = "server:1/scenario:18/avatar:100"
    def old():
        started.set()
        release.wait(2)
        raise ValueError("obsolete all-account timeout")
    try:
        view._submit("daily", old, view.results.append, replace=True)
        assert started.wait(1)
        view._runtime = {"namespace": namespace}
        view._render_overview({"available": True, "accounts": [{"namespace": namespace}]})
        assert view._namespace() == namespace and view.load_calls == [namespace]
        release.set()
        view.wait_idle("daily")
        assert view.results == [namespace]
        assert not any("obsolete all-account timeout" in str(message) for message in view.messages + view.logs)
    finally:
        release.set()
        view.close()


def test_explicit_all_account_selection_is_not_overwritten_by_overview():
    view = Harness()
    namespace = "server:1/scenario:18/avatar:100"
    try:
        view._runtime = {"namespace": namespace}
        view.account_var.set("全部账号")
        view._account_changed()
        view._render_overview({"available": True, "accounts": [{"namespace": namespace}]})
        view._render_overview({"available": True, "accounts": [{"namespace": namespace}]})
        view.wait_idle("daily")
        assert view.account_var.get() == "全部账号" and view._namespace() is None
        assert view.load_calls == [None] and view.results == [None]
    finally:
        view.close()


def test_initial_account_selection_waits_for_late_runtime_identity():
    view = Harness()
    namespace = "server:1/scenario:18/avatar:100"
    try:
        view._render_overview({"available": True, "accounts": [{"namespace": namespace}]})
        assert not view._account_initialized and view._namespace() is None
        assert view.load_calls == []
        view._runtime = {"namespace": namespace}
        if view._select_initial_runtime_account():
            view._account_changed()
        view.wait_idle("daily")
        assert view._namespace() == namespace and view.results == [namespace]
        assert view._account_initialized and view.load_calls == [namespace]
        assert not view._select_initial_runtime_account()
    finally:
        view.close()


def test_late_runtime_identity_keeps_an_explicit_all_account_choice():
    view = Harness()
    namespace = "server:1/scenario:18/avatar:100"
    try:
        view._render_overview({"available": True, "accounts": [{"namespace": namespace}]})
        view._account_changed()
        view._runtime = {"namespace": namespace}
        assert not view._select_initial_runtime_account()
        view.wait_idle("daily")
        assert view.account_var.get() == "全部账号" and view.results == [None]
    finally:
        view.close()


def test_late_runtime_identity_does_not_interrupt_an_already_selected_report():
    view = Harness()
    namespace = "server:1/scenario:18/avatar:100"
    selected = {"namespace": "another-account", "battle_uid": "42"}
    try:
        view._render_overview({"available": True, "accounts": [{"namespace": namespace}]})
        view._selected = selected
        view._runtime = {"namespace": namespace}
        assert not view._select_initial_runtime_account()
        view._render_overview({"available": True, "accounts": [{"namespace": namespace}]})
        assert view._selected is selected and view._namespace() is None
        assert view._account_initialized and not view.load_calls
    finally:
        view.close()
