"""Shared download windows, based only on battle timestamps from list rows."""
from __future__ import annotations

import math
from typing import Any


TIME_RANGE_HOURS = {"all": None, "24h": 24, "72h": 72, "7d": 168}
TIME_RANGE_LABELS = {"all": "全时段", "24h": "最近 24 小时", "72h": "最近 72 小时", "7d": "最近 7 天"}
DEFAULT_TIME_RANGE = "24h"


def validate_time_range(value: Any) -> str:
    if not isinstance(value, str) or value not in TIME_RANGE_HOURS:
        raise ValueError("下载时段必须为全时段、最近 24 小时、最近 72 小时或最近 7 天。")
    return value


def _unix_seconds(value: Any) -> float | None:
    # Current runtime list fields are numeric Unix seconds, including fractions.
    # The upper bound also prevents mistaking current millisecond epochs for seconds.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 < number < 253402300800 else None


def battle_timestamp(entry: dict[str, Any]) -> float | None:
    """Prefer observed battle end time; never substitute observation/capture time.

    One report can appear in several scopes. Use end timestamps whenever any
    valid end is present, then start timestamps only if no valid end exists.
    Conflicting observations at the chosen priority remain unclassified.
    """
    ends, starts = set(), set()
    observations = entry.get("observations", {})
    if not isinstance(observations, dict):
        return None
    for observed in observations.values():
        if not isinstance(observed, dict):
            continue
        row = observed.get("row")
        if not isinstance(row, dict):
            continue
        combat = row if observed.get("parent") is not None else row.get("combatInfo")
        if not isinstance(combat, dict):
            continue
        for field, values in (("endTimeStampTime", ends), ("timeStamp", starts)):
            stamp = _unix_seconds(combat.get(field))
            if stamp is not None:
                values.add(stamp)
    values = ends or starts
    return next(iter(values)) if len(values) == 1 else None
