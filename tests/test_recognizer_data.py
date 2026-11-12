"""The screen recognizer reads isolated historical lineups without archive writes."""
from __future__ import annotations

import copy
import json
import pytest

from battle_ui.data import BattleDataError
from battle_ui.recognizer_data import EnemyDataLoader
from parser.report_archive import ReportArchive
from parser.report_semantics import SemanticMapper


NS = "server:1/scenario:18/avatar:100"
OTHER = "server:2/scenario:18/avatar:300"


def army(uid, name, heroes=(4012, 4003, 4020)):
    return {"army_id": 10000 + uid, "avatar_data": {"avatar_id": uid, "avatar_name": name},
            "army_member": [{"heros_id": hero, "name": f"将{hero}", "index": index,
                             "evolutionNum": 0, "hero_lv": 1} for index, hero in enumerate(heroes)]}


def report(bid="1", name="对手", uid=200, heroes=(4012, 4003, 4020), stamp=1794000000):
    data = {"battleID": bid, "endTimeStampTime": stamp,
            "battleSettlement": {"attacker": army(100, "本人"), "defensive": army(uid, name, heroes)},
            "eventDataList": [{"eventId": 0, "intList": [], "tab": 0}]}
    return {"battleID": bid, "captureVersion": 2, "source": "all", "eventCount": 1, "data": data,
            "components": {"basic": copy.deepcopy(data), "detail": copy.deepcopy(data),
                           "all": copy.deepcopy(data), "statics": {},
                           "snapshots": {"isComplete": True, "battleSnapData": {}}},
            "componentStatus": {key: {"available": True, "complete": True} for key in
                                ("basic", "detail", "all", "statics", "snapshots")},
            "serializationIssues": []}


def put(archive, value, namespace=NS):
    return archive.import_bytes(json.dumps(value, ensure_ascii=False).encode(), namespace=namespace)


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "recognizer.db"
    with ReportArchive(path) as archive:
        yield path, archive


def test_all_history_low_red_and_both_player_sides_are_kept(store):
    path, archive = store
    put(archive, report("1", stamp=1794000000))
    value = report("2", name="较新", uid=201, stamp=1794400000)
    value["data"]["battleSettlement"]["attacker"] = army(222, "另一方")
    put(archive, value)
    put(archive, report("3", name="系统守军", uid=0))
    loader = EnemyDataLoader(db_path=path)
    assert set(loader.known_player_names) == {"对手", "较新", "另一方"}
    assert "本人" not in loader.known_player_names
    assert loader.stats["reports"] == 3
    assert "将4012" in loader.get_teams("对手")[0]
    assert "历史1次" in loader.get_teams("对手")[0]


def test_unscoped_legacy_is_not_mixed_into_only_identified_account(store):
    path, archive = store
    put(archive, report("1", name="当前"))
    put(archive, report("2", name="旧数据"), "default")
    loader = EnemyDataLoader(db_path=path)
    assert loader.namespace == NS and loader.known_player_names == ["当前"]


def test_multiple_accounts_need_explicit_namespace_and_never_merge(store):
    path, archive = store
    put(archive, report("1", name="当前"))
    put(archive, report("2", name="其他账号"), OTHER)
    with pytest.raises(ValueError, match="无法唯一确定账号"):
        EnemyDataLoader(db_path=path)
    assert EnemyDataLoader(db_path=path, namespace=NS).known_player_names == ["当前"]
    with pytest.raises(ValueError, match="不能使用 default"):
        EnemyDataLoader(db_path=path, namespace="default")


def test_duplicate_player_name_is_excluded_instead_of_merging_ids(store):
    path, archive = store
    put(archive, report("1", name="同名", uid=200))
    put(archive, report("2", name="同名", uid=201, heroes=(1, 2, 3)))
    loader = EnemyDataLoader(db_path=path)
    assert loader.ambiguous_names == ["同名"]
    assert loader.known_player_names == [] and loader.get_teams("同名") == []


def test_new_wal_commit_refreshes_without_restarting_and_account_stays_pinned(store):
    path, archive = store
    put(archive, report("1", name="先前"))
    loader = EnemyDataLoader(db_path=path)
    assert loader.reload() is False
    put(archive, report("2", name="新增"))
    put(archive, report("3", name="其他"), OTHER)
    assert loader.reload() is True
    assert loader.namespace == NS
    assert set(loader.known_player_names) == {"先前", "新增"}
    assert loader.reload() is False


def test_repeated_import_does_not_inflate_observation_count(store):
    path, archive = store
    value = report("1")
    put(archive, value)
    put(archive, value)
    put(archive, report("2"))
    loader = EnemyDataLoader(db_path=path)
    assert len(loader.get_teams("对手")) == 1
    assert "历史2次" in loader.get_teams("对手")[0]


def test_refresh_failure_keeps_successful_data_and_can_retry(store, monkeypatch):
    path, archive = store
    put(archive, report())
    loader = EnemyDataLoader(db_path=path)
    original = loader._sqlite
    previous = dict(loader.player_teams_map)
    put(archive, report("2", name="新增"))
    def fail():
        raise BattleDataError("busy")
    monkeypatch.setattr(loader, "_sqlite", fail)
    with pytest.raises(BattleDataError, match="busy"):
        loader.reload()
    assert loader.player_teams_map == previous
    monkeypatch.setattr(loader, "_sqlite", original)
    assert loader.reload() is True and "新增" in loader.known_player_names


def test_read_only_connection_rejects_mutations_and_missing_db_is_not_created(store, tmp_path):
    path, archive = store
    put(archive, report())
    loader = EnemyDataLoader(db_path=path)
    with pytest.raises(BattleDataError, match="readonly"):
        with loader.data._connection() as connection:
            connection.execute("DELETE FROM report_records")
    missing = tmp_path / "not-created" / "missing.db"
    with pytest.raises(BattleDataError):
        EnemyDataLoader(db_path=missing)
    assert not missing.parent.exists()


def test_explicit_json_mode_remains_compatible_and_db_errors_do_not_fall_back(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps([{"enemy_player": "旧敌人", "enemy_teams": "甲+乙+丙"}]), encoding="utf-8")
    loader = EnemyDataLoader(path, source="json")
    assert loader.get_teams("旧敌人") == ["甲+乙+丙"]
    with pytest.raises(BattleDataError):
        EnemyDataLoader(path, db_path=tmp_path / "missing.db")


def test_source_backed_semantic_hero_names_are_used(tmp_path):
    path = tmp_path / "named.db"
    mapper = SemanticMapper("test-recognizer", {},
        lambda kind, uid: {"mapped": True, "name_zh": f"武将名{uid}"}, lambda event, context: {})
    with ReportArchive(path, semantic_mapper=mapper) as archive:
        put(archive, report())
    loader = EnemyDataLoader(db_path=path)
    assert "武将名4012" in loader.get_teams("对手")[0]


def test_c_drive_database_is_rejected_before_connecting():
    with pytest.raises(ValueError, match="C 盘"):
        EnemyDataLoader(db_path=r"C:\not-created\battle_reports.db")
