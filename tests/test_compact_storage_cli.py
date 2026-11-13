"""Compact CLI defaults and round trip on real retained battle reports."""
from pathlib import Path
import json
from unittest.mock import patch
import pytest

import battle_reports as cli
from battle_ui.data import BattleData
from parser.report_archive import ReportArchive


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = sorted((ROOT / "capture/output/processed/battle_reports").glob("battle_*_decrypted.json"))


def test_collect_defaults_to_lean_import_can_request_previous_modes():
    parser = cli.build_parser()
    assert parser.parse_args(["collect"]).storage_mode == "lean"
    assert parser.parse_args(["collect", "--storage-mode", "compact"]).storage_mode == "compact"
    assert parser.parse_args(["collect", "--storage-mode", "full"]).storage_mode == "full"
    assert parser.parse_args(["import"]).storage_mode == "full"
    assert parser.parse_args(["import", "--storage-mode", "compact"]).storage_mode == "compact"


@pytest.mark.parametrize("mode", ["compact", "lean"])
def test_real_reports_compact_cli_import_verify_export_and_readonly_explain(tmp_path, capsys, mode):
    if not SAMPLES:
        pytest.skip("Private retained report samples are not distributed")
    source = next(path for path in SAMPLES if json.loads(path.read_bytes()).get("eventCount", 0) > 0)
    db = tmp_path / "compact.db"
    assert cli.main(["import", str(source), "--db", str(db), "--storage-mode", mode]) == 0
    capsys.readouterr()
    with ReportArchive(db, read_only=True) as archive:
        snapshot = archive.connection.execute("SELECT * FROM report_snapshots").fetchone()
        assert snapshot["storage_mode"] == mode
        assert snapshot["indexed_field_count"] < snapshot["field_count"]
        assert archive.connection.execute("SELECT count(*) FROM report_event_meanings").fetchone()[0] == 0
        assert archive.original_bytes(snapshot["id"]) == source.read_bytes()
        assert archive.verify()["ok"]
        before = archive.connection.total_changes
        explanation = cli.explain_report(archive, archive.list_reports()[0]["battle_uid"], limit=3)
        assert explanation["event_storage"] == "on_demand"
        assert len(explanation["events"]) == min(snapshot["event_count"], 3)
        assert archive.connection.total_changes == before
    exported = tmp_path / "original.json"
    assert cli.main(["export", str(snapshot["id"]), str(exported), "--db", str(db)]) == 0
    capsys.readouterr()
    assert exported.read_bytes() == source.read_bytes()
    page = BattleData(db, timeout_seconds=30).detail(snapshot["id"], event_limit=3)
    assert len(page["events"]) == min(snapshot["event_count"], 3)
    assert all(event["status"] != "not_generated" for event in page["events"])
    with ReportArchive(db, read_only=True) as archive:
        assert archive.connection.execute("SELECT count(*) FROM report_event_meanings").fetchone()[0] == 0


@pytest.mark.parametrize("mode", ["compact", "lean"])
def test_readonly_explain_compact_without_stored_semantics_can_recover_or_show_raw(tmp_path, mode):
    if not SAMPLES:
        pytest.skip("Private retained report samples are not distributed")
    source = next(path for path in SAMPLES if json.loads(path.read_bytes()).get("eventCount", 0) > 0)
    db = tmp_path / "no-semantics.db"
    with ReportArchive(db, storage_mode=mode) as archive:
        imported = archive.import_bytes(source.read_bytes())
        battle_id = imported["reports"][0]["battle_uid"]
    with ReportArchive(db, read_only=True) as archive:
        result = cli.explain_report(archive, battle_id, limit=2)
        assert result["event_storage"] == "on_demand"
        assert result["event_mapping_version"]
        assert len(result["events"]) == 2
        with patch("parser.semantic_catalog.build_default_mapper", return_value=None):
            unavailable = cli.explain_report(archive, battle_id, limit=2)
        assert all(event["status"] == "not_generated" and event["raw"] and event["error"]
                   for event in unavailable["events"])
        assert archive.connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='report_semantic_runs'").fetchone()[0] == 0
