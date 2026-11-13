"""Quark offload invariants with an in-memory provider; no real cloud or game."""
from __future__ import annotations

from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace

import pytest

from parser import quark_cloud as cloud
from parser.report_archive import ReportArchive


@pytest.fixture
def work(monkeypatch):
    base = (Path(__file__).resolve().parents[1] / ".runtime" / "test-temp" / "quark-cloud")
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="quark-cloud-", dir=base) as folder:
        root = Path(folder)
        monkeypatch.setenv("TEMP", str(root))
        monkeypatch.setenv("TMP", str(root))
        monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
        yield root


def config_file(work, **updates):
    value = {"enabled": True, "cli_command": ["fixture-official-cli"],
             "folder_id": "fixture-folder", "cache_dir": str(work / "cache"),
             "work_dir": str(work / "cli"), "temp_dir": str(work / "temp"),
             "cache_max_bytes": 1024 * 1024}
    value.update(updates)
    path = work / "quark.local.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def raw_report(uid="1001", marker="first"):
    value = {"battleID": uid, "source": "all", "marker": marker,
             "data": {"battleID": uid, "timeStamp": 1794041000,
                      "battleSettlement": {"attacker": {"army_id": "10", "avatar_data": {
                          "avatar_id": "100", "avatar_name": "测试武将😀"}, "army_member": []}},
                      "eventDataList": [{"eventId": 101, "tab": 0, "intList": [0, None, 2**80]}]}}
    return b"\xef\xbb\xbf" + json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\r\n"


def put(db, raw=None, namespace="one"):
    raw = raw or raw_report()
    with ReportArchive(db, storage_mode="lean") as archive:
        result = archive.import_bytes(raw, namespace=namespace)
    assert result["status"] == "imported", result
    return result["file_id"], result["snapshot_ids"][0], raw


def query(db, sql, params=()):
    with closing(sqlite3.connect(db)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(sql, params).fetchone()
        return dict(row) if row is not None else None


class FakeProvider:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.downloads = []
        self.before_download = None
        self.transform = None
        self.upload_error = None

    def upload(self, path):
        if self.upload_error:
            raise self.upload_error
        raw = Path(path).read_bytes()
        fid = "remote-" + str(len(self.uploads) + 1)
        self.uploads.append((fid, Path(path).name, raw))
        self.objects[fid] = raw
        return fid

    def download(self, fid, target):
        self.downloads.append(fid)
        if self.before_download:
            self.before_download(fid)
        raw = self.objects[fid]
        if self.transform:
            raw = self.transform(raw)
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


def test_verified_roundtrip_precedes_release_and_keeps_exact_original(work, monkeypatch):
    db = work / "archive.db"
    fid, sid, raw = put(db)
    config = config_file(work)
    provider = FakeProvider()

    def before_download(_):
        assert query(db, "SELECT raw_bytes FROM report_files WHERE id=?", (fid,))["raw_bytes"] == raw
        assert query(db, "SELECT status FROM report_cloud_files WHERE file_id=?", (fid,))["status"] == "uploaded"

    provider.before_download = before_download
    result = cloud.sync_archive(db, config_path=config, client=provider)
    assert (result["verified"], result["failed"], result["released_bytes"]) == (1, 0, len(raw))
    assert gzip.decompress(provider.uploads[0][2]) == raw
    assert provider.uploads[0][1] == hashlib.sha256(raw).hexdigest() + ".json.gz"
    assert query(db, "SELECT raw_bytes,byte_count,sha256 FROM report_files WHERE id=?", (fid,)) == {
        "raw_bytes": b"", "byte_count": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    assert query(db, "SELECT status,verified_at FROM report_cloud_files WHERE file_id=?", (fid,))["verified_at"]
    monkeypatch.setattr(cloud, "QuarkClient", lambda _config: provider)
    with ReportArchive(db, read_only=True) as archive:
        assert archive.original_bytes(sid) == raw
        assert archive.verify()["ok"]


@pytest.mark.parametrize("failure", ["upload", "corrupt_download"])
def test_failed_transfer_or_corrupt_cloud_copy_never_releases_original(work, failure):
    db = work / "archive.db"
    fid, _, raw = put(db)
    provider = FakeProvider()
    if failure == "upload":
        provider.upload_error = cloud.CloudError("fixture upload unavailable")
    else:
        provider.transform = lambda stored: stored + b"corruption"
    result = cloud.sync_archive(db, config_path=config_file(work), client=provider)
    assert result["verified"] == result["released_bytes"] == 0
    assert result["failed"] == 1 and result["errors"]
    assert query(db, "SELECT raw_bytes FROM report_files WHERE id=?", (fid,))["raw_bytes"] == raw
    assert query(db, "SELECT status FROM report_cloud_files WHERE file_id=?", (fid,))["status"] != "verified"


def test_uploaded_verification_retry_reuses_remote_id_without_duplicate_upload(work):
    db = work / "archive.db"
    fid, _, raw = put(db)
    config = config_file(work)
    provider = FakeProvider()
    provider.transform = lambda stored: b"bad object"
    assert cloud.sync_archive(db, config_path=config, client=provider)["failed"] == 1
    uploaded = query(db, "SELECT status,remote_file_id FROM report_cloud_files WHERE file_id=?", (fid,))
    assert uploaded == {"status": "uploaded", "remote_file_id": "remote-1"}
    provider.transform = None
    assert cloud.sync_archive(db, config_path=config, client=provider)["verified"] == 1
    assert len(provider.uploads) == 1 and provider.downloads == ["remote-1", "remote-1"]
    assert cloud.sync_archive(db, config_path=config, client=provider)["verified"] == 0
    assert len(provider.uploads) == 1
    assert query(db, "SELECT byte_count FROM report_files WHERE id=?", (fid,))["byte_count"] == len(raw)


def test_keep_local_mode_retains_inline_original_after_verification(work):
    db = work / "archive.db"
    fid, _, raw = put(db)
    result = cloud.sync_archive(db, config_path=config_file(work), client=FakeProvider(), release_local=False)
    assert result["verified"] == 1 and result["released_bytes"] == 0
    assert query(db, "SELECT raw_bytes FROM report_files WHERE id=?", (fid,))["raw_bytes"] == raw


def test_missing_cache_fetches_on_demand_and_checks_sha_before_cache_publication(work, monkeypatch):
    db = work / "archive.db"
    fid, sid, raw = put(db)
    provider = FakeProvider()
    config = config_file(work)
    cloud.sync_archive(db, config_path=config, client=provider)
    cache = next((work / "cache").glob("*.json.gz"))
    cache.unlink()
    monkeypatch.setattr(cloud, "QuarkClient", lambda _config: provider)
    with ReportArchive(db, read_only=True) as archive:
        assert archive.original_bytes(sid) == raw
        assert cache.is_file() and len(provider.downloads) == 2
        cache.unlink()
        provider.transform = lambda stored: b"untrusted remote bytes"
        with pytest.raises(cloud.CloudError, match="校验失败"):
            archive.original_bytes(sid)
        assert not cache.exists()
    assert query(db, "SELECT raw_bytes FROM report_files WHERE id=?", (fid,))["raw_bytes"] == b""


def test_cache_only_never_constructs_network_client_for_missing_or_corrupt_cache(work, monkeypatch):
    db = work / "archive.db"
    fid, _, raw = put(db)
    cloud.sync_archive(db, config_path=config_file(work), client=FakeProvider())
    cache = next((work / "cache").glob("*.json.gz"))

    def forbidden_client(_):
        pytest.fail("cache-only lookup attempted network provider construction")

    monkeypatch.setattr(cloud, "QuarkClient", forbidden_client)
    with ReportArchive(db, read_only=True) as archive:
        assert cloud.resolve_original(archive.connection, fid, db, allow_network=False) == raw
        cache.write_bytes(b"invalid cache")
        with pytest.raises(cloud.CloudError):
            cloud.resolve_original(archive.connection, fid, db, allow_network=False)
        cache.unlink()
        with pytest.raises(cloud.CloudError, match="缓存"):
            cloud.resolve_original(archive.connection, fid, db, allow_network=False)


def test_offloaded_export_never_overwrites_existing_different_file(work):
    db = work / "archive.db"
    _, sid, raw = put(db)
    cloud.sync_archive(db, config_path=config_file(work), client=FakeProvider())
    destination = work / "original.json"
    destination.write_bytes(b"keep my file")
    with ReportArchive(db, read_only=True) as archive:
        with pytest.raises(FileExistsError):
            archive.export_original(sid, destination)
        assert destination.read_bytes() == b"keep my file"
        destination.unlink()
        assert archive.export_original(sid, destination)["sha256"] == hashlib.sha256(raw).hexdigest()
        assert destination.read_bytes() == raw


def test_namespaces_and_file_ids_do_not_mix_cloud_objects(work, monkeypatch):
    db = work / "archive.db"
    first, sid_one, raw_one = put(db, raw_report(marker="one"), namespace="one")
    second, sid_two, raw_two = put(db, raw_report(marker="two"), namespace="two")
    provider = FakeProvider()
    cloud.sync_archive(db, config_path=config_file(work), client=provider)
    monkeypatch.setattr(cloud, "QuarkClient", lambda _config: provider)
    with ReportArchive(db, read_only=True) as archive:
        assert archive.original_bytes(sid_one) == raw_one
        assert archive.original_bytes(sid_two) == raw_two
    with closing(sqlite3.connect(db)) as connection:
        other = connection.execute("SELECT remote_file_id FROM report_cloud_files WHERE file_id=?", (second,)).fetchone()[0]
        connection.execute("UPDATE report_cloud_files SET remote_file_id=? WHERE file_id=?", (other, first))
        connection.commit()
    for cache in (work / "cache").glob("*.json.gz"):
        cache.unlink()
    with ReportArchive(db, read_only=True) as archive:
        with pytest.raises(cloud.CloudError, match="校验失败"):
            archive.original_bytes(sid_one)
        assert archive.original_bytes(sid_two) == raw_two


def test_resolver_rejects_reference_digest_that_does_not_match_file_identity(work):
    db = work / "archive.db"
    fid, _, _ = put(db)
    cloud.sync_archive(db, config_path=config_file(work), client=FakeProvider())
    with closing(sqlite3.connect(db)) as connection:
        connection.execute("UPDATE report_cloud_files SET raw_sha256=? WHERE file_id=?", ("0" * 64, fid))
        connection.commit()
    with ReportArchive(db, read_only=True) as archive:
        with pytest.raises(cloud.CloudError, match="身份不匹配"):
            cloud.resolve_original(archive.connection, fid, db)


def test_simultaneous_local_change_prevents_release_after_remote_verification(work):
    db = work / "archive.db"
    fid, _, _ = put(db)
    provider = FakeProvider()

    def change_original(_):
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("UPDATE report_files SET raw_bytes=? WHERE id=?", (b"changed concurrently", fid))
            connection.commit()

    provider.before_download = change_original
    result = cloud.sync_archive(db, config_path=config_file(work), client=provider)
    assert result["verified"] == 0 and result["failed"] == 1 and result["released_bytes"] == 0
    assert query(db, "SELECT raw_bytes FROM report_files WHERE id=?", (fid,))["raw_bytes"] == b"changed concurrently"


@pytest.mark.parametrize("budget", [0, -1, True, 1.25, "1000"])
def test_config_requires_positive_integer_cache_budget(work, budget):
    with pytest.raises(cloud.CloudError, match="cache_max_bytes"):
        cloud.load_config(config_file(work, cache_max_bytes=budget))


@pytest.mark.skipif(os.name != "nt", reason="Windows path policy")
@pytest.mark.parametrize("key", ["cache_dir", "work_dir"])
def test_config_rejects_c_drive_without_creating_any_directory(work, key):
    with pytest.raises(ValueError, match="C:"):
        cloud.load_config(config_file(work, **{key: "C:/codex-quark-no-write-test"}))


def test_official_cli_subprocess_uses_f_drive_scratch_and_safe_argument_list(work, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"type":"result","code":0,"data":{}}', stderr="")

    monkeypatch.setattr(cloud.subprocess, "run", fake_run)
    client = cloud.QuarkClient(cloud.load_config(config_file(work)))
    client.run("get-user-info")
    command, kwargs = calls[0]
    assert command == ["fixture-official-cli", "get-user-info"]
    assert Path(kwargs["cwd"]) == work / "cli"
    for key in ("TEMP", "TMP", "TMPDIR", "npm_config_cache"):
        assert Path(kwargs["env"][key]).resolve().is_relative_to(work)
    assert kwargs["capture_output"] and kwargs["creationflags"] == getattr(cloud.subprocess, "CREATE_NO_WINDOW", 0)


@pytest.mark.parametrize("key", ["TEMP", "TMP", "TMPDIR", "npm_config_cache"])
def test_cli_environment_cannot_redirect_managed_scratch_to_c(work, monkeypatch, key):
    observed = []

    def fake_run(_command, **kwargs):
        observed.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout='{"code":0}', stderr="")

    monkeypatch.setattr(cloud.subprocess, "run", fake_run)
    client = cloud.QuarkClient(cloud.load_config(config_file(work, cli_env={key: "C:/codex-no-write-test"})))
    try:
        client.run("get-user-info")
    except (ValueError, cloud.CloudError):
        return
    assert observed and Path(observed[0][key]).resolve().is_relative_to(work)


def test_status_uses_official_account_command_and_redacts_credential_fields(work, monkeypatch):
    arguments = []

    def fake_run(*args, **_kwargs):
        arguments.append(args)
        return {"code": 0, "data": {"nickname": "fixture", "access_token": "fixture-only", "nested": {"secret": "fixture-only"}}}

    client = cloud.QuarkClient(cloud.load_config(config_file(work)))
    monkeypatch.setattr(client, "run", fake_run)
    result = client.status()
    assert arguments == [("get-user-info",)]
    assert "fixture-only" not in json.dumps(result)


@pytest.mark.parametrize("label", ["access_token", "agent_auth_code", "code_verifier"])
def test_cli_error_text_does_not_expose_token_values(work, monkeypatch, capsys, label):
    marker = "fixture-credential-DO-NOT-PRINT"
    message = json.dumps({"type": "result", "code": 401, "msg": label + "=" + marker})
    monkeypatch.setattr(cloud.subprocess, "run", lambda *_args, **_kwargs:
        SimpleNamespace(returncode=1, stdout=message, stderr="Bearer " + marker))
    with pytest.raises(cloud.CloudError) as caught:
        cloud.QuarkClient(cloud.load_config(config_file(work))).run("get-user-info")
    assert marker not in str(caught.value)
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err


def test_sync_error_result_and_database_do_not_expose_token_values(work, capsys):
    db = work / "archive.db"
    fid, _, raw = put(db)
    provider = FakeProvider()
    marker = "fixture-credential-DO-NOT-PRINT"
    provider.upload_error = RuntimeError("access_token=" + marker)
    result = cloud.sync_archive(db, config_path=config_file(work), client=provider)
    assert result["failed"] == 1
    assert marker not in json.dumps(result)
    assert marker not in query(db, "SELECT last_error FROM report_cloud_files WHERE file_id=?", (fid,))["last_error"]
    assert query(db, "SELECT raw_bytes FROM report_files WHERE id=?", (fid,))["raw_bytes"] == raw
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err


def test_disabled_worker_never_starts_sync_thread(work, monkeypatch):
    def forbidden_sync(*_args, **_kwargs):
        pytest.fail("disabled worker tried to sync")
    monkeypatch.setattr(cloud, "sync_archive", forbidden_sync)
    worker = cloud.CloudUploadWorker(work / "not-created.db", config_file(work, enabled=False)).start()
    assert worker.thread is None
    worker.close()
    assert not (work / "not-created.db").exists()


def test_worker_runs_bounded_batch_and_stops_without_game_calls(work, monkeypatch):
    worker = cloud.CloudUploadWorker(work / "fixture.db", config_file(work))
    calls = []

    def fake_sync(db_path, **kwargs):
        calls.append((db_path, kwargs))
        worker.stop_event.set()
        return {"verified": 1}

    monkeypatch.setattr(cloud, "sync_archive", fake_sync)
    worker._run()
    assert len(calls) == 1 and calls[0][1]["limit"] == 2
    assert worker.last_error is None


@pytest.mark.parametrize("url", [
    "http://pan.quark.cn/authorize", "https://example.invalid/authorize",
    "https://quark.cn@example.invalid/authorize", "https://pan.quark.cn/?access_token=fixture-only",
])
def test_authorization_page_rejects_untrusted_or_credential_urls_before_opening(work, monkeypatch, url):
    monkeypatch.setattr(cloud.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("Unexpected browser launch"))
    config = cloud.load_config(config_file(work))
    with pytest.raises(cloud.CloudError):
        cloud.open_authorization_page(url, config)
    assert not (work / "cli").exists()


def test_authorization_chrome_uses_explicit_local_profile_and_cache(work, monkeypatch):
    executable = work / "fixture-chrome.exe"
    executable.write_bytes(b"fixture, never executed")
    monkeypatch.setattr(cloud.shutil, "which", lambda _name: str(executable))
    calls = []

    def fake_popen(arguments, **options):
        calls.append((arguments, options))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(cloud.subprocess, "Popen", fake_popen)
    config = cloud.load_config(config_file(work))
    url = "https://pan.quark.cn/oauth/authorize?nonce=fixture-only"
    result = cloud.open_authorization_page(url, config)
    assert result["opened"] is True and len(calls) == 1
    arguments, options = calls[0]
    assert arguments[0] == str(executable) and arguments[-1] == url
    for prefix in ("--user-data-dir=", "--disk-cache-dir=", "--crash-dumps-dir="):
        location = next(value[len(prefix):] for value in arguments if value.startswith(prefix))
        assert Path(location).resolve().is_relative_to(work / "cli")
    assert options["cwd"] == work / "cli"
    assert options["stdout"] == options["stderr"] == cloud.subprocess.DEVNULL


def test_cloud_object_larger_than_cache_budget_is_readable_without_persistent_oversize_cache(work, monkeypatch):
    db = work / "oversize.db"
    _, sid, raw = put(db)
    provider = FakeProvider()
    config = config_file(work, cache_max_bytes=1)
    assert cloud.sync_archive(db, config_path=config, client=provider)["verified"] == 1
    assert list((work / "cache").glob("*.json.gz")) == []
    monkeypatch.setattr(cloud, "QuarkClient", lambda _config: provider)
    with ReportArchive(db, read_only=True) as archive:
        assert archive.original_bytes(sid) == raw
        assert archive.verify()["ok"]
    assert list((work / "cache").glob("*.json.gz")) == []


def test_authorization_uses_separate_work_drive_chrome_profile(work, monkeypatch):
    chrome = work / "chrome.exe"
    chrome.write_bytes(b"fixture")
    monkeypatch.setattr(cloud.shutil, "which", lambda name: str(chrome))
    calls = []
    monkeypatch.setattr(cloud.subprocess, "Popen", lambda args, **options: calls.append((args, options)))
    result = cloud.open_authorization_page("https://pan.quark.cn/open/v1/oauth/agent?page_code=fixture", {
        "work_dir": str(work / "runtime")})
    assert result["opened"] is True
    args, options = calls[0]
    assert "--user-data-dir=" + str(work / "runtime/chrome-profile") in args
    assert "--disk-cache-dir=" + str(work / "runtime/chrome-cache") in args
    assert options["env"]["TEMP"] == str(work / "runtime")
    with pytest.raises(cloud.CloudError):
        cloud.open_authorization_page("https://example.org/open", {"work_dir": str(work / "runtime")})
    with pytest.raises(cloud.CloudError):
        cloud.open_authorization_page("https://pan.quark.cn/?access_token=fixture", {"work_dir": str(work / "runtime")})
