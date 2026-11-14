"""Cloud archive browsing must use only fresh cloud reads and RAM SQLite."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3

import pytest

from battle_ui.cloud_data import CloudBattleData
from battle_ui.data import BattleDataError


def sample(uid="1001", player="云端玩家"):
    data = {"battleID": uid, "timeStamp": 1794700000,
            "eventDataList": [{"eventId": 101, "intList": [0, None, 2**90]},
                              {"eventId": 999999, "tab": 0, "intList": [False]}],
            "battleSettlement": {"attacker": {"army_id": 0,
                "avatar_data": {"avatar_id": 0, "avatar_name": player},
                "army_member": [{"heros_id": 999999, "index": 0, "hero_lv": 0,
                    "origin_troop_num": 0, "troop_num": None,
                    "skills": [{"skillId": 888888, "level": 0}]}]}}}
    return {"battleID": uid, "source": "all", "captureVersion": 2, "eventCount": 2,
        "data": data, "components": {key: copy.deepcopy(data) if key == "all" else {}
            for key in ("basic", "detail", "all", "statics", "snapshots")},
        "componentStatus": {key: {"requested": True, "available": True, "complete": True}
            for key in ("basic", "detail", "all", "statics", "snapshots")}, "serializationIssues": []}


class Store:
    def __init__(self, document=None):
        self.raw = json.dumps(document or sample(), ensure_ascii=False).encode()
        self.rows = [{"key": "one/1001", "namespace": "one", "battle_uid": "1001", "document_index": 0,
            "completeness": "complete", "event_count": 2, "source": "all", "attacker_name": "云端玩家",
            "timeStamp": 1794700000, "imported_at": "2026-11-15T00:00:00Z", "byte_count": len(self.raw)}]
        self.offline = False
        self.refreshes = 0
        self.reads = []

    def refresh(self):
        self.refreshes += 1
        if self.offline:
            raise OSError("fixture cloud offline")
        return {"reports": self.rows}

    def list_reports(self):
        return [dict(row) for row in self.rows]

    def read_report(self, key):
        self.reads.append(key)
        return self.raw

    def status(self):
        return {"reports": len(self.rows), "files": 1, "disk_cache_bytes": 0}


@pytest.fixture(autouse=True)
def no_dictionary_disk_work(monkeypatch):
    from parser import semantic_catalog
    monkeypatch.setattr(semantic_catalog, "build_default_mapper", lambda: None)


def test_cloud_detail_uses_only_ram_sqlite_and_preserves_raw_event_values(monkeypatch):
    store = Store()
    data = CloudBattleData(store)
    connections = []
    original_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        assert database == ":memory:", "Cloud detail opened a filesystem SQLite database"
        connection = original_connect(database, *args, **kwargs)
        connections.append(connection)
        return connection

    def forbidden(*_args, **_kwargs):
        pytest.fail("Cloud browsing tried to write a local file or directory")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    row = data.list_reports()["items"][0]
    detail = data.detail(row["snapshot_id"], event_limit=1)
    assert len(connections) == 1 and not hasattr(data, "db_path")
    assert detail["report"]["battle_uid"] == "1001"
    assert detail["report"]["snapshot_id"] == row["snapshot_id"]
    assert detail["events"][0]["raw"]["intList"] == [0, None, 2**90]
    hero = detail["sides"][0]["heroes"][0]
    assert hero["hero_uid"] == "999999" and hero["level"] == 0
    assert hero["troops_initial"] == 0 and hero["troops_remaining"] is None
    assert detail["event_has_more"] is True and detail["allow_export"] is False
    second = data.detail(row["snapshot_id"], event_limit=1, event_offset=1)
    assert second["events"][0]["sequence"] == 1
    assert second["events"][0]["event_id"] == "999999"
    assert second["event_has_more"] is False


def test_cloud_browser_cannot_fall_back_to_stale_ram_when_cloud_is_offline():
    store = Store()
    data = CloudBattleData(store)
    selected = data.list_reports()["items"][0]
    store.offline = True
    for operation in (data.overview, data.list_reports, lambda: data.detail(selected["snapshot_id"]),
                      lambda: data.original_preview(selected["snapshot_id"])):
        with pytest.raises(OSError, match="offline"):
            operation()
    assert not store.reads


def test_cloud_batched_original_selects_correct_document_and_rejects_identity_mismatch():
    store = Store([sample("9000"), sample()])
    store.rows[0]["document_index"] = 1
    data = CloudBattleData(store)
    selected = data.list_reports()["items"][0]
    assert data.detail(selected["snapshot_id"])["report"]["battle_uid"] == "1001"
    store.rows[0]["document_index"] = 0
    with pytest.raises(BattleDataError, match="不匹配"):
        data.detail(selected["snapshot_id"])


def test_cloud_search_and_best_version_are_scoped_to_namespace():
    store = Store()
    store.rows += [{**store.rows[0], "key": "one/1001-partial", "source": "basic", "event_count": 0,
                    "completeness": "partial"},
                   {**store.rows[0], "key": "two/1001", "namespace": "two", "attacker_name": "另一人"}]
    data = CloudBattleData(store)
    overview = data.overview()
    assert overview["total"] == 2 and len(overview["accounts"]) == 2
    assert data.list_reports(namespace="one")["items"][0]["key"] == "one/1001"
    assert len(data.list_reports(search="1001")["items"]) == 2
    assert data.list_reports(search="另一")["items"][0]["namespace"] == "two"
    assert data.list_reports(status="partial")["items"] == []


def test_cloud_preview_is_exact_and_local_export_is_forbidden(tmp_path):
    store = Store()
    data = CloudBattleData(store)
    selected = data.list_reports()["items"][0]
    preview = data.original_preview(selected["snapshot_id"], max_bytes=100)
    assert preview["text"] == store.raw[:100].decode("utf-8", "replace")
    assert preview["bytes"] == len(store.raw) and preview["truncated"]
    target = tmp_path / "must-not-exist.json"
    with pytest.raises(PermissionError, match="禁止"):
        data.export_original(selected["snapshot_id"], target)
    assert not target.exists()


def test_cloud_large_report_is_rejected_without_disk_fallback():
    store = Store()
    data = CloudBattleData(store, max_report_bytes=10)
    selected = data.list_reports()["items"][0]
    with pytest.raises(BattleDataError, match="内存读取上限"):
        data.detail(selected["snapshot_id"])
    assert not store.reads
