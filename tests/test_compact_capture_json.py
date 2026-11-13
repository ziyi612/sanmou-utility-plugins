"""New captures omit formatting space while retained checkpoints keep exact bytes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from capture.game_assets.battle_collector import BattleCollector
from capture.game_assets import export_battle_report_runtime as exporter


def test_discovery_uses_compact_json_without_losing_unicode_or_unknown_values(tmp_path):
    collector = BattleCollector(None, tmp_path, namespace="account")
    observation = {"scope": "personal", "parent": None,
                   "row": {"combatInfo": {"battleID": 123, "name": "玩家中文",
                                           "unknown": [None, False, 0, {"x": "line\nnext"}]}}}
    entry = {"battleID": "123", "observations": {"source": observation}}
    collector.archive_discovery(entry)
    raw = Path(entry["discoveryArchivePath"]).read_bytes()
    payload = json.loads(raw)
    assert payload["data"] == observation["row"]["combatInfo"]
    assert payload["discovery"] == [observation]
    assert raw == json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    assert len(raw) < len(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    assert Path(entry["discoveryArchivePath"]).stem == hashlib.sha256(raw).hexdigest()


def test_existing_pretty_checkpoint_is_copied_byte_for_byte_and_not_compacted(tmp_path):
    collector = BattleCollector(None, tmp_path / "runtime", namespace="account")
    original = b'\xef\xbb\xbf{\r\n  "battleID": 123,\r\n  "data": {"battleID": 123},\r\n  "components": {}\r\n}\r\n'
    checkpoint = tmp_path / "old-checkpoint.json"
    checkpoint.write_bytes(original)
    retained = collector._capture_checkpoint({"battleID": "123"}, checkpoint)
    assert checkpoint.read_bytes() == retained.read_bytes() == original
    assert retained.stem == hashlib.sha256(original).hexdigest()


@pytest.mark.parametrize("kind", ["cache_before_retry", "final"])
def test_new_cache_checkpoint_and_final_export_use_compact_json(tmp_path, kind):
    data = {"battleID": 123, "eventDataList": [{"eventId": 101, "intList": [0, 1], "tab": 0}],
            "unknown": {"中文": [None, False, 0]}}
    payload = {"battleID": 123, "captureVersion": 2, "source": "all", "eventCount": 1,
               "data": data, "components": {"basic": data, "all": data, "detail": data,
                   "statics": {}, "snapshots": {"isComplete": True, "battleSnapData": {}}}}
    envelope = json.dumps({"payload": payload})
    with patch.object(exporter, "run_checked", return_value=envelope):
        if kind == "cache_before_retry":
            path = Path(exporter._checkpoint_before_cache_retry(None, 123, tmp_path, False, None, None))
        else:
            result = exporter._capture_report_impl(None, 123, tmp_path, request_missing=False)
            path = Path(result["path"])
    raw = path.read_bytes()
    saved = json.loads(raw)
    assert saved["data"] == data and saved["components"] == payload["components"]
    assert raw == json.dumps(saved, ensure_ascii=False, separators=(",", ":"),
                            allow_nan=False).encode("utf-8")
    if kind == "cache_before_retry":
        assert path.stem == hashlib.sha256(raw).hexdigest()
