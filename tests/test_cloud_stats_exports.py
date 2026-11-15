from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from battle_ui import cloud_exports


class Client:
    def __init__(self):
        self.calls = []
        self.payload = None
        self.corrupt = False

    def upload_bytes(self, payload, name, parent_fid):
        self.calls.append(("upload", name, parent_fid))
        self.payload = payload
        return {"fid": "fresh-csv", "size": len(payload)}

    def download_bytes(self, fid, max_bytes):
        self.calls.append(("download", fid, max_bytes))
        return b"wrong" if self.corrupt else self.payload


class Store:
    folder_id = "existing-cloud-folder"

    def __init__(self):
        self.client = Client()


def test_csv_goes_from_ram_to_existing_quark_folder_and_is_verified(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Cloud CSV attempted a local file operation")
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    store = Store()
    headers, rows = ["姓名", "武勋"], [["云端玩家", 123], ["=SUM(A1)", 0], ["-1", None]]
    result = cloud_exports.export_csv_to_quark(store, "daily.csv", headers, rows)
    assert result["verified"] and result["writes_local_report_files"] is False
    assert store.client.calls[0][2] == store.folder_id
    assert store.client.calls[1] == ("download", "fresh-csv", len(store.client.payload))
    assert list(csv.reader(io.StringIO(store.client.payload.decode("utf-8-sig")))) == [
        headers, ["云端玩家", "123"], ["'=SUM(A1)", "0"], ["'-1", ""]]
    assert rows[1][0] == "=SUM(A1)"
    other = cloud_exports.export_csv_to_quark(store, "daily.csv", headers, rows)
    assert result["name"] != other["name"]


def test_corrupt_export_fails_verification_without_local_fallback():
    store = Store()
    store.client.corrupt = True
    with pytest.raises(ValueError, match="校验失败"):
        cloud_exports.export_csv_to_quark(store, "daily.csv", ["列"], [[1]])


@pytest.mark.parametrize("filename", ["../daily.csv", "F:\\daily.csv", "daily.xlsx", "x/y.csv"])
def test_invalid_export_name_never_uploads(filename):
    store = Store()
    with pytest.raises(ValueError):
        cloud_exports.export_csv_to_quark(store, filename, [], [])
    assert store.client.calls == []


def test_oversize_export_does_not_upload_or_save_to_disk(monkeypatch):
    monkeypatch.setattr(cloud_exports, "MAX_CSV_BYTES", 50)
    store = Store()
    with pytest.raises(ValueError, match="内存上限"):
        cloud_exports.export_csv_to_quark(store, "daily.csv", ["列"], [["云" * 49]])
    assert store.client.calls == []
