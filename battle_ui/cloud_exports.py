"""Create a statistics CSV in RAM and verify its copy in the existing Quark folder."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
from pathlib import PurePath
import uuid


MAX_CSV_BYTES = 16 * 1024 * 1024


def export_csv_to_quark(store, filename, headers, rows):
    """Publish a separate readable CSV; never use a local path or cloud report catalog."""
    if (not isinstance(filename, str) or not filename.lower().endswith(".csv") or
            any(character in filename for character in ("/", "\\", ":", "\0"))):
        raise ValueError("统计表名称必须是不含路径的 CSV 文件名。")
    folder = getattr(store, "folder_id", None)
    if not folder:
        raise ValueError("尚未配置夸克战报目录。")
    # The unpredictable suffix avoids replacing any previously exported statistics.
    name = (PurePath(filename).stem + "_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") +
            "_" + uuid.uuid4().hex[:10] + ".csv")
    def cell(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    for row in (headers, *rows):
        writer.writerow([cell(value) for value in row])
        if output.tell() > MAX_CSV_BYTES:
            raise ValueError("统计表超出单次云端导出的内存上限，请缩小日期或玩家范围。")
    payload = output.getvalue().encode("utf-8-sig")
    output.close()
    if len(payload) > MAX_CSV_BYTES:
        raise ValueError("统计表超出单次云端导出的内存上限，请缩小日期或玩家范围。")
    receipt = store.client.upload_bytes(payload, name, parent_fid=str(folder))
    if not isinstance(receipt, dict) or not receipt.get("fid"):
        raise ValueError("夸克上传没有返回可校验的文件标识。")
    verified = store.client.download_bytes(receipt["fid"], max_bytes=len(payload))
    digest = hashlib.sha256(payload).hexdigest()
    if len(verified) != len(payload) or hashlib.sha256(verified).hexdigest() != digest:
        raise ValueError("夸克统计表回读校验失败；没有创建本地文件，请稍后检查云端。")
    return {"name": name, "bytes": len(payload), "sha256": digest, "verified": True,
            "writes_local_report_files": False}
