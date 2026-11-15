"""Verified cloud objects, journals and report aliases with an offline client."""
from __future__ import annotations

import copy
import gzip
import io
import json
import os
from pathlib import Path
import threading

import pytest

from parser import cloud_store as module
from parser.cloud_store import CloudStore, sha
from parser.quark_cloud import CloudError


class MemoryClient:
    def __init__(self):
        self.objects, self.uploads, self.downloads = {}, [], []
        self.list_calls = 0
        self.upload_hook = self.download_hook = self.list_hook = None

    def list_files(self, _folder):
        self.list_calls += 1
        if self.list_hook:
            self.list_hook(self.list_calls)
        return [{"fid": fid, "file_name": value[0]} for fid, value in self.objects.items()]

    def upload_bytes(self, raw, name, *, parent_fid):
        assert parent_fid == "fixture-folder"
        if self.upload_hook:
            self.upload_hook(name, raw)
        fid = str(len(self.objects) + 1)
        self.objects[fid] = name, raw
        self.uploads.append((fid, name))
        return {"fid": fid}

    def download_bytes(self, fid, *, max_bytes):
        name, raw = self.objects[str(fid)]
        self.downloads.append((str(fid), name, self.list_calls))
        if self.download_hook:
            raw = self.download_hook(name, raw)
        if len(raw) > max_bytes:
            raise CloudError("fixture response too large")
        return raw

    def chunk_uploads(self):
        return [name for _fid, name in self.uploads if name.startswith("memory-part-")]


def config_file(tmp_path):
    path = tmp_path / "quark.local.json"
    path.write_text(json.dumps({"cli_command": ["fixture-unused-cli"], "folder_id": "fixture-folder",
        "enabled": True, "cache_dir": str(tmp_path / "unused-cache"),
        "work_dir": str(tmp_path / "unused-work"), "temp_dir": str(tmp_path / "unused-temp")}), encoding="utf-8")
    return path


def report(complete=True, battle_id="123", marker="whole"):
    data = {"battleID": battle_id, "timeStamp": 1794600000,
            "eventDataList": [{"eventId": 1, "intList": [0, 2], "tab": 0}], "unknown": marker}
    if complete:
        components = {"basic": data, "all": data, "detail": data, "statics": {},
                      "snapshots": {"isComplete": True, "battleSnapData": {}}}
    else:
        components, data = {"basic": {"battleID": battle_id}}, {"battleID": battle_id}
    value = {"battleID": battle_id, "source": "all" if complete else "basic", "data": data,
             "components": components, "captureComplete": complete,
             "componentStatus": {name: {"complete": complete, "available": True, "requested": complete}
                                 for name in components}, "eventCount": 1 if complete else -1}
    return json.dumps(value, ensure_ascii=False).encode()


def test_packed_files_cross_split_chunks_and_deduplicate_by_whole_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_RAW_CHUNK", 1024)
    monkeypatch.setattr(module, "MAX_STORED_CHUNK", 64)
    client = MemoryClient()
    store = CloudStore(config_file(tmp_path), client)
    one, two = bytes(range(90)), bytes(range(90, 150))
    store.put_files([("one", one, "n", False), ("same", one, "n", False),
                     ("two", two, "n", False), ("empty", b"", "n", False)])
    assert len(store.catalog["blobs"]) == 3
    assert len(client.chunk_uploads()) > 1
    assert all(len(raw) <= 64 for name, raw in client.objects.values() if name.startswith("memory-part-"))
    fresh = CloudStore(config_file(tmp_path), client)
    fresh.refresh()
    for key, raw in [("one", one), ("same", one), ("two", two), ("empty", b"")]:
        assert fresh.read_file(key) == raw
    assert len(fresh.catalog["blobs"][sha(one)]["parts"]) > 1


def test_stream_reuses_identical_chunks_in_one_stream_and_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_RAW_CHUNK", 256)
    client, config = MemoryClient(), config_file(tmp_path)
    store = CloudStore(config, client)
    raw = b"one-compressible-fragment".ljust(256, b"x") * 4
    store.put_stream("backup", io.BytesIO(raw), len(raw))
    assert len(client.chunk_uploads()) == 1
    fresh = CloudStore(config, client)
    fresh.refresh()
    fresh.put_stream("backup-again", io.BytesIO(raw), len(raw))
    assert len(client.chunk_uploads()) == 1
    assert fresh.read_file("backup") == fresh.read_file("backup-again") == raw


def test_failed_catalog_publish_rolls_back_metadata_and_resume_reuses_uploaded_chunk(tmp_path):
    client, config = MemoryClient(), config_file(tmp_path)
    store = CloudStore(config, client)
    before = copy.deepcopy(store.catalog)
    def reject(name, _raw):
        if name.startswith("memory-catalog-"):
            raise OSError("fixture disconnected during commit")
    client.upload_hook = reject
    with pytest.raises(OSError):
        store.put_file("original", b"retained original", index=False)
    assert store.catalog == before and store.catalog_fid is None
    client.upload_hook = None
    fresh = CloudStore(config, client)
    fresh.refresh()
    assert not fresh.catalog["files"]
    fresh.put_file("original", b"retained original", index=False)
    assert len(client.chunk_uploads()) == 1
    assert fresh.read_file("original") == b"retained original"


def test_direct_checkpoint_failure_keeps_previous_generation_and_head(tmp_path):
    client, config = MemoryClient(), config_file(tmp_path)
    store = CloudStore(config, client)
    store.put_file("one", b"a", index=False)
    before = store.catalog["generation"], store.catalog_fid, store._head_generation
    client.upload_hook = lambda _name, _raw: (_ for _ in ()).throw(OSError("fixture"))
    with pytest.raises(OSError):
        store.checkpoint()
    assert (store.catalog["generation"], store.catalog_fid, store._head_generation) == before


def test_journals_recover_without_base_then_after_checkpoint(tmp_path):
    client, config = MemoryClient(), config_file(tmp_path)
    store = CloudStore(config, client, journal_mode=True)
    store.put_file("one", b"first", index=False)
    store.save_state("n", {"cursor": 3})
    fresh = CloudStore(config, client)
    fresh.refresh()
    assert fresh.read_file("one") == b"first" and fresh.load_state("n") == {"cursor": 3}
    store.checkpoint()
    store.put_file("two", b"second", index=False)
    newest = CloudStore(config, client)
    newest.refresh()
    assert newest.read_file("two") == b"second" and newest.load_state("n")["cursor"] == 3


def test_corrupt_latest_journal_fails_instead_of_returning_stale_catalog(tmp_path):
    client, config = MemoryClient(), config_file(tmp_path)
    store = CloudStore(config, client, journal_mode=True)
    store.put_file("one", b"first", index=False)
    client.download_hook = lambda name, raw: raw + b"changed" if name.startswith("memory-change-") else raw
    with pytest.raises(CloudError, match="SHA-256"):
        CloudStore(config, client).refresh()


def test_partial_alias_never_replaces_best_complete_report_blob(tmp_path):
    client, config = MemoryClient(), config_file(tmp_path)
    store = CloudStore(config, client)
    complete, partial = report(), report(False)
    store.put_file("capture/alias.json", complete, namespace="n")
    store.put_file("capture/alias.json", partial, namespace="n")
    fresh = CloudStore(config, client)
    fresh.refresh()
    rows = fresh.list_reports()
    assert len(rows) == 1 and rows[0]["blob_sha256"] == sha(complete)
    assert fresh.read_report(rows[0]["key"]) == complete
    assert fresh.read_file("capture/alias.json") == partial


def test_whole_file_identity_detects_wrong_valid_chunk_slice(tmp_path):
    client, config = MemoryClient(), config_file(tmp_path)
    store = CloudStore(config, client)
    store.put_file("one", b"whole original", index=False)
    part = store.catalog["blobs"][sha(b"whole original")]["parts"][0]
    part["offset"], part["length"] = 1, part["length"] - 1
    with pytest.raises(CloudError, match="整份"):
        store.read_file("one")


def test_alias_does_not_replace_an_unverified_write_receipt_with_a_checkpoint(tmp_path, monkeypatch):
    from parser.cloud_store import CloudArtifacts
    client, config = MemoryClient(), config_file(tmp_path)
    store = CloudStore(config, client)
    adapter = CloudArtifacts(store, namespace="n", logical_root=tmp_path / "logical")
    monkeypatch.setattr(store, "put_file", lambda *_args, **_kwargs: {"verified": False})
    checkpoints = []
    monkeypatch.setattr(store, "checkpoint", lambda: checkpoints.append(True) or {"verified": True})
    with pytest.raises(CloudError, match="尚未"):
        adapter.write_bytes(tmp_path / "logical/battle_123_decrypted.json", report())
    assert checkpoints == []


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel writer lease")
def test_folder_writer_lease_excludes_another_thread_and_releases_on_error(tmp_path):
    config, client = config_file(tmp_path), MemoryClient()
    first, second = CloudStore(config, client), CloudStore(config, client)
    results = []
    def compete():
        try:
            with second.writer_lease():
                results.append("unexpected concurrent writer")
        except CloudError:
            results.append("blocked")
    with first.writer_lease():
        thread = threading.Thread(target=compete)
        thread.start()
        thread.join(5)
        assert not thread.is_alive() and results == ["blocked"]
    with second.writer_lease():
        pass


def two_chunks(tmp_path):
    client, config = MemoryClient(), config_file(tmp_path)
    store = CloudStore(config, client)
    store.put_file("a", b"a" * 100, index=False)
    store.put_file("b", b"b" * 100, index=False)
    refs = {name: store.catalog["blobs"][sha(name.encode() * 100)]["parts"][0]["chunk"] for name in ("a", "b")}
    return client, config, store, refs


def downloads_of(client, fid):
    return sum(item[0] == fid for item in client.downloads)


def test_compressed_ram_cache_default_is_disabled(tmp_path):
    client, config, _store, refs = two_chunks(tmp_path)
    fresh = CloudStore(config, client)
    fresh.refresh()
    before = downloads_of(client, refs["a"]["fid"])
    assert fresh.read_file("a") == b"a" * 100
    assert fresh.read_file("b") == b"b" * 100
    assert fresh.read_file("a") == b"a" * 100
    assert downloads_of(client, refs["a"]["fid"]) - before == 2
    assert fresh.compressed_cache_bytes == 0 and not fresh._compressed_cache


def test_fresh_compressed_ram_cache_downloads_alternating_chunk_only_once(tmp_path):
    client, config, _store, refs = two_chunks(tmp_path)
    fresh = CloudStore(config, client, compressed_cache_bytes=1024)
    assert not fresh._compressed_cache and fresh._cached_chunk is None
    fresh.refresh()
    before = {key: downloads_of(client, value["fid"]) for key, value in refs.items()}
    for name in ["a", "b", "a", "b", "a"]:
        assert fresh.read_file(name) == name.encode() * 100
    for key, value in refs.items():
        assert downloads_of(client, value["fid"]) - before[key] == 1
    assert fresh._compressed_cache_size == sum(map(len, fresh._compressed_cache.values())) <= 1024


def test_compressed_ram_lru_enforces_byte_budget_and_reloads_evicted_chunk(tmp_path):
    client, config, store, refs = two_chunks(tmp_path)
    store.put_file("c", b"c" * 100, index=False)
    refs["c"] = store.catalog["blobs"][sha(b"c" * 100)]["parts"][0]["chunk"]
    budget = refs["a"]["stored_size"] + refs["b"]["stored_size"]
    fresh = CloudStore(config, client, compressed_cache_bytes=budget)
    fresh.refresh()
    before_b = downloads_of(client, refs["b"]["fid"])
    for name in ["a", "b", "a", "c"]:
        fresh.read_file(name)
        assert fresh._compressed_cache_size == sum(map(len, fresh._compressed_cache.values())) <= budget
    assert fresh._chunk_cache_key(refs["b"]) not in fresh._compressed_cache
    fresh.read_file("b")
    assert downloads_of(client, refs["b"]["fid"]) - before_b == 2


def test_corrupt_compressed_ram_entry_is_rejected_before_returning_file(tmp_path):
    client, config, _store, refs = two_chunks(tmp_path)
    fresh = CloudStore(config, client, compressed_cache_bytes=1024)
    fresh.refresh()
    fresh.read_file("a")
    fresh.read_file("b")
    identity = fresh._chunk_cache_key(refs["a"])
    valid = fresh._compressed_cache[identity]
    fresh._compressed_cache[identity] = bytes([valid[0] ^ 1]) + valid[1:]
    before = downloads_of(client, refs["a"]["fid"])
    with pytest.raises(CloudError, match="SHA-256"):
        fresh.read_file("a")
    assert downloads_of(client, refs["a"]["fid"]) == before


def test_raw_validation_failure_is_not_admitted_and_cached_reads_still_check_whole_file(tmp_path):
    client, config, _store, refs = two_chunks(tmp_path)
    fresh = CloudStore(config, client, compressed_cache_bytes=1024)
    fresh.refresh()
    fresh.catalog["blobs"][sha(b"a" * 100)]["parts"][0]["chunk"]["raw_sha256"] = "0" * 64
    with pytest.raises(CloudError, match="原文分片"):
        fresh.read_file("a")
    assert not fresh._compressed_cache and fresh._cached_chunk is None
    other = CloudStore(config, client, compressed_cache_bytes=1024)
    other.refresh()
    other.read_file("a")
    part = other.catalog["blobs"][sha(b"a" * 100)]["parts"][0]
    part["offset"], part["length"] = 1, 99
    with pytest.raises(CloudError, match="整份"):
        other.read_file("a")


def test_valid_stored_chunk_larger_than_default_write_limit_reads_with_actual_size(tmp_path):
    raw = b"large-cold-archive-fixture" * ((8 * 1024 * 1024) // 25 + 100)
    stored = gzip.compress(raw, compresslevel=0, mtime=0)
    assert 8 * 1024 * 1024 < len(stored) < 64 * 1024 * 1024
    client = MemoryClient()
    client.objects["large"] = ("large-read-fixture.gz", stored)
    store = CloudStore(config_file(tmp_path), client, compressed_cache_bytes=16 * 1024 * 1024)
    requested = []
    original_download = client.download_bytes
    def download(fid, *, max_bytes):
        requested.append((fid, max_bytes))
        return original_download(fid, max_bytes=max_bytes)
    client.download_bytes = download
    ref = {"fid": "large", "stored_sha256": sha(stored), "stored_size": len(stored),
           "raw_sha256": sha(raw), "raw_size": len(raw)}
    assert store._read_chunk(ref) == raw
    assert requested == [("large", len(stored))]
    assert store._compressed_cache_size == len(stored)


@pytest.mark.parametrize("layer", ["decoded", "compressed"])
@pytest.mark.parametrize("bad", [True, False, 0, -1, 64 * 1024 * 1024 + 1, "24", 24.0, None])
def test_invalid_stored_size_is_rejected_before_either_cache_can_answer(tmp_path, layer, bad):
    client, config, _store, refs = two_chunks(tmp_path)
    fresh = CloudStore(config, client, compressed_cache_bytes=1024)
    fresh.refresh()
    raw = fresh.read_file("a")
    ref = {**refs["a"], "stored_size": bad}
    key = fresh._chunk_cache_key(ref)
    if layer == "decoded":
        fresh._cached_chunk = (key, raw)
    else:
        fresh._cached_chunk = None
        fresh._compressed_cache[key] = client.objects[refs["a"]["fid"]][1]
    before = len(client.downloads)
    with pytest.raises(CloudError, match="压缩长度"):
        fresh._read_chunk(ref)
    assert len(client.downloads) == before


@pytest.mark.parametrize("layer", ["decoded", "compressed"])
@pytest.mark.parametrize("field", ["stored_size", "raw_size", "raw_sha256", "stored_sha256", "fid"])
def test_valid_range_but_wrong_identity_cannot_reuse_old_cache_entry(tmp_path, layer, field):
    client, config, _store, refs = two_chunks(tmp_path)
    fresh = CloudStore(config, client, compressed_cache_bytes=1024)
    fresh.refresh()
    fresh.read_file("a")
    if layer == "compressed":
        fresh.read_file("b")
    ref = dict(refs["a"])
    if field in ("stored_size", "raw_size"):
        ref[field] += 1
    elif field in ("stored_sha256", "raw_sha256"):
        ref[field] = "0" * 64
    else:
        ref["fid"] = "wrong-position"
        client.objects[ref["fid"]] = ("wrong-position.gz", gzip.compress(b"other", mtime=0))
    expected_request_size = ref["stored_size"]
    requested = []
    original_download = client.download_bytes
    def download(fid, *, max_bytes):
        requested.append((fid, max_bytes))
        return original_download(fid, max_bytes=max_bytes)
    client.download_bytes = download
    with pytest.raises(CloudError):
        fresh._read_chunk(ref)
    assert requested == [(ref["fid"], expected_request_size)]


def test_inclusive_64m_read_bound_reaches_transport_but_still_rejects_wrong_actual_length(tmp_path):
    client, config, _store, refs = two_chunks(tmp_path)
    fresh = CloudStore(config, client)
    requested = []
    original_download = client.download_bytes
    def download(fid, *, max_bytes):
        requested.append(max_bytes)
        return original_download(fid, max_bytes=max_bytes)
    client.download_bytes = download
    with pytest.raises(CloudError, match="SHA-256 或长度"):
        fresh._read_chunk({**refs["a"], "stored_size": 64 * 1024 * 1024})
    assert requested == [64 * 1024 * 1024]


def test_default_write_cap_stays_8m_while_read_cap_is_64m(tmp_path):
    assert module.MAX_STORED_CHUNK == 8 * 1024 * 1024
    assert module.MAX_STORED_READ == 64 * 1024 * 1024
    assert module.MAX_RAW_CHUNK == 256 * 1024 * 1024
    client = MemoryClient()
    store = CloudStore(config_file(tmp_path), client)
    with pytest.raises(CloudError, match="单次内存"):
        store._upload_verified(b"x" * (8 * 1024 * 1024 + 1), "oversize-default-write.gz")
    assert client.uploads == []
