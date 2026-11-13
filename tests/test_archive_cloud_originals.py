"""Cloud-offloaded originals retain strict byte validation and recovery paths."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import runpy
import sys
import types

import pytest

from parser.component_recovery import ComponentRecoveryError, build_component_recovery
from parser.report_archive import COMPONENTS, ReportArchive, SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS


def sample(uid="1001"):
    data = {"battleID": uid, "timeStamp": 1794500000,
            "eventDataList": [{"eventId": 211, "intList": [123, 4012], "tab": 0}],
            "battleSettlement": {"attacker": {"army_id": 123,
                "avatar_data": {"avatar_id": 100, "avatar_name": "原始玩家"},
                "army_member": [{"heros_id": 4012, "index": 0, "hero_lv": 40,
                                 "skills": [{"skillId": 3001, "level": 10}]}]}}}
    return {"battleID": uid, "captureVersion": 2, "source": "all", "data": data,
            "components": {name: copy.deepcopy(data) if name == "all" else {} for name in COMPONENTS},
            "componentStatus": {name: {"requested": True, "available": True, "complete": True}
                                for name in COMPONENTS},
            "eventCount": 1, "serializationIssues": []}


def add(archive, value, namespace="one"):
    raw = b"\xef\xbb\xbf" + json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\r\n"
    result = archive.import_bytes(raw, namespace=namespace)
    assert result["status"] == "imported", result
    return result["snapshot_ids"][0], result["file_id"], raw


@pytest.fixture
def fake_cloud(monkeypatch):
    originals, cached, calls = {}, set(), []

    def resolve_original(connection, file_id, db_path, *, allow_network=True):
        calls.append({"file_id": file_id, "in_transaction": connection.in_transaction,
                      "allow_network": allow_network, "db_path": Path(db_path)})
        if allow_network:
            assert not connection.in_transaction, "Cloud download must finish before a verification/recovery snapshot"
            cached.add(file_id)
        elif file_id not in cached:
            raise OSError("Cloud original is unavailable in the local cache")
        return originals[file_id]

    module = types.ModuleType("parser.quark_cloud")
    module.resolve_original = resolve_original
    monkeypatch.setitem(sys.modules, "parser.quark_cloud", module)
    return originals, cached, calls, module


def offload(archive, file_id, raw, fake_cloud, *, status="verified"):
    originals, _, _, _ = fake_cloud
    originals[file_id] = raw
    with archive.connection:
        archive.connection.execute("CREATE TABLE IF NOT EXISTS report_cloud_files(file_id INTEGER PRIMARY KEY,status TEXT NOT NULL)")
        archive.connection.execute("INSERT INTO report_cloud_files VALUES(?,?)", (file_id, status))
        archive.connection.execute("UPDATE report_files SET raw_bytes=? WHERE id=?", (b"", file_id))


def test_inline_original_has_priority_and_keeps_sha_without_cloud_import(tmp_path, fake_cloud):
    with ReportArchive(tmp_path / "inline.db") as archive:
        sid, fid, raw = add(archive, sample())
        assert archive.original_file_bytes(fid) == archive.original_bytes(sid) == raw
        assert archive.verify()["ok"]
        assert fake_cloud[2] == []
        with pytest.raises(KeyError):
            archive.original_file_bytes(999)


def test_new_archive_is_v2_legacy_v1_read_write_stays_v1_until_cloud_release(tmp_path, fake_cloud):
    assert SCHEMA_VERSION == 2 and SUPPORTED_SCHEMA_VERSIONS == ("1", "2")
    path = tmp_path / "legacy-v1.db"
    with ReportArchive(path) as archive:
        sid, fid, raw = add(archive, sample())
        assert archive.connection.execute("SELECT value FROM report_meta WHERE key='schema_version'").fetchone()[0] == "2"
        with archive.connection:
            archive.connection.execute("UPDATE report_meta SET value='1' WHERE key='schema_version'")
    with ReportArchive(path, read_only=True) as reader:
        assert reader.original_bytes(sid) == raw and reader.verify()["ok"]
        assert reader.connection.execute("SELECT value FROM report_meta WHERE key='schema_version'").fetchone()[0] == "1"
    with ReportArchive(path) as writer:
        assert writer.connection.execute("SELECT value FROM report_meta WHERE key='schema_version'").fetchone()[0] == "1"
        offload(writer, fid, raw, fake_cloud)
        with writer.connection:
            writer.connection.execute("UPDATE report_meta SET value='2' WHERE key='schema_version'")
    with ReportArchive(path, read_only=True) as reader:
        assert reader.connection.execute("SELECT value FROM report_meta WHERE key='schema_version'").fetchone()[0] == "2"
        assert reader.original_bytes(sid) == raw and reader.verify()["ok"]


@pytest.mark.parametrize("status", [None, "pending", "uploaded", "failed"])
def test_missing_local_original_requires_verified_cloud_reference(tmp_path, fake_cloud, status):
    with ReportArchive(tmp_path / "unverified.db") as archive:
        sid, fid, raw = add(archive, sample())
        if status is None:
            with archive.connection:
                archive.connection.execute("UPDATE report_files SET raw_bytes=? WHERE id=?", (b"", fid))
        else:
            offload(archive, fid, raw, fake_cloud, status=status)
        with pytest.raises(ValueError, match="no verified cloud reference"):
            archive.original_bytes(sid)
        assert not archive.verify()["ok"]
        assert fake_cloud[2] == []


def test_cloud_original_export_and_verification_preserve_exact_bytes_read_only(tmp_path, fake_cloud):
    path = tmp_path / "cloud.db"
    with ReportArchive(path, storage_mode="lean") as archive:
        sid, fid, raw = add(archive, sample())
        offload(archive, fid, raw, fake_cloud)
    with ReportArchive(path, read_only=True) as reader:
        before = reader.connection.total_changes
        assert reader.original_bytes(sid) == reader.original_file_bytes(fid) == raw
        target = tmp_path / "restored.json"
        exported = reader.export_original(sid, target)
        assert target.read_bytes() == raw
        assert exported["sha256"] == hashlib.sha256(raw).hexdigest()
        fake_cloud[2].clear()
        result = reader.verify()
        assert result["ok"], result
        assert fake_cloud[2][0]["allow_network"] is True
        assert fake_cloud[2][0]["in_transaction"] is False
        assert len(fake_cloud[2]) == 1  # The fixed snapshot uses validated document hashes, never downloaded bytes.
        assert reader.connection.total_changes == before
        assert reader.connection.execute("SELECT raw_bytes FROM report_files").fetchone()[0] == b""


def test_wrong_download_fails_sha_and_is_never_exported_or_verified(tmp_path, fake_cloud):
    with ReportArchive(tmp_path / "bad-cloud.db") as archive:
        sid, fid, raw = add(archive, sample())
        offload(archive, fid, raw, fake_cloud)
        fake_cloud[0][fid] = b"wrong cloud original"
        target = tmp_path / "must-not-export.json"
        with pytest.raises(ValueError, match="Cloud original"):
            archive.export_original(sid, target)
        assert not target.exists()
        result = archive.verify()
        assert not result["ok"]
        assert any(error["check"] == "raw_bytes_sha256" for error in result["errors"])


def test_verification_needs_only_document_hashes_when_cache_disappears_after_prewarm(tmp_path, fake_cloud):
    with ReportArchive(tmp_path / "evicted-cache.db") as archive:
        _, fid, raw = add(archive, sample())
        offload(archive, fid, raw, fake_cloud)
        resolver = fake_cloud[3].resolve_original

        def disappearing_cache(connection, file_id, db_path, *, allow_network=True):
            result = resolver(connection, file_id, db_path, allow_network=allow_network)
            if allow_network:
                fake_cloud[1].discard(file_id)
            return result

        fake_cloud[3].resolve_original = disappearing_cache
        result = archive.verify()
        assert result["ok"], result
        assert sum(call["allow_network"] for call in fake_cloud[2]) == 1
        assert not archive.connection.in_transaction
        assert archive.original_bytes(1) == raw  # Verification's cache-only flag was restored.


def test_cloud_verification_supports_many_files_with_a_single_file_cache(tmp_path, fake_cloud):
    with ReportArchive(tmp_path / "tiny-cache.db") as archive:
        for index in range(5):
            _, fid, raw = add(archive, sample(str(index + 1)))
            offload(archive, fid, raw, fake_cloud)
        resolver = fake_cloud[3].resolve_original

        def single_file_cache(connection, file_id, db_path, *, allow_network=True):
            if allow_network:
                fake_cloud[1].clear()
            return resolver(connection, file_id, db_path, allow_network=allow_network)

        fake_cloud[3].resolve_original = single_file_cache
        result = archive.verify()
        assert result["ok"] and result["files_checked"] == result["snapshots_checked"] == 5, result
        assert len(fake_cloud[1]) == 1
        assert len(fake_cloud[2]) == 5
        assert all(call["allow_network"] and not call["in_transaction"] for call in fake_cloud[2])


def test_cloud_document_hash_detects_tampered_payload_without_trusting_its_projections(tmp_path, fake_cloud):
    with ReportArchive(tmp_path / "tampered-payload.db") as archive:
        sid, fid, raw = add(archive, sample())
        offload(archive, fid, raw, fake_cloud)
        with archive.connection:
            archive.connection.execute("UPDATE report_snapshots SET payload_json='{}' WHERE id=?", (sid,))
        result = archive.verify()
        assert not result["ok"]
        assert any(error["check"] == "payload_matches_original" for error in result["errors"])
        assert result["fields_checked"] == 0


def test_cloud_batch_document_hashes_preserve_snapshot_indexes(tmp_path, fake_cloud):
    with ReportArchive(tmp_path / "batch-cloud.db") as archive:
        raw = json.dumps([sample("201"), sample("202")], ensure_ascii=False).encode("utf-8")
        imported = archive.import_bytes(raw)
        assert imported["status"] == "imported"
        offload(archive, imported["file_id"], raw, fake_cloud)
        result = archive.verify()
        assert result["ok"] and result["files_checked"] == 1 and result["snapshots_checked"] == 2, result
        with archive.connection:
            archive.connection.execute("UPDATE report_snapshots SET document_index=-1 WHERE id=?",
                                       (imported["snapshot_ids"][0],))
        result = archive.verify()
        assert not result["ok"]
        assert any(error["check"] == "file_snapshot_coverage" for error in result["errors"])


def test_cloud_file_metadata_change_after_download_is_not_verified(tmp_path, fake_cloud):
    with ReportArchive(tmp_path / "changed-meta.db") as archive:
        _, fid, raw = add(archive, sample())
        offload(archive, fid, raw, fake_cloud)
        resolver = fake_cloud[3].resolve_original

        def change_after_read(connection, file_id, db_path, *, allow_network=True):
            result = resolver(connection, file_id, db_path, allow_network=allow_network)
            with connection:
                connection.execute("UPDATE report_files SET byte_count=byte_count+1 WHERE id=?", (file_id,))
            return result

        fake_cloud[3].resolve_original = change_after_read
        result = archive.verify()
        assert not result["ok"]
        assert any(error["check"] == "cloud_original_metadata" for error in result["errors"])


def test_caller_owned_transaction_only_verifies_available_cached_originals(tmp_path, fake_cloud):
    with ReportArchive(tmp_path / "outer-read.db") as archive:
        _, fid, raw = add(archive, sample())
        offload(archive, fid, raw, fake_cloud)
        archive.connection.execute("BEGIN")
        try:
            result = archive.verify()
            assert not result["ok"]
            assert all(not call["allow_network"] for call in fake_cloud[2])
            assert archive.connection.in_transaction
        finally:
            archive.connection.rollback()
        fake_cloud[1].add(fid)
        fake_cloud[2].clear()
        archive.connection.execute("BEGIN")
        try:
            assert archive.verify()["ok"]
            assert all(not call["allow_network"] for call in fake_cloud[2])
        finally:
            archive.connection.rollback()


def test_settlement_projection_repair_reads_cloud_original_before_writer_lock(tmp_path, fake_cloud):
    with ReportArchive(tmp_path / "repair-cloud.db") as archive:
        sid, fid, raw = add(archive, sample())
        offload(archive, fid, raw, fake_cloud)
        with archive.connection:
            for table in ("report_skills", "report_heroes", "report_sides"):
                archive.connection.execute(f"DELETE FROM {table} WHERE snapshot_id=?", (sid,))
        result = archive.repair_settlement_projections([sid])
        assert result["repaired"] == 1 and result["failed"] == 0, result
        assert archive.connection.execute("SELECT hero_uid FROM report_heroes").fetchone()[0] == "4012"
        assert fake_cloud[2][0]["in_transaction"] is False
        assert archive.verify()["ok"]


def test_component_recovery_cloud_sources_match_inline_result_and_enforce_hash(tmp_path, fake_cloud):
    fixture = runpy.run_path(str(Path(__file__).with_name("test_component_recovery.py")))
    donor, primary = fixture["pair"]()
    path, namespace, uid = tmp_path / "recover-cloud.db", fixture["NS"], fixture["UID"]
    with ReportArchive(path) as archive:
        donor_sid, donor_fid, donor_raw = add(archive, donor, namespace)
        primary_sid, primary_fid, primary_raw = add(archive, primary, namespace)
        arguments = {"namespace": namespace, "battle_uid": uid,
                     "primary_snapshot_id": primary_sid, "snapshot_source_id": donor_sid}
        expected = build_component_recovery(path, **arguments)
        offload(archive, donor_fid, donor_raw, fake_cloud)
        offload(archive, primary_fid, primary_raw, fake_cloud)
        result = build_component_recovery(path, **arguments)
        assert result.raw_bytes == expected.raw_bytes
        assert result.sha256 == expected.sha256
        assert [call["allow_network"] for call in fake_cloud[2]] == [True, True]
        resolver = fake_cloud[3].resolve_original

        def single_file_cache(connection, file_id, db_path, *, allow_network=True):
            if allow_network:
                fake_cloud[1].clear()
            return resolver(connection, file_id, db_path, allow_network=allow_network)

        fake_cloud[3].resolve_original = single_file_cache
        fake_cloud[2].clear()
        assert build_component_recovery(path, **arguments).raw_bytes == expected.raw_bytes
        assert len(fake_cloud[1]) == 1
        assert [call["allow_network"] for call in fake_cloud[2]] == [True, True]
        fake_cloud[0][donor_fid] = b"wrong donor original"
        with pytest.raises(ComponentRecoveryError, match="original"):
            build_component_recovery(path, **arguments)
