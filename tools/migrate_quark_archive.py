"""Upload existing compressed archives through RAM, verify, and optionally clean sources."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class InputError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        if message.startswith("unrecognized arguments:"):
            message = "无法识别命令参数，请使用 --help 查看支持的参数。"
        raise InputError(message)


COUNT_KEYS = {
    "source_files", "source_bytes", "source_parts", "total_files", "total_bytes", "total_parts",
    "processed_files", "processed_bytes", "uploaded_files", "uploaded_bytes", "uploaded_parts",
    "verified_files", "verified_bytes", "verified_parts", "removed_files", "released_bytes",
    "parts", "files", "bytes", "count", "file_count", "part_count", "object_count",
    "completed_files", "completed_parts", "skipped_files", "failed_files", "removed_bytes",
}
PHASES = {
    "preflight", "preflight_file", "preflight_complete", "ready", "resuming", "uploading",
    "uploaded", "uploaded_part", "uploaded_file", "verified_part", "verifying", "verified_file",
    "verification_complete", "verified", "verified_only", "verified_local_retained", "complete",
    "retained", "progress", "failed", "partial", "partial_cleanup", "incomplete", "stopped",
}
SUCCESS_PHASES = {"complete", "verified", "verified_only", "verified_local_retained", "retained"}


def build_parser():
    parser = Parser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="明确的源根目录；所有归档必须位于其内。")
    parser.add_argument("--source", type=Path, action="append", required=True, help="已有归档文件，可重复传入。")
    parser.add_argument("--config", type=Path, help="现有夸克本地配置，不会修改。")
    parser.add_argument("--folder-name", default="旧完整战报归档", help="现有授权目录下的独立冷归档文件夹名称。")
    parser.add_argument("--expected-sha256", action="append", default=[], metavar="RELATIVE_PATH=HEX",
                        help="可重复指定相对根目录的预期 SHA-256；全部源预检通过后才云写。")
    parser.add_argument("--delete-local", action="store_true", help="全部云端独立回读及源复核通过后清理本地；默认保留。")
    parser.add_argument("--verify-only", action="store_true", help="只验证已有云端对象，不创建、上传或删除。")
    return parser


def expected_hashes(values):
    result = {}
    for value in values:
        if "=" not in value:
            raise InputError("--expected-sha256 需要 RELATIVE_PATH=64位十六进制摘要。")
        relative, digest = value.split("=", 1)
        relative = relative.replace("\\", "/")
        path = PurePosixPath(relative)
        if (not relative or path.is_absolute() or PureWindowsPath(relative).drive or ":" in relative
                or ".." in relative.split("/") or path.as_posix() == "."):
            raise InputError("预期摘要的路径必须是根目录内的相对文件路径，不能含父级跳转。")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise InputError("预期 SHA-256 必须是64位十六进制摘要。")
        key, digest = path.as_posix(), digest.lower()
        if key in result and result[key] != digest:
            raise InputError("同一相对文件被指定了冲突的预期 SHA-256。")
        result[key] = digest
    return result


def _safe_error(error):
    from parser.quark_cloud import public_result
    text = str(public_result(str(error)))
    text = re.sub(r"(?i)https?://[^\s\"'<>]+", "[URL omitted]", text)
    text = re.sub(r"(?is)\b(?:payload|body|request_body|response_body)[\"']?\s*[=:].*",
                  "[request details omitted]", text)
    return " ".join(text.split())[:320] or "冷归档操作未完成。"


def emit_counts(value):
    output = {}
    phase = value.get("phase")
    if phase is not None:
        output["phase"] = phase if phase in PHASES else "progress"
    for key in COUNT_KEYS:
        item = value.get(key)
        if (type(item) in (int, float) and item >= 0
                and (not isinstance(item, float) or math.isfinite(item))):
            output[key] = item
    if type(value.get("ok")) is bool:
        output["ok"] = value["ok"]
    print(json.dumps(output, ensure_ascii=False, sort_keys=True), flush=True)


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        if args.verify_only and args.delete_local:
            raise InputError("--verify-only 为只读操作，不能与 --delete-local 同时使用。")
        if not args.folder_name.strip() or args.folder_name in (".", "..") or any(char in args.folder_name for char in ("/", "\\", "\0")):
            raise InputError("冷归档目录名必须是一个非空文件夹名称。")
        expected = expected_hashes(args.expected_sha256)
        from parser.cold_archive import migrate_cold_archives
        result = migrate_cold_archives(args.root, args.source, config_path=args.config,
            folder_name=args.folder_name, expected_sha256=expected, delete_local=args.delete_local,
            verify_only=args.verify_only, progress=emit_counts)
        if not isinstance(result, dict):
            raise RuntimeError("冷归档没有返回可核验的结果。")
        emit_counts(result)
        return 0 if (result.get("phase") in SUCCESS_PHASES and result.get("ok") is not False
                     and not result.get("failed_files")) else 2
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "操作已中断；请重新验证云端状态后再继续。"}, ensure_ascii=False), flush=True)
        return 2
    except Exception as error:
        print(json.dumps({"ok": False, "error": _safe_error(error), "error_type": type(error).__name__},
                         ensure_ascii=False), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
