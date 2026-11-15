"""The four existing statistics keep their evidence rules on RAM-only rows."""
from __future__ import annotations

import builtins
import copy
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sqlite3
import time

import pytest

from battle_ui.data import BattleData, BattleDataError, BattleQueryTimeout
from battle_ui.daily import DailyStats, MAX_UNION_CHECKPOINT_AGE
from battle_ui.opponents import OpponentRoster
from battle_ui.peerless import PeerlessRecords
from battle_ui.team_winrates import TeamWinrateService
from parser.report_archive import ReportArchive


NS = "server:1/scenario:18/avatar:100"
STAMP = 1794470400
IDENTITY = {"serverId": 1, "scenarioId": 18, "avatarId": 100}
SKILL_TYPES = {str(3000 + hero * 10 + slot): 0 if slot == 0 else 3
               for hero in range(3) for slot in range(3)}


def runtime_state():
    return {"namespace": NS, "runtimeIdentity": dict(IDENTITY),
            "runtimeCapabilitiesObservedAt": time.time(),
            "runtimeCapabilities": {"unionId": 10, "battleMatchTypes": {"PVP": 7, "PVE": 8}}}


def payload(battle_uid="1001", *, legend=True, match_type=7):
    def army(uid, union, base):
        return {"army_id": uid * 100, "zgNum": 12 if uid == 100 else 0,
                "avatar_data": {"avatar_id": uid, "avatar_name": str(uid),
                                "avatar_union_id": union, "avatar_union_name": str(union)},
                "army_member": [{"heros_id": base + hero, "index": hero,
                    "evolutionNum": 5, "enlighten": 5,
                    "skills": [{"skillId": 3000 + hero * 10 + slot, "level": 10}
                               for slot in range(3)]} for hero in range(3)]}

    ci = {"avatarId": 200, "zgNum": 20000, "winBattleNum": 5,
          "isMuti": True, "continuousInfoType": 0}
    evaluation = {"version": 1, "evaluator": "client_BattleReportHelper.IsLegend",
                  "status": "evaluated", "legend": legend, "battleID": battle_uid,
                  **ci, "endTimeStamp": STAMP, "category": "normal", "category_state": "runtime_enum",
                  "rule": {"normalType": 0, "pvBarbarianArmyType": 1, "maxWarContribute": 20000,
                           "maxGame": 5, "bannerMinFeat": 5000, "bannerMinBattles": 2}}
    data = {"battleID": battle_uid, "battleJobId": int(battle_uid) + 5000,
            "timeStamp": STAMP - 10, "endTimeStampTime": STAMP, "childNum": 0,
            "matchType": match_type, "eventDataList": [{"eventId": 0, "intList": [], "tab": 0}],
            "battleSettlement": {"winer": 10000, "attacker": army(100, 10, 4000),
                                 "defensive": army(200, 20, 5000)}}
    return {"namespace": NS, "runtimeIdentity": dict(IDENTITY), "battleID": battle_uid,
            "source": "all", "captureVersion": 2, "eventCount": 1, "captureComplete": True,
            "serializationIssues": [], "data": data,
            "components": {key: copy.deepcopy(data) if key in ("basic", "detail", "all") else {}
                           for key in ("basic", "detail", "all", "statics", "snapshots")},
            "componentStatus": {key: {"available": True, "complete": True}
                                for key in ("basic", "detail", "all", "statics", "snapshots")},
            "discovery": [{"scope": "personal", "parent": None,
                "row": {"blockHash": "block-" + battle_uid, "childNum": 0,
                        "combatInfo": copy.deepcopy(data), "otherInfo": {"continuousInfo": ci}},
                "clientPeerless": [evaluation]}]}


@contextmanager
def memory_archive(values):
    connection = sqlite3.connect(":memory:")
    archive = ReportArchive(connection=connection, storage_mode="lean")
    try:
        for value in values:
            archive.import_bytes(json.dumps(value, ensure_ascii=False).encode(), namespace=NS)
        yield connection, BattleData(connection=connection, timeout_seconds=10)
    finally:
        archive.close()


def summaries(data, state):
    provider = lambda namespace: copy.deepcopy(state) if namespace == NS else None
    return {
        "daily": DailyStats(data=data, own_union_only=True, runtime_state_provider=provider).summary(namespace=NS),
        "teams": TeamWinrateService(data=data, skill_types=SKILL_TYPES,
                        runtime_state_provider=provider).summary(namespace=NS),
        "opponents": OpponentRoster(data=data).summary(namespace=NS),
        "peerless": PeerlessRecords(data=data).summary(namespace=NS),
    }


def stable(value):
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items()
                if key not in {"elapsed_seconds", "database_path"}}
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value


def test_all_four_ram_results_equal_file_queries(tmp_path):
    values = [payload(), payload("1002", legend=False)]
    state = runtime_state()
    path = tmp_path / "synthetic-reference.db"
    with ReportArchive(path, storage_mode="lean") as archive:
        for value in values:
            archive.import_bytes(json.dumps(value, ensure_ascii=False).encode(), namespace=NS)
    expected = summaries(BattleData(path, timeout_seconds=10), state)
    with memory_archive(values) as (connection, data):
        before = connection.total_changes
        actual = summaries(data, state)
        assert stable(actual) == stable(expected)
        assert actual["daily"]["totals"]["known_merit"] == 24
        assert len(actual["teams"]["without_skills"]) == 2
        assert actual["opponents"]["players"]
        assert len(actual["peerless"]["records"]) == 1
        assert connection.total_changes == before and not connection.in_transaction
        assert data.database_path is None and not hasattr(data, "db_path")
        assert all(not row[2] for row in connection.execute("PRAGMA database_list"))


def test_four_ram_queries_and_imports_never_open_a_file(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("RAM statistics must never open a local file")

    with monkeypatch.context() as guarded:
        guarded.setattr(builtins, "open", forbidden)
        guarded.setattr(io, "open", forbidden)
        with memory_archive([payload()]) as (_connection, data):
            actual = summaries(data, runtime_state())
            assert all(value["available"] for value in actual.values())
            assert len(actual["peerless"]["records"]) == 1
            assert data.original_preview(1)["text"].startswith("{")
            with pytest.raises(BattleDataError, match="导出"):
                data.export_original(1, "forbidden-report.json")


@pytest.mark.parametrize("change", ["expired", "wrong_account", "missing_timestamp", "no_union", "missing"])
def test_ram_daily_identity_fails_closed_without_local_fallback(monkeypatch, change):
    state = runtime_state()
    if change == "expired":
        state["runtimeCapabilitiesObservedAt"] -= MAX_UNION_CHECKPOINT_AGE + 61
        state["updatedAt"] = time.time()
    elif change == "wrong_account":
        state["runtimeIdentity"]["avatarId"] = 999
    elif change == "missing_timestamp":
        del state["runtimeCapabilitiesObservedAt"]
        state["updatedAt"] = time.time()
    elif change == "no_union":
        state["runtimeCapabilities"]["unionId"] = 0
    else:
        state = None
    with memory_archive([payload()]) as (_connection, data):
        service = DailyStats(data=data, own_union_only=True,
                            runtime_dir=Path("unused-runtime"),
                            runtime_state_provider=lambda _namespace: state)
        with monkeypatch.context() as guarded:
            guarded.setattr(Path, "read_text", lambda *a, **k: pytest.fail("local fallback is forbidden"))
            result = service.summary(namespace=NS)
        assert result["players"] == []
        assert result["union_filter"]["contexts"][NS]["status"] in {"unknown", "no_union"}


@pytest.mark.parametrize("change", ["no_state", "no_enum", "wrong_account", "unknown_match_type"])
def test_ram_pvp_stays_unknown_without_captured_enum(monkeypatch, change):
    state = runtime_state()
    value = payload(match_type=999 if change == "unknown_match_type" else 7)
    if change == "no_state":
        state = None
    elif change == "no_enum":
        state["runtimeCapabilities"].pop("battleMatchTypes")
    elif change == "wrong_account":
        state["runtimeIdentity"]["avatarId"] = 999
    with memory_archive([value]) as (_connection, data):
        service = TeamWinrateService(data=data, runtime_dir="unused-runtime",
                    skill_types=SKILL_TYPES, runtime_state_provider=lambda _namespace: state)
        with monkeypatch.context() as guarded:
            guarded.setattr(Path, "read_text", lambda *a, **k: pytest.fail("local fallback is forbidden"))
            result = service.summary(namespace=NS)
        assert result["without_skills"] == []
        assert result["coverage"]["unknown_reports"] == 1


def test_ram_missing_metadata_and_legend_proof_are_not_invented():
    value = payload()
    value["discovery"] = []
    for source in (value["data"], *(value["components"][key] for key in ("basic", "detail", "all"))):
        source.pop("childNum")
    with memory_archive([value]) as (_connection, data):
        result = summaries(data, runtime_state())
        assert result["daily"]["coverage"]["unknown_family_reports"] == 1
        assert result["peerless"]["records"] == []


def test_ram_preserves_daily_cutoff_and_reads_peerless_evidence_after_128():
    value = payload()
    observation = value["discovery"][0]
    value["discovery"] = []
    for index in range(130):
        item = copy.deepcopy(observation)
        item["row"]["blockHash"] = "independent-block-" + str(index)
        item["clientPeerless"][0]["legend"] = index == 129
        value["discovery"].append(item)
    with memory_archive([value]) as (_connection, data):
        result = summaries(data, runtime_state())
        assert result["daily"]["coverage"]["observations_truncated_reports"] == 1
        assert len(result["peerless"]["records"]) == 1


def test_ram_read_scope_blocks_file_attach_and_write_pragmas_and_remains_reusable(tmp_path):
    target = tmp_path / "must-not-exist.db"
    with memory_archive([payload()]) as (connection, data):
        with data._connection() as read:
            for query in ("ATTACH DATABASE ? AS leaked", "VACUUM INTO ?"):
                with pytest.raises(sqlite3.DatabaseError):
                    read.execute(query, (str(target),))
            for query in ("PRAGMA query_only=OFF", "PRAGMA temp_store=FILE", "PRAGMA temp_store_directory='.'"):
                with pytest.raises(sqlite3.DatabaseError):
                    read.execute(query)
            with pytest.raises(sqlite3.DatabaseError):
                read.execute("DELETE FROM report_records")
        assert not target.exists()
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        assert connection.execute("PRAGMA temp_store").fetchone()[0] == 2
        # The owner can refresh the projection after the borrowed read ends.
        connection.execute("UPDATE report_meta SET value=value")
        connection.commit()
        assert data.overview()["total"] == 1


def test_ram_injection_rejects_disk_connections_and_attachments(tmp_path):
    with sqlite3.connect(tmp_path / "synthetic-disk.db") as disk:
        with pytest.raises(ValueError, match="memory"):
            BattleData(connection=disk)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("ATTACH DATABASE ? AS disk", (str(tmp_path / "synthetic-attached.db"),))
        with pytest.raises(ValueError, match="memory"):
            BattleData(connection=connection)
    finally:
        connection.close()


def test_ram_query_timeout_releases_transaction_and_progress_handler():
    with memory_archive([payload()]) as (connection, data):
        data.timeout_seconds = .000001
        with pytest.raises(BattleQueryTimeout):
            with data._connection() as read:
                read.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n").fetchone()
        assert not connection.in_transaction
        data.timeout_seconds = 10
        assert data.overview()["total"] == 1
