"""Migration deletes only independently verified temporary F-drive fixtures."""
from __future__ import annotations

import gzip
import io
import json
import os
from pathlib import Path
import runpy
import sqlite3

import pytest

from parser import cloud_migration as migration
from parser import cloud_store
from parser.cloud_store import CloudStore, sha
from parser.quark_cloud import CloudError, _DDL
from parser.report_archive import ReportArchive

HELPERS = runpy.run_path(str(Path(__file__).with_name("test_cloud_store.py")))
MemoryClient, config_file, report = (HELPERS[key] for key in ("MemoryClient", "config_file", "report"))


def source(root, name, raw=b"retained"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    client, config = MemoryClient(), config_file(tmp_path)
    return root, client, config, CloudStore(config, client)


def test_default_sources_pack_stream_verify_reconstruct_then_delete_only_report_artifacts(setup, monkeypatch):
    root, client, config, store = setup
    monkeypatch.setattr(migration, "MAX_RAW_CHUNK", 4096)
    monkeypatch.setattr(cloud_store, "MAX_RAW_CHUNK", 4096)
    originals = {
        source(root, "data/incoming/runtime/account/battle_123_decrypted.json", report()): report(),
        source(root, "data/archive/backup.bin", bytes(range(256)) * 60): bytes(range(256)) * 60,
        source(root, "capture/output/processed/battle_reports/old.json", report(battle_id="124")): report(battle_id="124"),
        source(root, "output/exports/a.csv", b"id,name\n123,fixture\n"): b"id,name\n123,fixture\n",
        source(root, "logs/gui/collector.log", b"fixture old collector\n"): b"fixture old collector\n",
    }
    keep = source(root, "capture/output/processed/decompiled_lua/keep.lua", b"static-source")
    resource = source(root, "data/resource_planner/keep.json", b"private unrelated source")
    plan = migration.plan_sources(root)
    paths = {item["path"]: item["key"] for item in plan}
    result = migration.migrate_local(store, root, delete_after_verify=True)
    assert result["phase"] == "complete" and result["removed_files"] == len(originals)
    assert result["released_bytes"] == sum(map(len, originals.values()))
    assert keep.read_bytes() == b"static-source" and resource.exists()
    fresh = CloudStore(config, client)
    fresh.refresh()
    for path, raw in originals.items():
        assert not path.exists() and fresh.read_file(paths[path]) == raw
    assert client.list_calls >= 2
    assert any(phase >= 2 and name.startswith("memory-part-") for _fid, name, phase in client.downloads)


def test_explicit_single_files_and_root_identity_do_not_collide(setup, tmp_path):
    root, client, config, store = setup
    one = source(root, "output/reports/battle_debug.json", report(marker="root-one"))
    unrelated = source(root, "output/reports/resource_debug.json", b"leave")
    extra = tmp_path / "second-project"
    extra.mkdir()
    two = source(extra, "output/reports/battle_debug.json", report(marker="root-two"))
    first_result = migration.migrate_local(store, root, roots=[one], delete_after_verify=True)
    second_result = migration.migrate_local(store, extra, roots=[two], delete_after_verify=True)
    assert not one.exists() and not two.exists() and unrelated.exists()
    assert first_result["migration_id"] != second_result["migration_id"]
    histories = [key for key in store.catalog["files"] if key.startswith("history/")]
    assert len(histories) == 2
    assert {store.read_file(key) for key in histories} == {report(marker="root-one"), report(marker="root-two")}


def test_empty_explicit_scope_never_falls_back_to_all_default_sources(setup):
    root, _client, _config, store = setup
    path = source(root, "data/incoming/keep.json", report())
    assert migration.plan_sources(root, []) == []
    result = migration.migrate_local(store, root, roots=[], delete_after_verify=True)
    assert result["source_files"] == 0 and path.exists()


def test_failed_upload_keeps_all_local_sources_and_does_not_commit_completion(setup):
    root, client, _config, store = setup
    paths = [source(root, "data/incoming/one.json", report()), source(root, "data/incoming/two.json", report(battle_id="124"))]
    client.upload_hook = lambda *_args: (_ for _ in ()).throw(OSError("fixture offline"))
    with pytest.raises(OSError):
        migration.migrate_local(store, root, delete_after_verify=True)
    assert all(path.exists() for path in paths)
    assert not store.catalog["migrations"] and not store.catalog["files"]


def test_new_independent_verifier_detects_cloud_corruption_before_any_cleanup(setup):
    root, client, _config, store = setup
    path = source(root, "data/incoming/one.json", report())
    client.download_hook = lambda name, raw: raw + b"corrupt" if client.list_calls >= 2 and name.startswith("memory-part-") else raw
    with pytest.raises(CloudError, match="SHA-256"):
        migration.migrate_local(store, root, delete_after_verify=True)
    assert path.read_bytes() == report()
    assert not any(value.get("phase") == "complete" for value in store.catalog["migrations"].values())


def test_changed_same_size_source_is_not_deleted_even_when_mtime_is_restored(setup):
    root, _client, _config, store = setup
    path = source(root, "data/incoming/one.bin", b"original")
    stamp = path.stat().st_mtime_ns
    def change(event):
        if event["phase"] == "verification_complete":
            path.write_bytes(b"changed!")
            os.utime(path, ns=(stamp, stamp))
    with pytest.raises(CloudError):
        migration.migrate_local(store, root, delete_after_verify=True, progress=change)
    assert path.read_bytes() == b"changed!"


def test_background_new_file_prevents_fake_complete_or_any_initial_cleanup(setup):
    root, _client, _config, store = setup
    one = source(root, "data/incoming/one.bin", b"one")
    new = root / "data/incoming/new.bin"
    def writer(event):
        if event["phase"] == "uploaded" and not new.exists():
            new.write_bytes(b"background write")
    with pytest.raises(CloudError, match="新增"):
        migration.migrate_local(store, root, delete_after_verify=True, progress=writer)
    assert one.exists() and new.exists()


def test_resume_does_not_reupload_already_verified_source_chunks(setup):
    root, client, _config, store = setup
    one, two = source(root, "data/incoming/one.bin", b"one" * 100), source(root, "data/archive/two.bin", b"two" * 100)
    migration.migrate_local(store, root, delete_after_verify=False)
    count = len(client.chunk_uploads())
    result = migration.migrate_local(store, root, delete_after_verify=True)
    assert result["removed_files"] == 2 and not one.exists() and not two.exists()
    assert len(client.chunk_uploads()) == count


def test_outside_root_parent_traversal_and_hard_links_are_rejected(setup, tmp_path):
    root, _client, _config, _store = setup
    outside = source(tmp_path, "outside.bin", b"outside")
    with pytest.raises(ValueError):
        migration.plan_sources(root, [outside])
    with pytest.raises(ValueError):
        migration.plan_sources(root, [root / "data/../outside"])
    linked = root / "hard-link.bin"
    os.link(outside, linked)
    with pytest.raises(ValueError, match="hard-linked"):
        migration.plan_sources(root, [linked])
    assert outside.read_bytes() == b"outside"


def test_internal_symbolic_link_is_rejected_before_resolve_can_hide_it(setup):
    root, _client, _config, _store = setup
    target = source(root, "data/archive/target.bin", b"keep-target")
    link = root / "data/archive/internal-link.bin"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"Host does not permit test symlinks: {error}")
    with pytest.raises(ValueError, match="symbolic"):
        migration.plan_sources(root)
    assert target.read_bytes() == b"keep-target"


def test_walk_access_error_is_not_silently_skipped(setup, monkeypatch):
    root, _client, _config, _store = setup
    source(root, "data/incoming/one.bin")
    def blocked(*_args, onerror, **_kwargs):
        onerror(PermissionError("fixture inaccessible directory"))
        return iter(())
    monkeypatch.setattr(migration.os, "walk", blocked)
    with pytest.raises(PermissionError):
        migration.plan_sources(root)


def make_database(root, client):
    path = root / "data/database/battle_reports.db"
    inline, offloaded = report(battle_id="123"), report(battle_id="124")
    with ReportArchive(path, storage_mode="lean") as archive:
        first = archive.import_bytes(inline, namespace="one")
        second = archive.import_bytes(offloaded, namespace="one")
        compressed = gzip.compress(offloaded, mtime=0)
        fid = client.upload_bytes(compressed, sha(offloaded) + ".json.gz", parent_fid="fixture-folder")["fid"]
        archive.connection.executescript(_DDL)
        with archive.connection:
            archive.connection.execute("INSERT INTO report_cloud_files(file_id,provider,status,remote_file_id,object_name,raw_sha256,raw_byte_count,stored_sha256,stored_byte_count,config_path) VALUES(?,'quark','verified',?,?,?,?,?,?,?)",
                (second["file_id"], fid, sha(offloaded) + ".json.gz", sha(offloaded), len(offloaded), sha(compressed), len(compressed), "fixture-old-config"))
            archive.connection.execute("UPDATE report_files SET raw_bytes=? WHERE id=?", (b"", second["file_id"]))
    return path, inline, offloaded


def test_database_offloaded_raw_and_consistent_snapshot_are_independently_recoverable_without_new_sidecars(setup):
    root, client, config, store = setup
    path, inline, offloaded = make_database(root, client)
    before = {p for p in root.rglob("*") if p.is_file()}
    result = migration.migrate_local(store, root, delete_after_verify=False)
    assert result["phase"] == "verified_local_retained"
    assert {p for p in root.rglob("*") if p.is_file()} == before
    assert not Path(str(path) + "-wal").exists() and not Path(str(path) + "-shm").exists()
    fresh = CloudStore(config, client)
    fresh.refresh()
    originals = [key for key in fresh.catalog["files"] if key.startswith("database-originals/")]
    assert {fresh.read_file(key) for key in originals} == {inline, offloaded}
    snapshot_key = next(key for key in fresh.catalog["files"] if key.startswith("database-snapshots/"))
    memory = sqlite3.connect(":memory:")
    try:
        memory.deserialize(fresh.read_file(snapshot_key))
        assert memory.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert memory.execute("SELECT count(*) FROM report_files").fetchone()[0] == 2
    finally:
        memory.close()
    assert {value["battle_uid"] for value in fresh.list_reports()} == {"123", "124"}
    count = len(client.chunk_uploads())
    final = migration.migrate_local(store, root, delete_after_verify=True)
    assert final["phase"] == "complete" and not path.exists()
    assert len(client.chunk_uploads()) == count


def test_bad_old_cloud_original_keeps_database_and_never_marks_migration_complete(setup):
    root, client, _config, store = setup
    path, _inline, _offloaded = make_database(root, client)
    client.download_hook = lambda name, raw: raw + b"changed" if name.endswith(".json.gz") and not name.startswith("memory-") else raw
    with pytest.raises(CloudError, match="旧云端"):
        migration.migrate_local(store, root, delete_after_verify=True)
    assert path.exists() and not store.catalog["migrations"]


def test_missing_shm_nonempty_wal_is_preserved_without_immutable_data_loss(setup):
    root, _client, _config, store = setup
    path = source(root, "data/database/battle_reports.db", b"not-opened")
    wal = source(root, "data/database/battle_reports.db-wal", b"unmerged-wal")
    with pytest.raises(CloudError, match="非空 WAL"):
        migration.migrate_local(store, root, delete_after_verify=True)
    assert path.exists() and wal.exists() and not Path(str(path) + "-shm").exists()


def test_nonempty_wal_commits_are_merged_in_memory_snapshot_without_new_sidecars(setup):
    root, client, config, store = setup
    path = root / "data/database/battle_reports.db"
    path.parent.mkdir(parents=True)
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE committed(value TEXT)")
        writer.execute("INSERT INTO committed VALUES('only-in-wal')")
        writer.commit()
        assert Path(str(path) + "-wal").stat().st_size > 0
        names = {p for p in path.parent.iterdir()}
        migration.migrate_local(store, root, delete_after_verify=False)
        assert {p for p in path.parent.iterdir()} == names
        fresh = CloudStore(config, client)
        fresh.refresh()
        key = next(key for key in fresh.catalog["files"] if key.startswith("database-snapshots/"))
        raw = fresh.read_file(key)
        assert raw[18:20] == b"\x01\x01"
        probe = sqlite3.connect(":memory:")
        try:
            probe.deserialize(raw)
            assert probe.execute("SELECT value FROM committed").fetchone()[0] == "only-in-wal"
        finally:
            probe.close()
    finally:
        writer.close()


def test_change_after_verify_preserves_all_sources_before_first_delete(setup):
    root, _client, _config, store = setup
    first = source(root, "data/incoming/a.bin", b"original")
    last = source(root, "data/incoming/z.bin", b"original")
    stamp = last.stat().st_mtime_ns
    def change(event):
        if event["phase"] == "verification_complete":
            last.write_bytes(b"changed!")
            os.utime(last, ns=(stamp, stamp))
    with pytest.raises(CloudError, match="内容变化"):
        migration.migrate_local(store, root, delete_after_verify=True, progress=change)
    assert first.exists() and last.read_bytes() == b"changed!"


def test_partial_cleanup_failure_is_resumable_without_reupload_or_losing_prior_file(setup, monkeypatch):
    root, client, config, store = setup
    one, two = source(root, "data/incoming/a.bin", b"first"), source(root, "data/incoming/b.bin", b"second")
    keys = {item["path"]: item["key"] for item in migration.plan_sources(root)}
    original_unlink = Path.unlink
    def unavailable(path, *args, **kwargs):
        if path == two:
            raise PermissionError("fixture source became locked")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unavailable)
    with pytest.raises(PermissionError):
        migration.migrate_local(store, root, delete_after_verify=True)
    assert not one.exists() and two.exists()
    assert not any(value.get("phase") == "complete" for value in store.catalog["migrations"].values())
    count = len(client.chunk_uploads())
    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert migration.migrate_local(store, root, delete_after_verify=True)["phase"] == "complete"
    assert not two.exists() and len(client.chunk_uploads()) == count
    fresh = CloudStore(config, client)
    fresh.refresh()
    assert fresh.read_file(keys[one]) == b"first" and fresh.read_file(keys[two]) == b"second"


def legacy_remote_offline(client):
    def unavailable(name, raw):
        if name.endswith(".json.gz") and not name.startswith("memory-"):
            raise OSError("fixture legacy API unavailable")
        return raw
    client.download_hook = unavailable


def test_matching_existing_source_avoids_old_remote_download_but_new_cloud_is_verified(setup):
    root, client, config, store = setup
    path, inline, offloaded = make_database(root, client)
    original = source(root, "data/incoming/runtime/original.json", offloaded)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE report_files SET source_path=? WHERE sha256=?", (str(original), sha(offloaded)))
    connection.close()
    legacy_remote_offline(client)
    result = migration.migrate_local(store, root, delete_after_verify=True)
    assert result["phase"] == "complete" and not path.exists() and not original.exists()
    assert not any(name.endswith(".json.gz") and not name.startswith("memory-") for _fid, name, _phase in client.downloads)
    assert any(phase >= 2 and name.startswith("memory-part-") for _fid, name, phase in client.downloads)
    fresh = CloudStore(config, client)
    fresh.refresh()
    originals = [key for key in fresh.catalog["files"] if key.startswith("database-originals/")]
    assert {fresh.read_file(key) for key in originals} == {inline, offloaded}


@pytest.mark.parametrize("name_kind", ["stored-sha", "object-name"])
def test_matching_existing_gzip_cache_avoids_legacy_network_and_creates_no_new_local_cache(setup, name_kind):
    root, client, _config, store = setup
    path, inline, offloaded = make_database(root, client)
    compressed = gzip.compress(offloaded, mtime=0)
    name = (sha(compressed) if name_kind == "stored-sha" else sha(offloaded)) + ".json.gz"
    cache = source(root, "data/cloud/cache/" + name, compressed)
    store.config["cache_dir"] = str(cache.parent)
    before = {item for item in root.rglob("*") if item.is_file()}
    legacy_remote_offline(client)
    values = list(migration._db_rows(path, store, root=root))
    assert {raw for _namespace, _digest, raw in values} == {inline, offloaded}
    assert {item for item in root.rglob("*") if item.is_file()} == before
    assert not client.downloads
    result = migration.migrate_local(store, root, delete_after_verify=False)
    assert result["verified_files"] == len(before)
    assert {item for item in root.rglob("*") if item.is_file()} == before
    assert any(phase >= 2 and name.startswith("memory-part-") for _fid, name, phase in client.downloads)


def test_bad_existing_cache_falls_back_to_matching_legacy_remote_before_migration(setup):
    root, client, _config, store = setup
    path, inline, offloaded = make_database(root, client)
    compressed = gzip.compress(offloaded, mtime=0)
    # Keep the expected length so the compressed SHA is necessary to reject it.
    bad = bytes([compressed[0] ^ 1]) + compressed[1:]
    cache = source(root, "data/cloud/cache/" + sha(compressed) + ".json.gz", bad)
    store.config["cache_dir"] = str(cache.parent)
    values = list(migration._db_rows(path, store, root=root))
    assert {raw for _namespace, _digest, raw in values} == {inline, offloaded}
    assert any(name == sha(offloaded) + ".json.gz" for _fid, name, _phase in client.downloads)
    assert cache.read_bytes() == bad
    assert migration.migrate_local(store, root, delete_after_verify=True)["phase"] == "complete"


def test_bad_cache_and_offline_legacy_keep_all_sources_and_no_completion(setup):
    root, client, _config, store = setup
    path, _inline, offloaded = make_database(root, client)
    compressed = gzip.compress(offloaded, mtime=0)
    bad = bytes([compressed[0] ^ 1]) + compressed[1:]
    cache = source(root, "data/cloud/cache/" + sha(compressed) + ".json.gz", bad)
    store.config["cache_dir"] = str(cache.parent)
    legacy_remote_offline(client)
    with pytest.raises(OSError, match="legacy API"):
        migration.migrate_local(store, root, delete_after_verify=True)
    assert path.exists() and cache.read_bytes() == bad and not store.catalog["migrations"]


def test_source_path_outside_explicit_root_is_not_read_even_if_its_bytes_match(setup, tmp_path, monkeypatch):
    root, client, _config, store = setup
    path, _inline, offloaded = make_database(root, client)
    outside = source(tmp_path, "outside-original.json", offloaded)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE report_files SET source_path=? WHERE sha256=?", (str(outside), sha(offloaded)))
    connection.close()
    original_read = Path.read_bytes
    def forbidden(candidate):
        assert candidate != outside, "Archive metadata escaped the explicit migration root"
        return original_read(candidate)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    legacy_remote_offline(client)
    with pytest.raises(OSError, match="legacy API"):
        migration.migrate_local(store, root, delete_after_verify=True)
    assert path.exists() and outside.exists()


def test_fresh_verifier_groups_reads_without_skipping_whole_files_or_changing_delete_order(setup, monkeypatch):
    root, _client, _config, store = setup
    monkeypatch.setattr(migration, "MAX_RAW_CHUNK", 50)
    monkeypatch.setattr(cloud_store, "MAX_RAW_CHUNK", 50)
    for name, raw in [("a.bin", b"z" * 40), ("b.bin", b"a" * 40), ("c.bin", b"c" * 40)]:
        source(root, "data/incoming/" + name, raw)
    planned = migration.plan_sources(root)
    verified, deleted, fresh_cache_initialization = [], [], []
    original_verify, original_unlink, original_init = migration._verify_object, Path.unlink, CloudStore.__init__
    def verify(verifier, key, digest, size):
        verified.append(key)
        return original_verify(verifier, key, digest, size)
    def remove(path, *args, **kwargs):
        if path in {item["path"] for item in planned}:
            deleted.append(path)
        return original_unlink(path, *args, **kwargs)
    def initialize(instance, *args, **kwargs):
        original_init(instance, *args, **kwargs)
        if kwargs.get("compressed_cache_bytes"):
            fresh_cache_initialization.append((instance.compressed_cache_bytes, len(instance._compressed_cache)))
    monkeypatch.setattr(migration, "_verify_object", verify)
    monkeypatch.setattr(Path, "unlink", remove)
    monkeypatch.setattr(CloudStore, "__init__", initialize)
    result = migration.migrate_local(store, root, delete_after_verify=True)
    expected = sorted((item["key"] for item in planned), key=lambda key: migration._verification_key(store, key))
    assert verified == expected and len(verified) == len(planned)
    assert deleted == [item["path"] for item in planned]
    assert result["verified_files"] == len(planned) and result["removed_files"] == len(planned)
    assert fresh_cache_initialization == [(128 * 1024 * 1024, 0)]
