"""Collection status reads current scalar metadata, never large historical data."""
import copy
import json
import sqlite3

import pytest

from parser.report_archive import ReportArchive


def report(uid="1", events=2, *, complete=True, marker="first"):
    data = {"battleID": uid, "eventDataList": [{"eventId": index, "intList": [], "tab": 0} for index in range(events)]}
    return {"captureVersion": 2, "battleID": uid, "source": "all", "data": data,
            "eventCount": events, "captureComplete": complete, "serializationIssues": [], "marker": marker,
            "components": {key: copy.deepcopy(data) if key in ("all", "detail", "basic") else {}
                           for key in ("all", "detail", "basic", "statics", "snapshots")},
            "componentStatus": {key: {"available": True, "complete": complete or key != "snapshots"}
                                for key in ("all", "detail", "basic", "statics", "snapshots")}}


def put(archive, value, namespace="one"):
    result = archive.import_bytes(json.dumps(value).encode(), namespace=namespace)
    assert result["status"] == "imported"
    return result["snapshot_ids"][0]


def restrict_status_reads(connection, observed):
    allowed = {"report_records": {"", "id", "current_snapshot_id"},
               "report_snapshots": {"", "id", "report_id", "completeness", "event_count", "field_count", "indexed_field_count"},
               "report_files": {"", "id", "namespace", "sha256"}}
    def authorize(action, table, column, _database, _trigger):
        if action == sqlite3.SQLITE_READ:
            observed.append((table, column))
            if table not in allowed or column not in allowed[table]:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    connection.set_authorizer(authorize)


def test_empty_archive_has_known_zero_counts_without_historical_counters(tmp_path):
    with ReportArchive(tmp_path / "empty.db") as archive:
        result = archive.collection_stats()
    assert result == {"statistics_mode": "current_reports", "records": 0, "current_reports": 0,
        "current_events": 0, "current_fields": 0, "current_completeness": {}, "files": 0,
        "snapshots": 0, "missing_current_snapshots": 0, "collection_coverage": "unknown"}


def test_current_best_versions_and_namespaces_are_counted_once(tmp_path):
    with ReportArchive(tmp_path / "small.db") as archive:
        put(archive, report(complete=False, events=1))
        put(archive, report(complete=True, events=3, marker="complete"))
        put(archive, report(complete=False, events=8, marker="later partial"))
        put(archive, report(uid="2", events=0, complete=False))
        put(archive, report(uid="1", events=4), namespace="another")
        full = archive.stats()  # Only this tiny isolated test database.
        current = archive.collection_stats()
        assert current["records"] == 3 and current["snapshots"] == current["files"] == 5
        assert current["current_events"] == 7
        assert current["current_fields"] == full["current_fields"]
        assert current["current_completeness"] == full["current_completeness"] == {"complete": 2, "partial": 1}
        assert current["current_events"] != full["events"]
        assert not {"events", "fields", "sides", "heroes", "skills", "imports", "failed_files"} & current.keys()


def test_sql_authorizer_forbids_all_large_tables_and_payload_columns(tmp_path):
    with ReportArchive(tmp_path / "guarded.db") as archive:
        put(archive, report())
        before = archive.connection.total_changes
        observed = []
        restrict_status_reads(archive.connection, observed)
        result = archive.collection_stats()
        assert result["current_events"] == 2 and result["records"] == 1
        assert archive.connection.total_changes == before
        assert not archive.connection.in_transaction
        assert {table for table, _ in observed} <= {"report_records", "report_snapshots", "report_files"}
        for query in ("SELECT raw_bytes FROM report_files", "SELECT payload_json FROM report_snapshots",
                      "SELECT COUNT(*) FROM report_fields", "SELECT COUNT(*) FROM report_events"):
            with pytest.raises(sqlite3.DatabaseError):
                archive.connection.execute(query).fetchone()
        archive.connection.set_authorizer(None)


def test_compact_status_counts_actual_index_rows_without_large_table_reads(tmp_path):
    with ReportArchive(tmp_path / "compact_status.db", storage_mode="compact") as archive:
        put(archive, report(events=20))
        stored = archive.connection.execute(
            "SELECT field_count,indexed_field_count FROM report_snapshots").fetchone()
        expected = archive.stats()["current_fields"]
        assert expected == stored["indexed_field_count"] < stored["field_count"]
        observed = []
        restrict_status_reads(archive.connection, observed)
        current = archive.collection_stats()
        assert current["current_fields"] == expected
        assert current["current_events"] == 20
        assert {table for table, _ in observed} <= {"report_records", "report_snapshots", "report_files"}
        archive.connection.set_authorizer(None)


def test_read_only_method_preserves_database_bytes_and_needs_no_counter_migration(tmp_path):
    path = tmp_path / "readonly.db"
    with ReportArchive(path) as writer:
        put(writer, report())
    before = path.read_bytes()
    with ReportArchive(path, read_only=True) as archive:
        statements = []
        archive.connection.set_trace_callback(statements.append)
        result = archive.collection_stats()
        assert result["records"] == 1
        assert not any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"))
                       for statement in statements)
    assert path.read_bytes() == before


def test_missing_or_foreign_current_pointer_is_reported_not_fabricated(tmp_path):
    with ReportArchive(tmp_path / "broken_pointer.db") as archive:
        sid = put(archive, report())
        with archive.connection:
            archive.connection.execute("INSERT INTO report_records(namespace,battle_uid,current_snapshot_id,first_seen,last_seen) VALUES(?,?,?,?,?)",
                                       ("one", "missing", None, "now", "now"))
            archive.connection.execute("INSERT INTO report_records(namespace,battle_uid,current_snapshot_id,first_seen,last_seen) VALUES(?,?,?,?,?)",
                                       ("one", "foreign", sid, "now", "now"))
        result = archive.collection_stats()
        assert result["records"] == 3 and result["current_reports"] == 1
        assert result["missing_current_snapshots"] == 2
        assert result["current_completeness"] == {"complete": 1}


def test_multiple_counters_share_one_wal_read_snapshot(tmp_path):
    path = tmp_path / "consistent.db"
    with ReportArchive(path) as reader, ReportArchive(path) as writer:
        put(reader, report())
        inserted = False
        def trace(statement):
            nonlocal inserted
            if statement == "SELECT COUNT(*) FROM report_files" and not inserted:
                inserted = True
                put(writer, report("2", events=4))
        reader.connection.set_trace_callback(trace)
        result = reader.collection_stats()
        reader.connection.set_trace_callback(None)
        assert inserted
        assert result["records"] == result["files"] == result["snapshots"] == 1
        assert result["current_events"] == 2
        assert reader.collection_stats()["records"] == 2
