"""One real Lua sidecar flows through durable discovery into the new view."""
from pathlib import Path
import runpy

from battle_ui.peerless import PeerlessRecords
from parser.report_archive import ReportArchive


def test_client_verdict_to_immutable_archive_to_occurrence_view(tmp_path):
    helpers = runpy.run_path(str(Path(__file__).with_name("test_peerless_capture.py")))
    runner = helpers["Runner"]()
    value = helpers["row"]()
    value["combatInfo"]["battleSettlement"] = {
        "attacker": {"army_id": 1, "avatar_data": {"avatar_id": 100, "avatar_name": "本人"}},
        "defensive": {"army_id": 2, "avatar_data": {"avatar_id": 200, "avatar_name": "这次无双的人",
            "avatar_union_id": 30, "avatar_union_name": "该人的同盟"}},
    }
    collector, (rows, _, response, original_page) = helpers["page"](tmp_path / "runtime", runner, [value])
    assert runner.calls == 2 and runner.lua.eval("requestCalls") == 1
    db = tmp_path / "reports.db"
    with ReportArchive(db) as archive:
        collector.on_export = lambda path: archive.import_file(path, namespace="account")
        collector.remember("personal", rows[0], response, original_page)
        collector.remember("personal", rows[0], response, original_page)
        result = PeerlessRecords(db).summary(namespace="account")
        assert result["counts"] == {"occurrences": 1, "players": 1, "reports": 1}
        row = result["records"][0]
        assert row["battle_uid"] == "123" and row["player_uid"] == "200"
        assert row["player_name"] == "这次无双的人" and row["union_name"] == "该人的同盟"
        assert row["side"] == "defensive" and row["completeness"] == "partial"
        assert row["merit"] == 33 and row["win_battle_num"] == 2  # Loaded client, not historical 20000/5.
        assert archive.stats()["records"] == 1
        assert archive.connection.execute("SELECT count(*) FROM report_snapshots").fetchone()[0] == 1
        assert archive.verify()["ok"]
        assert runner.calls == 2  # Reading the occurrence never connects to the game.
