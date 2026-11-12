"""Publish flushed files despite short Windows reader-sharing conflicts."""
from __future__ import annotations

import os
from pathlib import Path
import time


def replace_with_retry(source: Path, destination: Path) -> None:
    """Atomically replace, retrying Windows access/sharing conflicts for 2s.

    Never remove the previous destination or the durable source on failure.
    Other errors propagate immediately, including permissions on other OSes.
    """
    deadline = time.monotonic() + 2.0
    delay = 0.02
    while True:
        try:
            source.replace(destination)
            return
        except OSError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.25)
