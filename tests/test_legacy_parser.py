"""Regression tests for old parser/writer entry points; no production paths."""
import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import pandas as pd

from parser.battle_parser import BattleParser
from parser.db_writer import BattleDBWriter
from parser.report_archive import ReportArchive


ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(".runtime/test-temp")


def legacy(uid, rounds=0, damage=0):
    return {"battleId": uid, "rounds": rounds,
            "attacker": {"playerId": "one", "heroes": [{
                "heroId": 1001, "name": "甲", "troops": 0,
                "damageDealt": damage, "survived": False,
                "skills": [{"name": "战法", "skill_type": "active", "casts": 0,
                            "total_damage": 655, "total_heal": 0, "targets_hit": 2}]}]},
            "defender": {"heroes": []}}


class LegacyTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="legacy_parser_test_", dir=TEMP)
        self.root = Path(self.temp.name)
        self.source = self.root / "raw"
        self.source.mkdir()
        self.db = self.root / "database.db"
        self.writer = None

    def tearDown(self):
        if self.writer:
            self.writer.engine.dispose()
        self.temp.cleanup()

    def write(self, name, value):
        path = self.source / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def make_writer(self):
        self.writer = BattleDBWriter(str(self.db))
        return self.writer

    def test_batch_zero_missing_large_values_and_repeated_parse(self):
        records = [legacy(str(i), 0, 10**9 if i == 12 else i) for i in range(13)]
        records[0].pop("rounds")
        records[1]["battleId"] = 9007199254740993
        self.write("battle_batch.json", {"response_body": {"data": records}})
        parser = BattleParser(str(self.source))
        first = parser.clean(parser.parse_all())
        second = parser.clean(parser.parse_all())
        self.assertEqual(len(first), 13)
        self.assertEqual(len(second), 13)
        self.assertEqual(first.iloc[1]["battle_uid"], "9007199254740993")
        self.assertEqual(first.iloc[12]["attacker_damage_total"], 10**9)
        self.assertIsNone(first.iloc[0]["total_rounds"])
        self.assertEqual(first.iloc[1]["total_rounds"], 0)
        self.assertFalse(parser.errors)
        writer = self.make_writer()
        self.assertEqual(writer.insert_battles(first), 13)
        self.assertEqual(writer.insert_battles(second), 0)
        self.assertEqual(writer.last_result["duplicates"], 13)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM battle_records").fetchone()[0], 13)

    def test_invalid_batch_item_does_not_hide_other_items(self):
        self.write("battle_batch.json", [legacy("first"), {"missing_id": True}, legacy("last")])
        parser = BattleParser(str(self.source))
        frame = parser.parse_all()
        self.assertEqual(list(frame["battle_uid"]), ["first", "last"])
        self.assertEqual(len(parser.errors), 1)
        writer = self.make_writer()
        self.assertEqual(writer.insert_battles(frame), 2)
        self.assertEqual(len(writer.last_result["errors"]), 1)

    def test_runtime_original_bytes_routed_even_without_legacy_rows(self):
        real = ROOT / "capture/output/processed/battle_reports/battle_10676989120025_decrypted.json"
        original = real.read_bytes()
        path = self.source / real.name
        path.write_bytes(original)
        parser = BattleParser(str(self.source))
        frame = parser.clean(parser.parse_all())
        self.assertTrue(frame.empty)
        self.assertEqual(len(frame.attrs["runtime_sources"]), 1)
        # Reading changed files in the writer would silently index the wrong capture.
        path.write_bytes(b"changed after parser read")
        writer = self.make_writer()
        self.assertEqual(writer.insert_battles(frame, namespace="test-account"), 1)
        self.assertEqual(writer.last_result["inserted_archived"], 1)
        self.assertEqual(writer.insert_battles(frame, namespace="test-account"), 0)
        with ReportArchive(self.db) as archive:
            self.assertEqual(archive.stats()["records"], 1)
            sid = archive.list_reports()[0]["current_snapshot_id"]
            self.assertEqual(archive.original_bytes(sid), original)
            self.assertTrue(archive.verify()["ok"])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM battle_records").fetchone()[0], 0)

    def test_malformed_runtime_bytes_are_archived_as_failed(self):
        path = self.source / "battle_bad_decrypted.json"
        original = b'{"battleID":123,bad}'
        path.write_bytes(original)
        frame = BattleParser(str(self.source)).parse_all()
        writer = self.make_writer()
        self.assertEqual(writer.insert_battles(frame), 0)
        self.assertEqual(len(writer.last_result["errors"]), 1)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT raw_bytes,parse_status FROM report_files").fetchone(), (original, "failed"))

    def test_ce_unverified_candidates_do_not_become_formal_reports(self):
        path = self.write("battle_ce_candidate.json", {
            "source_kind": "legacy_ce_unverified", "method": "CE_MEMORY_READ", "url": "ce://memory/123",
            "raw_source": {"candidate": True}, "response_body": {"data": {"battleReport": legacy("123")}}})
        original = path.read_bytes()
        parser = BattleParser(str(self.source))
        frame = parser.parse_all()
        self.assertTrue(frame.empty)
        self.assertEqual(len(parser.errors), 1)
        writer = self.make_writer()
        self.assertEqual(writer.insert_battles(frame), 0)
        self.assertEqual(path.read_bytes(), original)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM battle_records").fetchone()[0], 0)

    def test_late_skill_failure_rolls_back_one_battle_and_counts_only_commits(self):
        self.write("battle_batch.json", [legacy("first"), legacy("bad"), legacy("last")])
        frame = BattleParser(str(self.source)).parse_all()
        frame.iloc[1]["attacker_heroes"][0]["hero_id"] = 1002
        frame.iloc[1]["attacker_heroes"][0]["skills"][0]["casts"] = "invalid"
        writer = self.make_writer()
        self.assertEqual(writer.insert_battles(frame), 2)
        self.assertEqual(len(writer.last_result["errors"]), 1)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT battle_uid FROM battle_records ORDER BY id").fetchall(), [("first",), ("last",)])
            self.assertEqual(conn.execute("SELECT count(*) FROM battle_heroes").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM heroes WHERE hero_id=1002").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM skill_usages").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(writer.insert_battles(frame), 0)
        self.assertEqual(writer.last_result["duplicates"], 2)

    def test_missing_values_stay_null_and_snake_case_skill_numbers_survive(self):
        self.write("battle_one.json", legacy("one"))
        writer = self.make_writer()
        self.assertEqual(writer.insert_battles(BattleParser(str(self.source)).parse_all()), 1)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT timestamp,time_iso,defender_damage_total FROM battle_records").fetchone(), (None, None, None))
            self.assertEqual(conn.execute("SELECT level,star,troops,survived FROM battle_heroes").fetchone(), (None, None, 0, 0))
            self.assertEqual(conn.execute("SELECT skill_type,casts,total_damage,total_heal,targets_hit FROM skill_usages").fetchone(), ("active", 0, 655, 0, 2))
        self.assertEqual(len(writer.query_battles()), 1)

    def test_legacy_cli_save_db_archives_runtime_and_ignores_readable_derivatives(self):
        real = ROOT / "capture/output/processed/battle_reports/battle_10632008620025_decrypted.json"
        (self.source / real.name).write_bytes(real.read_bytes())
        self.write("battle_123_readable.json", {"battleID": 123, "sides": []})
        self.write("battle_reports_readable_summary.json", [{"battleID": 123}])
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TEMP=str(TEMP), TMP=str(TEMP), PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, "-B", "-m", "parser.battle_parser", "--input", str(self.source),
                                 "--output", str(self.root / "processed"), "--db", str(self.db), "--save-db"],
                                cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        with ReportArchive(self.db) as archive:
            self.assertEqual(archive.stats()["records"], 1)
            self.assertEqual(archive.stats()["current_events"], 0)
            self.assertTrue(archive.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
