import json
import pytest

from capture.game_assets.export_battle_report_runtime import atomic_json
from capture.game_assets.battle_collector import BattleCollector


def test_compact_checkpoint_preserves_every_json_value(tmp_path):
    value = {"中文": [None, False, 0, [], {}, 9007199254740991],
             "nested": {"line": "一\n二", "parameters": [1, -2, 3.125]}}
    pretty, compact = tmp_path / "pretty.json", tmp_path / "compact.json"
    atomic_json(pretty, value)
    atomic_json(compact, value, compact=True)
    assert json.loads(pretty.read_bytes()) == json.loads(compact.read_bytes()) == value
    assert compact.stat().st_size < pretty.stat().st_size


def test_invalid_compact_checkpoint_preserves_previous_complete_target(tmp_path):
    target = tmp_path / "state.json"
    atomic_json(target, {"saved": True}, compact=True)
    original = target.read_bytes()
    with pytest.raises(ValueError):
        atomic_json(target, {"bad": float("nan")}, compact=True)
    assert target.read_bytes() == original


def test_initial_batch_keeps_new_reports_first_and_retains_old_partials(tmp_path):
    collector = BattleCollector(None, tmp_path)
    collector.state["reports"] = {
        "1": {"battleID": "1", "complete": False, "attempts": 0},
        "2": {"battleID": "2", "complete": False, "attempts": 1,
              "lastResult": {"componentStatus": {name: {"complete": name != "snapshots"}
                  for name in ("basic", "detail", "all", "statics", "snapshots")}}},
    }
    # New reports get a first attempt before the bounded retry backlog; the
    # existing partial report remains queued for the following work slot.
    assert collector._initial_pending_ids(1) == ["1"]
    assert collector._initial_pending_ids(2) == ["1", "2"]
