"""Desktop entry point; opening the window never starts collection automatically."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import traceback

from battle_ui.collection_settings import read_launcher_settings, saved_time_range
from capture.game_assets.collection_window import TIME_RANGE_HOURS


ROOT = Path(__file__).resolve().parent


def memory_storage_enabled(root=None):
    root = Path(root or ROOT)
    settings_path = root / "config/launcher.json"
    strict = False
    if settings_path.is_file():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError("启动配置必须为 JSON 对象。")
        strict = settings.get("storage_backend") == "quark-memory"
    default_path = root / "config/quark.local.json"
    selected_path = Path(os.environ.get("SANMOU_QUARK_CONFIG", default_path))
    # A custom provider path cannot conceal the persistent default strict marker.
    for path in {default_path, selected_path}:
        if path.is_file():
            configuration = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(configuration, dict):
                raise ValueError("夸克配置必须为 JSON 对象，已阻止不明确的存储模式。")
            strict = strict or configuration.get("storage_backend") == "quark-memory"
    if not settings_path.is_file() and not strict:
        raise ValueError("启动配置缺失，已拒绝本地保存；请先运行 tools/setup_quark.py --memory-only。")
    if strict and not selected_path.is_file():
        raise ValueError("严格云端模式的夸克配置缺失，已阻止回退到本地存储。")
    return strict


def main(argv=None):
    parser = argparse.ArgumentParser(description="三谋战报图形界面")
    parser.add_argument("--db", type=Path, default=ROOT / "data/database/battle_reports.db")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data/incoming/runtime")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--page", choices=("overview", "archive", "daily", "team_winrates", "opponents", "peerless", "cloud", "logs"),
                        default="overview", help="打开界面后显示的页面，不会自动启动采集")
    parser.add_argument("--parallel", type=int, choices=range(1, 5),
                        help="并行预取窗口 1–4；未指定时读取 config/launcher.json 的 collector_parallelism")
    parser.add_argument("--pvp-only", action=argparse.BooleanOptionalAction, default=True,
                        help="默认仅采集 PVP；--no-pvp-only 包含 PVE，也可在窗口中切换")
    parser.add_argument("--time-range", choices=tuple(TIME_RANGE_HOURS),
                        help="下载时段；未指定时读取上次选择，首次默认最近24小时")
    args = parser.parse_args(argv)
    memory_only = memory_storage_enabled()
    settings_path = ROOT / "config/launcher.json"
    try:
        settings = read_launcher_settings(settings_path) if args.parallel is None or args.time_range is None else {}
        if args.time_range is None:
            args.time_range = saved_time_range(settings)
    except (OSError, ValueError, AttributeError) as error:
        parser.error(f"Cannot read collector settings from {settings_path}: {error}")
    if args.parallel is None:
        value = settings.get("collector_parallelism", 1)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4:
            parser.error("config/launcher.json collector_parallelism must be an integer from 1 through 4")
        args.parallel = value
    if memory_only:
        args.parallel = 1
    if not 5 <= args.interval <= 3600:
        parser.error("--interval must be 5 through 3600 seconds")
    if args.pid is not None and args.pid <= 0:
        parser.error("--pid must be positive")
    temporary = str(Path(os.environ.get("SANMOU_TEMP_DIR", ROOT / ".runtime/temp")).resolve())
    os.environ["TEMP"] = os.environ["TMP"] = os.environ["TMPDIR"] = temporary
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    from battle_ui.app import BattleApp
    if memory_only:
        from battle_ui.memory_backend import MemoryCollectorController
        from battle_ui.cloud_data import CloudBattleData
        controller = MemoryCollectorController(ROOT, db_path=args.db, out_dir=args.out_dir)
        data = CloudBattleData()
    else:
        from battle_ui.backend import Controller
        from battle_ui.data import BattleData
        controller = Controller(ROOT, db_path=args.db, out_dir=args.out_dir)
        data = BattleData(args.db)
    app = BattleApp(ROOT, controller, data, interval=args.interval, game_pid=args.pid,
                    pvp_only=args.pvp_only, parallelism=args.parallel, time_range=args.time_range,
                    settings_path=settings_path)
    if args.page != "overview":
        app.after(350, lambda: app.show_page(args.page))
    app.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        message = traceback.format_exc()
        try:
            memory_only = memory_storage_enabled()
        except (OSError, ValueError):
            memory_only = True  # An unreadable mode must never enable a disk fallback.
        if memory_only:
            from parser.quark_cloud import public_result
            message = str(public_result(str(sys.exc_info()[1])))
            location = "严格云端模式未写入错误日志。"
        else:
            directory = ROOT / "logs"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "battle_gui_error.log").write_text(message, encoding="utf-8")
            location = "错误已保存到：\n" + str(directory / "battle_gui_error.log")
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "战报界面未能启动。" + location,
                                            "三谋战报中心", 0x10)
        raise SystemExit(1)
