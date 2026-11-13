"""Compact meanings keep raw events and decode only a read-only display page."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

from battle_ui.data import BattleData
from parser.report_archive import ReportArchive
from parser.report_semantics import SemanticArchive, SemanticMapper, decode_events_on_demand


@pytest.fixture
def compact_meanings():
    base = (Path(__file__).resolve().parents[1] / ".runtime" / "test-temp")
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="compact-meanings-", dir=base) as folder:
        path = Path(folder) / "reports.db"
        calls = []

        def lookup(kind, value):
            name = "测试武将" if kind == "hero" and value == 4012 else None
            return {"mapped": name is not None, "name_zh": name, "source": {"fixture": True}}

        def decode(event, context):
            calls.append(event["eventId"])
            assert context["scenario_id"] == 18
            assert context["teams"]["168"]["heroes"]["4012"]["name_zh"] == "测试武将"
            known = event["eventId"] == 211
            return {"event_name_zh": "测试事件" if known else None,
                    "summary_zh": "测试武将发生事件" if known else None,
                    "parameters": event["intList"], "status": "mapped" if known else "unmapped"}

        mapper = SemanticMapper("compact-fixture", {"fixture": True}, lookup, decode)
        payload = {"battleID": "1001", "source": "all", "runtimeIdentity": {"scenarioId": 18},
                   "data": {"battleID": "1001", "timeStamp": 1794041000,
                            "battleSettlement": {"attacker": {"army_id": 168,
                                "army_member": [{"heros_id": 4012, "index": 0, "hero_lv": 40}]}},
                            "eventDataList": [{"eventId": 211, "tab": 0, "intList": [168, 4012]},
                                              {"eventId": 99999, "tab": 1, "intList": [2**90, None, False]},
                                              {"eventId": 211, "tab": 0, "intList": [168, 4012]}]}}
        raw = json.dumps(payload, ensure_ascii=False).encode()
        with ReportArchive(path, storage_mode="compact") as archive:
            sid = archive.import_bytes(raw)["snapshot_ids"][0]
            helper = SemanticArchive(archive.connection, mapper)
            yield archive, helper, mapper, calls, sid, raw


def test_compact_enrichment_persists_names_without_event_explanations(compact_meanings):
    archive, helper, _, calls, sid, raw = compact_meanings
    result = helper.enrich_snapshot(sid, include_events=False)
    assert result["status"] == "ready" and result["event_count"] == 0
    assert calls == []
    assert archive.connection.execute("SELECT count(*) FROM report_events").fetchone()[0] == 3
    assert archive.connection.execute("SELECT count(*) FROM report_event_meanings").fetchone()[0] == 0
    assert archive.connection.execute("SELECT name_zh FROM report_id_meanings").fetchone()[0] == "测试武将"
    assert archive.connection.execute("SELECT event_mode FROM report_semantic_event_modes").fetchone()[0] == "on_demand"
    assert archive.original_bytes(sid) == raw
    assert archive.verify()["ok"]


def test_compact_import_automatically_uses_id_only_semantics(compact_meanings):
    archive, _, mapper, calls, _, raw = compact_meanings
    with ReportArchive(archive.db_path.parent / "auto.db", storage_mode="compact", semantic_mapper=mapper) as compact:
        result = compact.import_bytes(raw)
        assert result["semantics"]["failed"] == 0
        assert calls == []
        assert compact.connection.execute("SELECT count(*) FROM report_event_meanings").fetchone()[0] == 0
        assert compact.connection.execute("SELECT event_mode FROM report_semantic_event_modes").fetchone()[0] == "on_demand"
        assert compact.connection.execute("SELECT count(*) FROM report_events").fetchone()[0] == 3
        assert compact.verify()["ok"]


def test_full_enrichment_can_upgrade_ready_id_only_run(compact_meanings):
    archive, helper, _, calls, sid, _ = compact_meanings
    helper.enrich_snapshot(sid, include_events=False)
    assert helper.enrich_snapshot(sid, include_events=False)["status"] == "duplicate"
    assert calls == []
    assert helper.enrich_snapshot(sid)["event_count"] == 3
    assert calls == [211, 99999, 211]
    assert archive.connection.execute("SELECT event_mode FROM report_semantic_event_modes").fetchone()[0] == "stored"
    assert helper.enrich_snapshot(sid, include_events=False)["status"] == "duplicate"
    assert archive.connection.execute("SELECT count(*) FROM report_event_meanings").fetchone()[0] == 3


def test_explicit_backfill_includes_ready_on_demand_runs(compact_meanings):
    _, helper, _, calls, sid, _ = compact_meanings
    helper.enrich_snapshot(sid, include_events=False)
    assert helper.backfill(only_current=True)["ready"] == 1
    assert calls == [211, 99999, 211]
    assert helper.backfill()["snapshots"] == 0


def test_on_demand_uses_only_requested_page_and_small_context_read_only(compact_meanings):
    archive, helper, mapper, calls, sid, raw = compact_meanings
    helper.enrich_snapshot(sid, include_events=False)
    with ReportArchive(archive.db_path, read_only=True) as reader:
        connection = reader.connection
        events = list(connection.execute(
            "SELECT sequence,event_id,raw_json FROM report_events WHERE snapshot_id=? AND sequence=1", (sid,)))
        before = connection.total_changes

        def authorizer(action, arg1, arg2, *_):
            if action == sqlite3.SQLITE_READ and arg1 == "report_snapshots" and arg2 == "payload_json":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        result = decode_events_on_demand(connection, sid, events, mapper)
        assert result["events"][0]["parameters"] == [2**90, None, False]
        assert result["events"][0]["status"] == "unmapped"
        assert result["version"] == mapper.version
        assert calls == [99999]
        assert connection.total_changes == before
    assert archive.original_bytes(sid) == raw
    assert archive.connection.execute("SELECT count(*) FROM report_event_meanings").fetchone()[0] == 0


def test_ui_decodes_only_visible_page_and_does_not_persist_results(compact_meanings):
    archive, helper, mapper, calls, sid, _ = compact_meanings
    helper.enrich_snapshot(sid, include_events=False)
    archive.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = hashlib.sha256(archive.db_path.read_bytes()).hexdigest()
    with patch("parser.semantic_catalog.build_default_mapper", return_value=mapper):
        detail = BattleData(archive.db_path).detail(sid, event_limit=1, event_offset=1)
    assert calls == [99999]
    assert detail["event_has_more"] and len(detail["events"]) == 1
    assert detail["events"][0]["raw"]["intList"] == [2**90, None, False]
    assert detail["events"][0]["status"] == "unmapped"
    assert detail["sides"][0]["heroes"][0]["name_zh"] == "测试武将"
    assert detail["semantics"]["event_storage"] == "on_demand"
    assert detail["semantics"]["on_demand_version"] == mapper.version
    assert hashlib.sha256(archive.db_path.read_bytes()).hexdigest() == before
    assert archive.connection.execute("SELECT count(*) FROM report_event_meanings").fetchone()[0] == 0


def test_ui_reports_dictionary_failure_and_retains_raw_event(compact_meanings):
    archive, helper, _, calls, sid, _ = compact_meanings
    helper.enrich_snapshot(sid, include_events=False)
    with patch("parser.semantic_catalog.build_default_mapper", side_effect=ValueError("bad dictionary")):
        detail = BattleData(archive.db_path).detail(sid, event_limit=1)
    assert "bad dictionary" in detail["semantics"]["on_demand_error"]
    assert detail["events"][0]["status"] == "not_generated"
    assert detail["events"][0]["raw"]["eventId"] == 211
    assert calls == []


def test_ui_can_decode_compact_page_before_semantic_backfill(compact_meanings):
    archive, _, mapper, calls, _, raw = compact_meanings
    with ReportArchive(archive.db_path.parent / "no-semantic-tables.db", storage_mode="compact") as compact:
        sid = compact.import_bytes(raw)["snapshot_ids"][0]
        with patch("parser.semantic_catalog.build_default_mapper", return_value=mapper):
            detail = BattleData(compact.db_path).detail(sid, event_limit=1)
        assert calls == [211]
        assert detail["events"][0]["status"] == "mapped"
        assert detail["semantics"]["event_storage"] == "on_demand"
        assert compact.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='report_semantic_settings'").fetchone() is None


def test_existing_stored_runs_remain_idempotent_without_old_mode_row(compact_meanings):
    archive, helper, mapper, calls, sid, _ = compact_meanings
    helper.enrich_snapshot(sid)
    with archive.connection:
        archive.connection.execute("DELETE FROM report_semantic_event_modes")
    calls.clear()
    assert SemanticArchive(archive.connection, mapper).enrich_snapshot(sid)["status"] == "duplicate"
    assert calls == []


def test_bad_event_is_explicitly_failed_without_losing_other_page_results(compact_meanings):
    archive, _, mapper, calls, sid, _ = compact_meanings
    result = decode_events_on_demand(archive.connection, sid,
        [{"sequence": 8, "event_id": "211", "raw_json": "{truncated"},
         {"sequence": 9, "event_id": "211", "raw": {"eventId": 211, "intList": [168, 4012]}}], mapper)
    assert result["events"][0]["status"] == "failed"
    assert result["events"][0]["error"]
    assert result["events"][1]["status"] == "mapped"
    assert calls == [211]
