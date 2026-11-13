"""Small desktop controls for Quark login and verified original migration."""
from __future__ import annotations

import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk


class CloudPanel(tk.Frame):
    def __init__(self, parent, *, root_path, db_path, submit):
        super().__init__(parent, bg="#f3f5f9")
        self.root_path, self.db_path = Path(root_path), Path(db_path)
        self.submit = submit
        self.config_path = self.root_path / "config/quark.local.json"
        self.auth_link = tk.StringVar(value="")
        self.note = tk.StringVar(value="连接夸克后，采集会在后台同步原文；统计和阵容查询继续使用本地数据。")
        self._watching_auth = False
        self._auth_baseline = None
        self._watch_handle = None
        self._opened_auth_url = None
        controls = tk.Frame(self, bg=self.cget("bg"))
        controls.pack(fill="x", pady=(0, 14))
        for title, action in (("查看状态", "status"), ("登录夸克", "login"),
                              ("启用战报归档", "init"), ("同步接下来10份原文", "sync")):
            ttk.Button(controls, text=title, command=lambda key=action: self.action(key)).pack(side="left", padx=(0, 9))
        tk.Label(self, textvariable=self.note, bg=self.cget("bg"), fg="#24364b", anchor="w",
                 justify="left", wraplength=840).pack(fill="x", pady=(0, 15))
        tk.Label(self, text="授权链接（出现后可复制到浏览器，由你本人登录确认）", bg=self.cget("bg"),
                 fg="#6d7d90", anchor="w").pack(fill="x")
        ttk.Entry(self, textvariable=self.auth_link, state="readonly").pack(fill="x", pady=(6, 15))
        self.output = tk.Text(self, wrap="word", relief="flat", padx=14, pady=14,
                              font=("Microsoft YaHei UI", 10), height=15, state="disabled")
        self.output.pack(fill="both", expand=True)
        tk.Label(self, text="只有上传并完整回读校验成功的原文，才会释放数据库内嵌副本。\n"
                 "本地保留事件、组件和统计投影；续采文件与旧历史目录不会自动删除。",
                 bg=self.cget("bg"), fg="#6d7d90", justify="left", anchor="w").pack(fill="x", pady=(12, 0))

    def refresh(self):
        self.action("status")

    def action(self, action):
        from parser.quark_cloud import (cloud_status, load_config, QuarkClient,
                                       initialize_folder, sync_archive, public_result, read_authorization_handoff)
        if action == "status":
            operation = lambda: cloud_status(self.db_path, self.config_path)
        elif action == "login":
            self._watching_auth = True
            self._auth_baseline = None
            try:
                config = load_config(self.config_path)
                name = config.get("cli_env", {}).get("SANMOU_QUARK_AUTH_URL_FILE")
                previous = read_authorization_handoff(name) if name else None
                self._auth_baseline = previous[0] if previous else None
            except (OSError, ValueError, RuntimeError):
                pass
            self._opened_auth_url = None
            self.auth_link.set("")
            self._watch_auth_url()
            operation = lambda: public_result(QuarkClient(load_config(self.config_path)).run("login", timeout=240))
        elif action == "init":
            operation = lambda: initialize_folder(self.config_path)
        else:
            operation = lambda: sync_archive(self.db_path, config_path=self.config_path, limit=10)
        self.note.set("正在处理……登录需要你本人在授权页面确认；网络失败时本地原件保留。")
        def guarded_operation():
            try:
                return operation()
            except Exception as exc:
                return {"action_error": str(public_result(str(exc)))}
        self.submit("cloud-status" if action == "status" else "cloud-action", guarded_operation,
                    lambda result: self.render(result, action=action), replace=False)

    def _watch_auth_url(self):
        if not self._watching_auth or not self.winfo_exists():
            return
        try:
            from parser.quark_cloud import load_config, read_authorization_handoff
            config = load_config(self.config_path)
            name = config.get("cli_env", {}).get("SANMOU_QUARK_AUTH_URL_FILE")
            if name:
                path = Path(name)
                record = read_authorization_handoff(path)
                if record is not None and record[0] != self._auth_baseline:
                    value = json.loads(record[1])
                    url = value.get("url")
                    if isinstance(url, str) and url.startswith("https://"):
                        self.auth_link.set(url)
                        if self._opened_auth_url != url:
                            from parser.quark_cloud import open_authorization_page
                            self._opened_auth_url = url
                            try:
                                open_authorization_page(url, config)
                                self.note.set("授权页面已在 Chrome 打开，请用本人夸克账号确认。浏览器缓存保存在工作目录。")
                            except RuntimeError as exc:
                                self.note.set(str(exc))
        except (OSError, ValueError, RuntimeError):
            pass
        self._watch_handle = self.after(500, self._watch_auth_url)

    def render(self, result, *, action=None):
        from parser.quark_cloud import public_result
        if action == "login":
            self._watching_auth = False
        if result.get("action_error"):
            message = result["action_error"]
        elif "configured" in result:
            if not result["configured"]:
                message = result.get("error") or "尚未配置夸克官方工具。"
            elif not result["enabled"]:
                message = "先登录夸克，再点击“启用战报归档”。"
            else:
                saved = result.get("released_original_bytes", 0) / 1024 ** 2
                message = (f"已验证云端原文 {result['verified']} 份，待同步 {result['pending']} 份。"
                           f"已释放内嵌原文 {saved:.2f} MiB，数据库空闲页可供后续复用。")
                if result.get("error"):
                    message += "\n最近错误：" + str(result["error"])
        elif result.get("failed"):
            message = "部分同步未完成，本地未验证原件继续保留，可稍后重试。"
        else:
            message = result.get("message") or "操作已结束，可点击“查看状态”核对。"
        self.note.set(message)
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("end", json.dumps(public_result(result), ensure_ascii=False, indent=2))
        self.output.configure(state="disabled")

    def destroy(self):
        self._watching_auth = False
        if self._watch_handle is not None:
            try:
                self.after_cancel(self._watch_handle)
            except tk.TclError:
                pass
        super().destroy()
