"""Keep standalone disk collectors from bypassing a configured RAM-only mode.

The desktop entry points have stricter setup requirements. Legacy standalone
tools may still run without configuration, but must never hide a persistent
memory-only marker behind an alternate provider or unreadable settings.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
_LOCAL_BACKENDS = {"local", "sqlite", "quark"}
_USE_CLOUD_ENTRY = "请使用 start_battle_archive.cmd 或 python -B battle_reports.py collect。"


def _memory_marker(path: Path, *, required: bool = False) -> bool:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        if not required:
            try:
                path.lstat()
            except FileNotFoundError:
                return False  # There is genuinely no optional configuration.
            except OSError:
                pass
            # A dangling link or another existing but unreadable entry is not
            # equivalent to an unconfigured legacy installation.
            raise ValueError("无法读取存储模式配置，已阻止旧入口本地采集。" + _USE_CLOUD_ENTRY) from None
        raise ValueError("指定的夸克配置不存在，已阻止旧入口本地采集。" + _USE_CLOUD_ENTRY) from None
    except (OSError, UnicodeError):
        raise ValueError("无法读取存储模式配置，已阻止旧入口本地采集。" + _USE_CLOUD_ENTRY) from None
    try:
        settings = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("存储模式配置不是有效 JSON，已阻止旧入口本地采集。" + _USE_CLOUD_ENTRY) from None
    if not isinstance(settings, dict):
        raise ValueError("存储模式配置必须为 JSON 对象，已阻止旧入口本地采集。" + _USE_CLOUD_ENTRY)
    if "storage_backend" not in settings:
        return False
    backend = settings["storage_backend"]
    if not isinstance(backend, str) or backend not in _LOCAL_BACKENDS | {"quark-memory"}:
        raise ValueError("存储模式配置无效，已阻止旧入口本地采集。" + _USE_CLOUD_ENTRY)
    return backend == "quark-memory"


def require_local_capture() -> None:
    """Raise before legacy CLI side effects when local report writes are unsafe.

    This is called only by standalone ``main`` functions. Capture libraries
    retain their explicit storage injection and local compatibility defaults.
    """
    paths = [(ROOT / "config/launcher.json", False), (ROOT / "config/quark.local.json", False)]
    selected = os.environ.get("SANMOU_QUARK_CONFIG")
    if selected is not None:
        if not selected.strip():
            raise ValueError("指定的夸克配置路径为空，已阻止旧入口本地采集。" + _USE_CLOUD_ENTRY)
        paths.append((Path(selected), True))
    strict = False
    for path, required in paths:
        strict = _memory_marker(path, required=required) or strict
    if strict:
        raise ValueError("当前配置为纯云端模式，旧入口会写入本地战报，已拒绝启动。" + _USE_CLOUD_ENTRY)
