"""Desktop entry point; opening the window never starts collection automatically."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import traceback


ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description="三谋战报图形界面")
    parser.add_argument("--db", type=Path, default=ROOT / "data/database/battle_reports.db")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data/incoming/runtime")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--page", choices=("overview", "archive", "daily", "team_winrates", "opponents", "peerless", "logs"),
                        default="overview", help="打开界面后显示的页面，不会自动启动采集")
    parser.add_argument("--parallel", type=int, choices=range(1, 5),
                        help="并行预取窗口 1–4；未指定时读取 config/launcher.json 的 collector_parallelism")
    parser.add_argument("--pvp-only", action=argparse.BooleanOptionalAction, default=True,
                        help="默认仅采集 PVP；--no-pvp-only 包含 PVE，也可在窗口中切换")
    args = parser.parse_args(argv)
    if args.parallel is None:
        settings_path = ROOT / "config/launcher.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8-sig")) if settings_path.exists() else {}
            value = settings.get("collector_parallelism", 1)
        except (OSError, ValueError, AttributeError) as error:
            parser.error(f"Cannot read collector parallelism from {settings_path}: {error}")
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4:
            parser.error("config/launcher.json collector_parallelism must be an integer from 1 through 4")
        args.parallel = value
    if not 5 <= args.interval <= 3600:
        parser.error("--interval must be 5 through 3600 seconds")
    if args.pid is not None and args.pid <= 0:
        parser.error("--pid must be positive")
    temp = Path(os.environ.get("SANMOU_TEMP_DIR", str(ROOT / ".runtime" / "temp"))).expanduser().resolve()
    temp.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = str(temp)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    from battle_ui.backend import Controller
    from battle_ui.data import BattleData
    from battle_ui.app import BattleApp
    controller = Controller(ROOT, db_path=args.db, out_dir=args.out_dir)
    app = BattleApp(ROOT, controller, BattleData(args.db), interval=args.interval, game_pid=args.pid,
                    pvp_only=args.pvp_only, parallelism=args.parallel)
    if args.page != "overview":
        app.after(350, lambda: app.show_page(args.page))
    app.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        directory = ROOT / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        message = traceback.format_exc()
        (directory / "battle_gui_error.log").write_text(message, encoding="utf-8")
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "战报界面未能启动。错误已保存到：\n" + str(directory / "battle_gui_error.log"),
                                            "三谋战报中心", 0x10)
        raise SystemExit(1)
