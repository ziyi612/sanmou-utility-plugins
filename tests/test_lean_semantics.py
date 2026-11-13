"""Shared ID provenance remains complete, scoped and transactionally durable."""
from __future__ import annotations

import copy
import json
import pytest

from parser.report_archive import ReportArchive
from parser.report_semantics import (
    SHARED_ID_MAPPING_STORAGE, SemanticArchive, SemanticMapper, resolve_id_meaning,
)


def payload(uid="1001"):
    hero = {"heros_id": 4012, "hero_lv": 40, "index": 0,
            "skills": [{"skillId": 300401}], "warbooks": {"idList": [11, 12]},
            "armsAdvancedSkillIdList": [1], "heroSkinInfo": {"skinId": 96040}}
    data = {"battleID": uid, "timeStamp": 1794041000,
            "battleSettlement": {"attacker": {"army_id": 168, "formationId": 1, "army_member": [hero]}},
            "eventDataList": [{"eventId": 211, "tab": 0, "intList": [168, 4012]}]}
    return {"battleID": uid, "source": "all", "runtimeIdentity": {"scenarioId": 18}, "data": data,
            "components": {"basic": copy.deepcopy(data), "all": copy.deepcopy(data),
                           "snapshots": {"hero": copy.deepcopy(hero), "extra": {"equipId": 9001}},
                           "statics": {"extra": {"skillId": 400001}}}}


def lookup(kind, value):
    return {"mapped": True, "name_zh": f"名称-{kind}-{value}", "status": "mapped",
            "raw_id": value, "verification": "fixture_only",
            "source": {"path": "fixture.lua", "line": 42,
                       "detail": "完整来源说明" * 100}}


def mapper(version="shared-fixture", lookup_function=lookup):
    return SemanticMapper(version, {"fixture": True}, lookup_function,
                          lambda event, context: {"status": "mapped", "parameters": event["intList"]})


def put(archive, value):
    raw = json.dumps(value, ensure_ascii=False).encode()
    result = archive.import_bytes(raw, namespace="fixture")
    assert result["status"] == "imported", result
    assert not result.get("semantics", {}).get("failed"), result
    return result["snapshot_ids"][0], raw


def rows(connection, table="report_id_meanings"):
    return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY pointer")]


def test_shared_provenance_expands_exactly_like_full_and_compact(tmp_path):
    resolved = {}
    for mode in ("full", "compact", "lean"):
        with ReportArchive(tmp_path / (mode + ".db"), storage_mode=mode, semantic_mapper=mapper()) as archive:
            sid, raw = put(archive, payload())
            stored = rows(archive.connection)
            resolved[mode] = [resolve_id_meaning(archive.connection, item) for item in stored]
            assert resolved[mode] == rows(archive.connection, "report_resolved_id_meanings")
            assert archive.original_bytes(sid) == raw and archive.verify()["ok"]
            if mode == "lean":
                for item in stored:
                    assert json.loads(item["mapping_json"])["storage"] == SHARED_ID_MAPPING_STORAGE
                pointers = {item["pointer"] for item in stored}
                assert "/components/snapshots/extra/equipId" in pointers
                assert "/components/statics/extra/skillId" in pointers
                assert "/components/snapshots/hero/heroSkinInfo/skinId" in pointers
                assert archive.connection.execute("SELECT count(*) FROM report_event_meanings").fetchone()[0] == 0
            else:
                assert archive.connection.execute("SELECT count(*) FROM report_semantic_mapping_cache").fetchone()[0] == 0
                assert all("detail" in json.loads(item["mapping_json"])["source"] for item in stored)
    assert resolved["full"] == resolved["compact"] == resolved["lean"]


def test_identical_sources_are_shared_across_snapshots_and_force_is_idempotent(tmp_path):
    with ReportArchive(tmp_path / "lean.db", storage_mode="lean", semantic_mapper=mapper()) as archive:
        sid, _ = put(archive, payload())
        c = archive.connection
        first_cache = c.execute("SELECT count(*) FROM report_semantic_mapping_cache").fetchone()[0]
        first_links = c.execute("SELECT count(*) FROM report_semantic_id_links").fetchone()[0]
        assert first_cache < first_links
        put(archive, payload("1002"))
        assert c.execute("SELECT count(*) FROM report_semantic_mapping_cache").fetchone()[0] == first_cache
        assert c.execute("SELECT count(*) FROM report_semantic_id_links").fetchone()[0] == 2 * first_links
        helper = archive.semantic_archive()
        assert helper.enrich_snapshot(sid, include_events=False)["status"] == "duplicate"
        assert helper.enrich_snapshot(sid, force=True, include_events=False)["status"] == "ready"
        assert c.execute("SELECT count(*) FROM report_semantic_mapping_cache").fetchone()[0] == first_cache
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_full_literal_reference_like_json_is_not_interpreted_as_shared(tmp_path):
    def literal_lookup(kind, value):
        return {"storage": SHARED_ID_MAPPING_STORAGE, "mapping_ref": "literal-user-value"}
    with ReportArchive(tmp_path / "literal.db", semantic_mapper=mapper(lookup_function=literal_lookup)) as archive:
        put(archive, payload())
        first = rows(archive.connection)[0]
        assert resolve_id_meaning(archive.connection, first) == first
        assert json.loads(first["mapping_json"])["mapping_ref"] == "literal-user-value"
        assert rows(archive.connection, "report_resolved_id_meanings") == rows(archive.connection)


def test_readonly_resolver_detects_corruption_and_does_not_write(tmp_path):
    with ReportArchive(tmp_path / "lean.db", storage_mode="lean", semantic_mapper=mapper()) as archive:
        put(archive, payload())
        c = archive.connection
        first = rows(c)[0]
        with ReportArchive(archive.db_path, read_only=True) as reader:
            before = reader.connection.total_changes
            assert "detail" in json.loads(resolve_id_meaning(reader.connection, first)["source_json"])["source"]
            assert reader.connection.total_changes == before
        ref = json.loads(first["mapping_json"])["mapping_ref"]
        with c:
            c.execute("UPDATE report_semantic_mapping_cache SET mapping_json='{}' WHERE mapping_ref=?", (ref,))
        with pytest.raises(ValueError, match="hash validation"):
            resolve_id_meaning(c, first)


def test_resolver_reports_missing_link_instead_of_returning_small_reference(tmp_path):
    with ReportArchive(tmp_path / "missing-link.db", storage_mode="lean", semantic_mapper=mapper()) as archive:
        put(archive, payload())
        c = archive.connection
        first = rows(c)[0]
        with c:
            c.execute("DELETE FROM report_semantic_id_links WHERE snapshot_id=? AND pointer=? AND version=?",
                      (first["snapshot_id"], first["pointer"], first["version"]))
        with pytest.raises(ValueError, match="link is missing"):
            resolve_id_meaning(c, first)


def test_failed_meaning_insert_rolls_back_cache_links_and_preserves_original(tmp_path):
    with ReportArchive(tmp_path / "failure.db", storage_mode="lean") as archive:
        sid, raw = put(archive, payload())
        helper = SemanticArchive(archive.connection, mapper())
        archive.connection.execute("CREATE TRIGGER fail_meaning BEFORE INSERT ON report_id_meanings "
                                   "BEGIN SELECT RAISE(ABORT,'fixture rejected meaning'); END")
        result = helper.enrich_snapshot(sid, include_events=False)
        assert result["status"] == "failed" and "fixture rejected" in result["error"]
        for table in ("report_semantic_mapping_cache", "report_semantic_id_links", "report_id_meanings"):
            assert archive.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert archive.original_bytes(sid) == raw and archive.verify()["ok"]


def test_other_connection_can_commit_during_shared_lookup(tmp_path):
    with ReportArchive(tmp_path / "concurrent.db", storage_mode="lean") as archive:
        sid, raw = put(archive, payload())
        committed = []
        with ReportArchive(archive.db_path, storage_mode="lean") as writer:
            def concurrent_lookup(kind, value):
                if not committed:
                    committed.append(True)
                    put(writer, payload("1002"))
                return lookup(kind, value)
            helper = SemanticArchive(archive.connection, mapper("concurrent", concurrent_lookup))
            result = helper.enrich_snapshot(sid, include_events=False)
            assert result["status"] == "ready", result
            assert archive.connection.execute("SELECT count(*) FROM report_snapshots").fetchone()[0] == 2
            assert archive.original_bytes(sid) == raw
