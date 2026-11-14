"""Install a pinned official Quark runtime and write a token-free local config."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from parser.report_archive import _write_path
from parser.quark_cloud import _atomic

VERSION = "1.0.25"
URL = "https://pdds.quark.cn/download/stfile/eekkigineefiegmgo/quarkclouddrive-1.0.25.zip"
SHA256 = "56c34e9a9313ae29d39475b4ff9da9e92fbfd59699bf5843e3382e54434fffb8"


def install_memory_dependencies(root):
    root = _write_path(root)
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is required for the RAM-only Quark adapter")
    npm_cli = Path(node).resolve().parent / "node_modules/npm/bin/npm-cli.js"
    if npm_cli.is_file():
        command = [node, str(npm_cli)]
    elif os.name != "nt" and shutil.which("npm"):
        command = [shutil.which("npm")]
    else:
        raise RuntimeError("Cannot locate npm's CLI next to Node.js; no global installation was changed")
    destination = _write_path(root / ".runtime/quark-memory")
    temporary = _write_path(root / ".runtime/temp")
    temporary.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    for key in list(environment):
        if key.lower() in {"temp", "tmp", "tmpdir", "npm_config_cache", "npm_config_prefix"}:
            environment.pop(key)
    environment.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary),
        npm_config_cache=str(destination / "npm-cache"), npm_config_prefix=str(destination),
        npm_config_update_notifier="false", NODE_DISABLE_COMPILE_CACHE="1")
    subprocess.run([*command, "--prefix", str(destination), "install", "--ignore-scripts", "--no-audit", "--no-fund",
                    "--save-exact", "memfs@4.80.1", "fs-monkey@1.1.0"],
        cwd=root, env=environment, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for package, version in (("memfs", "4.80.1"), ("fs-monkey", "1.1.0")):
        metadata = json.loads((destination / "node_modules" / package / "package.json").read_text(encoding="utf-8"))
        if metadata.get("version") != version:
            raise RuntimeError("RAM filesystem dependency version was not installed correctly: " + package)


def install(destination, *, root=ROOT, archive=None, config_path=None, memory_only=False):
    root = _write_path(root)
    destination = _write_path(destination)
    target = _write_path(config_path or root / "config/quark.local.json")
    original_config = target.read_bytes() if target.is_file() else None
    settings_file = _write_path(root / "config/launcher.json")
    original_settings = settings_file.read_bytes() if memory_only and settings_file.is_file() else None
    existing_config = json.loads(original_config.decode("utf-8")) if original_config is not None and memory_only else None
    settings = json.loads(original_settings.decode("utf-8")) if original_settings is not None else {}
    if memory_only and (existing_config is not None and not isinstance(existing_config, dict) or not isinstance(settings, dict)):
        raise ValueError("Existing program configuration must be a JSON object; strict mode was not activated")
    if existing_config is not None and not isinstance(existing_config.get("cli_env", {}), dict):
        raise ValueError("Existing cli_env must be a JSON object; strict mode was not activated")
    if archive is None:
        with urllib.request.urlopen(URL, timeout=60) as response:
            raw = response.read()
    else:
        raw = Path(archive).read_bytes()
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError("Official Quark runtime checksum does not match the pinned release")
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        for member in package.infolist():
            path = (destination / member.filename).resolve()
            if destination != path and destination not in path.parents:
                raise ValueError("Runtime archive contains a path outside its destination")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Runtime archive contains a symbolic link")
        # Official runtime package has scripts/references, never account data.
        for member in package.infolist():
            if member.is_dir():
                continue
            if not (member.filename.startswith(("scripts/", "references/")) or member.filename == "SKILL.md"):
                raise ValueError("Unexpected file in official runtime package")
            path = _write_path(destination / member.filename)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(package.read(member))
    config = existing_config
    if original_config is None:
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("Node.js 16+ is required; no Node installation was changed")
        config = {
            "enabled": False,
            "cli_command": [node, "--require", str(root / "tools/quark_runtime_guard.cjs"),
                            "--require", str(root / "tools/quark_http_transport.cjs"),
                            str(destination / "scripts/quark-drive.cjs")],
            "cli_env": {"SANMOU_QUARK_AUTH_URL_FILE": str(root / "data/cloud/authorization-url.json"),
                        "SANMOU_QUARK_BLOCK_C_WRITES": "0" if root.drive.lower() == "c:" else "1",
                        "SANMOU_QUARK_HTTP_TRANSPORT": "urllib", "SANMOU_QUARK_PYTHON": sys.executable},
            "cache_dir": str(root / "data/cloud/cache"), "cache_max_bytes": 1024 ** 3,
            "work_dir": str(root / "data/cloud/runtime"), "temp_dir": str(root / "data/cloud/temp"),
            "folder_id": None,
        }
        # Keep the real Codex adapter context for later desktop launches.
        # Never fabricate an Agent identity or persist per-chat IDs or homes.
        if os.environ.get("CODEX_SHELL") == "1":
            config["cli_env"]["CODEX_SHELL"] = os.environ["CODEX_SHELL"]
        if not memory_only:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Credentials remain in the official runtime's own account directory.
            with target.open("x", encoding="utf-8") as stream:
                json.dump(config, stream, ensure_ascii=False, indent=2)
    if memory_only:
        install_memory_dependencies(root)
        if "CODEX_SHELL" not in config.get("cli_env", {}) and os.environ.get("CODEX_SHELL") == "1":
            config.setdefault("cli_env", {})["CODEX_SHELL"] = os.environ["CODEX_SHELL"]
        config["storage_backend"] = "quark-memory"
        settings.update(storage_backend="quark-memory", collector_parallelism=1)
        # Validate and install everything before activating either marker. Use
        # atomic settings writes and restore the original bytes if publication
        # fails; report files are never involved in this transaction.
        try:
            _atomic(target, json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"))
            _atomic(settings_file, json.dumps(settings, ensure_ascii=False, indent=2).encode("utf-8"))
        except BaseException:
            for path, previous in ((target, original_config), (settings_file, original_settings)):
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic(path, previous)
            raise
    return {"runtime": str(destination), "config": str(target), "version": VERSION,
            "message": "官方工具已就绪，账号授权和同步状态以软件页面为准。首次初始化需在受支持的 Codex 环境中进行。"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / ".runtime/quarkclouddrive")
    parser.add_argument("--archive", type=Path, help="Use an already downloaded official ZIP")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--memory-only", action="store_true", help="Install the RAM adapter and disable local report storage")
    args = parser.parse_args()
    print(json.dumps(install(args.destination, archive=args.archive, config_path=args.config, memory_only=args.memory_only), ensure_ascii=False))
