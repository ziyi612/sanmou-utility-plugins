import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pytest

CEBridge = pytest.importorskip(
    "capture.cheatengine.ce_bridge",
    reason="The optional CE bridge is outside this public battle-software distribution",
).CEBridge


class BridgeRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=".runtime/test-temp", prefix="ce_bridge_retention_")
        self.root = Path(self.temp.name)
        self.source = self.root / "ce_battle_1.json"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, value):
        raw = json.dumps({"battle_uid": "sample", "unknown": value}, ensure_ascii=False).encode()
        self.source.write_bytes(raw)
        return raw

    def test_same_filename_changes_preserve_both_raw_exports(self):
        bridge = CEBridge(self.root)
        first = self.write({"未来字段": [None, 0, False]})
        self.assertEqual(bridge.process_all(), 1)
        second = self.write({"未来字段": [1]})
        self.assertEqual(bridge.process_all(), 1)
        files = list(bridge.processed_dir.glob("ce_battle_1_*.json"))
        self.assertEqual({p.read_bytes() for p in files}, {first, second})
        self.assertEqual(len(list(self.root.glob("battle_ce_*.json"))), 2)
        self.assertEqual(self.source.read_bytes(), second)
        self.assertEqual(CEBridge(self.root).process_all(), 0)

    def test_callback_failure_retries_after_restart(self):
        raw = self.write("keep")
        bridge = CEBridge(self.root)
        def fail(value):
            raise RuntimeError("database unavailable")
        self.assertEqual(bridge.process_all(callback=fail), 0)
        self.assertTrue(bridge.errors)
        self.assertEqual(self.source.read_bytes(), raw)
        committed = []
        self.assertEqual(CEBridge(self.root).process_all(callback=committed.append), 1)
        self.assertEqual(committed[0]["raw_source"]["unknown"], "keep")

    def test_malformed_bytes_remain_available(self):
        raw = b'{"unfinished":'
        self.source.write_bytes(raw)
        bridge = CEBridge(self.root)
        self.assertEqual(bridge.process_all(), 0)
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertEqual(next(bridge.processed_dir.glob("ce_battle_1_*.json")).read_bytes(), raw)
        self.assertEqual(len(bridge.find_ce_files()), 1)

    def test_unverified_ce_cli_cannot_write_formal_database(self):
        from capture.cheatengine.ce_bridge import main
        with patch("sys.argv", ["ce_bridge", "--save-db", "--watch-dir", str(self.root)]):
            with self.assertRaises(SystemExit) as stopped:
                main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertFalse((self.root / "_processed").exists())
