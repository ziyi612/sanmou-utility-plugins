"""Persist the desktop download choice without replacing other launcher settings."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

from capture.game_assets.collection_window import DEFAULT_TIME_RANGE, TIME_RANGE_HOURS


def read_launcher_settings(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError("launcher settings must be a JSON object")
    return value


def saved_time_range(settings: dict) -> str:
    value = settings.get("collector_time_range", DEFAULT_TIME_RANGE)
    if not isinstance(value, str) or value not in TIME_RANGE_HOURS:
        raise ValueError("collector_time_range must be all, 24h, 72h or 7d")
    return value


def save_time_range(path: Path, value: str) -> None:
    if value not in TIME_RANGE_HOURS:
        raise ValueError("Unknown download time range")
    path = path.resolve()
    settings = read_launcher_settings(path)
    if settings.get("collector_time_range") == value:
        return
    settings["collector_time_range"] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
