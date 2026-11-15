"""Four cloud statistics retain the local algorithms without local report I/O."""
from __future__ import annotations

import builtins
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from battle_ui.cloud_data import CloudBattleData
from battle_ui.data import BattleData, BattleDataError
from battle_ui.daily import DailyStats
from battle_ui.opponents import OpponentRoster
from battle_ui.peerless import PeerlessRecords
from battle_ui.team_winrates import TeamWinrateService
from parser.cloud_store import report_metadata
from parser.report_archive import ReportArchive


NS = "server:1/scenario:18/avatar:100"
OTHER = "server:2/scenario:18/avatar:300"


@pytest.fixture(autouse=True)
def no_dictionary_disk_work(monkeypatch):
    from parser import semantic_catalog
    monkeypatch.setattr(semantic_catalog, "build_default_mapper", lambda: None)


def encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


def army(uid, hero_base):
    return {"army_id": uid * 100, "zgNum": 15,
        "avatar_data": {"avatar_id": uid, "avatar_name": "玩家" + str(uid),
                        "avatar_union_id": 20, "avatar_union_name": "测试同盟"},
        "army_member": [{"heros_id": hero_base + n, "index": n, "name": "武将" + str(n),
            "evolutionNum": 4, "enlighten": 4, "hero_lv": 50,
            "skills": [{"skillId": 3000 + n * 10 + slot, "level": 10, "orderLevel": 0}
                       for slot in range(3)]} for n in range(3)]}


def observation(uid="1001", *, legend=True):
    stamp = 1794700000
    return {"scope": "personal", "parent": None,
        "row": {"childNum": 0, "blockHash": "co_" + str(uid),
            "combatInfo": {"battleID": str(uid), "endTimeStampTime": stamp},
            "otherInfo": {"continuousInfo": {"avatarId": 200, "zgNum": 20000,
                "winBattleNum": 5, "isMuti": True, "continuousInfoType": 0}}},
        "clientPeerless": [{"version": 1, "evaluator": "client_BattleReportHelper.IsLegend",
            "status": "evaluated", "legend": legend, "battleID": str(uid), "avatarId": 200,
            "isMuti": True, "continuousInfoType": 0, "zgNum": 20000,
            "winBattleNum": 5, "endTimeStamp": stamp, "category": "normal",
            "category_state": "runtime_enum", "rule": {"normalType": 0,
                "pvBarbarianArmyType": 1, "maxWarContribute": 20000, "maxGame": 5,
                "bannerMinFeat": 5000, "bannerMinBattles": 2}}]}


def payload(uid="1001", *, namespace=NS, discovery=True, events=3):
    fields = dict(part.split(":", 1) for part in namespace.split("/"))
    data = {"battleID": str(uid), "childNum": 0, "matchType": 7,
        "timeStamp": 1794699990, "endTimeStampTime": 1794700000,
        "eventDataList": [{"eventId": n, "intList": [0, False, None], "tab": 0}
                          for n in range(events)],
        "battleSettlement": {"attacker": army(100, 4000), "defensive": army(200, 5000), "winer": 10000}}
    return {"namespace": namespace, "battleID": str(uid), "captureVersion": 2,
        "runtimeIdentity": {"serverId": fields["server"], "scenarioId": fields["scenario"], "avatarId": fields["avatar"]},
        "source": "all", "data": data, "eventCount": events, "captureComplete": True,
        "serializationIssues": [], "discovery": [observation(uid)] if discovery else [],
        "components": {name: copy.deepcopy(data) if name in ("basic", "detail", "all") else {}
                       for name in ("basic", "detail", "all", "statics", "snapshots")},
        "componentStatus": {name: {"available": True, "complete": True}
                            for name in ("basic", "detail", "all", "statics", "snapshots")}}


def state(*values, namespace=NS):
    identity = payload(namespace=namespace)["runtimeIdentity"]
    return {"version": 1, "namespace": namespace, "runtimeIdentity": identity,
        "runtimeCapabilities": {"unionId": 20, "battleMatchTypes": {"PVP": 7, "PVE": 8}},
        "runtimeCapabilitiesObservedAt": time.time(), "reports": {str(value["row"]["combatInfo"]["battleID"]):
            {"battleID": str(value["row"]["combatInfo"]["battleID"]), "observations": {"observed": value}}
            for value in values}}


class Store:
    def __init__(self, *values):
        self.catalog = {"reports": {}, "files": {}, "states": {}}
        self.raw = {}
        self.reads = []
        self.refreshes = 0
        self.offline = False
        for value in values:
            self.put(value)

    def put(self, value):
        raw = encode(value)
        rows = report_metadata(raw, value.get("namespace", NS) if isinstance(value, dict) else NS, "fixture/report.json")
        for row in rows:
            self.catalog["reports"][row["key"]] = row
            self.raw[row["key"]] = raw

    def file(self, key, value, namespace="legacy"):
        raw = encode(value)
        self.catalog["files"][key] = {"namespace": namespace, "sha256": hashlib.sha256(raw).hexdigest(), "byte_count": len(raw)}
        self.raw[key] = raw

    def refresh(self):
        self.refreshes += 1
        if self.offline:
            raise OSError("fixture cloud offline")

    def list_reports(self):
        return list(self.catalog["reports"].values())

    def read_report(self, key):
        self.reads.append(("report", key))
        return self.raw[key]

    def read_file(self, key, maximum):
        assert len(self.raw[key]) <= maximum
        self.reads.append(("file", key))
        return self.raw[key]


def baseline(values, runtime=None):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    archive = ReportArchive(connection=connection, storage_mode="lean")
    for value in values:
        result = archive.import_bytes(encode(value), namespace=value["namespace"])
        assert result["status"] == "imported", result
    data = BattleData(connection=connection, timeout_seconds=30)
    provider = lambda namespace: runtime if runtime is not None and runtime["namespace"] == namespace else None
    return archive, {
        "daily": DailyStats(data=data, own_union_only=True, runtime_state_provider=provider),
        "team_winrates": TeamWinrateService(data=data, skill_types={}, runtime_state_provider=provider),
        "opponents": OpponentRoster(data=data), "peerless": PeerlessRecords(data=data)}


def cloud_service(data, page):
    return getattr(data, {"daily": "daily_service", "team_winrates": "team_winrate_service",
                          "opponents": "opponent_service", "peerless": "peerless_service"}[page])


def without_cloud_tags(value):
    return {key: item for key, item in value.items() if key not in {"storage_backend", "memory_only", "cloud_projection", "elapsed_seconds"}}


@pytest.mark.parametrize("page", ["daily", "team_winrates", "opponents", "peerless"])
def test_four_cloud_summaries_match_existing_statistics(page):
    values = [payload("1001"), payload("1002")]
    runtime = state()
    store = Store(*values)
    store.catalog["states"][NS] = runtime
    cloud = CloudBattleData(store)
    local_archive, services = baseline(values, runtime)
    try:
        if page == "team_winrates":
            cloud.daily_service.summary(namespace=NS)
            cloud._statistics()._service(page).skill_types = {}
        actual = cloud_service(cloud, page).summary(namespace=NS)
        expected = services[page].summary(namespace=NS)
        assert without_cloud_tags(actual) == without_cloud_tags(expected)
        assert actual["cloud_projection"]["projected_reports"] == 2
        assert actual["storage_backend"] == "quark-memory"
    finally:
        local_archive.close()


def test_shared_projection_keeps_actual_quality_and_drops_all_event_originals():
    value = payload(events=1000)
    value["data"]["eventDataList"][3]["bad"] = "<userdata:fixture>"
    store = Store(value)
    cloud = CloudBattleData(store)
    cloud.daily_service.summary()
    connection = cloud._statistics().connection
    row = connection.execute("SELECT completeness,quality_json,issues_json,event_count,field_count FROM report_snapshots").fetchone()
    archive, _ = baseline([value])
    try:
        expected = archive.connection.execute("SELECT completeness,quality_json,issues_json,event_count,field_count FROM report_snapshots").fetchone()
        assert tuple(row) == tuple(expected)
    finally:
        archive.close()
    assert row["event_count"] == 1000 and row["completeness"] == "partial"
    assert connection.execute("SELECT count(*) FROM report_events").fetchone()[0] == 0
    assert connection.execute("SELECT sum(length(raw_bytes)) FROM report_files").fetchone()[0] == 0
    assert connection.execute("SELECT payload_json FROM report_snapshots").fetchone()[0] == "{}"
    assert connection.execute("SELECT count(*) FROM report_fields WHERE pointer LIKE '%/eventDataList/%'").fetchone()[0] == 0
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
    assert connection.execute("PRAGMA temp_store").fetchone()[0] == 2
    assert all(not row[2] for row in connection.execute("PRAGMA database_list"))


def test_cached_projection_still_requires_cloud_on_every_query():
    store = Store(payload())
    cloud = CloudBattleData(store)
    for page in ("daily", "peerless", "opponents", "team_winrates"):
        cloud_service(cloud, page).summary(namespace=NS)
    assert len(store.reads) == 1 and store.refreshes == 4
    store.offline = True
    for page in ("daily", "peerless", "opponents", "team_winrates"):
        with pytest.raises(OSError, match="offline"):
            cloud_service(cloud, page).summary(namespace=NS)
    assert len(store.reads) == 1


def test_checkpoint_overlay_recovers_peerless_and_keeps_gui_snapshot_ids():
    value = payload(discovery=False)
    store = Store(value)
    runtime = state(observation())
    store.catalog["states"][NS] = runtime
    cloud = CloudBattleData(store)
    uid = cloud.list_reports()["items"][0]["snapshot_id"]
    result = cloud.peerless_service.summary(NS)
    assert result["counts"]["occurrences"] == 1
    assert result["records"][0]["snapshot_id"] == uid
    assert result["cloud_projection"]["checkpoint_observations"] == 1
    assert cloud.detail(uid)["report"]["battle_uid"] == "1001"
    assert cloud._statistics().connection.execute("SELECT completeness FROM report_snapshots").fetchone()[0] == "complete"


def test_historical_migrated_state_is_read_once_from_quark_and_validated():
    store = Store(payload(discovery=False))
    folder = hashlib.sha256(NS.encode()).hexdigest()[:20]
    key = "history/data/incoming/runtime/" + folder + "/collector_state.json"
    store.file(key, state(observation()))
    cloud = CloudBattleData(store)
    assert cloud.peerless_service.summary(NS)["counts"]["occurrences"] == 1
    assert cloud.peerless_service.summary(NS)["counts"]["occurrences"] == 1
    assert store.reads.count(("file", key)) == 1
    bad = state(observation())
    bad["runtimeIdentity"]["avatarId"] = "999"
    store.file(key, bad)
    result = cloud.peerless_service.summary(NS)
    assert result["records"] == []
    assert result["cloud_projection"]["invalid_checkpoints"] == 1


def test_observation_uid_and_namespace_cannot_be_borrowed_from_other_reports():
    store = Store(payload(discovery=False), payload("2001", namespace=OTHER, discovery=False))
    bad = state(observation("9999"))
    bad["reports"]["1001"] = bad["reports"].pop("9999")
    store.catalog["states"][NS] = bad
    store.catalog["states"][OTHER] = state(observation("2001"), namespace=OTHER)
    cloud = CloudBattleData(store)
    assert cloud.peerless_service.summary(NS)["records"] == []
    rows = cloud.peerless_service.summary(OTHER)["records"]
    assert len(rows) == 1 and rows[0]["namespace"] == OTHER


def test_original_discovery_beyond_128_and_dictionary_keys_are_preserved():
    value = payload(discovery=False)
    value["discovery"] = {}
    for index in range(131):
        item = observation(legend=index == 130)
        item["row"]["otherInfo"]["continuousInfo"]["zgNum"] += index
        item["clientPeerless"][0]["zgNum"] += index
        value["discovery"]["key_" + str(index)] = item
    cloud = CloudBattleData(Store(value))
    result = cloud.peerless_service.summary(NS)
    assert result["counts"]["occurrences"] == 1
    assert result["records"][0]["merit"] == 20130
    assert any("/discovery/key_130/" in entry["pointer"] for entry in result["records"][0]["evidence"])


def test_updated_raw_replaces_projection_without_counting_old_version():
    value = payload()
    store = Store(value)
    cloud = CloudBattleData(store)
    first = cloud.daily_service.summary(None, NS)
    sid = cloud.list_reports()["items"][0]["snapshot_id"]
    value["discovery"][0]["clientPeerless"][0]["legend"] = False
    store.put(value)
    assert cloud.peerless_service.summary(NS)["records"] == []
    assert cloud.list_reports()["items"][0]["snapshot_id"] == sid
    connection = cloud._statistics().connection
    assert connection.execute("SELECT count(*) FROM report_snapshots").fetchone()[0] == 1
    store.catalog["reports"].clear()
    assert cloud.daily_service.summary(namespace=NS)["days"] == []
    assert connection.execute("SELECT count(*) FROM report_records").fetchone()[0] == 0
    assert first["available"] is True


def test_oversize_report_is_rejected_before_cloud_read():
    store = Store(payload())
    cloud = CloudBattleData(store, max_report_bytes=10)
    with pytest.raises(BattleDataError, match="内存读取上限"):
        cloud.peerless_service.summary()
    assert store.reads == []


def test_projection_limit_is_explicit_and_does_not_return_a_sample():
    cloud = CloudBattleData(Store(payload()), max_projection_bytes=64 * 1024)
    with pytest.raises(BattleDataError, match="运行内存统计预算达到上限"):
        cloud.daily_service.summary()


def test_no_statistics_filesystem_writes_or_local_checkpoint_reads(monkeypatch):
    store = Store(payload())
    store.catalog["states"][NS] = state()
    cloud = CloudBattleData(store)
    actual_connect = sqlite3.connect
    actual_open = builtins.open

    def connect(database, *args, **kwargs):
        assert database == ":memory:"
        return actual_connect(database, *args, **kwargs)

    def no_write(file, mode="r", *args, **kwargs):
        assert not any(mark in mode for mark in "wax+"), "statistics attempted filesystem write"
        assert not str(file).endswith("collector_state.json"), "statistics read local checkpoint"
        return actual_open(file, mode, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("statistics attempted filesystem write")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(builtins, "open", no_write)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    for page in ("daily", "peerless", "opponents", "team_winrates"):
        assert cloud_service(cloud, page).summary(namespace=NS)["available"]
    assert not hasattr(cloud, "db_path")


def test_background_threads_serialize_on_the_same_memory_connection():
    cloud = CloudBattleData(Store(payload()))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda page: cloud_service(cloud, page).summary(namespace=NS),
                                ("daily", "peerless", "opponents", "team_winrates")))
    assert all(result["available"] for result in results)
    assert cloud.statistics_status()["phase"] == "ready"


def test_static_chinese_names_and_scenario_mapping_match_the_original_service(monkeypatch):
    from parser import semantic_catalog
    from parser.report_semantics import SemanticMapper, SemanticArchive
    mapper = SemanticMapper("cloud-name-parity", {},
        lambda kind, uid: {"mapped": True, "name_zh": "fallback_" + str(uid)},
        lambda event, context: pytest.fail("statistics decoded an event"),
        context_id_lookup=lambda kind, uid, context: {"mapped": True,
            "name_zh": "场景" + str(context.get("scenario_id")) + "/" + kind + "/" + str(uid)})
    monkeypatch.setattr(semantic_catalog, "build_default_mapper", lambda: mapper)
    value = payload()
    for branch in (value["data"], *(value["components"][name] for name in ("all", "detail", "basic"))):
        for side in ("attacker", "defensive"):
            for hero in branch["battleSettlement"][side]["army_member"]:
                hero.pop("name")
    archive, services = baseline([value], state())
    try:
        SemanticArchive(archive.connection, mapper).enrich_snapshot(1, include_events=False)
        store = Store(value)
        store.catalog["states"][NS] = state()
        cloud = CloudBattleData(store)
        actual = cloud.opponent_service.summary(NS)
        expected = services["opponents"].summary(NS)
        assert without_cloud_tags(actual) == without_cloud_tags(expected)
        assert actual["cloud_projection"]["semantic_names_available"] is True
        assert "场景18/hero/" in json.dumps(actual, ensure_ascii=False)
        # Replacement must delete the old semantic foreign-key dependants.
        value["data"]["battleSettlement"]["attacker"]["army_member"][0]["hero_lv"] = 51
        store.put(value)
        assert cloud.opponent_service.summary(NS)["available"] is True
        connection = cloud._statistics().connection
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT payload_json FROM report_snapshots").fetchone()[0] == "{}"
    finally:
        archive.close()


def test_chunk_order_reuses_one_decoded_chunk_and_bounds_compressed_cache():
    store = Store(payload("1001"), payload("1002"), payload("1003"))
    rows = list(store.catalog["reports"].values())
    store.catalog["blobs"] = {row["blob_sha256"]: {"parts": [{"chunk": {
        "stored_sha256": "b" if index != 1 else "a"}, "offset": 5 if index == 0 else 0}]}
        for index, row in enumerate(rows)}
    store.compressed_cache_bytes = 0
    cloud = CloudBattleData(store)
    cloud.opponent_service.summary()
    assert [key for kind, key in store.reads if kind == "report"] == [rows[1]["key"], rows[2]["key"], rows[0]["key"]]
    assert store.compressed_cache_bytes == 128 * 1024 * 1024


def test_progress_poll_remains_responsive_during_a_background_cloud_read():
    store = Store(payload())
    original = store.read_report
    entered, release = threading.Event(), threading.Event()

    def blocked(key):
        entered.set()
        assert release.wait(5)
        return original(key)

    store.read_report = blocked
    cloud = CloudBattleData(store)
    service = cloud.opponent_service
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(service.summary)
        assert entered.wait(5)
        try:
            poll = pool.submit(cloud.statistics_status)
            assert poll.result(timeout=.5)["phase"] == "reports"
        finally:
            release.set()
        assert future.result(timeout=5)["available"]


def test_fresh_save_time_never_extends_expired_union_identity():
    store = Store(payload())
    checkpoint = state()
    checkpoint["runtimeCapabilitiesObservedAt"] = time.time() - 601
    checkpoint["updatedAt"] = time.time()
    store.catalog["states"][NS] = checkpoint
    result = CloudBattleData(store).daily_service.summary(namespace=NS)
    assert result["union_filter"]["enabled"] is True
    assert result["players"] == []
    assert "unknown" in json.dumps(result["union_filter"])


def test_python_audit_confirms_zero_filesystem_mutations(tmp_path):
    script = r'''
import os, pathlib, runpy, sys
fixtures=runpy.run_path(sys.argv[1])
from parser import semantic_catalog
semantic_catalog.build_default_mapper=lambda: None
store=fixtures['Store'](fixtures['payload']())
store.catalog['states'][fixtures['NS']]=fixtures['state']()
writes=[]
connections=[]
def audit(event,args):
    if event=='open':
        mode=args[1]
        flags=args[2] if len(args)>2 else 0
        if (isinstance(mode,str) and any(x in mode for x in 'wax+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)):
            writes.append((event,str(args[0])))
            raise AssertionError('statistics attempted a filesystem write')
    if event in ('os.mkdir','os.rename','os.remove','os.rmdir','os.link','os.symlink'):
        writes.append((event,str(args[0])))
        raise AssertionError('statistics attempted filesystem mutation')
    if event=='sqlite3.connect':
        connections.append(args[0])
        assert args[0]==':memory:'
sys.addaudithook(audit)
cloud=fixtures['CloudBattleData'](store)
for page in ('daily','peerless','opponents','team_winrates'):
    assert fixtures['cloud_service'](cloud,page).summary(namespace=fixtures['NS'])['available']
assert writes==[] and connections==[':memory:']
print('CLOUD_STATS_AUDIT_OK writes=0 sqlite_memory=1')
'''
    environment = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path), TMPDIR=str(tmp_path),
        PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    completed = subprocess.run([sys.executable, "-B", "-c", script, str(Path(__file__).resolve())],
        cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True,
        encoding="utf-8", timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "CLOUD_STATS_AUDIT_OK writes=0 sqlite_memory=1" in completed.stdout


def test_progress_subscription_and_close_release_only_ram_state():
    cloud = CloudBattleData(Store(payload()))
    events = []
    cloud.set_stats_progress(events.append)
    cloud.peerless_service.summary()
    assert events[-1]["phase"] == "ready"
    assert all(set(item) == {"phase", "completed", "total", "coverage"} for item in events)
    connection = cloud._statistics().connection
    event_count = len(events)
    cloud.close()
    assert len(events) == event_count
    assert cloud.statistics_status()["phase"] == "closed"
    assert cloud.statistics_status()["coverage"] == {}
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_legacy_capture_fallback_binds_payload_identity_and_reuses_content_hash():
    value = payload(discovery=False)
    store = Store(value)
    source = payload()
    raw = encode(source)
    folder = hashlib.sha256(NS.encode()).hexdigest()[:20]
    key = "history/data/incoming/runtime/" + folder + "/captures/1001/" + hashlib.sha256(raw).hexdigest() + ".json"
    store.file(key, source, namespace="legacy")
    cloud = CloudBattleData(store)
    first = cloud.peerless_service.summary(NS)
    assert first["counts"]["occurrences"] == 1
    assert first["cloud_projection"]["capture_metadata_sources"] == 1
    cloud.peerless_service.summary(NS)
    assert store.reads.count(("file", key)) == 1


@pytest.mark.parametrize("page", ["daily", "team_winrates", "opponents", "peerless"])
def test_all_services_reject_a_catalog_report_bound_to_the_wrong_account(page):
    store = Store(payload())
    next(iter(store.catalog["reports"].values()))["namespace"] = OTHER
    cloud = CloudBattleData(store)
    with pytest.raises(BattleDataError, match="运行账号身份不匹配"):
        cloud_service(cloud, page).summary()


def test_progress_is_throttled_while_status_and_phase_changes_remain_current(monkeypatch):
    from battle_ui import cloud_stats
    cloud = CloudBattleData(Store(payload()))
    events = []
    cloud.set_stats_progress(events.append)
    engine = cloud._statistics()
    monkeypatch.setattr(cloud_stats.time, "monotonic", lambda: 10.0)
    for index in range(2271):
        engine._emit("reports", index, 2271)
    assert len(events) == 1
    assert cloud.statistics_status()["completed"] == 2270
    engine._emit("query", 2271, 2271)
    engine._emit("ready", 2271, 2271)
    assert [item["phase"] for item in events] == ["reports", "query", "ready"]


def test_cancel_does_not_wait_for_the_worker_lock_and_stops_remaining_reports():
    store = Store(payload("1001"), payload("1002"))
    original = store.read_report
    entered, release = threading.Event(), threading.Event()

    def blocked(key):
        entered.set()
        assert release.wait(5)
        return original(key)

    store.read_report = blocked
    cloud = CloudBattleData(store)
    service = cloud.opponent_service
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(service.summary)
        assert entered.wait(5)
        try:
            # A cancellation operation running on a second thread must not
            # wait for the first worker's owner/SQLite lock.
            pool.submit(cloud.cancel_statistics).result(timeout=.5)
        finally:
            release.set()
        with pytest.raises(BattleDataError, match="已取消"):
            result.result(timeout=5)
    assert len(store.reads) == 1
    cloud.close()
    assert service.summary()["available"] is True


def test_close_clears_store_raw_and_compressed_memory_caches():
    store = Store(payload())
    store._lock = threading.RLock()
    store._cached_chunk = ("chunk", b"report bytes")
    store._compressed_cache = {"chunk": b"compressed report"}
    store._compressed_cache_size = len(b"compressed report")
    cloud = CloudBattleData(store)
    cloud.opponent_service.summary()
    cloud.close()
    assert store._cached_chunk is None
    assert store._compressed_cache == {}
    assert store._compressed_cache_size == 0


def test_transient_cloud_failure_reuses_committed_projection_on_same_instance():
    from parser.quark_memory_client import _TransientTransportError
    values = [payload(str(uid)) for uid in (1001, 1002, 1003)]
    runtime = state()
    store = Store(*values)
    store.catalog["states"][NS] = runtime
    rows = list(store.catalog["reports"].values())
    # Stable chunk/offset ordering makes the failure occur after precisely one
    # successful commit; refreshed opaque FIDs must not invalidate that commit.
    store.catalog["blobs"] = {row["blob_sha256"]: {"parts": [{"chunk": {
        "fid": "opaque-before-refresh", "stored_sha256": "a" * 64}, "offset": index}]}
        for index, row in enumerate(rows)}
    original = store.read_report
    first_key, second_key = rows[0]["key"], rows[1]["key"]
    attempts = {}

    def read(key):
        attempts[key] = attempts.get(key, 0) + 1
        if key == second_key and attempts[key] == 1:
            raise _TransientTransportError("fixture audited transient EPROTO")
        return original(key)

    store.read_report = read
    cloud = CloudBattleData(store)
    archive, services = baseline(values, runtime)
    try:
        with pytest.raises(_TransientTransportError, match="EPROTO"):
            cloud.opponent_service.summary(namespace=NS)
        engine = cloud._statistics()
        assert len(engine._signatures) == 1
        assert cloud._ids[first_key] in engine._signatures
        assert engine.connection.execute("SELECT count(*) FROM report_snapshots").fetchone()[0] == 1

        store.catalog = copy.deepcopy(store.catalog)
        for blob in store.catalog["blobs"].values():
            blob["parts"][0]["chunk"]["fid"] = "opaque-after-refresh"
        resumed = cloud.opponent_service.summary(namespace=NS)
        assert resumed["counts"]["reports"] == 3
        assert attempts[first_key] == 1 and attempts[second_key] == 2
        assert store.refreshes == 2
        assert len(engine._signatures) == 3
        assert engine.connection.execute("SELECT count(*) FROM report_snapshots").fetchone()[0] == 3

        engine._service("team_winrates").skill_types = {}
        for page, expected_service in services.items():
            actual = cloud_service(cloud, page).summary(namespace=NS)
            assert without_cloud_tags(actual) == without_cloud_tags(expected_service.summary(namespace=NS))
        assert attempts[first_key] == 1 and attempts[second_key] == 2
    finally:
        cloud.close()
        archive.close()


def test_default_projection_budget_is_one_gib_in_both_cloud_interfaces():
    from battle_ui.cloud_stats import CloudStatsEngine
    cloud = CloudBattleData(Store())
    assert cloud._statistics().max_projection_bytes == 1024 ** 3
    assert CloudStatsEngine(cloud).max_projection_bytes == 1024 ** 3


def test_sqlite_full_then_larger_same_connection_budget_reuses_committed_rows():
    values = [payload(str(uid)) for uid in (1001, 1002, 1003)]
    runtime = state()
    store = Store(*values)
    store.catalog["states"][NS] = runtime
    cloud = CloudBattleData(store, max_projection_bytes=256 * 1024)
    archive, services = baseline(values, runtime)
    try:
        with pytest.raises(BattleDataError, match="运行内存统计预算达到上限") as failed:
            cloud.opponent_service.summary(namespace=NS)
        assert getattr(failed.value.__cause__, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL
        engine = cloud._statistics()
        connection = engine.connection
        committed_ids = set(engine._signatures)
        assert 0 < len(committed_ids) < len(values)
        assert not connection.in_transaction
        assert {row[0] for row in connection.execute("SELECT id FROM report_snapshots")} == committed_ids
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        committed_keys = {cloud._keys[sid] for sid in committed_ids}
        before = {key: store.reads.count(("report", key)) for key in committed_keys}

        # SQLITE_FULL rolled back only the in-progress report transaction.
        # Raising this connection's cap must not require discarding earlier RAM
        # rows or re-reading their already verified original documents.
        engine.max_projection_bytes = 4 * 1024 * 1024
        connection.execute("PRAGMA max_page_count=" + str(engine.max_projection_bytes // 4096))
        result = cloud.opponent_service.summary(namespace=NS)
        assert engine.connection is connection
        assert result["counts"]["reports"] == 3
        assert len(engine._signatures) == 3
        assert connection.execute("SELECT count(*) FROM report_snapshots").fetchone()[0] == 3
        assert {key: store.reads.count(("report", key)) for key in committed_keys} == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        engine._service("team_winrates").skill_types = {}
        for page, expected_service in services.items():
            actual = cloud_service(cloud, page).summary(namespace=NS)
            assert without_cloud_tags(actual) == without_cloud_tags(expected_service.summary(namespace=NS))
    finally:
        cloud.close()
        archive.close()
