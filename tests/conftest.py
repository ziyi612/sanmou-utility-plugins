"""Public-distribution test setup; all test scratch lives under this project."""
from pathlib import Path
import os
import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT / ".runtime" / "test-temp"
TEMP.mkdir(parents=True, exist_ok=True)
os.environ.update(TEMP=str(TEMP), TMP=str(TEMP), PYTHONDONTWRITEBYTECODE="1")


def pytest_collection_modifyitems(items):
    private_samples_missing = not any((ROOT / "capture/output/processed/battle_reports").glob("battle_*_decrypted.json"))
    private_sample_modules = {"test_battle_reports_cli.py", "test_entrypoint_review.py", "test_legacy_parser.py"}
    obsolete = {"test_encoded_publication_rejects_c_before_any_directory_or_file_creation"}
    for item in items:
        name = item.originalname or item.name
        if "c_drive" in name or name in obsolete:
            item.add_marker(pytest.mark.skip(reason="Personal drive-letter policy is not part of the portable distribution"))
        if private_samples_missing and (item.path.name in private_sample_modules or name == "test_real_exports_exact_coverage"):
            item.add_marker(pytest.mark.skip(reason="Historical integration case needs private retained battle-report fixtures, which are not distributed"))

        if (item.path.name == "test_event_semantics.py" and item.cls is not None
                and item.cls.__name__ == "EventSemanticsTests"
                and not any((ROOT / "capture/output/processed/decompiled_lua/battle_focus").glob("*_BattleDef_*.lua"))):
            item.add_marker(pytest.mark.skip(reason="Historical source-extraction tests need raw decompiled Lua, which is not distributed; public software uses verified cached catalogs"))
