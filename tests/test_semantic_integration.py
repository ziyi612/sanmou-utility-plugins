import hashlib
import json

import battle_reports
from parser.report_archive import ReportArchive
from parser.report_semantics import SemanticMapper


def mapper(decoder=None):
    def lookup(kind, value):
        return {"name_zh": "大乔" if (kind, value) == ("hero", 3004) else None,
                "mapped": (kind, value) == ("hero", 3004), "source": [{"test": True}]}
    return SemanticMapper("test-v1", {"test": True}, lookup,
                          decoder or (lambda event, context: {
                              "event_name_zh": "回合开始", "summary_zh": "第1回合",
                              "parameters": [{"raw_value": 1, "label_zh": "回合"}],
                              "status": "mapped", "source": {"test": True}}))


def raw():
    return json.dumps({"battleID": 123, "data": {"battleID": 123,
        "battleSettlement": {"attacker": {"army_id": 42, "army_member": [{"heros_id": 3004}]}},
        "eventDataList": [{"eventId": 1, "intList": [1], "tab": 2}]}}, ensure_ascii=False).encode()


def test_automatic_meanings_leave_originals_and_projection_verification_intact(tmp_path):
    source = raw()
    with ReportArchive(tmp_path / "archive.db", semantic_mapper=mapper()) as archive:
        result = archive.import_bytes(source)
        assert result["status"] == "imported"
        assert result["semantics"]["failed"] == 0
        assert archive.verify()["ok"]
        stored = archive.connection.execute("SELECT raw_bytes,sha256 FROM report_files").fetchone()
        assert stored[0] == source
        assert stored[1] == hashlib.sha256(source).hexdigest()
        explained = battle_reports.explain_report(archive, "123")
        assert explained["mapping_run"]["status"] == "ready"
        assert explained["ids"][0]["name_zh"] == "大乔"
        assert explained["events"][0]["parameters"][0]["raw_value"] == 1


def test_semantic_failure_cannot_reject_committed_original(tmp_path):
    def broken(event, context):
        raise RuntimeError("dictionary not available")
    with ReportArchive(tmp_path / "archive.db", semantic_mapper=mapper(broken)) as archive:
        result = archive.import_bytes(raw())
        assert result["status"] == "imported"
        assert result["semantics"]["failed"] == 1
        assert archive.stats()["records"] == 1
        assert archive.verify()["ok"]


def test_duplicate_can_backfill_meanings_without_duplicate_raw_snapshot(tmp_path):
    database = tmp_path / "archive.db"
    with ReportArchive(database) as archive:
        archive.import_bytes(raw())
    with ReportArchive(database, semantic_mapper=mapper()) as archive:
        result = archive.import_bytes(raw())
        assert result["status"] == "duplicate"
        assert result["semantics"]["failed"] == 0
        assert archive.stats()["snapshots"] == 1


def test_cli_enrich_and_readonly_explain_preserve_database(tmp_path, monkeypatch, capsys):
    database = tmp_path / "archive.db"
    with ReportArchive(database) as archive:
        archive.import_bytes(raw())
    monkeypatch.setattr("parser.semantic_catalog.build_default_mapper", lambda: mapper())
    assert battle_reports.main(["enrich", "--db", str(database)]) == 0
    capsys.readouterr()
    assert battle_reports.main(["explain", "123", "--db", str(database), "--limit", "1"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["events"][0]["event_name_zh"] == "回合开始"
    assert battle_reports.main(["explain", "123", "--db", str(database), "--out", str(database)]) == 2
    with ReportArchive(database, read_only=True) as archive:
        assert archive.verify()["ok"]


def test_mapping_setup_error_does_not_block_cli_original_import(tmp_path, monkeypatch, capsys):
    source = tmp_path / "battle.json"
    source.write_bytes(raw())
    def broken():
        raise ValueError("bad dictionary")
    monkeypatch.setattr("parser.semantic_catalog.build_default_mapper", broken)
    database = tmp_path / "archive.db"
    assert battle_reports.main(["import", str(source), "--db", str(database)]) == 0
    with ReportArchive(database, read_only=True) as archive:
        assert archive.stats()["records"] == 1
        assert archive.verify()["ok"]
