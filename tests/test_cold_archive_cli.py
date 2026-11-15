"""Cold archive CLI validates inputs and prints only counts/sanitized errors."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest

SPEC = importlib.util.spec_from_file_location("cold_archive_cli_under_test", Path(__file__).resolve().parents[1] / "tools/migrate_quark_archive.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


@pytest.fixture
def fake_core(monkeypatch):
    calls = []
    module = types.ModuleType("parser.cold_archive")
    def migrate(*arguments, **options):
        calls.append((arguments, options))
        return {"phase": "verified_local_retained", "source_files": len(arguments[1]), "source_bytes": 100,
                "verified_files": len(arguments[1]), "removed_files": 0}
    module.migrate_cold_archives = migrate
    monkeypatch.setitem(sys.modules, "parser.cold_archive", module)
    return module, calls


def test_cli_default_retains_sources_and_passes_repeated_sources_and_hashes(tmp_path, fake_core, capsys):
    _module, calls = fake_core
    digest = "a" * 64
    status = CLI.main(["--root", str(tmp_path), "--source", str(tmp_path / "one.tar.zst"),
        "--source", str(tmp_path / "two.zip"), "--config", str(tmp_path / "quark.local.json"),
        "--expected-sha256", "sub\\one.tar.zst=" + digest.upper()])
    assert status == 0 and len(calls) == 1
    arguments, options = calls[0]
    assert arguments == (tmp_path, [tmp_path / "one.tar.zst", tmp_path / "two.zip"])
    assert options["config_path"] == tmp_path / "quark.local.json"
    assert options["folder_name"] == "旧完整战报归档"
    assert options["expected_sha256"] == {"sub/one.tar.zst": digest}
    assert options["delete_local"] is options["verify_only"] is False
    assert json.loads(capsys.readouterr().out)["removed_files"] == 0


@pytest.mark.parametrize("arguments", [[], ["--source", "one.zip"], ["--root", "root"],
    ["--root", "root", "--source", "one.zip", "--verify-only", "--delete-local"]])
def test_invalid_or_conflicting_arguments_never_call_core(arguments, fake_core, capsys):
    _module, calls = fake_core
    assert CLI.main(arguments) == 2 and calls == []
    assert json.loads(capsys.readouterr().out)["ok"] is False


@pytest.mark.parametrize("value", ["missing-separator", "one=bad", "../one=" + "a" * 64,
    "/one=" + "a" * 64, "C:\\one=" + "a" * 64, "one:stream=" + "a" * 64])
def test_bad_expected_hash_never_calls_core(tmp_path, value, fake_core, capsys):
    _module, calls = fake_core
    assert CLI.main(["--root", str(tmp_path), "--source", str(tmp_path / "one"), "--expected-sha256", value]) == 2
    assert calls == [] and json.loads(capsys.readouterr().out)["ok"] is False


def test_conflicting_duplicate_hash_is_rejected_without_core(tmp_path, fake_core, capsys):
    _module, calls = fake_core
    assert CLI.main(["--root", str(tmp_path), "--source", "one", "--expected-sha256", "one=" + "a" * 64,
                     "--expected-sha256", "one=" + "b" * 64]) == 2
    assert calls == []
    assert "冲突" in json.loads(capsys.readouterr().out)["error"]


def test_verify_only_and_explicit_delete_are_separate_supported_actions(tmp_path, fake_core, capsys):
    _module, calls = fake_core
    for flag in ("--verify-only", "--delete-local"):
        assert CLI.main(["--root", str(tmp_path), "--source", "one", flag]) == 0
    assert calls[0][1]["verify_only"] and not calls[0][1]["delete_local"]
    assert calls[1][1]["delete_local"] and not calls[1][1]["verify_only"]
    capsys.readouterr()


def test_progress_and_result_do_not_print_payload_tokens_or_urls(tmp_path, fake_core, capsys):
    module, _calls = fake_core
    marker = "fixture-private-payload"
    def migrate(*_arguments, **options):
        value = {"phase": "uploaded_part", "uploaded_parts": 2, "uploaded_bytes": 500,
                 "payload": marker, "access_token": marker, "url": "https://invalid.example/" + marker}
        options["progress"](value)
        return {**value, "phase": "complete", "source_files": 1, "verified_files": 1}
    module.migrate_cold_archives = migrate
    assert CLI.main(["--root", str(tmp_path), "--source", "one"]) == 0
    output = capsys.readouterr().out
    assert marker not in output and "https://" not in output and "access_token" not in output
    rows = [json.loads(line) for line in output.splitlines()]
    assert rows[0] == {"phase": "uploaded_part", "uploaded_parts": 2, "uploaded_bytes": 500}
    assert rows[-1]["verified_files"] == 1


def test_core_error_is_small_and_credential_url_payload_details_are_hidden(tmp_path, fake_core, capsys):
    module, _calls = fake_core
    marker = "fixture-DO-NOT-PRINT"
    def fail(*_arguments, **_options):
        raise RuntimeError("access_token=" + marker + " request https://invalid.example/?token=" + marker + " payload=" + marker)
    module.migrate_cold_archives = fail
    assert CLI.main(["--root", str(tmp_path), "--source", "one"]) == 2
    output = capsys.readouterr().out
    assert marker not in output and "https://" not in output
    value = json.loads(output)
    assert value["ok"] is False and len(value["error"]) <= 320


def test_partial_core_result_is_not_success(tmp_path, fake_core, capsys):
    module, _calls = fake_core
    module.migrate_cold_archives = lambda *_args, **_options: {"phase": "partial_cleanup", "source_files": 2,
        "verified_files": 2, "removed_files": 1}
    assert CLI.main(["--root", str(tmp_path), "--source", "one", "--delete-local"]) == 2
    assert json.loads(capsys.readouterr().out)["removed_files"] == 1


def test_unknown_arguments_are_not_echoed_back_as_potential_secrets(tmp_path, fake_core, capsys):
    _module, calls = fake_core
    marker = "fixture-private-unlabeled-credential"
    assert CLI.main(["--root", str(tmp_path), "--source", "one", "--unknown", marker]) == 2
    output = capsys.readouterr().out
    assert marker not in output and not calls
