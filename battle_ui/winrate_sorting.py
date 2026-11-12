"""Shared ordering and sample threshold for win-rate views and exports."""
from __future__ import annotations

import math


MIN_KNOWN_RESULTS = 20


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def known_result_count(row):
    return sum(_count(row.get(key)) for key in ("wins", "draws", "losses"))


def win_rate_sort_key(row):
    value = row.get("win_rate")
    valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
             and 0 <= value <= 1 and math.isfinite(value))
    return (not valid, -float(value) if valid else 0,
            -known_result_count(row), -_count(row.get("samples")))


def ranked_winrate_rows(rows, minimum=MIN_KNOWN_RESULTS):
    """Retain all original aggregates; select only eligible displayed results."""
    return sorted((row for row in rows if known_result_count(row) >= minimum), key=win_rate_sort_key)
