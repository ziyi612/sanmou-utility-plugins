"""Native desktop view over the existing collector and read-only archive."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import copy
import json
import math
import os
import gc
import csv
import tempfile
from pathlib import Path
import queue
import time
import tkinter as tk
from tkinter import filedialog, font, ttk
from typing import Any

from .winrate_sorting import MIN_KNOWN_RESULTS, known_result_count, ranked_winrate_rows


BG = "#f3f5f9"
PANEL = "#ffffff"
NAVY = "#172b40"
TEXT = "#24364b"
MUTED = "#6d7d90"
BLUE = "#315ee8"
TEAL = "#16816b"
AMBER = "#a46b16"
RED = "#b83b43"
LINE = "#e4eaf2"
CHINA = timezone(timedelta(hours=8))
TEAM_TIER_LABELS = {"low": "低红", "medium": "中红", "high": "高满", "unknown": "未知/未分档"}
COMPONENTS = {"basic": "基础", "detail": "详情", "all": "事件", "statics": "统计", "snapshots": "快照"}
COMPLETENESS = {"complete": "组件完整", "partial": "待补齐", "legacy": "历史格式"}


def format_time(value: Any) -> str:
    if value in (None, ""):
        return "—"
    try:
        stamp = datetime.fromtimestamp(float(value), timezone.utc) if isinstance(value, (int, float)) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(CHINA).strftime("%m-%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError, OSError):
        return str(value)


def format_last_capture(value: Any, namespace: str | None = None) -> str:
    """Describe a finished attempt; an in-progress page is not a duration."""
    if not isinstance(value, dict):
        return "—"
    duration, battle_id = value.get("elapsed_seconds"), value.get("battleID")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return "—"
    try:
        duration = float(duration)
    except OverflowError:
        return "—"
    outcome_key, source = value.get("outcome"), value.get("source", "collector")
    if not isinstance(outcome_key, str) or not isinstance(source, str) or source not in {"collector", "recovered_log"}:
        return "—"
    outcome = {"complete": "完整", "partial": "待补齐", "failed": "失败"}.get(outcome_key)
    if (not math.isfinite(duration) or duration < 0 or not outcome
            or isinstance(battle_id, bool) or not isinstance(battle_id, (str, int))
            or not str(battle_id).strip()
            or (namespace and value.get("namespace") not in (None, namespace))):
        return "—"
    seconds = round(float(duration), 2)
    if seconds < 60:
        elapsed = f"{seconds:.2f} 秒"
    else:
        minutes = int(seconds // 60)
        elapsed = f"{minutes} 分 {seconds - minutes * 60:05.2f} 秒"
    approximate = "约 " if value.get("source") == "recovered_log" or value.get("approximate") is True else ""
    return f"{approximate}{elapsed} · {outcome} · 战报 {str(battle_id).strip()}"


def format_cycle_coverage(value: Any, pvp_only: bool) -> str | None:
    """Describe only an observed cycle, never all historical archive records."""
    if not isinstance(value, dict):
        return None
    filtering = value.get("filter")
    mode = "pvp_only" if pvp_only else "all"
    if not isinstance(filtering, dict) or filtering.get("mode") != mode:
        return None
    total, complete, finished = (value.get(key) for key in
                                 ("visibleReports", "completeVisibleReports", "finishedAt"))
    if (any(isinstance(number, bool) or not isinstance(number, int) or number < 0
            for number in (total, complete)) or complete > total
            or isinstance(finished, bool) or not isinstance(finished, (int, float))):
        return None
    try:
        if not math.isfinite(finished) or finished <= 0:
            return None
    except OverflowError:
        return None
    scope = "仅 PVP" if pvp_only else "全部类型"
    checked = "列表已读全" if value.get("enumerationComplete") is True else "列表未读全"
    unknown = filtering.get("unknownReports", 0)
    if isinstance(unknown, int) and not isinstance(unknown, bool) and unknown > 0:
        checked += f"，类型待确认 {unknown:,} 封"
    return (f"{scope} · 完整 {complete:,} / {total:,} 封 · {checked}"
            f" · {format_time(finished)}（最近一轮，不含被筛选的历史记录）")


def account_label(namespace: str) -> str:
    if namespace == "default":
        return "历史导入档案"
    parts = dict(item.split(":", 1) for item in namespace.split("/") if ":" in item)
    if "avatar" in parts:
        return f"{parts.get('server', '?')} 服 · 赛季 {parts.get('scenario', '?')} · 角色 {parts['avatar']}"
    return namespace


def pretty(value: Any, limit: int = 100_000) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) > limit:
        text = text[:limit] + "\n\n……界面仅预览前面的内容，导出原文可查看全部数据。"
    return text


class BattleApp(tk.Tk):
    """Tk stays on its own thread; all process/database work uses workers."""

    def __init__(self, root_path: Path, controller: Any, data: Any, *, interval: int = 60,
                 game_pid: int | None = None, daily_service=None, opponent_service=None,
                 peerless_service=None, team_winrate_service=None,
                 pvp_only: bool = True, parallelism: int = 1):
        super().__init__()
        self.root_path = Path(root_path)
        self.controller, self.data = controller, data
        if daily_service is None and hasattr(data, "db_path"):
            from .daily import DailyStats
            daily_service = DailyStats(data.db_path, own_union_only=True,
                                       runtime_dir=self.root_path / "data" / "incoming" / "runtime")
        if opponent_service is None and hasattr(data, "db_path"):
            from .opponents import OpponentRoster
            opponent_service = OpponentRoster(data.db_path)
        if peerless_service is None and hasattr(data, "db_path"):
            from .peerless import PeerlessRecords
            peerless_service = PeerlessRecords(data.db_path)
        if team_winrate_service is None and hasattr(data, "db_path"):
            from .team_winrates import TeamWinrateService
            team_winrate_service = TeamWinrateService(data.db_path)
        self.daily_service, self.opponent_service = daily_service, opponent_service
        self.peerless_service = peerless_service
        self.team_winrate_service = team_winrate_service
        self.game_pid = game_pid
        self.title("三谋战报中心")
        width, height = min(1380, self.winfo_screenwidth() - 60), min(790, self.winfo_screenheight() - 120)
        self.geometry(f"{width}x{height}+{max(0, (self.winfo_screenwidth()-width)//2)}+20")
        self.minsize(1050, 670)
        self.configure(bg=BG)
        self._closed = False
        self._events: queue.Queue = queue.Queue()
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="battle-ui")
        self._inflight: set[str] = set()
        self._generation: dict[str, int] = {}
        self._futures = {}
        self._callbacks = {}
        self._active_generations = {}
        self._pending_operations = {}
        self._overview: dict = {}
        self._runtime: dict = {}
        self._account_map = {"全部账号": None}
        self._account_initialized = False
        self._offset = 0
        self._has_more = False
        self._selected: dict | None = None
        self._detail: dict | None = None
        self._event_offset = 0
        self._rows: dict[str, dict] = {}
        self._daily_data = {}
        self._roster_data = {}
        self._daily_export_rows = []
        self._roster_export_rows = []
        self._roster_rows = {}
        self._peerless_data, self._peerless_rows = {}, {}
        self._peerless_export_rows = []
        self._team_winrate_data, self._team_winrate_rows = {}, {}
        self._team_winrate_context = None
        self.team_winrate_trees, self.team_winrate_texts = {}, {}
        self._team_winrate_offsets = {"without_skills": 0, "with_skills": 0}
        self.team_winrate_page_vars, self.team_winrate_previous, self.team_winrate_next = {}, {}, {}
        self.team_tier_buttons = {}
        self._team_skill_variants, self._team_variant_main = [], None
        self._team_variant_offset = 0
        self._next_overview = 0.0
        self._page = "overview"
        self._last_log_error = ""
        self._status_pending = False
        self._options_pid = None
        self._poll_id = None
        self.interval_var = tk.StringVar(value=str(interval))
        self.pid_var = tk.StringVar(value=str(game_pid) if game_pid is not None else "")
        self.parallel_var = tk.StringVar(value=str(parallelism))
        self.pvp_only_var = tk.BooleanVar(value=pvp_only)
        self.collection_mode_var = tk.StringVar(value="仅采集 PVP；PVE 和类型待确认的战报不下载正文。"
                                               if pvp_only else "采集全部类型，包含 PVP 和 PVE。")
        self.account_var = tk.StringVar(value="全部账号")
        self.filter_var = tk.StringVar(value="全部状态")
        self.search_var = tk.StringVar()
        self.message_var = tk.StringVar(value="正在读取现有档案……")
        self.title_var = tk.StringVar(value="采集总览")
        self.subtitle_var = tk.StringVar(value="采集当前登录角色；上方账号筛选仅用于查看已有档案。")
        self.phase_var = tk.StringVar(value="正在检查状态")
        self.current_var = tk.StringVar(value="—")
        self.last_capture_var = tk.StringVar(value="—")
        self.progress_var = tk.StringVar(value="—")
        self.scope_coverage_var = tk.StringVar(value="等待当前采集范围的一轮结果……")
        self._scope_coverage_context = None
        self._scope_coverage_text = None
        self.error_var = tk.StringVar()
        self.coverage_var = tk.StringVar(value="正在读取当前档案数量……")
        self.detail_title = tk.StringVar(value="选择一封战报查看详情")
        self.page_var = tk.StringVar(value="")
        self.event_page_var = tk.StringVar(value="")
        self.day_var = tk.StringVar(value="最近一天")
        self.player_search_var = tk.StringVar()
        self.daily_note_var = tk.StringVar(value="仅统计我盟成员的战报和武勋；同盟身份不明时不混计。")
        self.roster_day_var = tk.StringVar(value="正在读取最新战报日……")
        self.roster_note_var = tk.StringVar(value="高品：至少一将进阶≥3；满红：单将5阶／整队三将全5阶。")
        self.peerless_day_var = tk.StringVar(value="全部日期")
        self.peerless_search_var = tk.StringVar()
        self.peerless_note_var = tk.StringVar(value="只列明确记载无双的那次战报和当事人；保留所有已采集日期。")
        self.team_day_var = tk.StringVar(value="全部日期")
        self.team_tier_var = tk.StringVar(value="low")
        self.team_tier_summary_var = tk.StringVar(value="选择档位查看队伍")
        self.team_variant_var = tk.StringVar()
        self.team_variant_page_var = tk.StringVar(value="未选择队伍")
        self.team_battle_type_var = tk.StringVar(value="仅 PVP")
        self.team_threshold_vars = {key: tk.StringVar(value=str(value)) for key, value in
            (("high_red", 12), ("high_quality", 12), ("medium_red", 6), ("medium_quality", 6))}
        self.team_winrate_note_var = tk.StringVar(value="双方队伍均计入；胜率=胜/(胜+平+负)，平局率单列，未知结果不作胜负。")
        self.daily_metrics = {key: tk.StringVar(value="—") for key in ("reports", "players", "merit", "unknown")}
        self.roster_metrics = {key: tk.StringVar(value="—") for key in ("players", "teams", "full")}
        self.peerless_metrics = {key: tk.StringVar(value="—") for key in ("occurrences", "players", "reports")}
        self.scopes = {key: tk.BooleanVar(value=True) for key in ("personal", "union", "star")}
        self.metric_vars = {key: tk.StringVar(value="—") for key in ("total", "complete", "partial", "ratio")}
        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Control-r>", lambda _: self.refresh())
        self._initial_refresh_id = self.after(100, self.refresh)
        self._poll_id = self.after(150, self._poll)

    def _style(self):
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            font.nametofont(name).configure(family="Microsoft YaHei UI", size=10)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 8))
        style.configure("Primary.TButton", background=BLUE, foreground="white", borderwidth=0)
        style.map("Primary.TButton", background=[("disabled", "#aebbd8"), ("active", "#254dcc")])
        style.configure("Stop.TButton", foreground=RED)
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT)
        style.configure("TCombobox", padding=5)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=33, borderwidth=0, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", background="#edf1f7", foreground=TEXT,
                        padding=(8, 9), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#e5edff")], foreground=[("selected", "#173968")])
        style.configure("TNotebook", background=PANEL, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(13, 8))
        style.configure("Coverage.Horizontal.TProgressbar", troughcolor="#e7edf4", background=TEAL,
                        bordercolor="#e7edf4", lightcolor=TEAL, darkcolor=TEAL)

    def _build(self):
        shell = tk.Frame(self, bg=BG)
        shell.pack(fill="both", expand=True)
        sidebar = tk.Frame(shell, bg=NAVY, width=182)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="三谋\n战报中心", font=("Microsoft YaHei UI", 21, "bold"),
                 bg=NAVY, fg="white", justify="left").pack(anchor="w", padx=23, pady=(30, 4))
        tk.Label(sidebar, text="采集 · 归档 · 查阅", bg=NAVY, fg="#a4b6ca", font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=25, pady=(0, 28))
        self.nav_buttons = {}
        for key, title in (("overview", "采集总览"), ("archive", "战报档案"), ("daily", "每日统计"),
                           ("team_winrates", "队伍胜率"), ("opponents", "对手名单"),
                           ("peerless", "无双记录"), ("logs", "运行记录")):
            button = tk.Button(sidebar, text=title, anchor="w", command=lambda k=key: self.show_page(k),
                               relief="flat", bd=0, padx=22, pady=13, cursor="hand2",
                               bg=NAVY, activebackground="#28445f", fg="#d1dceb", activeforeground="white",
                               font=("Microsoft YaHei UI", 11))
            button.pack(fill="x", padx=10, pady=3)
            self.nav_buttons[key] = button
        tk.Label(sidebar, text="保留原有数据库\n断点续采 · 原文去重", bg=NAVY, fg="#a4b6ca",
                 justify="left", font=("Microsoft YaHei UI", 9), wraplength=150).pack(side="bottom", anchor="w", padx=24, pady=25)
        content = tk.Frame(shell, bg=BG)
        content.pack(side="left", fill="both", expand=True, padx=24, pady=(22, 12))
        header = tk.Frame(content, bg=BG)
        header.pack(fill="x", pady=(0, 16))
        heading = tk.Frame(header, bg=BG)
        heading.pack(side="left", fill="x", expand=True)
        tk.Label(heading, textvariable=self.title_var, bg=BG, fg=TEXT, font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        tk.Label(heading, textvariable=self.subtitle_var, bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(5, 0))
        ttk.Button(header, text="刷新", command=self.refresh).pack(side="right", padx=(10, 0))
        self.account_box = ttk.Combobox(header, textvariable=self.account_var, values=["全部账号"], state="readonly", width=39)
        self.account_box.pack(side="right")
        self.account_box.bind("<<ComboboxSelected>>", self._account_changed)
        self.pages = {}
        self.page_host = tk.Frame(content, bg=BG)
        self.page_host.pack(fill="both", expand=True)
        self.page_host.rowconfigure(0, weight=1)
        self.page_host.columnconfigure(0, weight=1)
        self._build_overview()
        self._build_archive()
        self._build_daily()
        self._build_team_winrates()
        self._build_opponents()
        self._build_peerless()
        self._build_logs()
        footer = tk.Frame(content, bg=BG)
        footer.pack(side="bottom", fill="x", pady=(10, 0), before=self.page_host)
        self.message_label = tk.Label(footer, textvariable=self.message_var, bg=BG, fg=MUTED,
                                      anchor="w", width=1, font=("Microsoft YaHei UI", 9))
        self.message_label.pack(side="left", fill="x", expand=True)
        tk.Label(footer, text="关闭界面后采集继续；用“停止采集”结束", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="right", before=self.message_label)
        self.show_page("overview", load=False)

    def _page_frame(self, key):
        frame = tk.Frame(self.page_host, bg=BG)
        frame.grid(row=0, column=0, sticky="nsew")
        self.pages[key] = frame
        return frame

    def _build_overview(self):
        page = self._page_frame("overview")
        cards = tk.Frame(page, bg=BG)
        cards.pack(fill="x", pady=(0, 17))
        for i, (key, caption, color) in enumerate((("total", "已发现战报", TEXT), ("complete", "组件完整", TEAL),
                                                  ("partial", "档案待补齐", AMBER), ("ratio", "已发现战报完整率", BLUE))):
            cards.columnconfigure(i, weight=1, uniform="metric")
            card = tk.Frame(cards, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 10, 0))
            tk.Label(card, text=caption, fg=MUTED, bg=PANEL, anchor="w", font=("Microsoft YaHei UI", 10)).pack(fill="x", padx=17, pady=(14, 4))
            tk.Label(card, textvariable=self.metric_vars[key], fg=color, bg=PANEL,
                     anchor="w", font=("Microsoft YaHei UI", 25, "bold")).pack(fill="x", padx=17, pady=(0, 13))
        panel = tk.Frame(page, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        panel.pack(fill="x")
        panel.columnconfigure(1, weight=1)
        tk.Label(panel, text="持续采集", bg=PANEL, fg=TEXT, font=("Microsoft YaHei UI", 14, "bold")).grid(row=0, column=0, sticky="w", padx=20, pady=(17, 8))
        self.phase_label = tk.Label(panel, textvariable=self.phase_var, bg="#eef3fb", fg=BLUE,
                                    padx=13, pady=5, font=("Microsoft YaHei UI", 10, "bold"))
        self.phase_label.grid(row=0, column=1, sticky="e", padx=20, pady=(17, 8))
        controls = tk.Frame(panel, bg=PANEL)
        controls.grid(row=1, column=0, columnspan=2, sticky="ew", padx=20, pady=(4, 14))
        self.scope_checks = []
        for key, label in (("personal", "个人"), ("union", "同盟"), ("star", "收藏")):
            check = ttk.Checkbutton(controls, text=label, variable=self.scopes[key])
            check.pack(side="left", padx=(0, 16))
            self.scope_checks.append(check)
        tk.Label(controls, text="每轮结束后等待", bg=PANEL, fg=MUTED).pack(side="left", padx=(14, 4))
        self.interval_box = ttk.Spinbox(controls, from_=5, to=3600, increment=5, textvariable=self.interval_var, width=6)
        self.interval_box.pack(side="left")
        tk.Label(controls, text="秒", bg=PANEL, fg=MUTED).pack(side="left", padx=5)
        tk.Label(controls, text="游戏进程 ID（留空自动）", bg=PANEL, fg=MUTED).pack(side="left", padx=(15, 5))
        self.pid_box = ttk.Entry(controls, textvariable=self.pid_var, width=8)
        self.pid_box.pack(side="left")
        actions = tk.Frame(panel, bg=PANEL)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 17))
        self.start_button = ttk.Button(actions, text="开始采集", style="Primary.TButton", command=self.start_collection)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止采集", style="Stop.TButton", command=self.stop_collection, state="disabled")
        self.stop_button.pack(side="left", padx=10)
        self.pvp_only_check = ttk.Checkbutton(actions, text="仅采集 PVP（玩家对战）",
                                              variable=self.pvp_only_var, command=self._collection_mode_changed)
        self.pvp_only_check.pack(side="left", padx=(12, 0))
        tk.Label(actions, text="并行数", bg=PANEL, fg=MUTED).pack(side="left", padx=(20, 6))
        self.parallel_box = ttk.Combobox(actions, textvariable=self.parallel_var, values=("1", "2", "3", "4"),
                                        width=3, state="readonly")
        self.parallel_box.pack(side="left")
        ttk.Button(actions, text="查看战报", command=lambda: self.show_page("archive")).pack(side="right")
        for row, label, var in ((3, "当前战报", self.current_var),
                                (4, "上一封采集用时", self.last_capture_var),
                                (5, "最近进度", self.progress_var)):
            tk.Label(panel, text=label, bg=PANEL, fg=MUTED).grid(row=row, column=0, sticky="w", padx=20, pady=7)
            tk.Label(panel, textvariable=var, bg=PANEL, fg=TEXT, anchor="w", wraplength=760).grid(row=row, column=1, sticky="ew", padx=20, pady=7)
        self.error_label = tk.Label(panel, textvariable=self.error_var, bg="#fff1f0", fg=RED,
                                    justify="left", anchor="w", wraplength=850, padx=12, pady=8)
        self.error_label.grid(row=6, column=0, columnspan=2, sticky="ew", padx=20, pady=(5, 13))
        self.error_label.grid_remove()
        tk.Label(panel, textvariable=self.collection_mode_var, bg=PANEL, fg=MUTED,
                 anchor="w", font=("Microsoft YaHei UI", 9)).grid(row=7, column=0, columnspan=2, sticky="ew", padx=20, pady=(3, 14))
        tk.Label(panel, textvariable=self.scope_coverage_var, bg=PANEL, fg=TEXT,
                 anchor="w", justify="left", wraplength=850,
                 font=("Microsoft YaHei UI", 9)).grid(row=8, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 14))
        bottom = tk.Frame(page, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        bottom.pack(fill="x", pady=(17, 0))
        tk.Label(bottom, text="档案完整度", bg=PANEL, fg=TEXT, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=20, pady=(15, 8))
        self.coverage_bar = ttk.Progressbar(bottom, style="Coverage.Horizontal.TProgressbar", maximum=100)
        self.coverage_bar.pack(fill="x", padx=20)
        tk.Label(bottom, textvariable=self.coverage_var, bg=PANEL, fg=MUTED, anchor="w").pack(fill="x", padx=20, pady=(9, 14))
        tk.Label(page, text="完整率针对库中所有已发现战报；PVP 筛选只限制后续下载，历史档案保留。",
                 bg=BG, fg=MUTED, anchor="w", wraplength=950, font=("Microsoft YaHei UI", 9)).pack(fill="x", pady=(12, 0))

    def _build_archive(self):
        page = self._page_frame("archive")
        filters = tk.Frame(page, bg=BG)
        filters.pack(fill="x", pady=(0, 12))
        box = ttk.Combobox(filters, textvariable=self.filter_var, values=["全部状态", "组件完整", "待补齐", "历史格式"], width=12, state="readonly")
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda _: self.load_reports(reset=True))
        search = ttk.Entry(filters, textvariable=self.search_var, width=25)
        search.pack(side="left", padx=10)
        search.bind("<Return>", lambda _: self.load_reports(reset=True))
        ttk.Button(filters, text="按战报 ID 查找", command=lambda: self.load_reports(reset=True)).pack(side="left")
        ttk.Button(filters, text="重置", command=self.reset_filter).pack(side="left", padx=8)
        panes = ttk.Panedwindow(page, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = tk.Frame(panes, bg=PANEL)
        right = tk.Frame(panes, bg=PANEL, padx=13, pady=12)
        panes.add(left, weight=3)
        panes.add(right, weight=2)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        self.report_tree = ttk.Treeview(left, columns=("id", "time", "players", "events", "status"), show="headings", selectmode="browse")
        for key, label, width in (("id", "战报 ID", 159), ("time", "战斗 / 入库时间", 134), ("players", "双方", 175),
                                   ("events", "事件", 65), ("status", "完整度", 91)):
            self.report_tree.heading(key, text=label)
            self.report_tree.column(key, width=width, minwidth=55, stretch=key == "players", anchor="e" if key == "events" else "w")
        self.report_tree.grid(row=0, column=0, sticky="nsew")
        sy = ttk.Scrollbar(left, orient="vertical", command=self.report_tree.yview)
        sy.grid(row=0, column=1, sticky="ns")
        sx = ttk.Scrollbar(left, orient="horizontal", command=self.report_tree.xview)
        sx.grid(row=1, column=0, sticky="ew")
        self.report_tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.report_tree.tag_configure("complete", foreground=TEAL)
        self.report_tree.tag_configure("partial", foreground=AMBER)
        self.report_tree.tag_configure("legacy", foreground=MUTED)
        self.report_tree.bind("<<TreeviewSelect>>", self._select_report)
        pager = tk.Frame(left, bg=PANEL)
        pager.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=9)
        self.prev_button = ttk.Button(pager, text="上一页", command=lambda: self.change_page(-1), state="disabled")
        self.prev_button.pack(side="left")
        self.next_button = ttk.Button(pager, text="下一页", command=lambda: self.change_page(1), state="disabled")
        self.next_button.pack(side="left", padx=6)
        tk.Label(pager, textvariable=self.page_var, bg=PANEL, fg=MUTED).pack(side="right")
        tk.Label(right, textvariable=self.detail_title, bg=PANEL, fg=TEXT, wraplength=360,
                 justify="left", font=("Microsoft YaHei UI", 11, "bold")).pack(fill="x", pady=(0, 10))
        chips = tk.Frame(right, bg=PANEL)
        chips.pack(fill="x", pady=(0, 10))
        self.component_labels = {}
        for i, (key, label) in enumerate(COMPONENTS.items()):
            chips.columnconfigure(i, weight=1)
            chip = tk.Label(chips, text=label, bg="#eef1f5", fg=MUTED, pady=5, font=("Microsoft YaHei UI", 9))
            chip.grid(row=0, column=i, sticky="ew", padx=(0, 3))
            self.component_labels[key] = chip
        tabs = ttk.Notebook(right)
        tabs.pack(fill="both", expand=True)
        info = tk.Frame(tabs, bg=PANEL)
        events = tk.Frame(tabs, bg=PANEL)
        raw = tk.Frame(tabs, bg=PANEL)
        tabs.add(info, text="概览")
        tabs.add(events, text="事件")
        tabs.add(raw, text="字段预览")
        self.info_text = self._text(info)
        epager = tk.Frame(events, bg=PANEL)
        epager.pack(side="bottom", fill="x", pady=5)
        self.event_prev = ttk.Button(epager, text="上一页", command=lambda: self.change_events(-1), state="disabled")
        self.event_prev.pack(side="left")
        self.event_next = ttk.Button(epager, text="下一页", command=lambda: self.change_events(1), state="disabled")
        self.event_next.pack(side="left", padx=4)
        tk.Label(epager, textvariable=self.event_page_var, bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side="right")
        self.events_text = self._text(events)
        self.raw_text = self._text(raw, mono=True)
        self.export_button = ttk.Button(right, text="导出完整原文…", command=self.export_selected, state="disabled")
        self.export_button.pack(side="bottom", fill="x", pady=(10, 0), before=tabs)
        tk.Label(page, text="每封只展示当前最完整版本；事件按页读取，导出原文包含所有字段。", bg=BG,
                 fg=MUTED, anchor="w", font=("Microsoft YaHei UI", 9)).pack(side="bottom", fill="x", pady=(8, 0), before=panes)

    def _build_logs(self):
        page = self._page_frame("logs")
        bar = tk.Frame(page, bg=BG)
        bar.pack(fill="x", pady=(0, 12))
        tk.Label(bar, text="采集输出和连接错误", bg=BG, fg=TEXT, font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        ttk.Button(bar, text="打开日志目录", command=self.open_logs).pack(side="right")
        ttk.Button(bar, text="清空显示", command=lambda: self._set_text(self.log_text, "")).pack(side="right", padx=8)
        self.log_text = self._text(page, mono=True)
        tk.Label(page, text="清空只影响窗口显示，磁盘日志保留。旧运行状态不会作为当前正在采集的证明。",
                 bg=BG, fg=MUTED, anchor="w", font=("Microsoft YaHei UI", 9)).pack(fill="x", pady=(8, 0))

    def _small_metrics(self, parent, definitions, variables):
        bar = tk.Frame(parent, bg=BG)
        for index, (key, caption, color) in enumerate(definitions):
            bar.columnconfigure(index, weight=1, uniform="stats")
            card = tk.Frame(bar, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
            card.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 8, 0))
            tk.Label(card, text=caption, bg=PANEL, fg=MUTED, anchor="w").pack(fill="x", padx=13, pady=(10, 2))
            tk.Label(card, textvariable=variables[key], bg=PANEL, fg=color, anchor="w",
                     font=("Microsoft YaHei UI", 20, "bold")).pack(fill="x", padx=13, pady=(0, 9))
        return bar

    def _table(self, parent, columns, height=8):
        panel = tk.Frame(parent, bg=PANEL)
        panel.rowconfigure(0, weight=1)
        panel.columnconfigure(0, weight=1)
        tree = ttk.Treeview(panel, columns=[row[0] for row in columns], show="headings", height=height, selectmode="browse")
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=55, stretch=True)
        tree.grid(row=0, column=0, sticky="nsew")
        sy = ttk.Scrollbar(panel, orient="vertical", command=tree.yview)
        sx = ttk.Scrollbar(panel, orient="horizontal", command=tree.xview)
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        return panel, tree

    def _build_daily(self):
        page = self._page_frame("daily")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(4, weight=1)
        bar = tk.Frame(page, bg=BG)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        tk.Label(bar, text="战斗日期", bg=BG, fg=MUTED).pack(side="left", padx=(0, 6))
        self.day_box = ttk.Combobox(bar, textvariable=self.day_var, values=["最近一天"], state="readonly", width=15)
        self.day_box.pack(side="left")
        self.day_box.bind("<<ComboboxSelected>>", lambda _: self._filter_daily())
        ttk.Entry(bar, textvariable=self.player_search_var, width=20).pack(side="left", padx=10)
        ttk.Button(bar, text="筛选玩家姓名 / ID", command=self._filter_daily).pack(side="left")
        self.daily_export_button = ttk.Button(bar, text="导出当前武勋表…", command=self.export_daily, state="disabled")
        self.daily_export_button.pack(side="right")
        cards = self._small_metrics(page, [("reports", "当天归档战报", TEXT), ("players", "记录玩家", BLUE),
                                          ("merit", "已记录武勋合计", TEAL), ("unknown", "武勋字段缺失场次", AMBER)], self.daily_metrics)
        cards.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        panel, self.days_tree = self._table(page, [("date", "北京时间日期", 130), ("total", "战报", 75),
            ("complete", "完整", 75), ("partial", "待补齐", 75), ("personal", "个人", 75),
            ("union", "同盟", 75), ("star", "收藏", 75), ("unknown", "来源未明", 90)], height=3)
        panel.grid(row=2, column=0, sticky="ew")
        self.days_tree.bind("<<TreeviewSelect>>", self._daily_day_selected)
        tk.Label(page, text="按玩家 ID 汇总；父汇总战报不与子战报重复累加。", bg=BG, fg=MUTED,
                 anchor="w").grid(row=3, column=0, sticky="ew", pady=8)
        panel, self.players_tree = self._table(page, [("date", "战斗日期", 110), ("name", "玩家", 145),
            ("uid", "玩家 ID", 135), ("battles", "结算场次", 80), ("merit", "已记录武勋", 100),
            ("unknown", "缺武勋字段", 90), ("coverage", "战报覆盖", 100), ("account", "服务器 / 角色", 160)], height=5)
        panel.grid(row=4, column=0, sticky="nsew")
        tk.Label(page, textvariable=self.daily_note_var, bg=BG, fg=MUTED, anchor="w", justify="left",
                 wraplength=1000, font=("Microsoft YaHei UI", 9)).grid(row=5, column=0, sticky="ew", pady=(8, 0))

    def _build_team_winrates(self):
        page = self._page_frame("team_winrates")
        bar = tk.Frame(page, bg=BG)
        bar.pack(fill="x", pady=(0, 8))
        tk.Label(bar, text="战斗日期", bg=BG, fg=MUTED).pack(side="left", padx=(0, 6))
        self.team_day_box = ttk.Combobox(bar, textvariable=self.team_day_var,
                                       values=["全部日期"], state="readonly", width=15)
        self.team_day_box.pack(side="left")
        self.team_day_box.bind("<<ComboboxSelected>>", self.load_team_winrates)
        self.team_battle_type_box = ttk.Combobox(bar, textvariable=self.team_battle_type_var,
            values=["仅 PVP", "全部战斗"], state="readonly", width=12)
        self.team_battle_type_box.pack(side="left", padx=10)
        self.team_battle_type_box.bind("<<ComboboxSelected>>", self.load_team_winrates)
        ttk.Button(bar, text="重新统计", command=self.load_team_winrates).pack(side="left")
        self.team_winrate_export_button = ttk.Menubutton(bar, text="导出 Excel / CSV…", state="disabled")
        self.team_winrate_export_menu = tk.Menu(self.team_winrate_export_button, tearoff=False)
        self.team_winrate_export_menu.add_command(label="Excel：全部档位的达标队伍与配置…", command=self.export_team_winrates_excel)
        self.team_winrate_export_menu.add_command(label="CSV：当前档位与当前表…", command=self.export_team_winrates)
        self.team_winrate_export_button.configure(menu=self.team_winrate_export_menu)
        self.team_winrate_export_button.pack(side="right")
        thresholds = tk.Frame(page, bg=BG)
        thresholds.pack(fill="x", pady=(0, 8))
        for key, label in (("high_red", "高满总红 ≥"), ("high_quality", "且总品 ≥"),
                           ("medium_red", "中红总红 ≥"), ("medium_quality", "且总品 ≥")):
            tk.Label(thresholds, text=label, bg=BG, fg=MUTED).pack(side="left", padx=(0 if key == "high_red" else 9, 4))
            ttk.Spinbox(thresholds, textvariable=self.team_threshold_vars[key], from_=0, to=15,
                        increment=1, width=4).pack(side="left")
        ttk.Button(thresholds, text="应用门槛", command=self.load_team_winrates).pack(side="left", padx=10)
        tk.Label(page, text="自定义分档门槛，可调整。先判断高满两项均达标，再判断中红，其余低红；属性缺失单列未知。",
                 bg=BG, fg=MUTED, anchor="w", wraplength=1000, font=("Microsoft YaHei UI", 9)).pack(fill="x", pady=(0, 8))
        tiers = tk.Frame(page, bg=BG)
        tiers.pack(fill="x", pady=(0, 8))
        for tier, title in TEAM_TIER_LABELS.items():
            button = tk.Button(tiers, text=title, command=lambda selected=tier: self._filter_team_winrate_tier(selected),
                relief="flat", padx=14, pady=7, cursor="hand2", bg=BLUE if tier == "low" else PANEL,
                fg="white" if tier == "low" else TEXT, font=("Microsoft YaHei UI", 10))
            button.pack(side="left", padx=(0, 7))
            self.team_tier_buttons[tier] = button
        tk.Label(tiers, textvariable=self.team_tier_summary_var, bg=BG, fg=MUTED).pack(side="right")
        self.team_winrate_note_label = tk.Label(page, textvariable=self.team_winrate_note_var, bg=BG, fg=MUTED,
            justify="left", anchor="w", wraplength=1000, font=("Microsoft YaHei UI", 9))
        self.team_winrate_note_label.pack(side="bottom", fill="x", pady=(8, 0))
        self.team_winrate_tabs = ttk.Notebook(page)
        self.team_winrate_tabs.pack(fill="both", expand=True)
        self.team_winrate_tabs.bind("<<NotebookTabChanged>>", self._team_winrate_tab_changed)
        columns = [("team", "队伍", 235), ("tier", "分档", 80), ("attributes", "总红 / 总品", 115),
                   ("samples", "参战", 65), ("wins", "胜", 55), ("draws", "平", 55),
                   ("losses", "负", 55), ("unknown", "未知", 60), ("rate", "胜率", 85), ("draw_rate", "平局率", 85)]
        for mode, title in (("without_skills", "不区分战法"), ("with_skills", "区分战法")):
            tab = tk.Frame(self.team_winrate_tabs, bg=BG)
            self.team_winrate_tabs.add(tab, text=title)
            details = tk.Frame(tab, bg=PANEL)
            details.pack(side="bottom", fill="x", pady=(7, 0))
            text = self._text(details)
            text.configure(height=4)
            self.team_winrate_texts[mode] = text
            if mode == "with_skills":
                variants = tk.Frame(details, bg=PANEL)
                variants.pack(side="top", fill="x", before=text.master)
                tk.Label(variants, text="达标战法配置", bg=PANEL, fg=MUTED).pack(side="left", padx=(7, 6))
                self.team_variant_box = ttk.Combobox(variants, textvariable=self.team_variant_var,
                    state="disabled", width=53, values=[])
                self.team_variant_box.pack(side="left", fill="x", expand=True)
                self.team_variant_box.bind("<<ComboboxSelected>>", self._team_skill_variant_selected)
                self.team_variant_previous = ttk.Button(variants, text="前20项", state="disabled",
                    command=lambda: self.change_team_skill_variant_page(-1))
                self.team_variant_next = ttk.Button(variants, text="后20项", state="disabled",
                    command=lambda: self.change_team_skill_variant_page(1))
                self.team_variant_next.pack(side="right", padx=(5, 0))
                self.team_variant_previous.pack(side="right", padx=(5, 0))
                tk.Label(details, textvariable=self.team_variant_page_var, bg=PANEL, fg=MUTED,
                         anchor="w").pack(side="bottom", fill="x", padx=8)
            paging = tk.Frame(tab, bg=BG)
            paging.pack(side="bottom", fill="x", pady=(7, 0))
            self.team_winrate_page_vars[mode] = tk.StringVar(value="0 / 0")
            tk.Label(paging, textvariable=self.team_winrate_page_vars[mode], bg=BG, fg=MUTED).pack(side="left")
            previous = ttk.Button(paging, text="上一页", state="disabled",
                command=lambda selected_mode=mode: self.change_team_winrate_page(selected_mode, -1))
            following = ttk.Button(paging, text="下一页", state="disabled",
                command=lambda selected_mode=mode: self.change_team_winrate_page(selected_mode, 1))
            following.pack(side="right")
            previous.pack(side="right", padx=8)
            self.team_winrate_previous[mode], self.team_winrate_next[mode] = previous, following
            panel, tree = self._table(tab, columns, height=7)
            panel.pack(fill="both", expand=True)
            tree.bind("<<TreeviewSelect>>", lambda event, selected_mode=mode: self._team_winrate_selected(selected_mode))
            self.team_winrate_trees[mode] = tree
            self._set_text(text, "选择一支队伍查看三名武将的红度、品度和战法。")

    def _build_opponents(self):
        page = self._page_frame("opponents")
        bar = tk.Frame(page, bg=BG)
        bar.pack(fill="x", pady=(0, 10))
        tk.Label(bar, textvariable=self.roster_day_var, bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        self.roster_export_button = ttk.Button(bar, text="导出最近一天名单…", command=self.export_roster, state="disabled")
        self.roster_export_button.pack(side="right")
        self._small_metrics(page, [("players", "符合条件的对手", TEXT), ("teams", "观察到的队伍", BLUE),
                                  ("full", "含满红武将的对手", TEAL)], self.roster_metrics).pack(fill="x", pady=(0, 10))
        tk.Label(page, text="高品：至少一将进阶≥3。满红：单将5阶；整队满红：三将全5阶。只显示最新有战报的一天。",
                 bg=BG, fg=MUTED, anchor="w", wraplength=1000, font=("Microsoft YaHei UI", 9)).pack(fill="x", pady=(0, 10))
        self.roster_note_label = tk.Label(page, textvariable=self.roster_note_var, bg=BG, fg=MUTED,
                 justify="left", anchor="w", wraplength=1000, font=("Microsoft YaHei UI", 9))
        self.roster_note_label.pack(side="bottom", fill="x", pady=(8, 0))
        panes = ttk.Panedwindow(page, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = tk.Frame(panes, bg=PANEL)
        right = tk.Frame(panes, bg=PANEL)
        panes.add(left, weight=3)
        panes.add(right, weight=2)
        panel, self.opponents_tree = self._table(left, [("name", "对手", 145), ("uid", "玩家 ID", 140),
            ("tags", "标记", 130), ("teams", "队伍数", 70), ("time", "最近观察", 125)], height=8)
        panel.pack(fill="both", expand=True)
        self.opponents_tree.bind("<<TreeviewSelect>>", self._opponent_selected)
        self.opponent_text = self._text(right)
        self._set_text(self.opponent_text, "选择一名对手查看其队伍。\n\n战报只能证明观察时的队伍，不代表实时阵容。")

    def _text(self, parent, mono=False):
        frame = tk.Frame(parent, bg=PANEL)
        frame.pack(fill="both", expand=True)
        widget = tk.Text(frame, wrap="word", relief="flat", borderwidth=0, padx=10, pady=10, width=1, height=8,
                         bg=PANEL, fg=TEXT, insertbackground=TEXT, state="disabled",
                         font=("Cascadia Mono", 9) if mono else ("Microsoft YaHei UI", 10))
        scroll = ttk.Scrollbar(frame, command=widget.yview)
        widget.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        widget.pack(side="left", fill="both", expand=True)
        return widget

    def _build_peerless(self):
        page = self._page_frame("peerless")
        bar = tk.Frame(page, bg=BG)
        bar.pack(fill="x", pady=(0, 10))
        tk.Label(bar, text="战斗日期", bg=BG, fg=MUTED).pack(side="left", padx=(0, 6))
        self.peerless_day_box = ttk.Combobox(bar, textvariable=self.peerless_day_var,
            values=["全部日期"], state="readonly", width=15)
        self.peerless_day_box.pack(side="left")
        self.peerless_day_box.bind("<<ComboboxSelected>>", self._filter_peerless)
        ttk.Entry(bar, textvariable=self.peerless_search_var, width=19).pack(side="left", padx=10)
        ttk.Button(bar, text="筛选玩家姓名 / ID", command=self._filter_peerless).pack(side="left")
        self.peerless_export_button = ttk.Button(bar, text="导出当前无双记录…", command=self.export_peerless,
                                                 state="disabled")
        self.peerless_export_button.pack(side="right")
        self._small_metrics(page, [("occurrences", "无双记录", TEAL), ("players", "当事玩家", BLUE),
                                  ("reports", "关联战报", TEXT)], self.peerless_metrics).pack(fill="x", pady=(0, 10))
        tk.Label(page, text="每条只关联打出无双的那次战报与当事人；同一人再次打出会另列一条。",
                 bg=BG, fg=MUTED, anchor="w", wraplength=1000,
                 font=("Microsoft YaHei UI", 9)).pack(fill="x", pady=(0, 10))
        tk.Label(page, textvariable=self.peerless_note_var, bg=BG, fg=MUTED, justify="left", anchor="w",
                 wraplength=1000, font=("Microsoft YaHei UI", 9)).pack(side="bottom", fill="x", pady=(8, 0))
        panes = ttk.Panedwindow(page, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left, right = tk.Frame(panes, bg=PANEL), tk.Frame(panes, bg=PANEL, width=330)
        right.pack_propagate(False)
        panes.add(left, weight=3)
        panes.add(right, weight=2)
        panel, self.peerless_tree = self._table(left, [("date", "战斗日期", 105), ("time", "发生时间", 125),
            ("name", "玩家", 120), ("uid", "玩家 ID", 125), ("union", "同盟", 100),
            ("battle", "这次战报", 130)], height=8)
        panel.pack(fill="both", expand=True)
        self.peerless_tree.bind("<<TreeviewSelect>>", self._peerless_selected)
        self.peerless_view_button = ttk.Button(right, text="查看这次战报", command=self.view_peerless_report,
                                               state="disabled")
        self.peerless_view_button.pack(fill="x", padx=10, pady=10)
        self.peerless_text = self._text(right)
        self._set_text(self.peerless_text, "选择一条无双记录，查看当事人信息和这次战报的判定依据。")

    @staticmethod
    def _set_text(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _message(self, text, error=False):
        self.message_var.set(str(text))
        self.message_label.configure(fg=RED if error else MUTED)

    def _submit(self, key, operation, callback, *, replace=False):
        if self._closed or (not replace and (key in self._inflight or key in self._active_generations)):
            return
        self._generation[key] = self._generation.get(key, 0) + 1
        generation = self._generation[key]
        self._inflight.add(key)
        if key in self._active_generations:
            previous = self._futures.get(key)
            if previous is not None and previous.cancel():
                # Work not yet started can be replaced immediately. Its queued
                # completion is harmless because it carries the old generation.
                active_generation = self._active_generations.pop(key)
                self._callbacks.pop((key, active_generation), None)
                self._futures.pop(key, None)
                self._pending_operations.pop(key, None)
            else:
                # Running reads retain their bounded timeout. Keep just one
                # latest request instead of filling all workers with the same
                # page, and start it from the UI thread after completion.
                self._pending_operations[key] = (generation, operation, callback)
                return
        self._start_operation(key, generation, operation, callback)

    def _start_operation(self, key, generation, operation, callback):
        if self._closed:
            return
        self._inflight.add(key)
        self._active_generations[key] = generation
        self._callbacks[(key, generation)] = callback
        future = self._executor.submit(operation)
        self._futures[key] = future
        target_queue = self._events
        def done(completed):
            try:
                result, error = completed.result(), None
            except Exception as exc:
                result, error = None, str(exc) or type(exc).__name__
            # No Tk object or bound Tk method is retained by a worker callback.
            # Tcl interpreters and variables must be released on the UI thread.
            target_queue.put((key, generation, result, error))
        future.add_done_callback(done)

    def _poll(self):
        if self._closed:
            return
        for _ in range(80):
            try:
                key, generation, result, error = self._events.get_nowait()
            except queue.Empty:
                break
            callback = self._callbacks.pop((key, generation), None)
            if self._active_generations.get(key) == generation:
                self._active_generations.pop(key, None)
                self._futures.pop(key, None)
                pending = self._pending_operations.pop(key, None)
                if pending is not None and pending[0] == self._generation.get(key):
                    self._start_operation(key, *pending)
                else:
                    self._inflight.discard(key)
            if generation != self._generation.get(key):
                continue
            if error:
                self._message(error, True)
                self._append_log(f"[{format_time(datetime.now(timezone.utc).isoformat())}] {error}\n")
            else:
                try:
                    if callback is not None:
                        callback(result)
                except Exception as exc:
                    self._message(f"界面更新失败：{exc}", True)
        if "runtime" not in self._inflight:
            controller = self.controller
            self._submit("runtime", lambda: BattleApp._read_runtime(controller), self._render_runtime)
        if time.monotonic() >= self._next_overview:
            self._next_overview = time.monotonic() + 15
            self._submit("overview", self.data.overview, self._render_overview)
        self._poll_id = self.after(500, self._poll)

    @staticmethod
    def _read_runtime(controller):
        snapshot = controller.snapshot()
        return snapshot, controller.drain_events(100)

    def _render_runtime(self, result):
        state, events = result
        self._runtime = state
        if self._select_initial_runtime_account():
            self._account_changed()
        running, stopping = bool(state.get("running")), bool(state.get("stopping"))
        if running and state.get("pid") != self._options_pid:
            options = state.get("collection_options") or {}
            if isinstance(options.get("scopes"), list):
                for key, var in self.scopes.items():
                    var.set(key in options["scopes"])
            if options.get("interval") is not None:
                self.interval_var.set(str(options["interval"]))
            self.pvp_only_var.set(bool(options.get("pvp_only", False)))
            self.parallel_var.set(str(options.get("parallel", 1)))
            self.pid_var.set(str(options["pid"]) if options.get("pid") else "")
            self._options_pid = state.get("pid")
        elif not running:
            self._options_pid = None
        self.phase_var.set(state.get("status") or ("正在采集" if running else "未启动"))
        self.phase_label.configure(fg=TEAL if running and not stopping else BLUE)
        self.start_button.configure(state="normal" if state.get("can_start", not running) and "start" not in self._inflight else "disabled")
        self.stop_button.configure(state="normal" if state.get("can_stop", running) and not stopping else "disabled")
        for widget in [*self.scope_checks, self.interval_box, self.pid_box, self.pvp_only_check]:
            widget.configure(state="disabled" if running or stopping else "normal")
        self.parallel_box.configure(state="disabled" if running or stopping else "readonly")
        self._collection_mode_changed()
        filtering = state.get("raw_status", {}).get("filter") or {}
        if running and self.pvp_only_var.get() and "pveReports" in filtering:
            self.collection_mode_var.set(
                f"仅采集 PVP · 本轮列表排除非 PVP {filtering.get('pveReports', 0)} 封"
                f" · 类型待确认 {filtering.get('unknownReports', 0)} 封")
        context = (state.get("namespace"), self.pvp_only_var.get())
        if context != self._scope_coverage_context:
            self._scope_coverage_context = context
            self._scope_coverage_text = None
        raw = state.get("raw_status") or {}
        text = format_cycle_coverage(raw.get("result"), self.pvp_only_var.get())
        if text and raw.get("namespace") == state.get("namespace") and state.get("namespace"):
            self._scope_coverage_text = text
        self.scope_coverage_var.set(self._scope_coverage_text or "等待当前采集范围的一轮结果……")
        self.current_var.set(str(state.get("current_battle_id") or "—") if running else "—")
        self.last_capture_var.set(format_last_capture(state.get("last_capture"), state.get("namespace")))
        self.progress_var.set(format_time(state.get("last_progress_at")))
        error = state.get("last_error")
        if state.get("status_stale") and running:
            error = error or "近期没有新的采集进度，请查看运行记录。"
        self.error_var.set(str(error or ""))
        if error:
            self.error_label.grid()
        else:
            self.error_label.grid_remove()
        for event in events:
            kind = event.get("type")
            if kind == "log":
                self._append_log(str(event.get("text", "")) + ("" if str(event.get("text", "")).endswith("\n") else "\n"))
            elif kind == "error":
                self._message(event.get("text", "操作失败"), True)
                self._append_log("[错误] " + str(event.get("text", "")) + "\n")

    def _append_log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        if int(self.log_text.index("end-1c").split(".")[0]) > 2200:
            self.log_text.delete("1.0", "501.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def show_page(self, page, *, load=True):
        self._page = page
        self.pages[page].tkraise()
        names = {"overview": ("采集总览", "采集当前登录角色；上方账号筛选仅用于查看已有档案。"),
                 "archive": ("战报档案", "查看当前最完整版本，原始字段和历史版本保留在数据库。"),
                 "daily": ("每日统计", "仅统计我盟，按北京时间分类并汇总盟友实际记载的武勋。"),
                 "team_winrates": ("队伍胜率", "双方队伍均统计；分别查看区分战法与不区分战法的组合表现。"),
                 "opponents": ("对手名单", "只保留最近有战报数据的一天；名单代表战报观察，不代表实时阵容。"),
                 "peerless": ("无双记录", "记录任一玩家打出无双的那次战报，按次保留当事人信息。"),
                 "logs": ("运行记录", "连接、采集和入库错误会在这里显示。")}
        self.title_var.set(names[page][0])
        self.subtitle_var.set(names[page][1])
        for key, button in self.nav_buttons.items():
            button.configure(bg="#28445f" if key == page else NAVY, fg="white" if key == page else "#d1dceb")
        if load and page == "archive":
            self.load_reports(reset=True)
        elif load and page == "daily":
            self.load_daily()
        elif load and page == "team_winrates":
            self.load_team_winrates()
        elif load and page == "opponents":
            self.load_opponents()
        elif load and page == "peerless":
            self.load_peerless()

    def refresh(self):
        self._next_overview = time.monotonic() + 15
        self._submit("overview", self.data.overview, self._render_overview)
        if self._page == "archive":
            self.load_reports(reset=False)
        elif self._page == "daily":
            self.load_daily()
        elif self._page == "team_winrates":
            self.load_team_winrates()
        elif self._page == "opponents":
            self.load_opponents()
        elif self._page == "peerless":
            self.load_peerless()
        self._message("正在刷新……")

    def _namespace(self):
        return self._account_map.get(self.account_var.get())

    def _select_initial_runtime_account(self):
        if self._account_initialized:
            return False
        if self._selected is not None:
            # Selecting an archived report is already an explicit browsing
            # choice. A late runtime identity must not clear its detail.
            self._account_initialized = True
            return False
        current = self._runtime.get("namespace") or self._runtime.get("identity_namespace")
        if not current:
            return False
        preferred = next((label for label, namespace in self._account_map.items() if namespace == current), None)
        if preferred is None:
            return False
        changed = self.account_var.get() != preferred
        self.account_var.set(preferred)
        self._account_initialized = True
        return changed

    def _account_changed(self, _=None):
        # A deliberate choice of "all accounts" must survive later overview
        # refreshes, including the first automatic account selection.
        self._account_initialized = True
        self._render_metrics()
        if self._page == "archive":
            self.load_reports(reset=True)
        elif self._page == "daily":
            self.load_daily()
        elif self._page == "team_winrates":
            self.load_team_winrates()
        elif self._page == "opponents":
            self.load_opponents()
        elif self._page == "peerless":
            self.load_peerless()

    def _team_winrate_options(self):
        values = {key: variable.get().strip() for key, variable in self.team_threshold_vars.items()}
        if any(values.values()) and not all(values.values()):
            raise ValueError("请填写四项门槛；全部留空时只显示未分档结果。")
        thresholds = {}
        for key, value in values.items():
            if not value:
                thresholds[key] = None
                continue
            try:
                amount = int(value)
            except ValueError as exc:
                raise ValueError("总红和总品门槛须为 0–15 之间的整数。") from exc
            if not 0 <= amount <= 15:
                raise ValueError("总红和总品门槛须为 0–15 之间的整数。")
            thresholds[key] = amount
        if thresholds["high_red"] is not None and (thresholds["high_red"] < thresholds["medium_red"]
                or thresholds["high_quality"] < thresholds["medium_quality"]):
            raise ValueError("高满总红和总品门槛分别不得低于对应的中红门槛。")
        battle_type = {"仅 PVP": "pvp", "全部战斗": "all"}.get(self.team_battle_type_var.get())
        if battle_type is None:
            raise ValueError("请选择仅 PVP 或全部战斗。")
        day = self.team_day_var.get()
        return {"namespace": self._namespace(), "day": None if day == "全部日期" else day,
                "battle_type": battle_type, **thresholds}

    def _clear_team_winrates(self):
        self._team_winrate_data, self._team_winrate_rows = {}, {}
        self._team_winrate_context = None
        self._reset_team_skill_variants()
        for mode, tree in self.team_winrate_trees.items():
            tree.delete(*tree.get_children())
            self._team_winrate_offsets[mode] = 0
            self.team_winrate_page_vars[mode].set("等待统计")
            self.team_winrate_previous[mode].configure(state="disabled")
            self.team_winrate_next[mode].configure(state="disabled")
            self._set_text(self.team_winrate_texts[mode], "正在读取当前筛选范围的队伍统计……")
        self.team_winrate_export_button.configure(state="disabled")

    def load_team_winrates(self, _=None):
        if self.team_winrate_service is None:
            self._message("此数据源暂不支持队伍胜率。", True)
            return
        self._clear_team_winrates()
        try:
            options = self._team_winrate_options()
        except ValueError as exc:
            self.team_winrate_note_var.set(str(exc))
            self._message(str(exc), True)
            return
        self._team_winrate_context = dict(options)
        service = self.team_winrate_service
        self.team_winrate_note_var.set("正在核对战报中的胜负、三将红度／品度和战法；读取在后台进行……")
        self._submit("team-winrates", lambda: {"request": options, "result": service.summary(**options)},
                     self._render_team_winrates, replace=True)

    @staticmethod
    def _team_rate(value):
        return "未取得" if value is None else f"{float(value) * 100:.1f}%"

    @staticmethod
    def _team_attribute(row, name):
        value = row.get(name)
        if value is not None:
            return str(value)
        low, high = row.get(name + "_min"), row.get(name + "_max")
        if low is None or high is None:
            return "未知"
        return str(low) if low == high else f"{low}–{high}"

    def _render_team_winrates(self, payload):
        options, result = payload["request"], payload["result"]
        try:
            current = self._team_winrate_options()
        except ValueError:
            return
        if options != current or options != self._team_winrate_context:
            return
        self._team_winrate_data = result
        dates = sorted({str(day) for day in result.get("dates", []) if day}, reverse=True)
        values = ["全部日期", *dates]
        if options["day"] and options["day"] not in values:
            values.append(options["day"])
        self.team_day_box.configure(values=values)
        self._filter_team_winrate_tier()
        rule = (f"自定义门槛：高满总红≥{options['high_red']}且总品≥{options['high_quality']}；"
                f"中红总红≥{options['medium_red']}且总品≥{options['medium_quality']}；其余低红。"
                if options["high_red"] is not None else "门槛尚未设置，当前只显示未分档和属性未知的结果。")
        cached_rows = result.get("without_skills", [])
        base_rows = ranked_winrate_rows(cached_rows)
        samples = sum(int(row.get("samples", 0)) for row in base_rows)
        unknown = sum(int(row.get("unknown_results", 0)) for row in base_rows)
        excluded_teams = len(cached_rows) - len(base_rows)
        cached_variants = [variant for row in result.get("with_skills", []) for variant in row.get("skill_variants", [])]
        excluded_variants = len(cached_variants) - len(ranked_winrate_rows(cached_variants))
        coverage = result.get("coverage", {})
        self.team_winrate_note_var.set(rule + f" 达标主队共 {len(base_rows):,} 队，含 {samples:,} 个参战样本，未知结果 {unknown:,} 个。\n"
            f"队伍与具体战法配置各需胜+平+负≥{MIN_KNOWN_RESULTS}场，未知结果不凑门槛；按胜率降序，同率有效场次多优先。"
            f"已排除不足{MIN_KNOWN_RESULTS}场的主队 {excluded_teams:,} 支、配置 {excluded_variants:,} 个。\n"
            "双方均计入；胜率=胜÷(胜+平+负)，平局率=平÷(胜+平+负)，未知结果不进入分母。"
            "红度为进阶，品度为升品；三将总计各0–15，15红且15品额外标注全红全品。\n"
            f"原始缓存字段待补：红度 {coverage.get('unknown_red_samples', 0)} 个样本，品度 {coverage.get('unknown_quality_samples', 0)} 个样本，"
            f"战法 {coverage.get('unknown_skills_samples', 0)} 个样本（可能重叠）；战斗类型待确认 {coverage.get('unknown_reports', 0)} 封。")
        self._team_winrate_tab_changed()
        self._message("队伍胜率已读取；这些比例只代表已采集且能判定结果的战报。")

    def _filter_team_winrate_tier(self, tier=None):
        if tier is not None:
            self.team_tier_var.set(tier)
        selected = self.team_tier_var.get()
        if selected not in TEAM_TIER_LABELS:
            selected = "low"
            self.team_tier_var.set(selected)
        self._reset_team_skill_variants()
        for mode in self.team_winrate_trees:
            rows = self._team_winrate_data.get(mode, [])
            self._team_winrate_rows[mode] = ranked_winrate_rows(row for row in rows if
                (row.get("tier") == selected if selected != "unknown" else row.get("tier") not in {"low", "medium", "high"}))
            self._team_winrate_offsets[mode] = 0
            self._render_team_winrate_table(mode)
            self._set_text(self.team_winrate_texts[mode], "选择一支队伍查看配置和结果。" if self._team_winrate_rows[mode] else
                           f"当前{TEAM_TIER_LABELS[selected]}没有胜+平+负至少{MIN_KNOWN_RESULTS}场的队伍；其他档位可通过上方标签查看。")
        for key, button in self.team_tier_buttons.items():
            button.configure(bg=BLUE if key == selected else PANEL, fg="white" if key == selected else TEXT)
        self._team_winrate_tab_changed()

    def _render_team_winrate_table(self, mode):
        tree = self.team_winrate_trees[mode]
        tree.delete(*tree.get_children())
        rows = self._team_winrate_rows.get(mode, [])
        offset = self._team_winrate_offsets[mode]
        for index, row in enumerate(rows[offset:offset + 100], offset):
            label = row.get("label") or " / ".join(str(hero.get("name_zh") or hero.get("name") or hero.get("hero_uid", "未知武将"))
                                                     for hero in row.get("heroes", []) if isinstance(hero, dict))
            tier = TEAM_TIER_LABELS.get(row.get("tier"), "未知/未分档")
            if row.get("full_red_full_quality"):
                tier += " · 全红全品"
            elif row.get("full_red_full_quality_samples", 0):
                tier += f" · 含全红全品 {row['full_red_full_quality_samples']} 次"
            tree.insert("", "end", iid=str(index), values=(label, tier,
                self._team_attribute(row, "total_red") + " / " + self._team_attribute(row, "total_quality"),
                row.get("samples", 0), row.get("wins", 0), row.get("draws", 0), row.get("losses", 0),
                row.get("unknown_results", 0), self._team_rate(row.get("win_rate")), self._team_rate(row.get("draw_rate"))))
        self.team_winrate_page_vars[mode].set(f"{offset + 1 if rows else 0}–{min(offset + 100, len(rows))} / {len(rows):,} 支队伍")
        self.team_winrate_previous[mode].configure(state="normal" if offset else "disabled")
        self.team_winrate_next[mode].configure(state="normal" if offset + 100 < len(rows) else "disabled")

    def change_team_winrate_page(self, mode, direction):
        rows = self._team_winrate_rows.get(mode, [])
        offset = max(0, self._team_winrate_offsets[mode] + direction * 100)
        if direction > 0 and offset >= len(rows):
            return
        self._team_winrate_offsets[mode] = offset
        self._render_team_winrate_table(mode)
        if mode == "with_skills":
            self._reset_team_skill_variants()
        self._set_text(self.team_winrate_texts[mode], "选择当前页的队伍查看详情。")

    def _team_winrate_mode(self):
        return ("without_skills", "with_skills")[self.team_winrate_tabs.index(self.team_winrate_tabs.select())]

    def _team_winrate_tab_changed(self, _=None):
        if not hasattr(self, "team_winrate_tabs") or not hasattr(self, "team_winrate_export_button"):
            return
        mode = self._team_winrate_mode()
        rows = self._team_winrate_rows.get(mode, [])
        all_rows = ranked_winrate_rows(self._team_winrate_data.get("without_skills", [])
                                      or self._team_winrate_data.get("with_skills", []))
        self.team_winrate_export_button.configure(state="normal" if all_rows else "disabled")
        self.team_winrate_export_menu.entryconfigure(1, state="normal" if rows else "disabled")
        tier = self.team_tier_var.get()
        self.team_tier_summary_var.set(f"{TEAM_TIER_LABELS.get(tier, '未知/未分档')}：{len(rows):,} 队 / "
            f"{sum(int(row.get('samples', 0)) for row in rows):,} 个参战样本")

    def _team_winrate_selected(self, mode):
        selection = self.team_winrate_trees[mode].selection()
        rows = self._team_winrate_rows.get(mode, [])
        if not selection or not selection[0].isdigit() or int(selection[0]) >= len(rows):
            return
        row = rows[int(selection[0])]
        if mode == "with_skills":
            self._reset_team_skill_variants()
            variants = row.get("skill_variants", [])
            self._team_variant_main = row
            self._team_skill_variants = ranked_winrate_rows(variants)
            if self._team_skill_variants:
                self._show_team_skill_variant_page(0)
                return
            self.team_variant_page_var.set(f"达标配置 0 个；原有 {len(variants)} 个配置")
            reason = (f"现有 {len(variants)} 个配置的已判定场次均不足{MIN_KNOWN_RESULTS}场。" if variants else
                      "尚未取得可验证的具体配置。")
            self._set_text(self.team_winrate_texts[mode], f"主队：{row.get('label') or '队伍'}\n"
                f"主队整体已判定 {known_result_count(row)} 场，整体胜率 {self._team_rate(row.get('win_rate'))}。\n"
                f"具体战法配置各需胜+平+负至少{MIN_KNOWN_RESULTS}场。" + reason + "该队尚无达标配置，不展示小样本配置胜率；"
                "队伍整体达标时仍保留主表统计，未知结果不计入门槛。")
            return
        self._render_team_winrate_detail(mode, row)

    def _reset_team_skill_variants(self):
        self._team_skill_variants, self._team_variant_main = [], None
        self._team_variant_offset = 0
        self.team_variant_box.configure(state="disabled", values=[])
        self.team_variant_var.set("")
        self.team_variant_page_var.set("未选择队伍")
        self.team_variant_previous.configure(state="disabled")
        self.team_variant_next.configure(state="disabled")

    def _show_team_skill_variant_page(self, offset):
        variants = self._team_skill_variants
        if not variants:
            return
        offset = max(0, min(offset, (len(variants) - 1) // 20 * 20))
        self._team_variant_offset = offset
        labels = [f"配置 {index + 1} · 已判定{known_result_count(row)}场 · 参战{row.get('samples', 0)}次 · 胜{row.get('wins', 0)} 平{row.get('draws', 0)} "
                  f"负{row.get('losses', 0)} · 胜率{self._team_rate(row.get('win_rate'))}"
                  + (" · 战法待补" if row.get("skills_complete") is False else "")
                  for index, row in enumerate(variants[offset:offset + 20], offset)]
        self.team_variant_box.configure(state="readonly", values=labels)
        self.team_variant_box.current(0)
        self.team_variant_previous.configure(state="normal" if offset else "disabled")
        self.team_variant_next.configure(state="normal" if offset + 20 < len(variants) else "disabled")
        self._team_skill_variant_selected()

    def _team_skill_variant_selected(self, _=None):
        selected = self.team_variant_box.current()
        index = self._team_variant_offset + selected
        if selected < 0 or not 0 <= index < len(self._team_skill_variants):
            return
        self.team_variant_page_var.set(
            f"达标配置 {self._team_variant_offset + 1}–{min(self._team_variant_offset + 20, len(self._team_skill_variants))} / "
            f"{len(self._team_skill_variants)}，当前第 {index + 1} 项")
        self._render_team_winrate_detail("with_skills", self._team_skill_variants[index],
                                       main=self._team_variant_main, variant_index=index)

    def change_team_skill_variant_page(self, direction):
        offset = self._team_variant_offset + direction * 20
        if direction > 0 and offset >= len(self._team_skill_variants):
            return
        self._show_team_skill_variant_page(offset)

    def _render_team_winrate_detail(self, mode, row, *, main=None, variant_index=None):
        lines = [str(row.get("label") or "队伍"),
            f"总红 {self._team_attribute(row, 'total_red')} / 总品 {self._team_attribute(row, 'total_quality')}；"
            f"胜 {row.get('wins', 0)} · 平 {row.get('draws', 0)} · 负 {row.get('losses', 0)} · 未知 {row.get('unknown_results', 0)}。",
            f"胜率 {self._team_rate(row.get('win_rate'))}；平局率 {self._team_rate(row.get('draw_rate'))}。"]
        if main is not None:
            lines[0] = f"主队：{main.get('label') or '队伍'} · 战法配置 {variant_index + 1}/{len(self._team_skill_variants)}"
            lines.insert(1, f"本配置样本 {row.get('samples', 0)} 次，已判定 {known_result_count(row)} 场；主队全部配置合计 {main.get('samples', 0)} 次。")
        if row.get("full_red_full_quality"):
            lines.append("全红全品：三名武将总红15、总品15。")
        elif row.get("full_red_full_quality_samples", 0):
            lines.append(f"本组含全红全品 {row['full_red_full_quality_samples']} 次，其余样本的红品见统计范围。")
        if row.get("skills_complete") is False:
            lines.append("战法字段未齐；未知组合不会混入已知战法组合。")
        if mode == "without_skills":
            lines.append("本表合并同一武将组合的不同战法；下列配置为一份样本，具体组合见“区分战法”。")
        if any(row.get(name) is None and row.get(name + "_min") is not None for name in ("total_red", "total_quality")):
            lines.append("同档的不同红品配置已合并；下列仅展示一份配置，变化范围见上方总红／总品。")
        for index, hero in enumerate(row.get("heroes", [])[:3], 1):
            if not isinstance(hero, dict):
                continue
            uid = hero.get("hero_uid", "未知")
            name = hero.get("name_zh") or hero.get("hero_name") or f"武将 ID {uid}"
            red, quality = hero.get("red"), hero.get("quality")
            lines.append(f"{index}. {name}（ID {uid}） · 红度 {red if red is not None else '未知'}，品度 {quality if quality is not None else '未知'}")
            skills = [str(skill.get("name_zh") or f"战法 ID {skill.get('skill_uid', '未知')}")
                      for skill in hero.get("skills", [])[:3] if isinstance(skill, dict)]
            lines.append("   战法：" + ("、".join(skills) if skills else "字段未取得"))
        self._set_text(self.team_winrate_texts[mode], "\n".join(lines))

    def export_team_winrates(self):
        mode = self._team_winrate_mode()
        rows = self._team_winrate_rows.get(mode, [])
        options = self._checked_team_winrate_export_options()
        if not rows or not options:
            return
        copied = [[options["day"] or "全部日期", options["namespace"], options["battle_type"], row.get("label"),
            TEAM_TIER_LABELS.get(row.get("tier"), "未知/未分档"), self._team_attribute(row, "total_red"), self._team_attribute(row, "total_quality"),
            row.get("samples", 0), row.get("wins", 0), row.get("draws", 0), row.get("losses", 0), row.get("unknown_results", 0),
            row.get("win_rate"), row.get("draw_rate"), bool(row.get("full_red_full_quality")),
            json.dumps(row.get("heroes", []), ensure_ascii=False), options["high_red"], options["high_quality"],
            options["medium_red"], options["medium_quality"], row.get("full_red_full_quality_samples")] for row in rows]
        self._export_csv("导出队伍胜率", f"team_winrates_{mode}.csv",
            ["战斗日期", "账号范围", "战斗类型", "队伍", "分档", "总红", "总品", "参战样本", "胜", "平", "负", "未知结果",
             "胜率（0–1）", "平局率（0–1）", "全红全品", "武将和战法样本", "高满总红门槛", "高满总品门槛",
             "中红总红门槛", "中红总品门槛", "全红全品样本数"], copied)

    def _checked_team_winrate_export_options(self):
        options = self._team_winrate_context
        if not options or not self._team_winrate_data:
            return None
        try:
            if options != self._team_winrate_options():
                self._message("筛选条件已改变，请应用门槛或重新统计后导出。", True)
                return None
        except ValueError as exc:
            self._message(str(exc), True)
            return None
        return dict(options)

    def export_team_winrates_excel(self):
        options = self._checked_team_winrate_export_options()
        if not options or not hasattr(self.data, "db_path"):
            return
        payload = self._team_winrate_data
        if not ranked_winrate_rows(payload.get("without_skills", []) or payload.get("with_skills", [])):
            self._message(f"暂无胜+平+负至少{MIN_KNOWN_RESULTS}场的达标主队可导出。", True)
            return
        folder = self.root_path / "output" / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        filename = "team_winrates_" + datetime.now(CHINA).strftime("%Y%m%d_%H%M%S") + ".xlsx"
        target = filedialog.asksaveasfilename(parent=self, title="导出全部档位 Excel（含达标队伍与达标战法配置）",
            initialdir=str(folder), initialfile=filename, defaultextension=".xlsx", filetypes=[("Excel 工作簿", "*.xlsx")])
        if not target or self._checked_team_winrate_export_options() != options:
            return
        if Path(target).exists():
            self._message("导出会保留已有文件，请换一个新文件名后导出 Excel。", True)
            return
        context = {**options, "selected_tier": self.team_tier_var.get(), "current_mode": self._team_winrate_mode(),
                   "export_scope": "all_tiers_and_eligible_skill_variants", "minimum_known_results": MIN_KNOWN_RESULTS}
        db_path = self.data.db_path
        def write_workbook():
            from .team_winrate_export import export_team_winrates
            return export_team_winrates(db_path, Path(target), copy.deepcopy(payload), copy.deepcopy(context))
        self._message("正在后台导出全部档位的达标队伍和达标战法配置，包含所有分页……")
        self._submit("team-winrate-export", write_workbook,
                     lambda result: self._message(f"完整 Excel 已导出：{result.get('path', target)}"))

    def load_daily(self):
        if self.daily_service is None:
            self._message("此数据源暂不支持每日统计。", True)
            return
        service, namespace = self.daily_service, self._namespace()
        self._daily_data, self._daily_export_rows = {}, []
        self.days_tree.delete(*self.days_tree.get_children())
        self.players_tree.delete(*self.players_tree.get_children())
        self.day_box.configure(values=[])
        for variable in self.daily_metrics.values():
            variable.set("—")
        self.daily_note_var.set("正在核对所选账号的同盟身份，读取我盟每日战报和武勋……")
        self.daily_export_button.configure(state="disabled")
        self._message("正在按战斗日期和玩家统计，读取在后台进行……")
        self._submit("daily", lambda: service.summary(namespace=namespace), self._render_daily, replace=True)

    def _render_daily(self, result):
        if "namespace" in result and result["namespace"] != self._namespace():
            if self._page == "daily":
                self.load_daily()
            return
        self._daily_data = result
        dates = sorted(result.get("dates", []), reverse=True)
        values = ["全部日期", *dates]
        self.day_box.configure(values=values)
        if self.day_var.get() not in values:
            self.day_var.set(dates[0] if dates else "全部日期")
        self.days_tree.delete(*self.days_tree.get_children())
        for day in result.get("days", []):
            self.days_tree.insert("", "end", iid=day["date"], values=(day["date"],
                day.get("reports", day.get("total", 0)), day.get("complete", 0), day.get("partial", 0),
                day.get("personal", 0), day.get("union", 0), day.get("star", 0), day.get("unknown", 0)))
        self._filter_daily()
        self._message("每日分类和玩家武勋已读取；已记录武勋只代表已采集到的结算数据。")

    def _daily_day_selected(self, _=None):
        chosen = self.days_tree.selection()
        if chosen:
            self.day_var.set(chosen[0])
            self._filter_daily()

    def _filter_daily(self):
        result = self._daily_data
        if not result:
            self.daily_export_button.configure(state="disabled")
            return
        day = None if self.day_var.get() in {"全部日期", "最近一天"} else self.day_var.get()
        search = self.player_search_var.get().strip().casefold()
        rows = [row for row in result.get("players", []) if (day is None or row.get("date") == day)
                and (not search or search in str(row.get("player_name", "")).casefold()
                     or search in str(row.get("player_uid", "")).casefold())]
        days = [row for row in result.get("days", []) if day is None or row.get("date") == day]
        self.players_tree.delete(*self.players_tree.get_children())
        self._daily_export_rows = []
        for index, row in enumerate(rows):
            amount = row.get("known_merit")
            label = "完整" if row.get("data_complete") else "有待补项"
            self.players_tree.insert("", "end", iid=str(index), values=(row.get("date", "日期未知"),
                row.get("player_name") or "姓名未记录", row.get("player_uid"), row.get("battles", 0),
                "未取得" if amount is None else f"{amount:,}", row.get("unknown_merit_reports", 0),
                label, account_label(str(row.get("namespace", "")))))
            self._daily_export_rows.append([row.get("date"), row.get("namespace"), row.get("player_uid"),
                row.get("player_name"), row.get("battles", 0), amount, row.get("unknown_merit_reports", 0),
                row.get("missing_children", 0), label])
        known = [row["known_merit"] for row in rows if row.get("known_merit") is not None]
        self.daily_metrics["reports"].set(f"{sum(int(row.get('reports', row.get('total', 0))) for row in days):,}")
        self.daily_metrics["players"].set(f"{len({(row.get('namespace'), row.get('player_uid')) for row in rows}):,}")
        self.daily_metrics["merit"].set(f"{sum(known):,}" if known else "未取得")
        self.daily_metrics["unknown"].set(f"{sum(int(row.get('unknown_merit_reports', 0)) for row in rows):,}")
        coverage = result.get("coverage", {})
        filtering = result.get("union_filter", {})
        union_note = ""
        if filtering.get("enabled"):
            contexts = list(filtering.get("contexts", {}).values())
            identified = [context for context in contexts if context.get("status") == "identified"]
            labels = [f"{context.get('union_name') or '同盟'}（ID {context.get('union_id')}，"
                      f"确认于 {format_time(context.get('observed_at'))}）" for context in identified]
            if labels:
                union_note = "仅统计我盟：" + "、".join(labels) + "。"
            elif contexts and all(context.get("status") == "no_union" for context in contexts):
                union_note = "已确认当前账号未加入同盟，暂不计入其他同盟的战报和武勋。"
            else:
                union_note = "当前同盟身份未确认，暂不计入任何玩家；请采集一轮确认所属同盟。"
            if any(context.get("status") == "unknown" for context in contexts):
                union_note += "身份不明的账号不混计。"
            union_note += (f"已排除其他同盟 {filtering.get('enemy_sides_excluded', 0)} 个参战方，"
                           f"同盟字段缺失或身份未确认 {filtering.get('unknown_union_sides_excluded', 0)} 个参战方。\n")
        self.daily_note_var.set(
            union_note + "日期按战斗时间；个人／同盟／收藏可能重叠，不相加为总数。武勋只统计实际结算值，缺值不当作零。\n"
            f"当前账号档案范围仍有 {coverage.get('incomplete_families', 0)} 个连战组未齐，"
            f"按已知子项数量还缺 {coverage.get('missing_children', 0)} 个子项；统计只代表已采集部分。")
        self.daily_export_button.configure(state="normal" if rows else "disabled")

    def load_opponents(self):
        if self.opponent_service is None:
            self._message("此数据源暂不支持对手名单。", True)
            return
        service, namespace = self.opponent_service, self._namespace()
        self._roster_data, self._roster_rows, self._roster_export_rows = {}, {}, []
        self.opponents_tree.delete(*self.opponents_tree.get_children())
        self._set_text(self.opponent_text, "正在读取所选账号的最新战报日和对手队伍……")
        self.roster_note_var.set("正在读取所选账号的对手名单……")
        for variable in self.roster_metrics.values():
            variable.set("—")
        self.roster_export_button.configure(state="disabled")
        self.roster_day_var.set("正在确定最新有战报的一天……")
        self._submit("opponents", lambda: service.summary(namespace=namespace), self._render_opponents, replace=True)

    @staticmethod
    def _tags(value):
        if isinstance(value, str):
            return value
        return "、".join(str(item) for item in (value or []))

    def _render_opponents(self, result):
        if "namespace" in result and result["namespace"] != self._namespace():
            if self._page == "opponents":
                self.load_opponents()
            return
        self._roster_data = result
        self.opponents_tree.delete(*self.opponents_tree.get_children())
        self._roster_rows, self._roster_export_rows = {}, []
        day = result.get("day")
        self.roster_day_var.set(f"最新战报日：{day}（北京时间）" if day else "暂无可确定日期的战报")
        team_count = 0
        full_count = 0
        players = result.get("players", [])
        for index, player in enumerate(players):
            teams = player.get("teams", [])
            team_count += len(teams)
            tags = self._tags(player.get("tags"))
            if "满红" in tags:
                full_count += 1
            key = str(index)
            self._roster_rows[key] = player
            latest = player.get("last_seen") or max((team.get("last_seen", 0) or 0 for team in teams), default=None)
            self.opponents_tree.insert("", "end", iid=key, values=(player.get("player_name") or "姓名未记录",
                player.get("player_uid"), tags, len(teams), format_time(latest)))
            for team in teams:
                heroes = team.get("heroes", [])
                lineup = " / ".join(self._hero_caption(hero) for hero in heroes)
                self._roster_export_rows.append([day, player.get("namespace", team.get("namespace")),
                    player.get("player_uid"), player.get("player_name"), self._tags(team.get("tags")),
                    lineup, team.get("battle_uid", team.get("battle_id")), format_time(team.get("last_seen"))])
        self.roster_metrics["players"].set(str(len(players)))
        self.roster_metrics["teams"].set(str(team_count))
        self.roster_metrics["full"].set(str(full_count))
        coverage = result.get("coverage", {})
        counts = result.get("counts", {})
        note = result.get("note") or "只收录可明确识别的对面真人玩家；系统守军及归属未明者不冒充对手。"
        self.roster_note_var.set(str(note) + "\n" +
            f"当日战报 {counts.get('latest_day_reports', 0)} 封；对方为系统部队 {coverage.get('npc_opponent_reports', 0)} 封，"
            f"真人对手 {coverage.get('reports_with_player_opponents', 0)} 封，"
            f"归属未判定 {coverage.get('unresolved_side_reports', 0) + coverage.get('unknown_owner_reports', 0)} 封，"
            f"双方数据缺失 {coverage.get('missing_side_reports', 0)} 封，"
            f"对方ID缺失或异常 {coverage.get('unknown_opponent_id_reports', 0)} 封。")
        self._set_text(self.opponent_text, "选择一名对手查看该日观察到的队伍。\n\n战报不能证明实时阵容。" if players else
                       "该日已采集战报中，没有识别到符合条件的真人对手。\n\n这不代表全服没有高品或满红人员。缺失战报、系统守军和归属未明的记录不会被补成虚构名单。")
        self.roster_export_button.configure(state="normal" if players else "disabled")
        self._message("最近一天对手名单已读取；原始历史战报继续保留。")

    @staticmethod
    def _hero_caption(hero):
        name = hero.get("name_zh") or hero.get("hero_name") or hero.get("name")
        uid = hero.get("hero_uid", hero.get("hero_id", hero.get("id", "?")))
        rank = hero.get("evolutionNum", hero.get("evolution_num", hero.get("rank")))
        return f"{name or ('武将 ID ' + str(uid))}（{str(rank)+'阶' if rank is not None else '进阶未记录'}）"

    def _opponent_selected(self, _=None):
        selection = self.opponents_tree.selection()
        if not selection or selection[0] not in self._roster_rows:
            return
        player = self._roster_rows[selection[0]]
        lines = [f"{player.get('player_name') or '姓名未记录'} · ID {player.get('player_uid')}",
                 self._tags(player.get("tags")), f"战报日：{self._roster_data.get('day')}", ""]
        for number, team in enumerate(player.get("teams", []), 1):
            lines += [f"队伍 {number} · {self._tags(team.get('tags'))}", f"观察时间：{format_time(team.get('last_seen'))}",
                      f"来源战报：{team.get('battle_uid', team.get('battle_id', '未记录'))}"]
            for hero in team.get("heroes", []):
                lines.append("  " + self._hero_caption(hero))
                if hero.get("level") is not None:
                    lines.append(f"  等级 {hero['level']}")
                if hero.get("skills"):
                    lines.append("  战法：" + "、".join(str(skill.get("name_zh") or skill.get("name") or
                        f"ID {skill.get('skill_uid', skill.get('skill_id', '?'))}") for skill in hero["skills"]))
            lines.append("")
        lines.append("只代表这份战报的观察结果，不代表实时队伍。")
        self._set_text(self.opponent_text, "\n".join(lines))

    def _export_csv(self, title, filename, headers, rows):
        if not rows or not hasattr(self.data, "db_path"):
            return
        folder = self.root_path / "output" / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(parent=self, title=title, initialdir=str(folder), initialfile=filename,
            defaultextension=".csv", filetypes=[("CSV 表格", "*.csv")])
        if not path:
            return
        db_path, copied = self.data.db_path, [list(row) for row in rows]
        self._submit("csv-export", lambda: write_csv(db_path, Path(path), headers, copied),
                     lambda _: self._message(f"统计已导出：{path}"))

    def load_peerless(self):
        if self.peerless_service is None:
            self._message("此数据源暂不支持无双记录。", True)
            return
        service, namespace = self.peerless_service, self._namespace()
        self._peerless_data, self._peerless_rows, self._peerless_export_rows = {}, {}, []
        self.peerless_tree.delete(*self.peerless_tree.get_children())
        self.peerless_day_box.configure(values=[])
        for variable in self.peerless_metrics.values():
            variable.set("—")
        self.peerless_export_button.configure(state="disabled")
        self.peerless_view_button.configure(state="disabled")
        self._set_text(self.peerless_text, "正在读取所选账号范围内的无双记录……")
        self.peerless_note_var.set("正在核对游戏的无双判定和当事人信息……")
        self._message("正在读取无双记录，读取在后台进行……")
        self._submit("peerless", lambda: service.summary(namespace=namespace), self._render_peerless, replace=True)

    def _render_peerless(self, result):
        if "namespace" in result and result["namespace"] != self._namespace():
            if self._page == "peerless":
                self.load_peerless()
            return
        self._peerless_data = result
        records = result.get("records", [])
        dates = sorted({str(day) for day in result.get("dates", []) if day}, reverse=True)
        values = ["全部日期", *dates]
        if any(not row.get("day") for row in records):
            values.append("日期未知")
        self.peerless_day_box.configure(values=values)
        if self.peerless_day_var.get() not in values:
            self.peerless_day_var.set("全部日期")
        coverage = result.get("coverage", {})
        note = result.get("note") or "只列战报明确记载的无双；缺少信息的战报不推算命中。"
        detail = coverage.get("note") or result.get("coverage_note")
        self.peerless_note_var.set(str(note) + ("\n" + str(detail) if detail else ""))
        self._filter_peerless()
        self._message("无双记录已读取；每条仅关联当次战报与当事人。")

    def _filter_peerless(self, _=None):
        if not self._peerless_data:
            self.peerless_export_button.configure(state="disabled")
            self.peerless_view_button.configure(state="disabled")
            return
        selected = self.peerless_day_var.get()
        search = self.peerless_search_var.get().strip().casefold()
        rows = [row for row in self._peerless_data.get("records", [])
            if (selected == "全部日期" or (selected == "日期未知" and not row.get("day"))
                or row.get("day") == selected)
            and (not search or search in str(row.get("player_name") or "").casefold()
                 or search in str(row.get("player_uid") or "").casefold())]
        self.peerless_tree.delete(*self.peerless_tree.get_children())
        self._peerless_rows, self._peerless_export_rows = {}, []
        for index, row in enumerate(rows):
            key = str(index)
            self._peerless_rows[key] = row
            self.peerless_tree.insert("", "end", iid=key, values=(row.get("day") or "日期未知",
                format_time(row.get("timestamp")), row.get("player_name") or "姓名未记录", row.get("player_uid"),
                row.get("union_name") or row.get("union_uid") or "未记录", row.get("battle_uid")))
            self._peerless_export_rows.append([row.get("day"), format_time(row.get("timestamp")),
                row.get("namespace"), row.get("player_uid"), row.get("player_name"), row.get("union_uid"),
                row.get("union_name"), row.get("battle_uid"), row.get("snapshot_id"),
                row.get("side"), COMPLETENESS.get(row.get("completeness"), "待核实"),
                json.dumps(row.get("avatar_data") or {}, ensure_ascii=False),
                json.dumps(row.get("evidence") or [], ensure_ascii=False)])
        self.peerless_metrics["occurrences"].set(str(len(rows)))
        self.peerless_metrics["players"].set(str(len({(row.get("namespace"), row.get("player_uid")) for row in rows})))
        self.peerless_metrics["reports"].set(str(len({(row.get("namespace"), row.get("battle_uid")) for row in rows})))
        self.peerless_export_button.configure(state="normal" if rows else "disabled")
        self.peerless_view_button.configure(state="disabled")
        self._set_text(self.peerless_text,
            "选择一条无双记录，查看当事人信息和这次战报的判定依据。" if rows else
            "当前筛选范围内，尚未发现可确认的无双记录。\n\n缺少连战汇总或游戏判定的数据会保留为未核实，空名单不代表所有人都没有打出过无双。")

    def _peerless_selected(self, _=None):
        selected = self.peerless_tree.selection()
        if not selected or selected[0] not in self._peerless_rows:
            self.peerless_view_button.configure(state="disabled")
            return
        row = self._peerless_rows[selected[0]]
        side = {"attacker": "进攻方", "defender": "防守方", "defensive": "防守方"}.get(row.get("side"), "归属未记录")
        lines = [f"{row.get('player_name') or '姓名未记录'} · ID {row.get('player_uid')}",
                 f"发生日期：{row.get('day') or '未知'}", f"发生时间：{format_time(row.get('timestamp'))}",
                 f"同盟：{row.get('union_name') or '未记录'} · ID {row.get('union_uid') or '未记录'}",
                 f"这次战报：{row.get('battle_uid')}", f"所属侧：{side}",
                 f"战报状态：{COMPLETENESS.get(row.get('completeness'), '待核实')}",
                 f"账号范围：{account_label(str(row.get('namespace', '')))}", "", "该次记录的玩家信息：",
                 pretty(row.get("avatar_data") or {}), "", "无双判定依据：", pretty(row.get("evidence") or []),
                 "", "本页只记录这次无双，不调取该玩家其他战报。"]
        self._set_text(self.peerless_text, "\n".join(lines))
        sid = row.get("snapshot_id")
        self.peerless_view_button.configure(state="normal" if isinstance(sid, int) and not isinstance(sid, bool) and sid > 0 else "disabled")

    def view_peerless_report(self):
        selected = self.peerless_tree.selection()
        if not selected or selected[0] not in self._peerless_rows:
            return
        row = dict(self._peerless_rows[selected[0]])
        sid = row.get("snapshot_id")
        if isinstance(sid, bool) or not isinstance(sid, int) or sid <= 0:
            self._message("这条无双记录没有可读取的来源战报。", True)
            return
        # An older archive query must not replace this exact occurrence while
        # its detail query runs. Opening it never searches the player's history.
        self._generation["reports"] = self._generation.get("reports", 0) + 1
        self._inflight.discard("reports")
        future = self._futures.get("reports")
        if future is not None:
            future.cancel()
        self._invalidate_detail()
        self.report_tree.delete(*self.report_tree.get_children())
        self._rows = {str(sid): row}
        self.report_tree.insert("", "end", iid=str(sid), values=(row.get("battle_uid"), format_time(row.get("timestamp")),
            row.get("player_name") or "姓名未记录", "—", COMPLETENESS.get(row.get("completeness"), "待核实")))
        self._selected, self._event_offset = row, 0
        self._has_more = False
        self.prev_button.configure(state="disabled")
        self.next_button.configure(state="disabled")
        self.page_var.set("当前只显示这次无双关联的战报")
        self.show_page("archive", load=False)
        self._load_detail()

    def export_peerless(self):
        self._export_csv("导出当前无双记录", f"无双记录_{self.peerless_day_var.get()}.csv",
            ["战斗日期", "发生时间", "账号范围", "玩家ID", "玩家名", "同盟ID", "同盟名", "这次战报ID", "来源快照ID",
             "所属侧", "战报状态", "该次玩家信息", "判定依据"], self._peerless_export_rows)

    def export_daily(self):
        self._export_csv("导出当前玩家武勋统计", f"玩家武勋_{self.day_var.get()}.csv",
            ["战斗日期", "账号范围", "玩家ID", "玩家名", "结算场次", "已记录武勋", "缺武勋字段场次", "已知缺子项", "战报覆盖"], self._daily_export_rows)

    def export_roster(self):
        self._export_csv("导出最近一天对手名单", f"对手名单_{self._roster_data.get('day', '最近一天')}.csv",
            ["战斗日期", "账号范围", "对手ID", "对手名", "队伍标记", "武将及进阶", "来源战报", "观察时间"], self._roster_export_rows)

    def _render_overview(self, value):
        previous_namespace = self._namespace()
        self._overview = value
        accounts = value.get("accounts", [])
        self._account_map = {"全部账号": None}
        for row in accounts:
            namespace = str(row.get("namespace", ""))
            self._account_map[account_label(namespace)] = namespace
        self.account_box.configure(values=list(self._account_map))
        self._select_initial_runtime_account()
        if self.account_var.get() not in self._account_map:
            self.account_var.set("全部账号")
        if self._namespace() != previous_namespace:
            self._account_changed()
        else:
            self._render_metrics()
        self._message("档案数量已更新；列表按当前最完整版本显示。" if value.get("available", True) else "还没有数据库，开始采集后会使用原有路径建立档案。")

    def _render_metrics(self):
        value = self._overview
        namespace = self._namespace()
        if namespace is not None:
            value = next((row for row in value.get("accounts", []) if row.get("namespace") == namespace), {})
        total, complete, partial = (int(value.get(key, 0) or 0) for key in ("total", "complete", "partial"))
        ratio = complete / total * 100 if total else 0
        for key, number in (("total", total), ("complete", complete), ("partial", partial)):
            self.metric_vars[key].set(f"{number:,}")
        self.metric_vars["ratio"].set(f"{ratio:.1f}%" if total else "—")
        self.coverage_bar.configure(value=ratio)
        legacy = int(value.get("legacy", 0) or 0)
        self.coverage_var.set(f"已发现 {total:,} 封，其中 {complete:,} 封五组件完整，{partial:,} 封等待补齐" + (f"，另有 {legacy:,} 封历史格式。" if legacy else "。"))

    def _collection_mode_changed(self):
        self.collection_mode_var.set("仅采集 PVP；PVE 和类型待确认的战报不下载正文。"
                                     if self.pvp_only_var.get() else "采集全部类型，包含 PVP 和 PVE。")

    def start_collection(self):
        scopes = [key for key, var in self.scopes.items() if var.get()]
        try:
            interval = float(self.interval_var.get())
            if interval.is_integer():
                interval = int(interval)
            pid = int(self.pid_var.get()) if self.pid_var.get().strip() else None
            parallel = int(self.parallel_var.get())
            if not scopes or not 5 <= interval <= 3600:
                raise ValueError
            if pid is not None and pid <= 0:
                raise ValueError
            if not 1 <= parallel <= 4:
                raise ValueError
        except ValueError:
            self._message("请至少选择一个战报范围；间隔填 5 至 3600，并行数填 1 至 4，游戏进程 ID 留空或填正整数。", True)
            return
        self.start_button.configure(state="disabled")
        self._message("正在启动采集，沿用已有数据库和续采进度……")
        options = {"scopes": scopes, "interval": interval, "pid": pid,
                   "pvp_only": self.pvp_only_var.get(), "parallel": parallel}
        controller = self.controller
        self._submit("start", lambda: controller.start(options), lambda _: self._message("采集已启动，正在连接游戏。"))

    def stop_collection(self):
        self.stop_button.configure(state="disabled")
        self._message("正在保存已有数据并停止采集，请等待当前操作退出……")
        self._submit("stop", self.controller.request_stop, lambda _: None)

    def reset_filter(self):
        self.search_var.set("")
        self.filter_var.set("全部状态")
        self.load_reports(reset=True)

    def load_reports(self, *, reset=False):
        if reset:
            self._offset = 0
            self._invalidate_detail()
        reverse = {value: key for key, value in COMPLETENESS.items()}
        namespace, status = self._namespace(), reverse.get(self.filter_var.get())
        search, offset = self.search_var.get().strip(), self._offset
        data = self.data
        self._message("正在读取当前版本列表……")
        self._submit("reports", lambda: data.list_reports(namespace=namespace, status=status, search=search, limit=100, offset=offset), self._render_reports, replace=True)

    def _render_reports(self, result):
        selected_key = (self._selected.get("namespace"), self._selected.get("battle_uid")) if self._selected else None
        selected_snapshot = self._selected.get("snapshot_id") if self._selected else None
        self.report_tree.delete(*self.report_tree.get_children())
        self._rows = {}
        for row in result.get("items", []):
            key = str(row["snapshot_id"])
            self._rows[key] = row
            players = f"{row.get('attacker_name') or '未知'} → {row.get('defender_name') or '未知'}"
            self.report_tree.insert("", "end", iid=key, values=(row["battle_uid"], format_time(row.get("battle_time") or row.get("imported_at")),
                                                               players, f"{int(row.get('event_count', 0)):,}", COMPLETENESS.get(row.get("completeness"), "待核实")),
                                    tags=(row.get("completeness", "partial"),))
        self._offset = int(result.get("offset", self._offset))
        self._has_more = bool(result.get("has_more"))
        self.prev_button.configure(state="normal" if self._offset else "disabled")
        self.next_button.configure(state="normal" if self._has_more else "disabled")
        self.page_var.set(f"第 {self._offset//100+1} 页 · {len(self._rows)} 封")
        choice = next((key for key, row in self._rows.items()
                       if (row.get("namespace"), row.get("battle_uid")) == selected_key), None)
        if choice:
            if self._rows[choice].get("snapshot_id") != selected_snapshot:
                self._invalidate_detail()
            self.report_tree.selection_set(choice)
        else:
            self._invalidate_detail()
        self._message(f"已显示 {len(self._rows)} 封，每封只显示当前最完整版本。")

    def _invalidate_detail(self):
        self._generation["detail"] = self._generation.get("detail", 0) + 1
        self._inflight.discard("detail")
        future = self._futures.get("detail")
        if future is not None:
            future.cancel()
        self._selected, self._detail = None, None
        self.detail_title.set("选择一封战报查看详情")
        self.export_button.configure(state="disabled")
        for widget in (self.info_text, self.events_text, self.raw_text):
            self._set_text(widget, "")
        for key, widget in self.component_labels.items():
            widget.configure(text=COMPONENTS[key], bg="#eef1f5", fg=MUTED)
        self.event_prev.configure(state="disabled")
        self.event_next.configure(state="disabled")
        self.event_page_var.set("")

    def change_page(self, direction):
        if direction > 0 and not self._has_more:
            return
        self._offset = max(0, self._offset + 100 * direction)
        self.load_reports()

    def _select_report(self, _=None):
        values = self.report_tree.selection()
        if not values or values[0] not in self._rows:
            return
        self._selected = self._rows[values[0]]
        self._event_offset = 0
        self._load_detail()

    def _load_detail(self):
        if not self._selected:
            return
        snapshot_id, offset = self._selected["snapshot_id"], self._event_offset
        data = self.data
        self.detail_title.set(f"战报 {self._selected['battle_uid']}\n正在读取详情……")
        self.export_button.configure(state="disabled")
        self._submit("detail", lambda: data.detail(snapshot_id, event_limit=100, event_offset=offset), self._render_detail, replace=True)

    def _render_detail(self, result):
        self._detail = result
        report = result.get("report", {})
        self.detail_title.set(f"战报 {report.get('battle_uid', self._selected.get('battle_uid') if self._selected else '')}\n{COMPLETENESS.get(report.get('completeness'), '待核实')} · {int(report.get('event_count', 0)):,} 条事件")
        self.export_button.configure(state="normal")
        components = result.get("components", {})
        for key, widget in self.component_labels.items():
            item = components.get(key, {})
            good = bool(item.get("complete")) and not item.get("error")
            widget.configure(text=COMPONENTS[key] + (" ✓" if good else " ·"),
                             fg=TEAL if good else AMBER, bg="#eaf7f1" if good else "#fff5e5")
        lines = [f"账号：{account_label(str(report.get('namespace', '')))}", f"战斗时间：{format_time(report.get('battle_time'))}",
                 f"入库时间：{format_time(report.get('imported_at'))}", ""]
        missing = result.get("missing_components", [])
        if missing:
            lines += ["待补齐：" + "、".join(COMPONENTS.get(name, name) for name in missing), ""]
        for side in result.get("sides", []):
            label = {"attacker": "进攻方", "defender": "防守方", "defensive": "防守方"}.get(str(side.get("side")), str(side.get("side", "参战方")))
            lines += [f"{label}：{side.get('player_name') or side.get('name') or '未知'}"]
            for hero in side.get("heroes", []):
                name = hero.get("name_zh") or hero.get("name") or f"武将 ID {hero.get('hero_uid', hero.get('hero_id', hero.get('id', '?')))}"
                lines.append("  " + str(name))
                if hero.get("level") is not None:
                    lines.append(f"  等级 {hero['level']}")
                strength = [(label, hero.get(key)) for key, label in (("troops_initial", "初始兵力"), ("troops_remaining", "剩余"),
                            ("troops_dead", "阵亡"), ("troops_wounded", "伤兵")) if hero.get(key) is not None]
                if strength:
                    lines.append("  " + " · ".join(f"{label} {value}" for label, value in strength))
                skills = hero.get("skills", [])
                if skills:
                    lines.append("  战法：" + "、".join(str(item.get("name_zh") or item.get("name") or f"ID {item.get('skill_uid', item.get('skill_id', item.get('id', '?')))}") for item in skills))
            lines.append("")
        sem = result.get("semantics", {})
        lines += ["中文解释：" + ("已有解释，未知项保留原值。" if sem.get("available") and sem.get("status") == "ready" else "该版本解释尚未就绪，原始数据可查。"),
                  "", "本页只读查看；导出原文包含全部字段。"]
        self._set_text(self.info_text, "\n".join(lines))
        events = result.get("events", [])
        parts = []
        for event in events:
            sequence = event.get("sequence", "?")
            name = event.get("event_name_zh") or f"事件 ID {event.get('event_id', '?')}"
            parts.append(f"#{sequence}  {name}\n{event.get('summary_zh') or '暂未提供中文摘要，原值如下。'}")
            parameters = event.get("parameters")
            if parameters:
                parts.append(pretty(parameters, 1600))
            else:
                parts.append(pretty(event.get("raw", {}), 1600))
            parts.append("")
        self._set_text(self.events_text, "\n".join(parts) if parts else "此页没有事件。缺少事件的数据仍会保留为待核实状态。")
        total = int(result.get("event_total", report.get("event_count", 0)) or 0)
        self.event_page_var.set(f"{self._event_offset+1 if events else 0}–{self._event_offset+len(events)} / {total}")
        self.event_prev.configure(state="normal" if self._event_offset else "disabled")
        self.event_next.configure(state="normal" if result.get("event_has_more") else "disabled")
        self._set_text(self.raw_text, pretty(result))
        self._message("详情已读取；事件按页展示，原文导出不截断。")

    def change_events(self, direction):
        if not self._selected or (direction > 0 and not (self._detail or {}).get("event_has_more")):
            return
        self._event_offset = max(0, self._event_offset + 100 * direction)
        self._load_detail()

    def export_selected(self):
        if not self._selected:
            return
        selected = dict(self._selected)
        folder = self.root_path / "output" / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        target = filedialog.asksaveasfilename(parent=self, title="导出完整原始战报", initialdir=str(folder),
                                             initialfile=f"battle_{selected['battle_uid']}_snapshot_{selected['snapshot_id']}.json",
                                             defaultextension=".json", filetypes=[("JSON 原文", "*.json")])
        if not target:
            return
        self._message("正在导出完整原文……")
        data = self.data
        self._submit("export", lambda: data.export_original(selected["snapshot_id"], Path(target)),
                     lambda _: self._message(f"完整原文已导出：{target}"))

    def open_logs(self):
        path = Path(self._runtime.get("log_path") or self.root_path / "logs")
        folder = path if path.is_dir() else path.parent
        if os.name == "nt":
            os.startfile(str(folder))
        else:
            self._message(str(folder))

    def close(self):
        if self._closed:
            return
        self._closed = True
        # Hide immediately, then let the bounded database/process work finish
        # before releasing Tcl. A worker must never garbage-collect a Tk cycle.
        self.withdraw()
        if self._poll_id:
            self.after_cancel(self._poll_id)
        self.after_cancel(self._initial_refresh_id)
        self._pending_operations.clear()
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._callbacks.clear()
        self._active_generations.clear()
        self._futures.clear()
        self.controller.close()
        self.destroy()
        for values in (self.pages, self.nav_buttons, self.component_labels, self.metric_vars, self.scopes,
                       self.daily_metrics, self.roster_metrics, self.peerless_metrics,
                       self.team_threshold_vars, self.team_winrate_trees, self.team_winrate_texts,
                       self.team_winrate_page_vars, self.team_winrate_previous, self.team_winrate_next,
                       self.team_tier_buttons):
            values.clear()
        self.scope_checks.clear()
        for name, value in list(vars(self).items()):
            if isinstance(value, (tk.Variable, tk.Misc)):
                delattr(self, name)
        gc.collect()


def write_csv(db_path: Path, target: Path, headers: list, rows: list[list]):
    """Publish one spreadsheet-friendly export, preserving prior files and DB."""
    from parser.report_archive import ReportArchive
    with ReportArchive(db_path, read_only=True) as archive:
        target = archive.protect_output_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", dir=target.parent,
                                         prefix=".battle-stats-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            writer = csv.writer(handle)
            writer.writerow(headers)
            for row in rows:
                writer.writerow([("'" + value) if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value for value in row])
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"path": str(target), "rows": len(rows)}
