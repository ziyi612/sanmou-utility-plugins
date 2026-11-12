"""Offline regressions for numeric identities observed in the live client."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType

import pytest

import battle_reports as cli
from capture.game_assets import export_battle_report_runtime as exporter
from parser.report_archive import ReportArchive


NUMERIC = {"avatarId": 123456789, "serverId": 760057, "scenarioId": 18}
CANONICAL = {key: str(value) for key, value in NUMERIC.items()}


@pytest.fixture
def work():
    base = Path(".runtime/test-temp").resolve()
    with tempfile.TemporaryDirectory(prefix="sanmou-identity-", dir=base) as folder:
        directory = Path(folder).resolve()
        assert directory.parent == base
        yield directory


class IdentityRunner:
    def __init__(self, payload):
        self.payload = payload

    def execute_on_update(self, code, timeout=10):
        return {"ok": True, "result": json.dumps({"payload": self.payload})}


@pytest.mark.parametrize("payload", [NUMERIC, CANONICAL, {**NUMERIC, "serverId": "760057"}])
def test_live_numeric_and_retained_string_identities_have_one_representation(monkeypatch, payload):
    monkeypatch.setitem(sys.modules, "export_battle_report_runtime", exporter)
    assert cli.read_identity(IdentityRunner(payload)) == CANONICAL


@pytest.mark.parametrize("key,value", [
    ("avatarId", 0), ("avatarId", "0"), ("serverId", 0), ("serverId", "000"),
    ("avatarId", True), ("serverId", False), ("scenarioId", True),
    ("avatarId", -1), ("scenarioId", -1), ("serverId", 1.0),
    ("avatarId", ""), ("avatarId", " 123"), ("serverId", "abc"),
    ("avatarId", "１２３"), ("scenarioId", None),
])
def test_invalid_live_identity_is_rejected(monkeypatch, key, value):
    monkeypatch.setitem(sys.modules, "export_battle_report_runtime", exporter)
    with pytest.raises(RuntimeError, match="identity"):
        cli.read_identity(IdentityRunner({**NUMERIC, key: value}))


@pytest.mark.parametrize("scenario", [0, "0"])
def test_scenario_zero_remains_supported(monkeypatch, scenario):
    monkeypatch.setitem(sys.modules, "export_battle_report_runtime", exporter)
    assert cli.read_identity(IdentityRunner({**NUMERIC, "scenarioId": scenario}))["scenarioId"] == "0"


@pytest.mark.parametrize("binding_identity,capture_identity", [
    (NUMERIC, CANONICAL), (CANONICAL, NUMERIC), (NUMERIC, NUMERIC),
])
def test_recovery_accepts_equivalent_identity_without_changing_original_bytes(work, binding_identity, capture_identity):
    raw = json.dumps({"battleID": 123, "runtimeIdentity": capture_identity,
                      "data": {"eventDataList": []}}).encode()
    source = work / "battle_123_decrypted.json"
    source.write_bytes(raw)
    with ReportArchive(work / "reports.db") as archive:
        result = cli.recover_captures(archive, work, "test", expected_identity=binding_identity)
        assert result == {"recovered": 1, "errors": []}
        snapshot_id = archive.list_reports()[0]["current_snapshot_id"]
        assert archive.original_bytes(snapshot_id) == raw
    assert source.read_bytes() == raw


@pytest.mark.parametrize("capture_identity", [
    {**NUMERIC, "avatarId": True}, {**NUMERIC, "serverId": 0},
    {**NUMERIC, "avatarId": 10545058801}, None,
])
def test_recovery_rejects_invalid_or_different_declared_identity(work, capture_identity):
    raw = json.dumps({"battleID": 123, "runtimeIdentity": capture_identity,
                      "data": {"eventDataList": []}}).encode()
    source = work / "battle_123_decrypted.json"
    source.write_bytes(raw)
    with ReportArchive(work / "reports.db") as archive:
        result = cli.recover_captures(archive, work, "test", expected_identity=NUMERIC)
        assert result["errors"]
        assert archive.stats()["records"] == 0
    assert source.read_bytes() == raw


def test_collection_reuses_numeric_account_binding_and_recovers_string_capture(work, monkeypatch, capsys):
    namespace = "server:760057/scenario:18/avatar:123456789"
    out = work / "incoming"
    directory = out / hashlib.sha256(namespace.encode()).hexdigest()[:20]
    directory.mkdir(parents=True)
    binding = directory / "account.json"
    binding.write_text(json.dumps({"namespace": namespace, "identity": NUMERIC}), "utf-8")
    raw = json.dumps({"battleID": 123, "runtimeIdentity": CANONICAL,
                      "namespace": namespace, "data": {"eventDataList": []}}).encode()
    (directory / "battle_123_decrypted.json").write_bytes(raw)
    calls = []

    class Runner(IdentityRunner):
        def __init__(self, pid=None):
            super().__init__(NUMERIC)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    class Collector:
        def __init__(self, runner, selected_directory, **kwargs):
            calls.append((selected_directory, kwargs["namespace"]))
        def run_cycle(self):
            return {"complete": True, "errors": []}

    runner_module, collector_module = ModuleType("run_lua_in_game"), ModuleType("battle_collector")
    runner_module.GameLuaRunner, collector_module.BattleCollector = Runner, Collector
    monkeypatch.setitem(sys.modules, "run_lua_in_game", runner_module)
    monkeypatch.setitem(sys.modules, "battle_collector", collector_module)
    monkeypatch.setitem(sys.modules, "export_battle_report_runtime", exporter)
    database = work / "reports.db"
    assert cli.main(["collect", "--once", "--db", str(database), "--out-dir", str(out)]) == 0
    capsys.readouterr()
    assert calls == [(directory, namespace)]
    assert json.loads(binding.read_text("utf-8"))["identity"] == CANONICAL
    status = json.loads((out / "collector_status.json").read_text("utf-8"))
    assert status["recovery_errors"] == []
    with ReportArchive(database, read_only=True) as archive:
        assert archive.stats()["records"] == 1
        assert archive.original_bytes(archive.list_reports()[0]["current_snapshot_id"]) == raw
