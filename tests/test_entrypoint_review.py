"""Regression tests from independent review of the CLI's storage boundaries."""
from __future__ import annotations
import json
import tempfile
from pathlib import Path

import pytest
import battle_reports as cli
from parser.report_archive import ReportArchive

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "capture/output/processed/battle_reports/battle_10632008620025_decrypted.json"


@pytest.fixture
def work():
    base=Path(".runtime/test-temp").resolve()
    base.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sanmou-entry-review-",dir=base) as directory:
        target=Path(directory).resolve()
        assert target.parent==base
        yield target


def initialise(work):
    db=work/"reports.db"
    with ReportArchive(db) as archive:
        result=archive.import_file(SAMPLE,namespace="review-test")
        assert result["status"]=="imported"
    return db


@pytest.mark.parametrize("operation",["attach","vacuum_into","pragma_write"])
def test_query_cannot_create_other_databases_or_enable_write_settings(work,capsys,operation):
    db=initialise(work)
    target=work/"should-not-exist.db"
    quoted=target.as_posix().replace("'","''")
    sql={
        "attach":f"ATTACH DATABASE '{quoted}' AS extra",
        "vacuum_into":f"VACUUM INTO '{quoted}'",
        "pragma_write":"PRAGMA writable_schema=1",
    }[operation]
    code=cli.main(["query",sql,"--db",str(db)])
    result=json.loads(capsys.readouterr().out)
    assert code==2, result
    assert result.get("error")
    assert not target.exists()


@pytest.mark.parametrize("sql",[
    "SELECT count(*) FROM report_records",
    "PRAGMA table_info(report_records)",
    "PRAGMA integrity_check",
])
def test_query_still_allows_select_and_readonly_pragmas(work,capsys,sql):
    db=initialise(work)
    code=cli.main(["query",sql,"--db",str(db)])
    result=json.loads(capsys.readouterr().out)
    assert code==0,result
    assert result["rows"]


def test_recovery_continues_after_bad_file_and_retains_success_without_duplicates(work):
    incoming=work/"capture"
    incoming.mkdir()
    bad=incoming/"battle_000_decrypted.json"
    bad_bytes=b'{"battleID":'
    bad.write_bytes(bad_bytes)
    good=incoming/"battle_999_decrypted.json"
    good.write_bytes(SAMPLE.read_bytes())
    with ReportArchive(work/"reports.db") as archive:
        # The lexically first bad file must not prevent processing the good one.
        cli.recover_captures(archive,incoming,"review-test")
        assert archive.stats()["records"]==1
        assert archive.stats()["snapshots"]==1
        assert archive.stats()["failed_files"]==1
        stored=archive.connection.execute(
            "SELECT raw_bytes FROM report_files WHERE parse_status='failed'"
        ).fetchone()[0]
        assert bytes(stored)==bad_bytes
        cli.recover_captures(archive,incoming,"review-test")
        assert archive.stats()["records"]==1
        assert archive.stats()["snapshots"]==1
