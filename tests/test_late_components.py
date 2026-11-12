import copy
import json
from pathlib import Path
from unittest.mock import patch

from capture.game_assets import export_battle_report_runtime as exporter


def complete_components(event_count=1):
    battle = {"battleID": 123, "eventDataList": [{"eventId": 1, "intList": [i], "tab": 0} for i in range(event_count)]}
    return {
        "basic": {"battleID": 123}, "all": copy.deepcopy(battle), "detail": copy.deepcopy(battle),
        "statics": {"damage": 9},
        "snapshots": {"isComplete": True, "battleSnapData": {"maxIDX": 1}, "pages": [{"idx": 1}]},
    }


def component_status(components):
    result = {}
    for name in exporter.COMPONENTS:
        value = components.get(name)
        entry = {"available": value is not None, "complete": value is not None}
        if name in ("all", "detail"):
            events = value.get("eventDataList") if isinstance(value, dict) else None
            entry.update(eventCount=len(events) if isinstance(events, list) else -1, complete=isinstance(events, list))
        elif name == "snapshots":
            entry["complete"] = isinstance(value, dict) and value.get("isComplete") is True and value.get("battleSnapData") is not None
        result[name] = entry
    return result


def run_capture(tmp_path, initial_components, final_components, waiting, issues=None, request_error=None):
    final_data = final_components.get("all") or final_components.get("detail") or final_components["basic"]
    payload = {
        "captureVersion": 2, "battleID": 123, "source": "all", "data": final_data,
        "eventCount": len(final_data.get("eventDataList", [])), "components": final_components,
    }
    envelope = {"payload": payload, "serializationIssues": issues or [], "luaTableKeyTypes": {}}
    def checked(runner, code, **kwargs):
        if "captureVersion=2" in code:
            return json.dumps(envelope)
        if request_error:
            raise RuntimeError(request_error)
        return "requested component 123"
    with patch.object(exporter, "read_status", return_value=component_status(initial_components)), \
         patch.object(exporter, "run_checked", side_effect=checked), \
         patch.object(exporter, "wait_for", side_effect=waiting):
        result = exporter.capture_report(object(), 123, tmp_path)
    saved = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    return result, saved


def test_late_all_is_complete_when_actual_export_contains_every_component(tmp_path):
    final = complete_components()
    initial = {**final, "all": None, "detail": None}
    # The all wait timed out; detail's later wait observed both arriving.
    late_status = component_status({**final, "all": None, "detail": None})
    result, saved = run_capture(tmp_path, initial, final,
                                [(False, late_status), (True, component_status(final))])
    assert result["complete"] is True
    assert saved["captureComplete"] is True
    assert "error" not in saved["componentStatus"]["all"]
    assert saved["componentStatus"]["all"]["resolvedAfterTimeout"] is True
    assert saved["requestHistory"] == [{"component": "all", "requested": True, "error": "timeout",
                                         "resolved": True, "resolution": "component_complete_in_export"}]


def test_late_snapshot_is_complete_and_original_timeout_is_retained(tmp_path):
    final = complete_components()
    initial = {**final, "snapshots": None}
    result, saved = run_capture(tmp_path, initial, final, [(False, component_status(initial))])
    assert result["complete"] is True
    assert saved["componentStatus"]["snapshots"]["complete"] is True
    assert "error" not in saved["componentStatus"]["snapshots"]
    assert saved["requestHistory"][0]["component"] == "snapshots"
    assert saved["requestHistory"][0]["error"] == "timeout"
    assert saved["requestHistory"][0]["requested"] is True
    assert saved["components"] == final


def test_incomplete_snapshot_pages_keep_active_error_and_partial(tmp_path):
    final = complete_components()
    final["snapshots"] = {"isComplete": False, "receivedCount": 1, "pages": [{"page": 1}], "battleSnapData": None}
    initial = {**final, "snapshots": None}
    result, saved = run_capture(tmp_path, initial, final, [(False, component_status(final))])
    assert result["complete"] is False
    assert saved["componentStatus"]["snapshots"]["error"] == "timeout"
    assert saved["componentStatus"]["snapshots"]["complete"] is False
    assert "requestHistory" not in saved
    assert saved["components"]["snapshots"]["pages"] == [{"page": 1}]


def test_zero_event_history_cannot_become_complete_after_timeout(tmp_path):
    final = complete_components(event_count=0)
    initial = {**final, "all": None}
    result, saved = run_capture(tmp_path, initial, final, [(False, component_status(final))])
    assert result["complete"] is False
    assert saved["eventCount"] == 0
    assert saved["componentStatus"]["all"]["error"] == "timeout"
    assert "event_history_empty_or_unavailable" in saved["captureWarnings"]


def test_serialization_issues_still_block_complete_even_with_late_data(tmp_path):
    final = complete_components()
    initial = {**final, "snapshots": None}
    issues = [{"path": "/components/all/unknown", "kind": "max_depth"}]
    result, saved = run_capture(tmp_path, initial, final, [(False, component_status(initial))], issues=issues)
    assert result["complete"] is False
    assert saved["serializationIssues"] == issues
    assert saved["requestHistory"][0]["error"] == "timeout"


def test_explicit_request_error_remains_active_even_when_data_arrives(tmp_path):
    final = complete_components()
    initial = {**final, "snapshots": None}
    result, saved = run_capture(tmp_path, initial, final, [], request_error="server rejected snapshot: code=42")
    assert result["complete"] is False
    assert saved["componentStatus"]["snapshots"]["error"] == "server rejected snapshot: code=42"
    assert "requestHistory" not in saved
