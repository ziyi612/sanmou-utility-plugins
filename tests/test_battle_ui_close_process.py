"""Keep Tk teardown in the main thread, tested in an isolated subprocess."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


SCRIPT = r'''
import gc
from pathlib import Path
import threading
from battle_ui.app import BattleApp

class Controller:
    def __init__(self):
        self.running = True
        self.closed = False
        self.stops = 0
    def close(self):
        self.closed = True
    def request_stop(self):
        self.stops += 1
        self.running = False

ready, release, finished = threading.Event(), threading.Event(), threading.Event()

def operation():
    ready.set()
    if not release.wait(4):
        raise RuntimeError('Timed test worker was never released')
    # A worker may run Python's cyclic GC even though its operation and done
    # callback contain no Tk references. The main thread must still own Tk here.
    gc.collect()
    finished.set()
    return {'read_only_test': True}

controller = Controller()
app = BattleApp(Path.cwd(), controller, object())
app.withdraw()
app._submit('close-regression', operation, lambda result: None)
assert ready.wait(2)
timer = threading.Timer(.2, release.set)
timer.start()
app.close()
completed_before_close_returned = finished.is_set()
del app
assert finished.wait(6), 'GUI teardown let the worker collect live Tk objects'
timer.join(2)
gc.collect()
assert completed_before_close_returned, 'GUI returned before its bounded UI workers finished'
assert controller.closed and controller.running and controller.stops == 0
print('GUI_CLOSE_PRESERVES_COLLECTOR_AND_TK_THREAD', flush=True)
'''


def test_closing_with_running_query_releases_tk_on_main_thread_and_keeps_collector():
    env = os.environ.copy()
    env.update(TEMP=".runtime/test-temp", TMP=".runtime/test-temp", PYTHONDONTWRITEBYTECODE="1",
               PYTHONIOENCODING="utf-8")
    completed = subprocess.run([sys.executable, "-B", "-c", SCRIPT], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert completed.returncode == 0, (completed.stdout + "\n" + completed.stderr)[-5000:]
    assert "GUI_CLOSE_PRESERVES_COLLECTOR_AND_TK_THREAD" in completed.stdout
    assert "Variable.__del__" not in completed.stderr
    assert "Tcl_AsyncDelete" not in completed.stderr
    assert "main thread is not in main loop" not in completed.stderr
