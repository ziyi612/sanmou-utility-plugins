"""Cloud-only capture writes no local artifacts and never acks unsafe bytes."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import runpy

import pytest

from capture.game_assets import battle_collector as collector_module
from capture.game_assets import export_battle_report_runtime as exporter
from capture.game_assets.battle_collector import BattleCollector
from capture.game_assets.memory_storage import ArtifactStoreFailure, VerifiedArtifactStore


class CloudFixture:
    """Remote provider stand-in; all artifact bytes stay in this test's RAM."""
    def __init__(self):
        self.files, self.states, self.events = {}, {}, []
        self.reject = None
        self.reject_state = False
        self.unverified = False

    def exists(self, path):
        return str(path) in self.files

    def read_bytes(self, path):
        return self.files[str(path)]

    def write_bytes(self, path, raw):
        self.events.append(("upload", str(path)))
        if self.reject and self.reject(path, raw):
            raise OSError("fixture cloud disconnected")
        self.files[str(path)] = raw
        if self.unverified:
            return {"verified": False}
        self.events.append(("verified", str(path)))
        return {"verified": True, "sha256": hashlib.sha256(raw).hexdigest()}

    def load_state(self, namespace):
        return deepcopy(self.states.get(namespace))

    def save_state(self, namespace, value):
        if self.reject_state:
            raise OSError("fixture state upload disconnected")
        self.states[namespace] = deepcopy(value)
        return {"verified": True}


@pytest.fixture
def no_artifact_disk(tmp_path, monkeypatch):
    logical = tmp_path / "cloud-only-artifacts"
    for method in ("open", "read_bytes", "read_text", "write_bytes", "write_text", "mkdir", "exists"):
        original = getattr(Path, method)

        def guard(path, *arguments, _method=method, _original=original, **options):
            if path == logical or path.is_relative_to(logical):
                raise AssertionError(f"Strict capture attempted local {_method}: {path}")
            return _original(path, *arguments, **options)

        monkeypatch.setattr(Path, method, guard)
    return logical


@pytest.fixture
def lua_capture(monkeypatch):
    pytest.importorskip("lupa.lua51")
    fixtures = runpy.run_path(str(Path(__file__).with_name("test_component_response_audit.py")))
    clock = fixtures["Clock"]()
    monkeypatch.setattr(exporter.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(exporter.time, "sleep", clock.sleep)
    return clock, fixtures["Runner"]


def full_cycle_runner(lua_capture):
    clock, runner_type = lua_capture
    runner = runner_type(clock, partial=True)
    runner.lua.execute("""
CommonConst={SEG_LEN=10,BattleBaseRelationType={ALL=1000}}
ClientProxy.GetUnionId=function() return 1 end
ClientProxy.RPCGetBattleBlockListResponse=function() end
ClientProxy.RPCGetChildCombatInfoListResponse=function() end
__list_calls=0
ClientProxy.RPCGetBattleBlockList=function(self,index,tp)
 __list_calls=__list_calls+1
 self:RPCGetBattleBlockListResponse(1,{sIndex=index,tp=tp,reqLen=1,
  blockInfoList={{combatInfo={battleID=123,unknown='list-kept'}}}})
end
ClientProxy.RPCGetChildCombatInfoList=function() error('unexpected child request') end
""")
    return runner


def retained_count(runner):
    return runner.lua.eval("""(function()
 local n=0
 for _,entry in pairs((__codexBattleReportRetained or {}).entries or {}) do n=n+1 end
 return n
end)()""")


def test_strict_full_cycle_keeps_discovery_checkpoint_retained_final_and_state_in_cloud(
        no_artifact_disk, lua_capture, monkeypatch):
    backend = CloudFixture()
    runner = full_cycle_runner(lua_capture)
    acknowledged = []
    original_run = exporter.run_checked

    def observe_ack(*arguments, **options):
        if "return envelope({acknowledged=count})" in arguments[1]:
            acknowledged.append(list(backend.events))
            assert any(event[0] == "verified" for event in backend.events)
        return original_run(*arguments, **options)

    monkeypatch.setattr(exporter, "run_checked", observe_ack)
    collector = BattleCollector(runner, no_artifact_disk, namespace="one", scopes=("personal",),
                               artifact_store=backend, request_delay=0)
    result = collector.run_cycle()
    assert result["complete"] and result["collection"]["complete"] == 1
    values = [json.loads(raw) for raw in backend.files.values()]
    assert any(value.get("captureKind") == "list_discovery" for value in values)
    assert any(value.get("captureKind") == "cache_before_retry" for value in values)
    assert any(value.get("source") in ("cache_fragments", "response_fragments") for value in values)
    assert any(path.endswith("collector_manifest.json") for path in backend.files)
    final = json.loads(backend.read_bytes(no_artifact_disk / "battle_123_decrypted.json"))
    assert final["captureComplete"] and final["data"]["unknown"] == "retained"
    assert final["discovery"][0]["row"]["combatInfo"]["unknown"] == "list-kept"
    assert acknowledged and retained_count(runner) == 0 and runner.restored()
    assert backend.states["one"]["reports"]["123"]["complete"]
    assert not collector.artifact_store.pending_artifacts and not collector.artifact_store.pending_states
    resumed = BattleCollector(None, no_artifact_disk, namespace="one", artifact_store=backend)
    assert resumed.state["reports"]["123"]["complete"]


def test_failed_retained_upload_does_not_ack_and_keeps_original_bytes_in_ram(
        no_artifact_disk, lua_capture, monkeypatch):
    clock, runner_type = lua_capture
    runner, backend = runner_type(clock, partial=True), CloudFixture()
    backend.reject = lambda _path, raw: json.loads(raw).get("source") in ("response_fragments", "cache_fragments")
    acknowledged = []
    original_run = exporter.run_checked

    def observe_ack(*arguments, **options):
        if "return envelope({acknowledged=count})" in arguments[1]:
            acknowledged.append(arguments[1])
        return original_run(*arguments, **options)

    monkeypatch.setattr(exporter, "run_checked", observe_ack)
    with pytest.raises(ArtifactStoreFailure) as caught:
        exporter.capture_report(runner, 123, no_artifact_disk, artifact_store=backend)
    assert not acknowledged and retained_count(runner) > 0 and runner.restored()
    assert caught.value.pending_artifacts
    assert any(json.loads(raw).get("source") in ("response_fragments", "cache_fragments")
               for raw in caught.value.pending_artifacts.values())
    assert any(json.loads(raw).get("captureKind") == "received_before_cloud_confirmation"
               and json.loads(raw)["data"]["unknown"] == "retained"
               for raw in caught.value.pending_artifacts.values())
    assert len(runner.requests()) == 1
    assert not any(path.endswith("battle_123_decrypted.json") for path in backend.files)


def test_failed_state_confirmation_aborts_cycle_before_any_component_request(no_artifact_disk, lua_capture):
    backend, runner = CloudFixture(), full_cycle_runner(lua_capture)
    backend.reject_state = True
    collector = BattleCollector(runner, no_artifact_disk, namespace="one", scopes=("personal",),
                               artifact_store=backend, request_delay=0)
    with pytest.raises(ArtifactStoreFailure) as caught:
        collector.run_cycle()
    assert runner.lua.globals().__list_calls == 1
    assert runner.requests() == []
    assert "one" in caught.value.pending_states
    assert runner.lua.globals().__codexBattleCollector is None


def test_unverified_receipt_is_fatal_even_when_provider_returned_normally(no_artifact_disk):
    backend = CloudFixture()
    backend.unverified = True
    store = VerifiedArtifactStore(backend)
    raw = b'{"battleID":123,"unknown":"keep"}'
    with pytest.raises(ArtifactStoreFailure) as caught:
        store.write_bytes(no_artifact_disk / "original.json", raw)
    assert caught.value.pending_artifacts[str(no_artifact_disk / "original.json")] == raw
    count = len(backend.events)
    with pytest.raises(ArtifactStoreFailure):
        store.write_bytes(no_artifact_disk / "second.json", raw)
    assert len(backend.events) == count
    backend.unverified = False
    receipt = caught.value.artifact_store.retry_pending()
    assert receipt == {"verified": True, "artifacts": 2, "states": 0}
    assert not store.pending_artifacts and store.failure is None
    assert backend.read_bytes(no_artifact_disk / "second.json") == raw


def test_pending_state_retry_follows_all_raw_objects_and_remains_frozen_on_failure(no_artifact_disk):
    backend, store = CloudFixture(), None
    backend.reject = lambda _path, _raw: True
    store = VerifiedArtifactStore(backend)
    raw = b'{"battleID":123}'
    with pytest.raises(ArtifactStoreFailure):
        store.write_bytes(no_artifact_disk / "one.json", raw)
    with pytest.raises(ArtifactStoreFailure):
        store.save_state("one", {"version": 1, "namespace": "one", "confirmed": True})
    with pytest.raises(ArtifactStoreFailure):
        store.retry_pending()
    assert store.failure and store.pending_artifacts and store.pending_states
    assert not backend.states
    backend.reject = None
    answer = store.flush_pending()
    assert answer == {"verified": True, "artifacts": 1, "states": 1}
    assert not store.pending_artifacts and not store.pending_states and store.failure is None
    assert backend.states["one"]["confirmed"] is True


def test_first_retained_upload_failure_preserves_entire_received_batch_before_any_ack(no_artifact_disk, monkeypatch):
    backend = CloudFixture()
    backend.reject = lambda _path, _raw: True
    identity = {"avatarId": "42", "serverId": "1", "scenarioId": "18"}
    audit = exporter._ComponentAudit(None, 123, False, artifact_store=backend)
    audit.out_dir, audit.expected_identity = no_artifact_disk, identity
    records = []
    for number in range(1, 5):
        payload = {"battleID": 123, "runtimeIdentity": identity, "source": "response_fragments",
                   "components": {"all": {"fragment": number}}}
        records.append({"battleID": 123, "ticket": number, "identity": identity,
                        "raw": json.dumps({"payload": payload})})
    acknowledgements = []
    monkeypatch.setattr(exporter, "run_checked", lambda *_args, **_kwargs: acknowledgements.append(True))
    with pytest.raises(ArtifactStoreFailure) as caught:
        audit.persist_retained(records)
    assert not acknowledgements
    assert len(caught.value.pending_artifacts) == 4
    assert {json.loads(raw)["components"]["all"]["fragment"]
            for raw in caught.value.pending_artifacts.values()} == {1, 2, 3, 4}
    assert len(backend.events) == 1


def test_strict_alias_replacement_preserves_exact_previous_bytes(no_artifact_disk, monkeypatch):
    backend = CloudFixture()
    old = b'\xef\xbb\xbf{\r\n "battleID":123,"unknown":"older"\r\n}\r\n'
    alias = no_artifact_disk / "battle_123_decrypted.json"
    backend.files[str(alias)] = old
    payload = {"battleID": 123, "data": {"battleID": 123, "eventDataList": []}, "components": {}}
    monkeypatch.setattr(exporter, "run_checked", lambda *_args, **_kwargs: json.dumps({"payload": payload}))
    exporter._capture_report_impl(None, 123, no_artifact_disk, request_missing=False, artifact_store=backend)
    retained = no_artifact_disk / "captures" / "123" / (hashlib.sha256(old).hexdigest() + ".json")
    assert backend.files[str(retained)] == old
    assert backend.files[str(alias)] != old


def test_strict_parallelism_rejected_before_loading_any_state(no_artifact_disk):
    backend = CloudFixture()
    with pytest.raises(ValueError, match="parallelism=1"):
        BattleCollector(None, no_artifact_disk, artifact_store=backend, parallelism=2)
    assert not backend.files and not backend.states


def test_strict_resume_rejects_another_account_state(no_artifact_disk):
    backend = CloudFixture()
    backend.states["one"] = {"version": 1, "namespace": "other", "reports": {}, "scopes": {}}
    with pytest.raises(ValueError, match="mismatch"):
        BattleCollector(None, no_artifact_disk, namespace="one", artifact_store=backend)


def test_full_capture_is_recoverable_from_cloud_catalog_and_journals_without_local_io(
        no_artifact_disk, lua_capture, monkeypatch):
    from parser import cloud_store

    class Transport:
        def __init__(self):
            self.objects = {}

        def list_files(self, _folder):
            return [{"fid": fid, "file_name": name} for fid, (name, _raw) in self.objects.items()]

        def upload_bytes(self, raw, name, *, parent_fid):
            assert parent_fid == "fixture-folder"
            fid = str(len(self.objects) + 1)
            self.objects[fid] = (name, raw)
            return {"fid": fid}

        def download_bytes(self, fid, *, max_bytes):
            raw = self.objects[fid][1]
            assert len(raw) <= max_bytes
            return raw

    monkeypatch.setattr(cloud_store, "load_config", lambda *_args: {"folder_id": "fixture-folder"})
    transport, runner = Transport(), full_cycle_runner(lua_capture)
    store = cloud_store.CloudStore(client=transport, journal_mode=True)
    backend = cloud_store.CloudArtifacts(store, namespace="one", logical_root=no_artifact_disk)
    collector = BattleCollector(runner, no_artifact_disk, namespace="one", scopes=("personal",),
                               artifact_store=backend, request_delay=0)
    assert collector.run_cycle()["complete"]
    # A new instance starts with no retained process-local catalog or chunks.
    restarted = cloud_store.CloudStore(client=transport)
    resumed = cloud_store.CloudArtifacts(restarted, namespace="one", logical_root=no_artifact_disk)
    assert resumed.load_state("one")["reports"]["123"]["complete"]
    raw = resumed.read_bytes(no_artifact_disk / "battle_123_decrypted.json")
    value = json.loads(raw)
    assert value["captureComplete"] and value["data"]["unknown"] == "retained"
    reports = restarted.list_reports()
    assert len(reports) == 1 and reports[0]["battle_uid"] == "123"
    assert retained_count(runner) == 0 and runner.restored()
