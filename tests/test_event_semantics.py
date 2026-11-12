import copy
import hashlib
import json
import unittest
from pathlib import Path

from parser.event_semantics import decode_event, load_event_catalog, PARAMETER_LABELS


class EventSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_event_catalog()

    def test_catalog_comes_from_enum_argument_schema_and_localization(self):
        self.assertEqual(len(self.catalog["events"]), 126)
        self.assertEqual(sum(e["fields"] is not None for e in self.catalog["events"].values()), 124)
        event = self.catalog["events"]["211"]
        self.assertEqual(event["symbol"], "DoTargetSkillTargetBuff")
        self.assertEqual(event["fields"], ["teamId", "heroId", "skillId", "buffId", "srcTeamId", "srcHeroId"])
        self.assertIn("buffName", event["template_zh"])
        self.assertIn("historical", event["source"]["validation"])
        self.assertTrue(all(len(s["sha256"]) == 64 for s in self.catalog["source_files"]))
        self.assertTrue(all(field in PARAMETER_LABELS for e in self.catalog["events"].values() for field in e["fields"] or []))

    def test_all_arguments_and_original_event_are_retained(self):
        raw = {"eventId": 211, "tab": 2, "intList": [168720940801, 4012, 4122, 4012012, 168720940801, 4012], "unknown": 0}
        unchanged = copy.deepcopy(raw)
        result = decode_event(raw, self.catalog)
        self.assertEqual(result["schema_status"], "mapped")
        self.assertEqual(result["status"], "partial")
        self.assertEqual([p["raw_value"] for p in result["parameters"]], raw["intList"])
        self.assertEqual(result["raw_event"], raw)
        self.assertEqual(raw, unchanged)

    def test_tab_is_indentation_not_round(self):
        result = decode_event({"eventId": 1, "tab": 3, "intList": [7]}, self.catalog)
        self.assertEqual(result["tab_meaning_zh"], "日志嵌套缩进层级")
        self.assertEqual(result["parameters"][0]["key"], "round")
        self.assertEqual(result["parameters"][0]["raw_value"], 7)

    def test_unknown_event_and_extra_parameters_are_not_guessed(self):
        result = decode_event({"eventId": 999999, "tab": 0, "intList": [0, -5]}, self.catalog)
        self.assertEqual(result["status"], "unmapped")
        self.assertEqual(len(result["parameters"]), 2)
        result = decode_event({"eventId": 1, "tab": 0, "intList": [3, 99]}, self.catalog)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["parameters"][-1]["raw_value"], 99)
        self.assertEqual(result["parameters"][-1]["status"], "unmapped")

    def test_missing_arguments_are_reported(self):
        result = decode_event({"eventId": 211, "tab": 0, "intList": [1, 4001]}, self.catalog)
        self.assertEqual(result["status"], "partial")
        self.assertIn({"missing_parameters": ["skillId", "buffId", "srcTeamId", "srcHeroId"]}, result["issues"])

    def test_source_fixed_precision_is_applied_without_losing_raw(self):
        result = decode_event({"eventId": 213, "tab": 2, "intList": [160721041234, 1022, 3, 200000, 3151000, 10000000]}, self.catalog)
        change, after, maximum = result["parameters"][3:]
        self.assertEqual(change["raw_value"], 200000)
        self.assertEqual(change["decoded_value"], 20)
        self.assertEqual(after["decoded_value"], 315.1)
        self.assertEqual(maximum["decoded_value"], 1000)
        self.assertIn("属性配置", after["display_note_zh"])

    def test_dynamic_action_order_pairs_are_retained_in_order(self):
        result = decode_event({"eventId": 100, "tab": 1, "intList": [111, 3001, 222, 4001]}, self.catalog)
        self.assertEqual(result["schema_status"], "mapped")
        self.assertEqual([p["key"] for p in result["parameters"]], ["teamId", "heroId", "teamId", "heroId"])
        self.assertIn("第2位", result["parameters"][3]["label_zh"])
        bad = decode_event({"eventId": 100, "tab": 1, "intList": [111, 3001, 222]}, self.catalog)
        self.assertEqual(bad["status"], "partial")
        self.assertEqual(len(bad["parameters"]), 3)

    def test_team_ids_are_army_ids_and_custom_hero_names_use_matching_team(self):
        context = {"settlement": {
            "attacker": {"army_id": 111, "avatar_data": {"avatar_name": "甲"}, "army_member": [{"heros_id": 3001, "name": "自定义甲"}]},
            "defensive": {"army_id": 222, "avatar_data": {"avatar_name": "乙"}, "army_member": [{"heros_id": 3001, "name": "自定义乙"}]}}}
        result = decode_event({"eventId": 207, "tab": 2, "intList": [111, 3001, 3001, 222]}, self.catalog, context=context)
        self.assertEqual(result["parameters"][0]["id_kind"], "army")
        self.assertEqual(result["parameters"][0]["id_name_zh"], "进攻方·甲")
        self.assertEqual(result["parameters"][1]["id_name_zh"], "自定义甲")
        self.assertEqual(result["parameters"][2]["id_name_zh"], "自定义乙")

    def test_id_namespace_for_property_variants_is_preserved(self):
        seen = []
        def resolver(kind, value):
            seen.append((kind, value))
            return {"name_zh": f"{kind}{value}", "source": {"table": kind}}
        direct = decode_event({"eventId": 213, "tab": 1, "intList": [111, 3001, 3, 0, 0, 0]}, self.catalog, resolver)
        self.assertEqual(direct["parameters"][2]["id_kind"], "property")
        altered = decode_event({"eventId": 248, "tab": 1, "intList": [111, 3001, 3, 0, 0]}, self.catalog, resolver)
        self.assertEqual(altered["parameters"][2]["id_kind"], "report_property")
        same = decode_event({"eventId": 252, "tab": 1, "intList": [111, 3001, 3, 0, 1, 0]}, self.catalog, resolver)
        self.assertEqual(same["parameters"][2]["id_kind"], "report_property")
        normal = decode_event({"eventId": 252, "tab": 1, "intList": [111, 3001, 3, 0, 0, 0]}, self.catalog, resolver)
        self.assertEqual(normal["parameters"][2]["id_kind"], "property")

    def test_malformed_arguments_and_indentation_are_explicit(self):
        result = decode_event({"eventId": 1, "tab": False, "intList": [True]}, self.catalog)
        self.assertEqual(result["status"], "partial")
        self.assertIn("invalid_log_indentation", result["issues"])
        self.assertIn({"non_integer_argument": 1}, result["issues"])
        broken = decode_event({"eventId": 1, "tab": 0, "intList": "123"}, self.catalog)
        self.assertEqual(broken["raw_event"]["intList"], "123")
        self.assertEqual(broken["status"], "partial")

    def test_runtime_schema_is_compared_and_has_provenance(self):
        runtime = {"ET": {"BattleRound": 1}, "ETARGS": {"1": ["round"]}, "templates": {"1": "实时第round回合"}, "source": {"observed_at": "2026-11-07"}}
        catalog = load_event_catalog(runtime_catalog=runtime)
        event = catalog["events"]["1"]
        self.assertEqual(event["source"]["validation"], "runtime_schema_matched")
        self.assertEqual(event["template_zh"], "实时第round回合")
        runtime["ETARGS"]["1"] = ["round", "newValue"]
        changed = load_event_catalog(runtime_catalog=runtime)
        self.assertEqual(changed["events"]["1"]["source"]["validation"], "runtime_schema_changed")
        decoded = decode_event({"eventId": 1, "tab": 0, "intList": [1, 7]}, changed)
        self.assertEqual(decoded["status"], "partial")
        self.assertEqual(decoded["parameters"][1]["raw_value"], 7)

    def test_energy_template_proves_stamina_not_invented_battle_resource(self):
        result = decode_event({"eventId": 54, "tab": 0, "intList": [111, 3001, 5, 95]}, self.catalog)
        self.assertIn("体力", result["event_name_zh"])
        self.assertIn("体力", result["template_zh"])

    def test_current_runtime_new_event_and_changed_trailing_arguments(self):
        runtime_path = Path("data/dictionaries/runtime_semantics_20261107T093112Z.json")
        catalog = load_event_catalog(runtime_catalog=runtime_path)
        self.assertEqual(len(catalog["events"]), 138)
        granted = decode_event({"eventId": 107, "tab": 3, "intList": [160480918065, 4034, 4006, 20220]}, catalog)
        self.assertEqual(granted["schema_status"], "mapped")
        self.assertEqual(granted["event_name_zh"], "武将获授战法")
        self.assertEqual([p["key"] for p in granted["parameters"]], ["teamId", "giverHeroId", "targetHeroId", "skillId"])
        self.assertEqual(granted["source"]["definition"]["sha256"], hashlib.sha256(runtime_path.read_bytes()).hexdigest())
        self.assertEqual(granted["source"]["definition"]["pointer"], "/payload/event_catalog/ETARGS/107")
        self.assertEqual(granted["source"]["source_date"], "2026-11-07")
        buff = decode_event({"eventId": 209, "tab": 0, "intList": [1, 3001, 123, 1, 3001, 20220]}, catalog)
        self.assertEqual(buff["schema_status"], "mapped")
        self.assertEqual(buff["parameters"][5]["key"], "skillId")
        normal = decode_event({"eventId": 208, "tab": 0, "intList": [1, 3001, 27, 6494, 2, 21, 0, 4006]}, catalog)
        self.assertEqual(normal["schema_status"], "mapped")
        self.assertEqual(normal["parameters"][7]["key"], "srcHeroId")
        old = decode_event({"eventId": 208, "tab": 0, "intList": [1, 3001, 27, 6494, 2, 21, 0]}, catalog)
        self.assertEqual(old["status"], "partial")
        self.assertEqual(len(old["parameters"]), 7)

    def test_unknown_id_name_keeps_schema_mapping_but_not_full_chinese_status(self):
        context = {"settlement": {"attacker": {"army_id": 111, "avatar_data": {"avatar_name": "甲"}, "army_member": [{"heros_id": 3001}]}}}
        raw = {"eventId": 2, "tab": 0, "intList": [111, 3001]}
        result = decode_event(raw, self.catalog, lambda kind, value: {"mapped": False, "name_zh": None, "status": "unmapped"}, context)
        self.assertEqual(result["schema_status"], "mapped")
        self.assertEqual(result["id_mapping_status"], "partial")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unmapped_id_parameters"], [2])
        self.assertEqual(result["parameters"][1]["raw_value"], 3001)
        no_resolver = decode_event(raw, self.catalog, context=context)
        self.assertEqual(no_resolver["status"], "partial")
        self.assertIn({"unmapped_id_parameters": [2]}, no_resolver["issues"])

    def test_fully_resolved_names_and_numeric_only_event_are_mapped(self):
        context = {"settlement": {"attacker": {"army_id": 111, "avatar_data": {"avatar_name": "甲"}, "army_member": [{"heros_id": 3001}]}}}
        resolver = lambda kind, value: {"mapped": True, "name_zh": "孙权", "verification": "current_runtime", "source": [{"long_source": "x" * 10000}]}
        result = decode_event({"eventId": 2, "tab": 0, "intList": [111, 3001]}, self.catalog, resolver, context)
        self.assertEqual(result["status"], "mapped")
        self.assertEqual(result["id_mapping_status"], "mapped")
        self.assertNotIn("long_source", json.dumps(result["parameters"][1]["id_source"]))
        numeric = decode_event({"eventId": 1, "tab": 0, "intList": [3]}, self.catalog)
        self.assertEqual(numeric["status"], "mapped")
        self.assertEqual(numeric["id_mapping_status"], "not_applicable")

    def test_zero_and_negative_ids_are_not_arbitrary_none_sentinels(self):
        result = decode_event({"eventId": 209, "tab": 0, "intList": [0, -1, 0, 0, -1]}, self.catalog)
        self.assertEqual(result["schema_status"], "mapped")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(all(p["id_status"] == "unmapped" for p in result["parameters"]))
        self.assertEqual([p["raw_value"] for p in result["parameters"]], [0, -1, 0, 0, -1])


if __name__ == "__main__":
    unittest.main()
