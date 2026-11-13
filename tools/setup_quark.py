"""Install a pinned official Quark runtime and write a token-free local config."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from parser.report_archive import _write_path

VERSION = "1.0.25"
URL = "https://pdds.quark.cn/download/stfile/eekkigineefiegmgo/quarkclouddrive-1.0.25.zip"
SHA256 = "56c34e9a9313ae29d39475b4ff9da9e92fbfd59699bf5843e3382e54434fffb8"


def install(destination, *, root=ROOT, archive=None, config_path=None):
    root = _write_path(root)
    destination = _write_path(destination)
    target = _write_path(config_path or root / "config/quark.local.json")
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
    if not target.exists():
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
        target.parent.mkdir(parents=True, exist_ok=True)
        # Credentials remain in the official runtime's own account directory.
        with target.open("x", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
    return {"runtime": str(destination), "config": str(target), "version": VERSION,
            "message": "官方工具已就绪，账号授权和同步状态以软件页面为准。首次初始化需在受支持的 Codex 环境中进行。"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / ".runtime/quarkclouddrive")
    parser.add_argument("--archive", type=Path, help="Use an already downloaded official ZIP")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    print(json.dumps(install(args.destination, archive=args.archive, config_path=args.config), ensure_ascii=False))
