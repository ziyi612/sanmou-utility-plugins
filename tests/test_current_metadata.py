"""Indexed metadata reconstruction on memory-only archives; no game/file I/O."""
import json
import sqlite3
import time

import pytest

from battle_ui.current_metadata import read_current_metadata, read_indexed_subtree
from battle_ui.data import BattleQueryTimeout
from parser.report_archive import iter_fields


@pytest.fixture
def connection():
    value = sqlite3.connect(":memory:")
    value.row_factory = sqlite3.Row
    value.executescript("""
    PRAGMA temp_store=MEMORY;
    CREATE TABLE report_records(id INTEGER PRIMARY KEY,namespace TEXT,battle_uid TEXT,current_snapshot_id INTEGER,
      UNIQUE(namespace,battle_uid));
    CREATE TABLE report_snapshots(id INTEGER PRIMARY KEY,report_id INTEGER,completeness TEXT,source TEXT,
      event_count INTEGER,capture_version INTEGER,payload_json TEXT);
    CREATE TABLE report_fields(snapshot_id INTEGER,pointer TEXT,parent_pointer TEXT,field_key TEXT,
      ordinal INTEGER,node_type TEXT,value_json TEXT,scalar_text TEXT,PRIMARY KEY(snapshot_id,pointer));
    """)
    yield value
    value.close()


def add(connection, document, snapshot_id=1, namespace="one", battle="1", *, current=True):
    row = connection.execute("SELECT id FROM report_records WHERE namespace=? AND battle_uid=?", (namespace, battle)).fetchone()
    if row is None:
        report_id = connection.execute("INSERT INTO report_records(namespace,battle_uid) VALUES(?,?)", (namespace, battle)).lastrowid
    else:
        report_id = row[0]
    connection.execute("INSERT INTO report_snapshots VALUES(?,?,?,?,?,?,?)",
                       (snapshot_id, report_id, "complete", "all", 1, 2, json.dumps(document)))
    connection.executemany("INSERT INTO report_fields VALUES(?,?,?,?,?,?,?,?)",
                           ((snapshot_id, *node) for node in iter_fields(document)))
    if current:
        connection.execute("UPDATE report_records SET current_snapshot_id=? WHERE id=?", (snapshot_id, report_id))
    connection.commit()


def test_current_scalar_nodes_keep_zero_null_missing_types_and_namespace(connection):
    add(connection, {"data": {"timeStamp": 11, "zero": 0, "null": None, "flag": False}})
    add(connection, {"data": {"timeStamp": 22, "zero": 3}}, snapshot_id=2)
    add(connection, {"data": {"timeStamp": 99}}, snapshot_id=3, namespace="other")
    before = connection.total_changes
    nodes = read_current_metadata(connection, ("/data/timeStamp", "/data/zero", "/data/null", "/data/missing"), "one")
    assert len(nodes) == 1 and nodes[0]["snapshot_id"] == 2
    assert nodes[0]["metadata"] == {"/data/timeStamp": 22, "/data/zero": 3}
    assert connection.total_changes == before
    first = read_indexed_subtree(connection, 1, "/data")[0]
    assert first == {"timeStamp": 11, "zero": 0, "null": None, "flag": False}


def test_subtree_reconstructs_array_object_empty_and_escaped_keys(connection):
    branch = {"a/b~c": [None, 0, False, "", {}, [], {"known": 1}], "10": "text", "2": {"nested": True}}
    add(connection, {"data": {"small": branch, "small.sibling": "not a descendant"}})
    value, truncated = read_indexed_subtree(connection, 1, "/data/small")
    assert not truncated and value == branch
    assert list(value) == list(branch)
    assert read_indexed_subtree(connection, 1, "/data/small/a~1b~0c")[0] == branch["a/b~c"]


def test_truncated_array_never_renumbers_lexicographic_indices(connection):
    add(connection, {"items": list(range(25))})
    value, truncated = read_indexed_subtree(connection, 1, "/items", max_nodes=6)
    assert truncated and value is None
    assert read_indexed_subtree(connection, 1, "/items", max_nodes=26) == (list(range(25)), False)


def observations(count):
    return [{"scope": "personal", "parent": str(index), "row": {"childNum": index,
             "combatInfo": {"childNum": index + 1}}} for index in range(count)]


def test_discovery_array_uses_original_index_order_and_allows_uncapped_consumer(connection):
    add(connection, {"discovery": observations(140)})
    daily = read_current_metadata(connection, (), include_discovery=True)[0]
    assert [item["parent"] for item in daily["discovery"]] == [str(index) for index in range(129)]
    assert daily["discovery_truncated"]
    full = read_current_metadata(connection, (), include_discovery=True, discovery_limit=None)[0]
    assert len(full["discovery"]) == 140 and not full["discovery_truncated"]
    assert full["discovery"][12] == {"pointer": "/discovery/12", "index": "12", "scope": "personal",
        "parent": "12", "children": 12, "combat_children": 13}


def test_discovery_dictionary_keeps_json_each_ordinal_and_ignores_nonobjects(connection):
    source = {"z": observations(1)[0], "0.foo": None, "0": observations(2)[1],
              "00": observations(3)[2], "a/b~": observations(4)[3], "10": False, "2": observations(5)[4]}
    add(connection, {"discovery": source})
    result = read_current_metadata(connection, (), include_discovery=True)[0]
    assert [item["parent"] for item in result["discovery"]] == ["0", "1", "2", "3", "4"]
    assert result["discovery"][3]["pointer"] == "/discovery/a~1b~0"


def test_discovery_skips_large_entry_body_instead_of_scanning_it(connection):
    observation = observations(1)[0]
    observation["row"]["combatInfo"]["eventDataList"] = list(range(5000))
    add(connection, {"discovery": {"0": observation, "1": observations(2)[1]}})
    instructions = 0
    def progress():
        nonlocal instructions
        instructions += 1
        return 0
    connection.set_progress_handler(progress, 1)
    try:
        nodes = read_current_metadata(connection, (), include_discovery=True)
    finally:
        connection.set_progress_handler(None, 0)
    assert len(nodes[0]["discovery"]) == 2
    assert instructions < 1000, "Discovery lookup traversed the large combat body"


def test_metadata_never_reads_payload_raw_events_and_plans_use_pointer_primary_key(connection):
    add(connection, {"data": {"timeStamp": 12}, "discovery": observations(2)})
    def authorize(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and ((table == "report_snapshots" and column == "payload_json")
                or table in ("report_events", "report_files")):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    connection.set_authorizer(authorize)
    assert read_current_metadata(connection, ("/data/timeStamp",), include_discovery=True)[0]["metadata"] == {"/data/timeStamp": 12}
    assert read_indexed_subtree(connection, 1, "/data") == ({"timeStamp": 12}, False)
    plan = [row[3] for row in connection.execute(
        "EXPLAIN QUERY PLAN SELECT f.value_json FROM report_records r CROSS JOIN report_fields f "
        "INDEXED BY sqlite_autoindex_report_fields_1 ON f.snapshot_id=r.current_snapshot_id "
        "AND f.pointer IN (?) WHERE r.namespace=?", ("/data/timeStamp", "one"))]
    assert any("SEARCH f USING" in detail and "snapshot_id=? AND pointer=?" in detail for detail in plan)
    range_plan = [row[3] for row in connection.execute(
        "EXPLAIN QUERY PLAN SELECT pointer FROM report_fields INDEXED BY sqlite_autoindex_report_fields_1 "
        "WHERE snapshot_id=? AND pointer>=? AND pointer<? ORDER BY pointer LIMIT ?", (1, "/data/", "/data0", 512))]
    assert any("snapshot_id=? AND pointer>? AND pointer<?" in detail for detail in range_plan)


def test_metadata_and_subtree_respect_shared_deadline(connection):
    add(connection, {"data": {"timeStamp": 12}})
    for operation in (lambda: read_current_metadata(connection, (), deadline=time.monotonic() - 1),
                      lambda: read_indexed_subtree(connection, 1, "/data", deadline=time.monotonic() - 1)):
        with pytest.raises(BattleQueryTimeout):
            operation()


def test_common_single_observation_uses_one_discovery_query_per_report(connection):
    for index in range(20):
        add(connection, {"data": {"timeStamp": 12}, "discovery": observations(1)},
            snapshot_id=index + 1, battle=str(index))
    statements = []
    connection.set_trace_callback(statements.append)
    nodes = read_current_metadata(connection, ("/data/timeStamp",), include_discovery=True)
    connection.set_trace_callback(None)
    assert len(nodes) == 20 and all(len(node["discovery"]) == 1 for node in nodes)
    selects = [statement for statement in statements if statement.startswith("SELECT ")]
    # One current-node read, one batched scalar read, one six-pointer probe for
    # each single-observation report, including its end-of-list proof.
    assert len(selects) == 22
