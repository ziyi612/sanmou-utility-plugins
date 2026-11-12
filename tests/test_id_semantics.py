import hashlib
import json
from pathlib import Path

import pytest

from parser.id_semantics import IdCatalog, build_id_catalog, load_id_catalog, save_id_catalog


def source_fixture(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "1_translate_cn_1_x.lua").write_text('''translate_excel_design["hero_hash"] = {message_cn = "诸葛亮"}
translate_excel_design["skill_hash"] = {message_cn = "火烧{连营}\\n说明"}
translate_excel_design["property_hash"] = {message_cn = "武力"}
''', encoding="utf-8")
    (root / "2_heros_x.lua").write_text('''-- chunkname: Data\\heros.lua
heros_heros[100] = setmetatable({
 name_hash = "hero_hash", nested = {name = "错误名称"}, biography = "姓名中有}与{符号"
}, __design_localize_metatable)
''', encoding="utf-8")
    (root / "3_skills_x.lua").write_text('''skills_skills[200] = setmetatable({name_hash="skill_hash"}, __design_localize_metatable)''', encoding="utf-8")
    (root / "4_fightprop_x.lua").write_text('''fightprop_BattleProp[1] = setmetatable({Name_hash="property_hash"}, __design_localize_metatable)
fightprop_BuffBattleProp[1] = {Name="兵刃伤害"}
''', encoding="utf-8")
    (root / "5_battle_reports_x.lua").write_text('''battle_reports_fightprop[1] = {fightprop_name="攻击"}
battle_reports_tags[101] = {tags_name="震慑",buff_tags="forbid_action"}
''', encoding="utf-8")
    (root / "7_appearance_x.lua").write_text('''appearance_appearance_cn[96040] = {name="山河绮梦",heroID=3004}''', encoding="utf-8")
    (root / "6_CommonConst_x.lua").write_text('''CommonConst.ArmyType = {Cavalry=4,SpearMan=3,Peltast=1,Archr=2}
CommonConst.HeroGroupType = {SHU=2,QUN=4,WU=3,WEI=1}
''', encoding="utf-8")
    return root


def test_localized_names_nested_tables_and_provenance(tmp_path):
    root = source_fixture(tmp_path / "sources")
    catalog = build_id_catalog(root)
    hero = catalog.lookup("hero", 100)
    assert hero["mapped"] and hero["name_zh"] == "诸葛亮"
    assert hero["verification"] == "historical_source"
    assert len(hero["source"]) == 2
    for source in hero["source"]:
        assert source["sha256"] == hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest()
        assert source["line"] > 0
    assert catalog.lookup("skill", 200)["name_zh"] == "火烧{连营}\n说明"
    assert catalog.lookup("arms", 1)["name_zh"] == "盾兵"
    assert catalog.lookup("arms", 2)["name_zh"] == "弓兵"
    assert catalog.lookup("arms", 3)["name_zh"] == "枪兵"
    assert catalog.lookup("arms", 4)["name_zh"] == "骑兵"


def test_property_namespaces_are_not_conflated(tmp_path):
    catalog = build_id_catalog(source_fixture(tmp_path / "sources"))
    assert catalog.lookup("property", 1)["name_zh"] == "武力"
    assert catalog.lookup("report_property", 1)["name_zh"] == "攻击"
    assert catalog.lookup("buff_property", 1)["name_zh"] == "兵刃伤害"


def test_skin_and_tag_keep_their_own_id_namespaces(tmp_path):
    catalog = build_id_catalog(source_fixture(tmp_path / "sources"))
    assert catalog.lookup("skin", 96040)["name_zh"] == "山河绮梦"
    assert catalog.lookup("tag", 101)["name_zh"] == "震慑"
    assert catalog.lookup("state", "forbid_action")["name_zh"] == "震慑"
    assert not catalog.lookup("state", 101)["mapped"]


def test_unknown_and_invalid_keep_original_value():
    catalog = IdCatalog()
    unknown = catalog.lookup("state", 99)
    assert unknown["id"] == 99 and not unknown["mapped"]
    assert unknown["name_zh"] is None and unknown["verification"] == "unmapped"
    assert catalog.lookup("hero", True)["status"] == "invalid_id"
    assert catalog.lookup("hero", {"id": 1})["status"] == "invalid_id"


def test_scenario_conflicts_do_not_pick_arbitrary_names():
    catalog = IdCatalog([
        {"kind": "item", "id": "1", "name_zh": "赛季甲装备", "scenario_id": "1", "verification": "historical_source"},
        {"kind": "item", "id": "1", "name_zh": "赛季乙装备", "scenario_id": "2", "verification": "historical_source"},
    ])
    assert catalog.lookup("equipment", 1, 1)["name_zh"] == "赛季甲装备"
    assert catalog.lookup("horse", 1, 2)["name_zh"] == "赛季乙装备"
    assert catalog.lookup("item", 1, 18)["status"] == "ambiguous_scenario"


def test_runtime_overrides_history_only_for_matching_scenario(tmp_path):
    catalog = build_id_catalog(source_fixture(tmp_path / "sources"))
    doc = {"capturedAt": "2026-11-07T10:00:00Z", "runtimeIdentity": {"scenarioId": "18"},
           "tables": {"hero": {"100": {"name": "当前武将名称"}}, "advanced_arms": {"1": {"Arms_name": "当前盾兵"}}},
           "enums": {"arms": {"Peltast": 1, "Archr": 2}}}
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    before = catalog.version
    catalog.merge_runtime(doc, path)
    current = catalog.lookup("hero", 100, 18)
    assert current["name_zh"] == "当前武将名称" and current["verification"] == "current_runtime"
    assert current["source"][0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert catalog.lookup("hero", 100, 17)["name_zh"] == "诸葛亮"
    assert catalog.lookup("hero", 100, 17)["verification"] == "historical_source"
    assert catalog.lookup("advanced_arms", 1, 18)["name_zh"] == "当前盾兵"
    assert catalog.lookup("arms", 2, 18)["verification"] == "current_runtime"
    assert catalog.version != before


def test_runtime_refresh_replaces_previous_current_snapshot():
    catalog = IdCatalog()
    catalog.merge_runtime({"tables": {"hero": {"1": {"name": "旧当前名称"}}}})
    catalog.merge_runtime({"tables": {"hero": {"1": {"name": "新当前名称"}}}})
    assert catalog.lookup("hero", 1)["name_zh"] == "新当前名称"
    assert len(catalog.entries) == 1


def test_cached_catalog_roundtrip_and_tamper_detection(tmp_path):
    catalog = build_id_catalog(source_fixture(tmp_path / "sources"))
    path = tmp_path / "catalog.json"
    save_id_catalog(catalog, path)
    loaded = load_id_catalog(cache_path=path)
    assert loaded.version == catalog.version
    assert loaded.lookup("hero", 100) == catalog.lookup("hero", 100)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["entries"][0]["name_zh"] = "被改动"
    path.write_text(json.dumps(document), encoding="utf-8")
    load_id_catalog.cache_clear()
    with pytest.raises(ValueError, match="hash mismatch"):
        load_id_catalog(cache_path=path)


def test_lua_decimal_utf8_escapes_are_decoded_without_evaluation(tmp_path):
    root = tmp_path / "sources"; root.mkdir()
    (root / "1_heros_x.lua").write_text(r'heros_heros[1] = {name="\232\175\184\232\145\155\228\186\174"}', encoding="utf-8")
    assert build_id_catalog(root).lookup("hero", 1)["name_zh"] == "诸葛亮"
