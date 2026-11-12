"""Archive invariants, using real exported reports and deliberate failure cases."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from parser.report_archive import COMPONENTS, ReportArchive, _write_path


ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(".runtime/test-temp")


def sample(battle_id="900719925474099312345", modern=False, complete=False):
    data = {
        "battleID": battle_id,
        "eventDataList": [{"eventId": 0, "intList": [], "tab": 0},
                          {"eventId": 101, "intList": [None, True, 2**80], "tab": 1}],
        "battleSettlement": {
            "attacker": {"army_id": "99", "avatar_data": {"avatar_name": "张三😀", "avatar_id": "123"},
                         "army_member": [{"heros_id": 100, "index": 1, "hero_lv": 0,
                                          "origin_troop_num": 0, "skills": [{"skillId": 2, "level": 0}]}]},
            "defensive": {"army_member": []},
        },
        "unknown": {"a/b~c": [None, {}, [], "", False, 0, 1.25, 2**100]},
    }
    value = {"battleID": battle_id, "source": "all", "eventCount": 2, "data": data}
    if modern:
        value.update(captureVersion=2, components={"all": copy.deepcopy(data)},
                     componentStatus={"all": {"requested": True, "available": True, "complete": True}}, serializationIssues=[])
        if complete:
            for name in COMPONENTS:
                value["components"].setdefault(name, {})
                value["componentStatus"][name] = {"requested": True, "available": True, "complete": True}
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="report_archive_test_", dir=TEMP)
        self.archive = ReportArchive(Path(self.temp.name) / "battle_reports.db")

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def test_roundtrip_every_type_unknown_fields_and_nulls(self):
        original = b"\xef\xbb\xbf" + encode(sample()) + b"\r\n"
        result = self.archive.import_bytes(original)
        self.assertEqual(result["status"], "imported")
        sid = result["snapshot_ids"][0]
        self.assertEqual(self.archive.original_bytes(sid), original)
        self.assertEqual(self.archive.get_payload(sid), sample())
        row = self.archive.connection.execute("SELECT node_type,scalar_text FROM report_fields WHERE pointer='/data/unknown/a~1b~0c/7'").fetchone()
        self.assertEqual(tuple(row), ("integer", str(2**100)))
        hero = self.archive.connection.execute("SELECT level,troops_initial,troops_remaining FROM report_heroes").fetchone()
        self.assertEqual(tuple(hero), (0, 0, None))
        self.assertEqual(self.archive.list_reports()[0]["battle_uid"], "900719925474099312345")
        self.assertEqual(self.archive.list_reports()[0]["completeness"], "legacy")
        self.assertTrue(self.archive.verify()["ok"])

    @unittest.skipUnless(__import__("os").name == "nt", "Windows drive policy")
    def test_c_drive_extended_paths_rejected_without_writing(self):
        for path in ("C:/codex-no-write-check.db", "\\\\?\\C:\\codex-no-write-check.db", "\\\\.\\C:\\codex-no-write-check.db"):
            with self.assertRaises(ValueError):
                _write_path(path)

    def test_duplicate_and_cross_namespace(self):
        raw = encode(sample())
        self.archive.import_bytes(raw, source_path="first.json")
        duplicate = self.archive.import_bytes(raw, source_path="second.json")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(self.archive.stats()["snapshots"], 1)
        self.archive.import_bytes(raw, namespace="another-account")
        self.assertEqual(self.archive.stats()["records"], 2)
        self.assertEqual(self.archive.stats()["events"], 4)

    def test_upgrade_and_partial_does_not_downgrade_complete(self):
        first = self.archive.import_bytes(encode(sample(modern=True)))
        full = self.archive.import_bytes(encode(sample(modern=True, complete=True)))
        self.assertEqual(full["reports"][0]["completeness"], "complete")
        partial = sample(modern=True)
        partial["exportedAt"] = "later"
        partial["data"]["eventDataList"] = []
        partial["eventCount"] = 0
        last = self.archive.import_bytes(encode(partial))
        self.assertFalse(last["reports"][0]["selected_current"])
        self.assertEqual(self.archive.list_reports()[0]["current_snapshot_id"], full["snapshot_ids"][0])
        self.assertEqual(self.archive.stats()["snapshots"], 3)
        self.assertNotEqual(first["snapshot_ids"], full["snapshot_ids"])

    def test_more_components_cannot_displace_existing_event_history(self):
        first = self.archive.import_bytes(encode(sample()))
        value = sample(modern=True)
        value["data"]["eventDataList"] = []
        value["eventCount"] = 0
        value["components"]["statics"] = {"some_new_field": 10}
        result = self.archive.import_bytes(encode(value))
        self.assertFalse(result["reports"][0]["selected_current"])
        self.assertEqual(self.archive.list_reports()[0]["current_snapshot_id"], first["snapshot_ids"][0])

    def test_bad_input_preserved_and_does_not_rollback_prior_file(self):
        self.archive.import_bytes(encode(sample()))
        malformed = b'{"battleID": 123, bad}'
        result = self.archive.import_bytes(malformed)
        self.assertEqual(result["status"], "failed")
        stored = self.archive.connection.execute("SELECT raw_bytes FROM report_files WHERE id=?", (result["file_id"],)).fetchone()[0]
        self.assertEqual(stored, malformed)
        self.assertEqual(self.archive.stats()["records"], 1)
        self.assertEqual(self.archive.stats()["failed_files"], 1)
        self.assertTrue(self.archive.verify()["ok"])

    def test_batch_all_reports_and_batch_rollback(self):
        result = self.archive.import_bytes(encode([sample("1"), sample("2")]))
        self.assertEqual(len(result["snapshot_ids"]), 2)
        result = self.archive.import_bytes(encode([sample("3"), {"data": {}}]))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.archive.stats()["records"], 2)
        self.assertTrue(self.archive.verify()["ok"])

    def test_zero_events_not_filtered_and_mismatch_detected(self):
        data = sample()
        data["data"]["eventDataList"] = []
        data["eventCount"] = 0
        result = self.archive.import_bytes(encode(data))
        self.assertEqual(result["reports"][0]["event_count"], 0)
        self.assertEqual(self.archive.stats()["records"], 1)
        bad = sample("2", modern=True, complete=True)
        bad["eventCount"] = 999
        result = self.archive.import_bytes(encode(bad))
        self.assertEqual(result["reports"][0]["completeness"], "partial")

    def test_missing_components_and_serialization_markers_not_complete(self):
        value = sample(modern=True, complete=True)
        value["components"]["snapshots"] = None
        self.assertEqual(self.archive.import_bytes(encode(value))["reports"][0]["completeness"], "partial")

    def test_explicit_capture_incomplete_and_warnings_override_component_flags(self):
        value = sample("zero", modern=True, complete=True)
        value["data"]["eventDataList"] = []
        value["components"]["all"]["eventDataList"] = []
        value["eventCount"] = 0
        value["captureComplete"] = False
        result = self.archive.import_bytes(encode(value))
        self.assertEqual(result["reports"][0]["completeness"], "partial")
        value = sample("warned", modern=True, complete=True)
        value["captureComplete"] = True
        value["captureWarnings"] = ["unverified_component"]
        result = self.archive.import_bytes(encode(value))
        self.assertEqual(result["reports"][0]["completeness"], "partial")
        self.assertTrue(self.archive.verify()["ok"])
        value = sample("2", modern=True, complete=True)
        value["data"]["unknown"] = "<max_depth>"
        self.assertEqual(self.archive.import_bytes(encode(value))["reports"][0]["completeness"], "partial")

    def test_conflicting_ids_duplicate_keys_and_missing_file(self):
        value = sample()
        value["data"]["battleID"] = "different"
        self.assertEqual(self.archive.import_bytes(encode(value))["status"], "failed")
        self.assertEqual(self.archive.import_bytes(b'{"battleID":1,"battleID":2,"data":{}}')["status"], "failed")
        self.assertEqual(self.archive.import_file(Path(self.temp.name) / "missing.json")["status"], "failed")

    def test_verification_detects_corrupted_projection(self):
        self.archive.import_bytes(encode(sample()))
        with self.archive.connection:
            self.archive.connection.execute("DELETE FROM report_fields WHERE pointer='/data/unknown/a~1b~0c/0'")
        self.assertFalse(self.archive.verify()["ok"])

    def test_concurrent_same_file_is_one_import_and_one_duplicate(self):
        from parser import report_archive as module
        raw = encode(sample())
        barrier = threading.Barrier(2)
        decode = module._decode
        def simultaneous_decode(value):
            if value == raw:
                barrier.wait(timeout=10)
            return decode(value)
        def import_concurrently():
            with ReportArchive(self.archive.db_path) as archive:
                return archive.import_bytes(raw)
        with patch.object(module, "_decode", side_effect=simultaneous_decode):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(import_concurrently) for _ in range(2)]
                results = [future.result(timeout=20) for future in futures]
        self.assertEqual(sorted(r["status"] for r in results), ["duplicate", "imported"])
        self.assertEqual(self.archive.stats()["records"], 1)
        self.assertEqual(self.archive.stats()["failed_files"], 0)
        self.assertEqual(self.archive.import_bytes(raw)["status"], "duplicate")
        self.assertTrue(self.archive.verify()["ok"])

    def test_verify_keeps_one_read_snapshot_during_concurrent_import(self):
        from parser import report_archive as module
        self.archive.import_bytes(encode(sample("first")))
        ready, start = threading.Event(), threading.Event()
        main_thread = threading.get_ident()
        def add_second():
            with ReportArchive(self.archive.db_path) as archive:
                ready.set()
                self.assertTrue(start.wait(10))
                return archive.import_bytes(encode(sample("second")))
        original_fields = module.iter_fields
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(add_second)
            self.assertTrue(ready.wait(10))
            def interleaved_fields(*args, **kwargs):
                if threading.get_ident() == main_thread and not start.is_set():
                    start.set()
                    self.assertEqual(future.result(timeout=15)["status"], "imported")
                return original_fields(*args, **kwargs)
            with patch.object(module, "iter_fields", side_effect=interleaved_fields):
                result = self.archive.verify()
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["files_checked"], result["snapshots_checked"]), (1, 1))
        self.assertEqual(self.archive.stats()["records"], 2)

    def test_export_cannot_overwrite_database_sidecars_or_hardlink(self):
        result = self.archive.import_bytes(encode(sample()))
        sid = result["snapshot_ids"][0]
        alias = Path(self.temp.name) / "database-alias.json"
        os.link(self.archive.db_path, alias)
        for destination in [self.archive.db_path, alias] + [Path(str(self.archive.db_path) + suffix) for suffix in ("-wal", "-shm", "-journal")]:
            with self.subTest(destination=destination):
                with self.assertRaises(ValueError):
                    self.archive.export_original(sid, destination)
        alias.unlink()
        self.assertTrue(self.archive.verify()["ok"])

    def test_export_refuses_different_data_and_is_idempotent(self):
        raw = encode(sample())
        result = self.archive.import_bytes(raw)
        sid = result["snapshot_ids"][0]
        target = Path(self.temp.name) / "keep.json"
        target.write_bytes(b"keep this user file")
        with self.assertRaises(FileExistsError):
            self.archive.export_original(sid, target)
        self.assertEqual(target.read_bytes(), b"keep this user file")
        target = Path(self.temp.name) / "new.json"
        self.archive.export_original(sid, target)
        self.archive.export_original(sid, target)
        self.assertEqual(target.read_bytes(), raw)
        self.assertFalse(list(target.parent.glob(".battle-export-*.tmp")))

    def test_verification_covers_structured_values_identity_and_metadata(self):
        self.archive.import_bytes(encode(sample()))
        for sql in ("UPDATE report_heroes SET troops_initial=999", "UPDATE report_sides SET player_uid='wrong'",
                    "UPDATE report_skills SET skill_uid='wrong'", "UPDATE report_records SET battle_uid='wrong'",
                    "UPDATE report_snapshots SET source='basic'"):
            with self.subTest(sql=sql):
                self.archive.connection.execute("SAVEPOINT corrupt")
                self.archive.connection.execute(sql)
                self.assertFalse(self.archive.verify()["ok"])
                self.archive.connection.execute("ROLLBACK TO corrupt")
                self.archive.connection.execute("RELEASE corrupt")
        self.assertTrue(self.archive.verify()["ok"])

    def test_verification_uses_type_exact_original_and_detects_lost_snapshot(self):
        value = sample()
        self.archive.import_bytes(encode(value))
        altered = copy.deepcopy(value)
        altered["data"]["unknown"]["a/b~c"][4] = 0  # False == 0 in Python, but different JSON types.
        with self.archive.connection:
            self.archive.connection.execute("UPDATE report_snapshots SET payload_json=?", (json.dumps(altered),))
            self.archive.connection.execute("UPDATE report_fields SET node_type='integer',value_json='0',scalar_text='0' WHERE pointer='/data/unknown/a~1b~0c/4'")
        self.assertFalse(self.archive.verify()["ok"])
        with self.archive.connection:
            self.archive.connection.execute("UPDATE report_records SET current_snapshot_id=NULL")
            for table in ("report_skills", "report_heroes", "report_sides", "report_components", "report_events", "report_fields", "report_snapshots", "report_records"):
                self.archive.connection.execute("DELETE FROM " + table)
        checked = self.archive.verify()
        self.assertFalse(checked["ok"])
        self.assertTrue(any(item["check"] == "file_snapshot_coverage" for item in checked["errors"]))

    def test_read_only_does_not_initialize_unrelated_database(self):
        unrelated = Path(self.temp.name) / "unrelated.db"
        connection = sqlite3.connect(unrelated)
        connection.execute("CREATE TABLE keep(value TEXT)")
        connection.commit()
        connection.close()
        before = unrelated.read_bytes()
        with self.assertRaisesRegex(ValueError, "Not an initialized"):
            ReportArchive(unrelated, read_only=True)
        self.assertEqual(unrelated.read_bytes(), before)
        self.archive.import_bytes(encode(sample()))
        with ReportArchive(self.archive.db_path, read_only=True) as reader:
            self.assertEqual(reader.stats()["records"], 1)
            self.assertTrue(reader.verify()["ok"])
            with self.assertRaises(PermissionError):
                reader.import_bytes(encode(sample("second")))
            with self.assertRaises(PermissionError):
                reader.import_file(Path(self.temp.name) / "missing")

    def test_future_schema_is_rejected_before_writes_and_connection_closed(self):
        future = Path(self.temp.name) / "future.db"
        with ReportArchive(future) as archive:
            archive.connection.execute("UPDATE report_meta SET value='999' WHERE key='schema_version'")
            archive.connection.commit()
        before = future.read_bytes()
        for read_only in (False, True):
            with self.assertRaisesRegex(ValueError, "Unsupported archive schema"):
                ReportArchive(future, read_only=read_only)
            self.assertEqual(future.read_bytes(), before)
        renamed = future.with_suffix(".renamed")
        future.rename(renamed)  # Windows catches an accidentally leaked handle.

    def test_real_exports_exact_coverage(self):
        paths = sorted((ROOT / "capture/output/processed/battle_reports").glob("battle_*_decrypted.json"))
        self.assertEqual(len(paths), 7)
        expected_events = 0
        for path in paths:
            expected_events += len(json.loads(path.read_text("utf-8"))["data"]["eventDataList"])
            result = self.archive.import_file(path)
            self.assertEqual(result["status"], "imported", result)
            self.assertEqual(self.archive.original_bytes(result["snapshot_ids"][0]), path.read_bytes())
        self.assertEqual(expected_events, 9658)
        self.assertEqual(self.archive.stats()["records"], 7)
        self.assertEqual(self.archive.stats()["current_events"], expected_events)
        self.assertEqual(self.archive.stats()["current_completeness"], {"legacy": 7})
        self.assertTrue(self.archive.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
