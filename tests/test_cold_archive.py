"""Offline integrity and cleanup proofs for opaque large archive migration."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from parser import cold_archive as module
from parser.cloud_store import CloudStore, sha
from parser.quark_cloud import CloudError


class FolderClient:
    def __init__(self):
        self.entries = {}
        self.uploads, self.downloads, self.lists, self.creates = [], [], [], []
        self.upload_hook = self.download_hook = None

    def _add(self, name, raw=None, parent="authorized-parent", is_dir=False):
        fid = str(len(self.entries) + 1)
        self.entries[fid] = {"fid": fid, "file_name": name, "raw": raw, "parent": parent, "is_dir": is_dir}
        return fid

    def list_files(self, parent):
        self.lists.append(str(parent))
        return [{key: value for key, value in item.items() if key != "raw"}
                for item in self.entries.values() if item["parent"] == str(parent)]

    def create_folder(self, name, *, parent_fid):
        self.creates.append((name, parent_fid))
        return self._add(name, parent=str(parent_fid), is_dir=True)

    def upload_bytes(self, raw, name, *, parent_fid):
        if self.upload_hook:
            self.upload_hook(name, raw)
        fid = self._add(name, bytes(raw), str(parent_fid))
        self.uploads.append((fid, name, str(parent_fid)))
        return {"fid": fid}

    def download_bytes(self, fid, *, max_bytes):
        entry = self.entries[str(fid)]
        self.downloads.append((str(fid), entry["file_name"], max_bytes))
        raw = entry["raw"]
        if self.download_hook:
            raw = self.download_hook(entry["file_name"], raw)
        if len(raw) > max_bytes:
            raise CloudError("fixture bounded read")
        return raw

    def parts(self):
        return [item for item in self.uploads if item[1].startswith("memory-part-")]


def config_file(root):
    config = root / "quark.local.json"
    config.write_text(json.dumps({"cli_command": ["fixture-unused"], "folder_id": "authorized-parent",
        "memory_folder_id": "active-reports", "cache_dir": str(root / "unused-cache"),
        "work_dir": str(root / "unused-work")}), encoding="utf-8")
    return config


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "COLD_RAW_BYTES", 64)
    root = tmp_path / "sources"
    root.mkdir()
    config = config_file(tmp_path)
    client = FolderClient()
    return root, config, client


def source(root, name="archive.zip", raw=b"opaque-archive" * 23):
    path = root / name
    path.write_bytes(raw)
    return path, raw


def run(fixture, sources, **kwargs):
    root, config, client = fixture
    return module.migrate_cold_archives(root, sources, config_path=config, client=client, **kwargs)


def test_preflight_checks_all_hashes_before_any_cloud_call(fixture):
    root, _config, client = fixture
    first, _ = source(root)
    second, _ = source(root, "second.tar.zst")
    with pytest.raises(CloudError, match="预期"):
        run(fixture, [first, second], expected_sha256={"second.tar.zst": "0" * 64}, delete_local=True)
    assert first.exists() and second.exists()
    assert client.lists == client.creates == client.uploads == client.downloads == []


def test_complete_cloud_proof_retains_by_default_and_standard_reader_restores(fixture):
    root, config, client = fixture
    path, raw = source(root)
    config_before = config.read_bytes()
    result = run(fixture, [path], expected_sha256={path.name: sha(raw)})
    assert result["phase"] == "verified_local_retained" and result["removed_files"] == 0
    assert result["verified_bytes"] == len(raw) and path.read_bytes() == raw
    assert client.creates == [(module.DEFAULT_FOLDER_NAME, "authorized-parent")]
    assert "active-reports" not in client.lists
    assert config.read_bytes() == config_before
    reader = module.cold_archive_reader(config, client, folder_id=result["folder_id"])
    assert reader.config["memory_folder_id"] == result["folder_id"]
    assert reader._cached_chunk is None and not reader._compressed_cache
    assert b"".join(reader.iter_file(result["keys"][0])) == raw
    assert all(parent == result["folder_id"] for _fid, _name, parent in client.uploads)
    assert all(gzip.decompress(client.entries[fid]["raw"]) for fid, _name, _parent in client.parts())


def test_all_sources_cloud_and_local_full_sha_precede_first_unlink(fixture, monkeypatch):
    root, _config, client = fixture
    first, first_raw = source(root, "one.zip", b"one" * 31)
    second, second_raw = source(root, "two.zip", b"two" * 33)
    hashes, unlinks, verified = [], [], []
    original_hash, original_unlink = module.file_sha, Path.unlink
    def tracking_hash(path):
        hashes.append(Path(path))
        return original_hash(path)
    def tracking_unlink(path, *args, **kwargs):
        assert verified == [1, 2]
        assert hashes.count(first) >= 2 and hashes.count(second) >= 2
        assert hashes.count(path) >= 3
        unlinks.append(path)
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(module, "file_sha", tracking_hash)
    monkeypatch.setattr(Path, "unlink", tracking_unlink)
    result = run(fixture, [first, second], delete_local=True,
                 progress=lambda event: verified.append(event["verified_files"])
                 if event["phase"] == "verified_file" else None)
    assert result["phase"] == "complete" and result["removed_files"] == 2
    assert result["released_bytes"] == len(first_raw) + len(second_raw)
    assert unlinks == [first, second]


def test_same_size_preserved_mtime_tamper_after_reuse_rejects_before_any_delete(fixture):
    root, _config, client = fixture
    first, raw = source(root, "one.zip")
    second, _ = source(root, "two.zip")
    run(fixture, [first, second])
    previous_uploads = len(client.uploads)
    before = first.stat()
    def tamper(event):
        if event["phase"] == "verification_complete":
            first.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
            os.utime(first, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(CloudError, match="本地归档"):
        run(fixture, [first, second], delete_local=True, progress=tamper)
    assert first.exists() and second.exists()
    assert len(client.uploads) == previous_uploads


@pytest.mark.parametrize("failure", ["part-upload", "part-readback", "catalog-upload", "fresh-readback"])
def test_cloud_failure_never_deletes_any_source(fixture, failure):
    root, _config, client = fixture
    path, _raw = source(root)
    part_reads = {}
    def upload(name, _raw):
        if failure == "part-upload" and name.startswith("memory-part-") or failure == "catalog-upload" and name.startswith("memory-catalog-"):
            raise OSError("fixture cloud unavailable")
    def download(name, raw):
        if name.startswith("memory-part-"):
            part_reads[name] = part_reads.get(name, 0) + 1
            if failure == "part-readback" or failure == "fresh-readback" and part_reads[name] >= 2:
                return bytes([raw[0] ^ 1]) + raw[1:]
        return raw
    client.upload_hook, client.download_hook = upload, download
    with pytest.raises((OSError, CloudError)):
        run(fixture, [path], delete_local=True)
    assert path.exists()


def test_independent_verifier_starts_empty_and_rereads_verified_parts(fixture, monkeypatch):
    root, _config, client = fixture
    path, raw = source(root)
    old_reader = module.cold_archive_reader
    readers = []
    def create_reader(*args, **kwargs):
        fresh = old_reader(*args, **kwargs)
        assert fresh._cached_chunk is None and not fresh._compressed_cache
        assert fresh.compressed_cache_bytes == 128 * 1024 * 1024
        readers.append(fresh)
        return fresh
    monkeypatch.setattr(module, "cold_archive_reader", create_reader)
    run(fixture, [path])
    assert len(readers) == 1
    for fid, _name, _parent in client.parts():
        assert sum(item[0] == fid for item in client.downloads) == 2
    assert not path.is_dir() and path.read_bytes() == raw


def test_in_process_retry_reuses_only_successful_parts_and_final_rereads(fixture):
    root, config, client = fixture
    path, raw = source(root, raw=b"A" * 64 + b"B" * 64 + b"C" * 23)
    failures = [True]
    def reject_second(name, stored):
        if name.startswith("memory-part-") and gzip.decompress(stored) == b"B" * 64 and failures:
            failures.pop()
            raise OSError("fixture second part failed")
    client.upload_hook = reject_second
    with pytest.raises(OSError) as raised:
        run(fixture, [path], delete_local=True)
    store = raised.value.cold_archive_store
    assert len(store._verified_parts) == 1 and path.exists()
    first_fid = client.parts()[0][0]
    assert sum(item[0] == first_fid for item in client.downloads) == 1
    client.upload_hook = None
    result = run(fixture, [path], delete_local=True, archive_store=store)
    assert result["phase"] == "complete" and not path.exists()
    assert len(client.parts()) == 3
    assert sum(item[0] == first_fid for item in client.downloads) == 2
    assert b"".join(module.cold_archive_reader(config, client, folder_id=result["folder_id"]).iter_file(result["keys"][0])) == raw


def test_verify_only_does_not_upload_create_or_delete(fixture):
    root, _config, client = fixture
    path, _raw = source(root)
    run(fixture, [path])
    before = len(client.uploads), len(client.creates)
    result = run(fixture, [path], verify_only=True)
    assert result["phase"] == "verified_only" and path.exists()
    assert (len(client.uploads), len(client.creates)) == before
    with pytest.raises(ValueError):
        run(fixture, [path], verify_only=True, delete_local=True)


def test_verify_only_missing_folder_never_creates(fixture):
    root, _config, client = fixture
    path, _raw = source(root)
    with pytest.raises(CloudError, match="不存在"):
        run(fixture, [path], verify_only=True)
    assert not client.creates and not client.uploads and path.exists()


@pytest.mark.parametrize("duplicates,is_dir", [(1, False), (2, True)])
def test_same_name_folder_ambiguity_fails_closed(fixture, duplicates, is_dir):
    root, _config, client = fixture
    path, _raw = source(root)
    for _ in range(duplicates):
        client._add(module.DEFAULT_FOLDER_NAME, is_dir=is_dir)
    with pytest.raises(CloudError, match="同名"):
        run(fixture, [path], delete_local=True)
    assert not client.creates and not client.uploads and path.exists()


@pytest.mark.parametrize("kind", ["outside", "parent", "hardlink", "directory", "empty"])
def test_source_boundaries_fail_before_cloud_calls(fixture, kind):
    root, _config, client = fixture
    path, _raw = source(root)
    if kind == "outside":
        selected = [root.parent / "other.zip"]
        selected[0].write_bytes(b"outside")
    elif kind == "parent":
        selected = [root / "child" / ".." / path.name]
    elif kind == "hardlink":
        alias = root / "linked.zip"
        os.link(path, alias)
        selected = [alias]
    elif kind == "directory":
        selected = [root]
    else:
        selected = []
    with pytest.raises((ValueError, FileNotFoundError)):
        run(fixture, selected, delete_local=True)
    assert client.lists == client.creates == client.uploads == []
    assert path.exists()


def test_source_link_rejected_before_cloud_calls(fixture):
    root, _config, client = fixture
    path, _raw = source(root)
    alias = root / "symlink.zip"
    try:
        alias.symlink_to(path)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError):
        run(fixture, [alias], delete_local=True)
    assert not client.lists and path.exists()


def test_partial_cleanup_reports_incomplete_and_preserves_remaining(fixture, monkeypatch):
    root, _config, _client = fixture
    first, _ = source(root, "one.zip")
    second, _ = source(root, "two.zip")
    unlink = Path.unlink
    def fail_second(path, *args, **kwargs):
        if path == second:
            raise PermissionError("fixture locked source")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_second)
    result = run(fixture, [first, second], delete_local=True)
    assert result["phase"] == "partial_cleanup" and result["ok"] is False
    assert result["removed_files"] == 1 and result["failed_files"] == 1
    assert not first.exists() and second.exists()


def test_upload_expected_digest_change_never_publishes_catalog(fixture):
    _root, config, client = fixture
    folder = client._add(module.DEFAULT_FOLDER_NAME, is_dir=True)
    store = module.ColdArchiveStore(config, client, folder_id=folder)
    with pytest.raises(CloudError, match="SHA-256"):
        store.put_stream("source", io.BytesIO(b"opaque changed bytes"), 20, expected_sha256="0" * 64)
    assert not store.catalog["files"] and store.catalog_fid is None
    assert not [item for item in client.uploads if item[1].startswith("memory-catalog-")]


def test_level_zero_part_limit_and_default_report_writes_remain_separate(fixture):
    _root, config, client = fixture
    folder = client._add(module.DEFAULT_FOLDER_NAME, is_dir=True)
    store = module.ColdArchiveStore(config, client, folder_id=folder)
    ref = store._archive_part(bytes(range(64)))
    stored = client.entries[ref["fid"]]["raw"]
    assert stored == gzip.compress(bytes(range(64)), compresslevel=0, mtime=0)
    assert ref["stored_size"] > ref["raw_size"]
    assert client.downloads[-1][2] == 50 * 1024 * 1024
    from parser.cloud_store import MAX_STORED_CHUNK
    assert MAX_STORED_CHUNK == 8 * 1024 * 1024
    with pytest.raises(CloudError, match="48 MiB"):
        store._archive_part(bytes(65))


def test_metadata_commit_failure_retains_source_and_retry_does_not_reupload_part(fixture):
    root, _config, client = fixture
    path, _raw = source(root, raw=b"small opaque")
    def reject_catalog(name, _raw):
        if name.startswith("memory-catalog-"):
            raise OSError("fixture metadata publish failed")
    client.upload_hook = reject_catalog
    with pytest.raises(OSError) as raised:
        run(fixture, [path], delete_local=True)
    assert path.exists() and not raised.value.cold_archive_store.catalog["files"]
    client.upload_hook = None
    result = run(fixture, [path], archive_store=raised.value.cold_archive_store)
    assert result["phase"] == "verified_local_retained"
    assert len(client.parts()) == 1


def test_actual_48_mib_level_zero_part_and_standard_bounded_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "COLD_RAW_BYTES", 48 * 1024 * 1024)
    config, client = config_file(tmp_path), FolderClient()
    folder = client._add(module.DEFAULT_FOLDER_NAME, is_dir=True)
    store = module.ColdArchiveStore(config, client, folder_id=folder)
    raw = b"opaque" * (module.COLD_RAW_BYTES // 6)
    assert len(raw) == 48 * 1024 * 1024
    ref = store._archive_part(raw)
    assert 48 * 1024 * 1024 < ref["stored_size"] <= 50 * 1024 * 1024
    reader = module.cold_archive_reader(config, client, folder_id=folder)
    assert reader._read_chunk(ref) == raw
    assert client.downloads[-1][2] == ref["stored_size"]
    assert max(item[2] for item in client.downloads) <= 50 * 1024 * 1024
    with pytest.raises(CloudError, match="48 MiB"):
        store._archive_part(raw + b"x")


def test_ram_transfer_creates_no_local_files_or_cache(fixture, monkeypatch):
    root, _config, _client = fixture
    path, _raw = source(root)
    original_open = Path.open
    def read_only_open(path, mode="r", *args, **kwargs):
        assert not any(flag in mode for flag in "wax+"), "unexpected local write"
        return original_open(path, mode, *args, **kwargs)
    def no_mkdir(*_args, **_kwargs):
        raise AssertionError("unexpected local directory creation")
    monkeypatch.setattr(Path, "open", read_only_open)
    monkeypatch.setattr(Path, "mkdir", no_mkdir)
    result = run(fixture, [path])
    assert result["phase"] == "verified_local_retained"
    assert list(root.iterdir()) == [path]


def test_cold_folder_cannot_alias_active_folder(fixture):
    root, _config, client = fixture
    path, _raw = source(root)
    client.entries["active-reports"] = {"fid": "active-reports", "file_name": module.DEFAULT_FOLDER_NAME,
        "raw": None, "parent": "authorized-parent", "is_dir": True}
    with pytest.raises(CloudError, match="分别独立"):
        run(fixture, [path], delete_local=True)
    assert path.exists() and not client.uploads and not client.creates


def test_empty_archive_and_duplicate_source_have_exact_counts(fixture):
    root, config, client = fixture
    path, _raw = source(root, raw=b"")
    result = run(fixture, [path, path], delete_local=True)
    assert result["source_files"] == result["verified_files"] == result["removed_files"] == 1
    assert result["source_bytes"] == result["verified_parts"] == result["released_bytes"] == 0
    assert not client.parts() and not path.exists()
    assert b"".join(module.cold_archive_reader(config, client, folder_id=result["folder_id"]).iter_file(result["keys"][0])) == b""


def test_created_plain_fid_accepts_latest_unique_opaque_listing_handle(fixture):
    root, _config, client = fixture
    path, raw = source(root)
    original_create = client.create_folder
    def create_with_opaque_listing(name, *, parent_fid):
        plain = original_create(name, parent_fid=parent_fid)
        entry = client.entries.pop(plain)
        opaque = "~1.fixture-opaque-directory"
        client.entries[opaque] = {**entry, "fid": opaque}
        return plain
    client.create_folder = create_with_opaque_listing
    result = run(fixture, [path])
    assert result["phase"] == "verified_local_retained" and path.read_bytes() == raw
    assert result["folder_id"] == "~1.fixture-opaque-directory"
    assert all(parent == result["folder_id"] for _fid, _name, parent in client.uploads)


@pytest.mark.parametrize("listing_count", [0, 2])
def test_created_folder_still_requires_unique_fresh_listing(fixture, listing_count):
    root, _config, client = fixture
    path, _raw = source(root)
    def create_without_unique_listing(name, *, parent_fid):
        for _ in range(listing_count):
            client._add(name, parent=parent_fid, is_dir=True)
        return "plain-created-fid"
    client.create_folder = create_without_unique_listing
    with pytest.raises(CloudError, match="同名|唯一目录"):
        run(fixture, [path], delete_local=True)
    assert path.exists() and not client.uploads


@pytest.mark.parametrize("receipt", ["authorized-parent", "active-reports"])
def test_created_receipt_cannot_identify_protected_folder(fixture, receipt):
    root, _config, client = fixture
    path, _raw = source(root)
    def create_with_protected_receipt(name, *, parent_fid):
        client._add(name, parent=parent_fid, is_dir=True)
        return receipt
    client.create_folder = create_with_protected_receipt
    with pytest.raises(CloudError, match="分别独立"):
        run(fixture, [path], delete_local=True)
    assert path.exists() and not client.uploads


def test_cold_stored_hard_ceiling_rejects_oversize_before_upload(fixture, monkeypatch):
    _root, config, client = fixture
    folder = client._add(module.DEFAULT_FOLDER_NAME, is_dir=True)
    store = module.ColdArchiveStore(config, client, folder_id=folder)
    monkeypatch.setattr(module, "COLD_STORED_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(module.gzip, "compress", lambda *_args, **_kwargs: bytes(50 * 1024 * 1024 + 1))
    with pytest.raises(CloudError, match="50 MiB"):
        store._archive_part(b"small opaque source")
    assert not client.uploads and not client.downloads


def test_cold_default_clients_use_fifty_mib_upload_budget(fixture, monkeypatch):
    from parser import quark_memory_client
    root, config, _client = fixture
    path, _raw = source(root)
    budgets = []
    class BudgetClient(FolderClient):
        def __init__(self, *, config, max_upload_bytes):
            super().__init__()
            budgets.append(max_upload_bytes)
    monkeypatch.setattr(quark_memory_client, "MemoryQuarkClient", BudgetClient)
    module.ColdArchiveStore(config, folder_id="cold-folder")
    module.cold_archive_reader(config, folder_id="cold-folder")
    result = module.migrate_cold_archives(root, [path], config_path=config)
    assert result["phase"] == "verified_local_retained"
    assert budgets == [50 * 1024 * 1024] * 3


class RotatingFolderClient(FolderClient):
    """One physical directory, different opaque handles in parent listings."""
    def __init__(self):
        super().__init__()
        self.aliases = {}
        self.parent_list_calls = 0
        self.reject_parent_list = False

    def list_files(self, parent):
        parent = self.aliases.get(str(parent), str(parent))
        if parent == "authorized-parent":
            if self.reject_parent_list:
                raise AssertionError("attested retry must retain the original folder handle")
            self.parent_list_calls += 1
        result = super().list_files(parent)
        for item in result:
            if item["is_dir"]:
                fid = item["fid"]
                alias = "~1.folder-list-" + str(self.parent_list_calls) + "-" + fid
                self.aliases[alias] = fid
                item["fid"] = alias
        return result

    def upload_bytes(self, raw, name, *, parent_fid):
        return super().upload_bytes(raw, name, parent_fid=self.aliases.get(str(parent_fid), str(parent_fid)))


def failed_attested_store(fixture):
    root, config, _old_client = fixture
    client = RotatingFolderClient()
    path, _raw = source(root, raw=b"opaque archive body")
    def reject_catalog(name, _raw):
        if name.startswith("memory-catalog-"):
            raise OSError("fixture catalog transfer interrupted")
    client.upload_hook = reject_catalog
    with pytest.raises(OSError) as raised:
        module.migrate_cold_archives(root, [path], config_path=config, client=client)
    client.upload_hook = None
    return path, client, raised.value.cold_archive_store


def test_attested_retry_keeps_original_handle_and_fresh_reads_every_part(fixture):
    root, config, _client = fixture
    path, client, store = failed_attested_store(fixture)
    original_handle = store.folder_id
    newer_handle = module._folder(module.load_config(config), client, module.DEFAULT_FOLDER_NAME, verify_only=False)
    assert newer_handle != original_handle
    client.reject_parent_list = True
    result = module.migrate_cold_archives(root, [path], config_path=config, client=client,
                                         archive_store=store, delete_local=True)
    assert result["phase"] == "complete" and not path.exists()
    assert result["folder_id"] == original_handle and len(client.parts()) == 1
    fid = client.parts()[0][0]
    assert sum(entry[0] == fid for entry in client.downloads) == 2


def test_unattested_external_store_with_different_opaque_handle_still_rejected(fixture):
    root, config, _client = fixture
    client = RotatingFolderClient()
    path, _raw = source(root)
    folder = module._folder(module.load_config(config), client, module.DEFAULT_FOLDER_NAME, verify_only=False)
    store = module.ColdArchiveStore(config, client, folder_id=folder)
    assert store._cold_archive_binding is None
    with pytest.raises(ValueError, match="selected cold folder"):
        module.migrate_cold_archives(root, [path], config_path=config, client=client,
                                     archive_store=store, delete_local=True)
    assert path.exists() and not client.uploads and store._cold_archive_binding is None


@pytest.mark.parametrize("changed", ["parent", "active", "folder-name", "handle", "client"])
def test_attested_retry_rejects_changed_logical_binding_before_cloud(fixture, changed):
    root, config, _client = fixture
    path, client, store = failed_attested_store(fixture)
    arguments = {}
    if changed in {"parent", "active"}:
        settings = json.loads(config.read_text(encoding="utf-8"))
        settings["folder_id" if changed == "parent" else "memory_folder_id"] = "changed-protected-folder"
        config.write_text(json.dumps(settings), encoding="utf-8")
    elif changed == "folder-name":
        arguments["folder_name"] = "different-cold-folder"
    elif changed == "handle":
        store.folder_id = "different-unattested-handle"
    else:
        client = RotatingFolderClient()
    before = len(client.lists), len(client.uploads), len(client.creates)
    with pytest.raises(ValueError, match="binding|client and namespace"):
        module.migrate_cold_archives(root, [path], config_path=config, client=client,
                                     archive_store=store, delete_local=True, **arguments)
    assert path.exists()
    assert (len(client.lists), len(client.uploads), len(client.creates)) == before


def child_writer(config, work_dir, *, folder_name=module.DEFAULT_FOLDER_NAME,
                 active_folder="active-reports", parent_folder="authorized-parent"):
    code = "\n".join([
        "from parser.cold_archive import ColdArchiveStore, COLD_NAMESPACE",
        "from parser.quark_cloud import CloudError",
        "client = object()",
        f"store = ColdArchiveStore({str(config)!r}, client, folder_id='~1.child-opaque-handle')",
        f"scope = ({parent_folder!r}, {folder_name!r}, {active_folder!r}, COLD_NAMESPACE)",
        "store._cold_archive_binding = (scope, str(store.folder_id), client)",
        "try:",
        "    with store.writer_lease():",
        "        print('acquired')",
        "except CloudError:",
        "    print('blocked')",
    ])
    environment = os.environ.copy()
    environment.update(PYTHONPATH=str(Path(module.__file__).parents[1]),
                       PYTHONDONTWRITEBYTECODE="1", TEMP=str(work_dir), TMP=str(work_dir))
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=work_dir,
        env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.skipif(os.name != "nt", reason="Windows cross-process kernel mutex")
@pytest.mark.parametrize("active_folder", ["active-reports", "different-active-reports"])
def test_logical_batch_lease_blocks_different_opaque_handle_client_and_cwd(fixture, active_folder):
    root, config, _client = fixture
    _path, _rotating_client, store = failed_attested_store(fixture)
    with store.writer_lease():
        assert child_writer(config, root, active_folder=active_folder) == "blocked"
    assert child_writer(config, root, active_folder=active_folder) == "acquired"


@pytest.mark.skipif(os.name != "nt", reason="Windows cross-process kernel mutex")
@pytest.mark.parametrize("different_scope", ["name", "parent"])
def test_logical_batch_lease_isolates_distinct_cold_destinations(fixture, different_scope):
    root, config, _client = fixture
    _path, _rotating_client, store = failed_attested_store(fixture)
    arguments = {"folder_name": "another-cold-folder"} if different_scope == "name" else {"parent_folder": "another-parent"}
    with store.writer_lease():
        assert child_writer(config, root, **arguments) == "acquired"


@pytest.mark.skipif(os.name != "nt", reason="Windows cross-process kernel mutex")
def test_logical_batch_lease_releases_both_mutexes_after_exception(fixture):
    root, config, _client = fixture
    _path, _rotating_client, store = failed_attested_store(fixture)
    with pytest.raises(RuntimeError, match="fixture exit"):
        with store.writer_lease():
            raise RuntimeError("fixture exit")
    assert child_writer(config, root) == "acquired"
    with store.writer_lease():
        pass


def test_logical_batch_lease_retains_original_folder_lease_and_probe_behavior(fixture, monkeypatch):
    _root, config, _client = fixture
    _path, client, store = failed_attested_store(fixture)
    from parser import quark_memory_client
    calls = []
    @contextmanager
    def record(identity, timeout):
        calls.append(("enter", identity, timeout))
        try:
            yield
        finally:
            calls.append(("exit", identity, timeout))
    monkeypatch.setattr(quark_memory_client, "_account_mutex", record)
    with store.writer_lease():
        assert [item[0] for item in calls] == ["enter", "enter"]
    assert calls[0][1].startswith("folder:cold-logical:")
    assert calls[1] == ("enter", "folder:" + str(store.folder_id), 0)
    assert calls[2] == ("exit", calls[1][1], 0)
    assert calls[3] == ("exit", calls[0][1], 0)
    calls.clear()
    probe = module.ColdArchiveStore(config, client, folder_id="~1.probe-handle")
    with probe.writer_lease():
        pass
    assert calls == [("enter", "folder:~1.probe-handle", 0), ("exit", "folder:~1.probe-handle", 0)]
