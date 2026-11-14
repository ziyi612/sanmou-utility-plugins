"""Piped, RAM-only operations using the existing authorized official Quark CLI."""
from __future__ import annotations

import base64
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from .quark_cloud import CloudError, ROOT, _path, load_config, public_result

_CALL_LOCK = threading.RLock()
MAX_OBJECT_BYTES = 16 * 1024 * 1024
_TRANSPORT_ERROR = re.compile(r"\b(?:EPROTO|ECONNRESET|ETIMEDOUT|ECONNABORTED|EAI_AGAIN|UND_ERR_CONNECT_TIMEOUT|UND_ERR_SOCKET)\b|socket hang up|client network socket disconnected", re.IGNORECASE)
_TRANSPORT_CODES = frozenset({"EPROTO", "ECONNRESET", "ETIMEDOUT", "ECONNABORTED", "EAI_AGAIN", "UND_ERR_CONNECT_TIMEOUT", "UND_ERR_SOCKET"})
_SDK_TIMEOUT = re.compile(r"\bRequest timeout after [0-9]+ms:", re.IGNORECASE)


class _TransientTransportError(CloudError):
    """Only an audited official-CLI response with a known transport failure."""


def _is_transient_transport(message, result, audit=None):
    if result.get("code") in {-103, -104, -118, -1408, 400, 401, 403, 404, 422, 10001, 11001, 23018, 31001, 32003, 32004}:
        return False
    lower = message.lower()
    if any(word in lower for word in ("permission", "forbidden", "unauthorized", "access denied", "configuration", "invalid config", "size limit", "too large", "memory limit", "audit", "tls_verification_failed", "certificate_verify_failed", "certificate verify failed", "certificate verification", "权限", "未授权", "签名", "配置", "空间不足", "大小限制", "内存限制", "证书")):
        return False
    network = audit.get("network") if isinstance(audit, dict) else None
    if isinstance(network, list):
        for item in network:
            if not isinstance(item, dict):
                continue
            lifecycle = item.get("lifecycle")
            if item.get("error_code") == "TLS_VERIFICATION_FAILED" or (isinstance(lifecycle, list) and any(
                isinstance(value, dict) and (value.get("error_code") == "TLS_VERIFICATION_FAILED"
                    or value.get("ssl_error_class") == "SSLCertVerificationError"
                    or value.get("ssl_reason") == "CERTIFICATE_VERIFY_FAILED") for value in lifecycle)):
                return False
    if _TRANSPORT_ERROR.search(message) is not None:
        return True
    # The SDK can replace a concrete socket failure with its timeout wrapper.
    # Its bridge timeout phase is also explicit ETIMEDOUT evidence when the
    # SDK destroys the request before an error event can be recorded. Only the
    # known wrapper plus audited Quark HTTPS evidence enters bounded retry.
    if _SDK_TIMEOUT.search(message) is None or not isinstance(audit, dict):
        return False
    if not isinstance(network, list):
        return False
    return any(isinstance(item, dict)
        and item.get("transport_protocol") == "https:" and item.get("protocol") == "https:"
        and item.get("port") == 443
        and isinstance(item.get("hostname"), str)
        and (item["hostname"] == "open-api-drive.quark.cn" or item["hostname"].endswith(".open-drive.quark.cn"))
        and ((isinstance(item.get("error_code"), str) and item["error_code"] in _TRANSPORT_CODES)
            or (isinstance(item.get("lifecycle"), list) and any(isinstance(value, dict)
                and value.get("phase") == "timeout" for value in item["lifecycle"]))) for item in network)


@contextmanager
def _account_mutex(runtime, timeout):
    """Serialize desktop/collector processes without creating a lock file."""
    if os.name != "nt":
        yield
        return
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.ReleaseMutex.argtypes = [wintypes.HANDLE]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    key = str(runtime)
    if not key.startswith("folder:"):
        key = str(Path(runtime).resolve()).lower()
    identity = hashlib.sha256(key.encode()).hexdigest()[:32]
    handle = api.CreateMutexW(None, False, "Local\\SanmouQuarkMemory-" + identity)
    if not handle:
        raise CloudError("无法创建夸克账号内存互斥锁。")
    acquired = False
    try:
        status = api.WaitForSingleObject(handle, min(int(timeout * 1000), 0xFFFFFFFE))
        if status not in (0, 0x80):
            raise CloudError("其他窗口正在使用夸克账号，请稍后重试。")
        acquired = True
        yield
    finally:
        if acquired:
            api.ReleaseMutex(handle)
        api.CloseHandle(handle)


class MemoryQuarkClient:
    """No report bytes, browse artifacts or resumable tasks are written to disk.

    The pinned official runtime retains ownership of account credentials. Its
    fs interface is replaced by memfs for each short-lived operation; only the
    existing account configuration may be refreshed on its original drive.
    """

    def __init__(self, config_path=None, config=None):
        self.config = dict(config) if config is not None else load_config(config_path)
        self.folder_id = self.config.get("folder_id")
        self.last_audit = None

    def _call(self, action, *, timeout=240, **payload):
        # Retry the exact same immutable request. Only the typed error below
        # enters this loop; policy/configuration/size/audit failures stay final.
        stable = copy.deepcopy(payload)
        for attempt in range(3):
            try:
                response = self._call_once(action, timeout=timeout, **stable)
                self.last_audit["transport_attempts"] = attempt + 1
                return response
            except _TransientTransportError:
                self.last_audit["transport_attempts"] = attempt + 1
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)

    def _call_once(self, action, *, timeout=240, **payload):
        command = list(self.config["cli_command"])
        if len(command) < 2:
            raise CloudError("内存模式需要 Node.js 与已安装的官方夸克运行时。")
        runtime = Path(command[-1]).resolve()
        if not runtime.is_file():
            raise CloudError("未找到现有夸克官方运行时。")
        # Account refresh is the only permitted persistent write. Honor the
        # host project's drive policy before giving the SDK that capability.
        _path(runtime.parent.parent / "codex/config.json")
        runner = ROOT / "tools/quark_memory_runner.cjs"
        dependencies = ROOT / ".runtime/quark-memory/node_modules"
        if not (dependencies / "memfs/package.json").is_file() or not (dependencies / "fs-monkey/package.json").is_file():
            raise CloudError("尚未安装内存文件系统依赖，请先运行内存模式安装步骤。")
        # Keep all preloads, which own the existing authorization and F-drive
        # protection; add the RAM runner before the unchanged SDK entrypoint.
        command = [*command[:-1], str(runner), str(runtime)]
        environment = os.environ.copy()
        for key, value in self.config.get("cli_env", {}).items():
            if key.upper() in {"HOME", "USERPROFILE", "CODEX_HOME"}:
                raise CloudError("内存模式不会改写系统用户目录。")
            environment[str(key)] = str(value)
        temporary = str(_path(self.config.get("temp_dir", ROOT / ".runtime/temp")))
        for key in list(environment):
            if key.lower() in {"temp", "tmp", "tmpdir", "npm_config_cache"}:
                environment.pop(key)
        environment.update(TEMP=temporary, TMP=temporary, TMPDIR=temporary,
                           PYTHONDONTWRITEBYTECODE="1", NODE_DISABLE_COMPILE_CACHE="1",
                           npm_config_cache=str(ROOT / ".runtime/quark-memory/npm-cache"))
        request = {"action": action, "max_bytes": MAX_OBJECT_BYTES, **payload}
        with _CALL_LOCK, _account_mutex(runtime, timeout):
            completed = subprocess.run(command, input=json.dumps(request, ensure_ascii=False),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                cwd=ROOT, env=environment, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            response = json.loads(completed.stdout)
        except (ValueError, TypeError):
            raise CloudError("夸克内存适配器没有返回有效结果；未写入本地战报文件。") from None
        self.last_audit = response.get("audit")
        if not isinstance(self.last_audit, dict) or self.last_audit.get("report_disk_writes") != 0:
            raise CloudError("夸克内存模式缺少有效的不落盘审计结果。")
        physical = self.last_audit.get("physical_writes")
        if not isinstance(physical, list) or any(not isinstance(item, dict) or item.get("account_configuration_only") is not True for item in physical):
            raise CloudError("夸克内存模式实盘写入审计未通过，已拒绝继续。")
        self.last_audit["account_lock"] = "Windows named kernel mutex" if os.name == "nt" else "in-process mutex"
        if completed.returncode or not response.get("ok"):
            result = response.get("result") or {}
            message = response.get("error") or result.get("msg") or "官方夸克调用失败"
            message = str(public_result(str(message)))[:400]
            error_type = _TransientTransportError if _is_transient_transport(message, result, self.last_audit) else CloudError
            raise error_type(message)
        return response

    def _parent(self, value):
        parent = value if value is not None else self.folder_id
        if parent is None or not str(parent):
            raise CloudError("尚未配置夸克归档目录。")
        return str(parent)

    def upload_bytes(self, data: bytes, name: str, parent_fid: str | None = None) -> dict:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        if len(data) > MAX_OBJECT_BYTES:
            raise CloudError("单个内存云对象不得超过16 MiB，请先分块。")
        if not isinstance(name, str) or not name or any(ch in name for ch in ('/', '\\', ':', '\0')) or name in {'.', '..'}:
            raise ValueError("Invalid cloud object name")
        response = self._call("upload", timeout=900, parent_fid=self._parent(parent_fid), name=name,
                              body_base64=base64.b64encode(data).decode("ascii"))
        result = response["result"].get("data", {})
        fids = result.get("fids") or ([result["fileId"]] if result.get("fileId") else [])
        if len(fids) != 1 or not isinstance(fids[0], (str, int)):
            raise CloudError("上传结果缺少唯一的云端文件ID。")
        return {"fid": str(fids[0]), "size": len(data)}

    def download_bytes(self, fid, max_bytes=MAX_OBJECT_BYTES) -> bytes:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1 or max_bytes > 64 * 1024 * 1024:
            raise ValueError("max_bytes must be an integer between 1 and 64 MiB")
        response = self._call("download", timeout=900, fid=str(fid), max_bytes=max_bytes)
        try:
            data = base64.b64decode(response["body_base64"], validate=True)
        except (KeyError, ValueError):
            raise CloudError("内存下载响应格式无效。") from None
        if len(data) > max_bytes:
            raise CloudError("云端对象超过允许的内存大小。")
        return data

    def list_files(self, parent_fid) -> list[dict]:
        response = self._call("list", parent_fid=self._parent(parent_fid))
        values = response.get("items")
        if not isinstance(values, list):
            raise CloudError("云端完整目录清单缺失。")
        result = []
        for item in values:
            if not isinstance(item, dict):
                raise CloudError("云端目录清单包含无效条目。")
            value = item.get("data", item)
            fid = value.get("fid")
            name = value.get("file_name", value.get("filename"))
            if fid is None or not isinstance(name, str):
                raise CloudError("云端目录清单条目缺少名称或ID。")
            result.append({"fid": str(fid), "file_name": name, "size": int(value.get("size") or value.get("file_size") or 0),
                           "is_dir": value.get("is_dir", value.get("file_type") in (0, "0"))})
        return result

    def create_folder(self, name, parent_fid=None) -> str:
        response = self._call("mkdir", parent_fid=self._parent(parent_fid), name=str(name))
        fid = response["result"].get("data", {}).get("fid")
        if fid is None or not str(fid):
            raise CloudError("云端目录创建结果缺少ID。")
        return str(fid)
