"""Read the last finished capture from bounded, account-bound diagnostic tails."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import json
import math
from pathlib import Path


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return instant.timestamp() if instant.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _battle(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value) or None


def _timing(start, finish, *, battle_id, namespace, pid, cycle, outcome, error=None,
            elapsed=None, formal=False):
    began, ended = _timestamp(start), _timestamp(finish)
    duration = ended - began if began is not None and ended is not None else None
    if duration is None or not math.isfinite(duration) or duration < 0:
        return None
    if elapsed is not None:
        duration = elapsed
    return {"namespace": namespace, "pid": pid, "cycle": cycle, "battleID": battle_id,
            "elapsed_seconds": duration, "started_at": start, "finished_at": finish,
            "outcome": outcome, "complete": outcome == "complete",
            "error": error if isinstance(error, str) else None,
            "source": "collector" if formal else "recovered_log", "approximate": not formal}


def _recover(raw, *, namespace, pid, process_created_at):
    active = last = cycle = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            active = None
            continue
        if not isinstance(event, dict) or event.get("pid") != pid or isinstance(event.get("pid"), bool):
            continue
        if event.get("namespace") != namespace:
            active = None
            continue
        current_cycle = event.get("cycle")
        if not isinstance(current_cycle, int) or isinstance(current_cycle, bool) or current_cycle < 0:
            active = None
            continue
        if cycle != current_cycle:
            active = None  # Never pair a start with an end from a reconnect cycle.
            cycle = current_cycle
        phase = event.get("phase")
        at = event.get("at")
        stamp = _timestamp(at)
        if stamp is None or (process_created_at is not None and stamp < process_created_at):
            active = None
            continue
        battle_id = _battle(event.get("battleID"))
        if phase == "capture_finished":
            elapsed = _number(event.get("elapsed_seconds"))
            outcome, finish = event.get("outcome"), event.get("finished_at")
            ended = _timestamp(finish)
            if (battle_id is None or elapsed is None or elapsed < 0 or ended is None
                    or outcome not in {"complete", "partial", "failed"}):
                continue
            start = event.get("started_at")
            if start is None:
                try:
                    start = datetime.fromtimestamp(ended - elapsed, timezone.utc).isoformat()
                except (ValueError, OverflowError, OSError):
                    continue
            began = _timestamp(start)
            if began is None or (process_created_at is not None and began < process_created_at):
                continue
            timing = _timing(start, finish, battle_id=battle_id, namespace=namespace, pid=pid,
                             cycle=cycle, outcome=outcome, elapsed=elapsed, formal=True,
                             error=event.get("error"))
            if timing:
                last = timing
                if active and active["battleID"] == battle_id:
                    active = None
            continue
        if phase in {"capture_started", "page_received"}:
            # A partial carrier alone does not prove that its attempt ended.
            # The next sequential capture/page boundary confirms the last
            # imported partial; its timestamp remains an approximate endpoint.
            if active and active.get("partial") is not None:
                end = active["partial"]
                last = _timing(active["started_at"], end, battle_id=active["battleID"],
                               namespace=namespace, pid=pid, cycle=cycle, outcome="partial") or last
            active = ({"battleID": battle_id, "started_at": at, "partial": None}
                      if phase == "capture_started" and battle_id is not None else None)
            continue
        if not active or battle_id != active["battleID"]:
            if phase in {"cycle_failed", "cycle_finished"}:
                active = None
            continue
        if phase == "capture_imported":
            if event.get("completeness") == "partial":
                active["partial"] = at
                continue
            if event.get("completeness") != "complete":
                continue
            outcome = "complete"
        elif phase == "capture_failed":
            outcome = "failed"
        else:
            continue
        last = _timing(active["started_at"], at, battle_id=battle_id, namespace=namespace,
                       pid=pid, cycle=cycle, outcome=outcome, error=event.get("error")) or last
        active = None
    return last


class LastCaptureTimingReader:
    """No database/game access; cache unchanged files and bound every disk read.

    Supply the collector's creation time when known to exclude reused PIDs.
    Missing starts outside the tail are deliberately reported as unknown.
    """

    def __init__(self, *, max_tail_bytes=256 * 1024, cache_entries=8):
        if (not isinstance(max_tail_bytes, int) or isinstance(max_tail_bytes, bool) or max_tail_bytes <= 0
                or not isinstance(cache_entries, int) or isinstance(cache_entries, bool) or cache_entries <= 0):
            raise ValueError("Tail size and cache entries must be positive integers")
        self.max_tail_bytes = max_tail_bytes
        self.cache_entries = cache_entries
        self._cache = OrderedDict()

    def read(self, path, *, namespace: str, pid: int, process_created_at: float | None = None) -> dict | None:
        if not isinstance(namespace, str) or not namespace or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        created = _number(process_created_at) if process_created_at is not None else None
        if process_created_at is not None and created is None:
            return None
        try:
            target = Path(path).resolve()
            info = target.stat()
            key = (str(target), namespace, pid, created)
            signature = (info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino)
            cached = self._cache.get(key)
            if cached is not None and cached[0] == signature:
                self._cache.move_to_end(key)
                return dict(cached[1]) if cached[1] is not None else None
            offset = max(0, info.st_size - self.max_tail_bytes)
            with target.open("rb") as handle:
                handle.seek(max(0, offset - 1))
                raw = handle.read(min(info.st_size, self.max_tail_bytes + (1 if offset else 0)))
            if offset:
                raw = raw[1:] if raw[:1] == b"\n" else raw.partition(b"\n")[2]
            # The writer may currently be appending its final line.
            raw = raw[:raw.rfind(b"\n") + 1]
            result = _recover(raw, namespace=namespace, pid=pid, process_created_at=created)
        except (OSError, ValueError, TypeError):
            return None
        self._cache[key] = (signature, result)
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_entries:
            self._cache.popitem(last=False)
        return dict(result) if result is not None else None
