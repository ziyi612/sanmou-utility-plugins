"""Verified gzip originals in Quark Drive, with bounded local read caches.

Quark's official CLI owns OAuth credentials. This module never reads tokens.
An original is released from SQLite only after a fresh cloud download validates
both the compressed object and the exact original bytes. Query projections and
collector checkpoint files remain local in this first integration.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config/quark.local.json"
_DDL = """CREATE TABLE IF NOT EXISTS report_cloud_files(
 file_id INTEGER PRIMARY KEY REFERENCES report_files(id), provider TEXT NOT NULL,
 status TEXT NOT NULL, remote_file_id TEXT, object_name TEXT,
 raw_sha256 TEXT NOT NULL, raw_byte_count INTEGER NOT NULL,
 stored_sha256 TEXT, stored_byte_count INTEGER, codec TEXT NOT NULL DEFAULT 'gzip-v1',
 verified_at TEXT, last_error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 config_path TEXT NOT NULL);
"""


class CloudError(RuntimeError):
    pass


def _path(value):
    from .report_archive import _write_path
    return _write_path(value)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _atomic(path, raw):
    from capture.game_assets.atomic_files import replace_with_retry
    path = _path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".quark-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_config(path=None):
    target = _path(path or os.environ.get("SANMOU_QUARK_CONFIG", DEFAULT_CONFIG))
    if not target.is_file():
        raise CloudError("尚未配置夸克云端，请先设置 config/quark.local.json 并登录。")
    config = json.loads(target.read_text(encoding="utf-8"))
    command = config.get("cli_command")
    if not isinstance(command, list) or not command or any(not isinstance(s, str) or not s for s in command):
        raise CloudError("夸克配置中的 cli_command 必须是非空命令参数列表。")
    cache = _path(config.get("cache_dir", ROOT / "data/cloud/cache"))
    work = _path(config.get("work_dir", ROOT / "data/cloud/runtime"))
    budget = config.get("cache_max_bytes", 1024 ** 3)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise CloudError("cache_max_bytes 必须是正整数。")
    config.update(config_path=str(target), cache_dir=str(cache), work_dir=str(work), cache_max_bytes=budget)
    return config


def parse_cli_output(text):
    """Accept JSONL progress/results and a single pretty-printed JSON result."""
    records = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            records.append(value)
    if not records:
        try:
            value = json.loads(text)
            records = [value] if isinstance(value, dict) else []
        except ValueError:
            pass
    results = [item for item in records if item.get("type") == "result"]
    if results:
        return results[-1]
    if records:
        return records[-1]
    raise CloudError("夸克工具没有返回可识别的结果；请检查官方工具是否已正确安装。")


def public_result(value):
    """Never surface credentials even if an external CLI includes them."""
    if isinstance(value, dict):
        return {key: ("[redacted]" if any(word in key.lower() for word in
                    ("token", "password", "secret", "authorization_code", "agent_auth_code", "code_verifier")) else public_result(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [public_result(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[redacted]", value)
        return re.sub(r"(?i)((?:access[_-]?token|refresh[_-]?token|token|password|secret|authorization_code|agent_auth_code|code_verifier)\s*[=:]\s*)[^\s&,;\"']+",
                      r"\1[redacted]", value)
    return value


class QuarkClient:
    def __init__(self, config):
        self.config = config

    def run(self, *arguments, timeout=180):
        work = _path(self.config["work_dir"])
        work.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        temporary = _path(self.config.get("temp_dir", ROOT / "data/cloud/temp"))
        temporary.mkdir(parents=True, exist_ok=True)
        for key, value in self.config.get("cli_env", {}).items():
            if key.upper() in {"HOME", "USERPROFILE", "CODEX_HOME"}:
                raise CloudError("请使用夸克工具的专用配置路径，不要改写系统用户目录。")
            env[str(key)] = str(value)
        for key in list(env):
            if key.lower() in {"temp", "tmp", "tmpdir", "npm_config_cache"}:
                env.pop(key)
        env.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary),
                   PYTHONDONTWRITEBYTECODE="1", NODE_DISABLE_COMPILE_CACHE="1",
                   npm_config_cache=str(work / "npm-cache"))
        completed = subprocess.run([*self.config["cli_command"], *arguments], cwd=work, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        result = parse_cli_output(completed.stdout)
        if completed.returncode or result.get("code", 0) not in (0, "0"):
            # Raw stdout/stderr may contain login credentials. Keep errors small.
            message = result.get("msg") or result.get("message") or "调用失败"
            code = result.get("code") if isinstance(result.get("code"), int) else "unknown"
            raise CloudError(f"夸克工具调用失败：{str(public_result(message))[:300]}（code={code}）")
        return result

    def status(self):
        return public_result(self.run("get-user-info", timeout=30))

    def create_folder(self, name):
        result = self.run("create-folder", "--dir-path", name)
        fid = result.get("data", {}).get("fid")
        if not isinstance(fid, (str, int)) or not str(fid):
            raise CloudError("夸克创建目录结果中缺少文件夹 ID。")
        return str(fid)

    def upload(self, path):
        folder = self.config.get("folder_id")
        if not folder:
            raise CloudError("尚未设置夸克战报归档目录，请先初始化云端目录。")
        result = self.run("upload", str(_path(path)), "--parent-fid", str(folder), timeout=900)
        data = result.get("data", {})
        fids = data.get("fids") or ([data["fileId"]] if data.get("fileId") else [])
        if len(fids) != 1 or not isinstance(fids[0], (str, int)):
            raise CloudError("夸克上传结果没有返回唯一的文件 ID，本地原件继续保留。")
        return str(fids[0])

    def download(self, file_id, target):
        target = _path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Isolate each transfer; never trust a remote file name to select a path.
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=".quark-download-") as folder:
            directory = Path(folder)
            self.run("download", "--fid", str(file_id), "--output-dir", str(directory),
                     "--overwrite", timeout=900)
            candidates = [p for p in directory.rglob("*") if p.is_file() and not p.is_symlink()]
            if len(candidates) != 1 or directory.resolve() not in candidates[0].resolve().parents:
                raise CloudError("夸克下载结果不是预期的单个文件。")
            _atomic(target, candidates[0].read_bytes())


def _validate(stored, ref):
    if ref["codec"] != "gzip-v1":
        raise CloudError("不支持的云端原文格式。")
    if len(stored) != ref["stored_byte_count"] or _sha(stored) != ref["stored_sha256"]:
        raise CloudError("云端压缩文件校验失败，本地原件不会被释放。")
    try:
        raw = gzip.decompress(stored)
    except (ValueError, OSError, EOFError) as exc:
        raise CloudError("云端原文解压失败。") from exc
    if len(raw) != ref["raw_byte_count"] or _sha(raw) != ref["raw_sha256"]:
        raise CloudError("云端原文字节与归档 SHA-256 不一致。")
    return raw


def _cache_path(config, ref):
    digest = str(ref["stored_sha256"])
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise CloudError("云端缓存摘要无效。")
    return _path(config["cache_dir"]) / (digest + ".json.gz")


def trim_cache(config, pinned=()):
    directory = _path(config["cache_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    protected = {Path(p).resolve() for p in pinned}
    files = sorted((p for p in directory.glob("*.json.gz") if p.is_file() and not p.is_symlink()),
                   key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    removed = 0
    for path in files:
        if total <= config["cache_max_bytes"]:
            break
        if path.resolve() not in protected:
            size = path.stat().st_size
            path.unlink()
            total -= size
            removed += 1
    return {"cache_bytes": total, "removed_files": removed}


def resolve_original(connection, file_id, db_path, *, allow_network=True):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_cloud_files'").fetchone():
        raise CloudError("原文未保存在本地，且没有已验证的云端副本。")
    row = connection.execute("SELECT * FROM report_cloud_files WHERE file_id=?", (file_id,)).fetchone()
    if row is None:
        raise CloudError("原文缺少云端位置记录。")
    ref = dict(row)
    if ref["status"] != "verified" or ref["provider"] != "quark" or not ref["remote_file_id"]:
        raise CloudError("云端副本尚未完成回读校验，不能替代本地原文。")
    file = connection.execute("SELECT sha256,byte_count FROM report_files WHERE id=?", (file_id,)).fetchone()
    if file is None or tuple(file) != (ref["raw_sha256"], ref["raw_byte_count"]):
        raise CloudError("云端记录与本地战报原件身份不匹配。")
    config = load_config(ref["config_path"])
    cache = _cache_path(config, ref)
    if cache.is_file():
        try:
            raw = _validate(cache.read_bytes(), ref)
            os.utime(cache, None)
            return raw
        except CloudError:
            if not allow_network:
                raise
    if not allow_network:
        raise CloudError("原文不在本地缓存中，请联网同步后重试核验。")
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache.parent, prefix=".quark-fetch-") as folder:
        download = Path(folder) / ref["object_name"]
        if download.name != ref["object_name"]:
            raise CloudError("云端文件名包含无效路径。")
        QuarkClient(config).download(ref["remote_file_id"], download)
        stored = download.read_bytes()
        raw = _validate(stored, ref)
        if len(stored) <= config["cache_max_bytes"]:
            _atomic(cache, stored)
    trim_cache(config, pinned=(cache,) if cache.exists() else ())
    return raw


@contextmanager
def _sync_lock(db_path):
    path = _path(str(db_path) + ".cloud.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise CloudError("已有夸克同步正在运行。") from exc
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def sync_archive(db_path, *, config_path=None, limit=10, client=None, release_local=True):
    from .report_archive import ReportArchive
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("同步数量必须为正整数。")
    config = load_config(config_path)
    if not config.get("enabled", False):
        raise CloudError("夸克同步尚未启用。")
    provider = client or QuarkClient(config)
    result = {"verified": 0, "failed": 0, "released_bytes": 0, "errors": []}
    with _sync_lock(db_path), ReportArchive(db_path) as archive:
        c = archive.connection
        c.executescript(_DDL)
        candidates = c.execute("SELECT f.id,f.sha256,f.byte_count FROM report_files f "
            "LEFT JOIN report_cloud_files q ON q.file_id=f.id "
            "WHERE f.parse_status='parsed' AND (q.status IS NULL OR q.status<>'verified') "
            "ORDER BY coalesce(q.attempts,0),f.id LIMIT ?", (limit,)).fetchall()
        for item in candidates:
            file_id, digest, size = item
            try:
                raw = archive.original_file_bytes(file_id)
                stored = gzip.compress(raw, compresslevel=6, mtime=0)
                stored_digest = _sha(stored)
                name = digest + ".json.gz"
                with c:
                    c.execute("INSERT INTO report_cloud_files(file_id,provider,status,object_name,raw_sha256,"
                        "raw_byte_count,stored_sha256,stored_byte_count,config_path) VALUES(?,'quark','pending',?,?,?,?,?,?) "
                        "ON CONFLICT(file_id) DO UPDATE SET last_error=NULL,attempts=attempts+1",
                        (file_id, name, digest, size, stored_digest, len(stored), config["config_path"]))
                ref = dict(c.execute("SELECT * FROM report_cloud_files WHERE file_id=?", (file_id,)).fetchone())
                # Never reuse a provider ID if the expected immutable object changed.
                if (ref["raw_sha256"], ref["stored_sha256"]) != (digest, stored_digest):
                    raise CloudError("云端任务摘要与原件不一致。")
                cache = _cache_path(config, ref)
                cache.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=cache.parent, prefix=".quark-upload-") as folder:
                    source = Path(folder) / name
                    source.write_bytes(stored)
                    remote_id = ref["remote_file_id"] or provider.upload(source)
                    with c:
                        c.execute("UPDATE report_cloud_files SET remote_file_id=?,status='uploaded' WHERE file_id=?",
                                  (remote_id, file_id))
                    received = Path(folder) / "verify.json.gz"
                    provider.download(remote_id, received)
                    downloaded = received.read_bytes()
                    _validate(downloaded, ref)
                    if len(downloaded) <= config["cache_max_bytes"]:
                        _atomic(cache, downloaded)
                with c:
                    c.execute("BEGIN IMMEDIATE")
                    current = c.execute("SELECT sha256,byte_count,raw_bytes FROM report_files WHERE id=?", (file_id,)).fetchone()
                    if current is None or tuple(current[:2]) != (digest, size) or _sha(bytes(current[2])) != digest:
                        raise CloudError("同步期间本地原文发生变化，未释放本地原件。")
                    c.execute("UPDATE report_cloud_files SET status='verified',verified_at=?,last_error=NULL WHERE file_id=?",
                              (_now(), file_id))
                    if release_local:
                        # Old v1 readers must refuse this archive rather than
                        # mistaking an offloaded original for an empty file.
                        c.execute("UPDATE report_meta SET value='2' WHERE key='schema_version'")
                        c.execute("UPDATE report_files SET raw_bytes=? WHERE id=?", (b"", file_id))
                result["verified"] += 1
                result["released_bytes"] += size if release_local else 0
                trim_cache(config, pinned=(cache,) if cache.exists() else ())
            except Exception as exc:
                # A transfer failure cannot turn a committed report into a failed capture.
                error = str(public_result(str(exc)))
                with c:
                    c.execute("UPDATE report_cloud_files SET last_error=? WHERE file_id=?", (error, file_id))
                result["failed"] += 1
                result["errors"].append({"file_id": file_id, "error": error})
    return result


def cloud_status(db_path, config_path=None):
    result = {"configured": False, "enabled": False, "verified": 0, "pending": 0,
              "released_original_bytes": 0, "error": None}
    try:
        config = load_config(config_path)
        result.update(configured=True, enabled=bool(config.get("enabled")),
                      cache_dir=config["cache_dir"], cache_max_bytes=config["cache_max_bytes"],
                      folder_id=config.get("folder_id"))
    except (ValueError, OSError, CloudError) as exc:
        result["error"] = str(exc)
    path = Path(db_path).resolve()
    if path.is_file():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as c:
            c.execute("PRAGMA temp_store=MEMORY")
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='report_cloud_files' AND type='table'").fetchone():
                result.update(verified=c.execute("SELECT count(*) FROM report_cloud_files WHERE status='verified'").fetchone()[0],
                    pending=c.execute("SELECT count(*) FROM report_files f LEFT JOIN report_cloud_files q ON q.file_id=f.id "
                                      "WHERE f.parse_status='parsed' AND (q.status IS NULL OR q.status<>'verified')").fetchone()[0],
                    released_original_bytes=c.execute("SELECT coalesce(sum(f.byte_count),0) FROM report_files f "
                        "JOIN report_cloud_files q ON q.file_id=f.id WHERE q.status='verified' AND length(f.raw_bytes)=0").fetchone()[0])
                last_error = c.execute("SELECT last_error FROM report_cloud_files WHERE last_error IS NOT NULL ORDER BY file_id DESC LIMIT 1").fetchone()
                if last_error:
                    result["error"] = public_result(last_error[0])
            elif c.execute("SELECT 1 FROM sqlite_master WHERE name='report_files' AND type='table'").fetchone():
                result["pending"] = c.execute("SELECT count(*) FROM report_files WHERE parse_status='parsed'").fetchone()[0]
    return result


def initialize_folder(config_path=None, client=None):
    config = load_config(config_path)
    provider = client or QuarkClient(config)
    provider.status()  # Prove a logged-in account before creating a destination.
    if not config.get("folder_id"):
        config["folder_id"] = provider.create_folder("三谋战报归档")
    config["enabled"] = True
    _atomic(config["config_path"], json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"enabled": True, "folder_id": config["folder_id"],
            "message": "已连接夸克战报归档目录；采集时会在后台上传并回读校验原文。"}


def read_authorization_handoff(path):
    """Read a private handoff with a change marker independent of wall time."""
    target = _path(path)
    try:
        raw = target.read_bytes()
        stamp = target.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    return (stamp, _sha(raw)), raw


def login_with_browser(config, *, client=None, notify=None):
    """Open only a fresh official handoff URL while the CLI waits for consent."""
    name = config.get("cli_env", {}).get("SANMOU_QUARK_AUTH_URL_FILE")
    if not name:
        return (client or QuarkClient(config)).run("login", timeout=240)
    handoff = _path(name)
    previous = read_authorization_handoff(handoff)
    baseline = previous[0] if previous else None
    stop = threading.Event()

    def watch():
        opened = None
        while not stop.is_set():
            try:
                record = read_authorization_handoff(handoff)
                if record is not None and record[0] != baseline:
                    url = json.loads(record[1]).get("url")
                    if isinstance(url, str) and url != opened:
                        opened = url
                        open_authorization_page(url, config)
                        if notify:
                            notify("夸克授权页面已在 Chrome 打开，请及时完成本人授权。")
            except (OSError, ValueError, RuntimeError) as exc:
                if notify:
                    notify("授权页面暂未打开：" + str(public_result(str(exc))))
            stop.wait(0.2)

    watcher = threading.Thread(target=watch, name="quark-login-browser", daemon=True)
    watcher.start()
    try:
        return (client or QuarkClient(config)).run("login", timeout=240)
    finally:
        stop.set()
        watcher.join(timeout=2)


def open_authorization_page(url, config):
    """Use a separate Chrome profile in the configured work directory."""
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or not (parsed.hostname == "quark.cn" or (parsed.hostname or "").endswith(".quark.cn"))
            or any(key.lower() in {"access_token", "refresh_token", "id_token"} for key in parse_qs(parsed.query))):
        raise CloudError("授权页面不是预期的夸克官方地址。")
    work = _path(config["work_dir"])
    profile, cache = work / "chrome-profile", work / "chrome-cache"
    profile.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    candidates = [shutil.which("google-chrome"), shutil.which("google-chrome-stable"), shutil.which("chrome"),
                  "C:/Program Files/Google/Chrome/Application/chrome.exe",
                  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"]
    chrome = next((str(path) for path in candidates if path and Path(path).is_file()), None)
    if chrome is None:
        raise CloudError("未找到 Google Chrome，请复制授权链接到浏览器完成确认。")
    env = os.environ.copy()
    env.update(TEMP=str(work), TMP=str(work), TMPDIR=str(work))
    subprocess.Popen([chrome, "--user-data-dir=" + str(profile), "--disk-cache-dir=" + str(cache),
        "--crash-dumps-dir=" + str(work / "chrome-crashes"), "--no-first-run", "--no-default-browser-check",
        "--disable-crash-reporter", "--new-window", url], cwd=work, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"opened": True, "profile": str(profile)}


class CloudUploadWorker:
    """Independent SQLite connection and bounded batches; never delays game RPCs."""
    def __init__(self, db_path, config_path=None):
        self.db_path, self.config_path = Path(db_path), config_path
        self.stop_event = threading.Event()
        self.thread = None
        self.last_error = None

    def start(self):
        try:
            if not load_config(self.config_path).get("enabled"):
                return self
        except (CloudError, OSError, ValueError):
            return self
        self.thread = threading.Thread(target=self._run, name="quark-battle-upload", daemon=True)
        self.thread.start()
        return self

    def _run(self):
        while not self.stop_event.is_set():
            try:
                sync_archive(self.db_path, config_path=self.config_path, limit=2)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
            self.stop_event.wait(15)

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
