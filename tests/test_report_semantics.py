"""Chinese projections remain sourced and independent of immutable report data."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from parser.report_archive import ReportArchive
from parser.report_semantics import SemanticArchive, SemanticMapper, build_context, ensure_schema, semantic_stats


TEMP = Path(".runtime/test-temp")


def payload(battle_id=123):
    return {"battleID": battle_id, "source": "all", "data": {
        "battleID": battle_id,
        "battleSettlement": {"attacker": {"army_id": 168720940801, "formationId": 1,
            "army_member": [{"heros_id": 4012, "index": 2, "group": 3,
                             "hero_lv": 15, "skills": [{"skillId": 300401}]}]},
            "defensive": {"army_id": -100004, "army_member": [{"heros_id": 5000, "index": 1}]}},
        "eventDataList": [{"eventId": 211, "tab": 3, "intList": [168720940801, 4012, 0]},
                          {"eventId": 99999, "intList": [2**80, None, False]}],
        "unknown": {"heroId": 1, "group": 3},
    }}


def lookup(kind, value):
    names = {("hero", "4012"): "测试武将", ("skill", "300401"): "测试战法", ("formation", "1"): "测试阵型"}
    name = names.get((kind, str(value)))
    return {"name_zh": name, "mapped": name is not None, "status": "mapped" if name else "unmapped",
            "source": {"path": "fixture.lua", "line": 10}, "verification": "fixture_only", "raw_id": value}


def decode(event, context):
    if event.get("eventId") != 211:
        return {"event_name_zh": None, "summary_zh": None, "status": "unmapped", "parameters": event.get("intList"),
                "source": {"reason": "unknown_event"}}
    hero = context["teams"]["168720940801"]["heroes"]["4012"]
    return {"event_name_zh": "测试事件", "summary_zh": hero["name_zh"] + "发生测试事件", "status": "mapped",
            "parameters": [{"name_zh": "武将", "raw_value": 4012, "name_value_zh": hero["name_zh"]}],
            "source": {"path": "fixture_event.lua", "line": 22}}


class SemanticArchiveTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="report_semantics_", dir=TEMP)
        self.archive = ReportArchive(Path(self.temp.name) / "reports.db")
        self.raw = json.dumps(payload(), ensure_ascii=False).encode()
        self.sid = self.archive.import_bytes(self.raw)["snapshot_ids"][0]
        self.mapper = SemanticMapper("fixture-v1", {"profile": "fixture"}, lookup, decode)

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def test_extra_mappings_preserve_original_bytes_and_all_projections(self):
        before = self.archive.stats()
        helper = SemanticArchive(self.archive.connection, self.mapper)
        result = helper.enrich_snapshot(self.sid)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["event_count"], 2)
        self.assertEqual(result["mapped_event_count"], 1)
        self.assertEqual(self.archive.original_bytes(self.sid), self.raw)
        self.assertEqual(self.archive.stats(), before)
        self.assertTrue(self.archive.verify()["ok"])
        row = self.archive.connection.execute("SELECT event_name_zh,summary_zh,parameters_json,status,source_json "
                                              "FROM report_current_event_meanings WHERE sequence=0").fetchone()
        self.assertEqual(row[0], "测试事件")
        self.assertIn("测试武将", row[1])
        self.assertEqual(json.loads(row[2])[0]["raw_value"], 4012)
        self.assertEqual(json.loads(row[4])["source"]["line"], 22)
        self.assertEqual(semantic_stats(self.archive.connection)["current_event_statuses"], {"mapped": 1, "unmapped": 1})

    def test_unknown_values_preserve_exact_integer_null_and_bool(self):
        SemanticArchive(self.archive.connection, self.mapper).enrich_snapshot(self.sid)
        row = self.archive.connection.execute("SELECT parameters_json,status FROM report_event_meanings WHERE sequence=1").fetchone()
        self.assertEqual(json.loads(row[0]), [2**80, None, False])
        self.assertEqual(row[1], "unmapped")
        unresolved = self.archive.connection.execute("SELECT kind,raw_value_json,mapped,name_zh FROM report_id_meanings "
                                                     "WHERE kind='hero' AND raw_value_json='5000'").fetchone()
        self.assertEqual(tuple(unresolved), ("hero", "5000", 0, None))

    def test_id_selection_does_not_guess_generic_hero_id_or_group(self):
        SemanticArchive(self.archive.connection, self.mapper).enrich_snapshot(self.sid)
        paths = [r[0] for r in self.archive.connection.execute("SELECT pointer FROM report_id_meanings")]
        self.assertNotIn("/data/unknown/heroId", paths)
        self.assertNotIn("/data/unknown/group", paths)
        self.assertIn("/data/battleSettlement/attacker/army_member/0/group", paths)

    def test_idempotent_versioned_incremental_backfill(self):
        second = self.archive.import_bytes(json.dumps(payload(456)).encode())["snapshot_ids"][0]
        helper = SemanticArchive(self.archive.connection, self.mapper)
        self.assertEqual(helper.backfill(limit=1)["ready"], 1)
        self.assertEqual(helper.backfill(limit=1)["ready"], 1)
        self.assertEqual(helper.backfill(limit=1)["snapshots"], 0)
        self.assertEqual(helper.enrich_snapshot(second)["status"], "duplicate")
        newer = SemanticMapper("fixture-v2", {"profile": "newer"}, lookup,
                               lambda e, c: {**decode(e, c), "event_name_zh": "新版本"})
        two = SemanticArchive(self.archive.connection, newer)
        self.assertEqual(self.archive.connection.execute("SELECT count(*) FROM report_current_event_meanings").fetchone()[0], 0)
        self.assertEqual(two.backfill()["ready"], 2)
        self.assertEqual(self.archive.connection.execute("SELECT count(*) FROM report_event_meanings").fetchone()[0], 8)
        self.assertEqual(self.archive.connection.execute("SELECT DISTINCT version FROM report_current_event_meanings").fetchone()[0], "fixture-v2")

    def test_failed_mapping_does_not_roll_back_raw_import_or_leave_partial_meanings(self):
        def broken(event, context):
            if event.get("eventId") == 99999:
                raise RuntimeError("decoder failure")
            return decode(event, context)
        mapper = SemanticMapper("broken", {"profile": "broken"}, lookup, broken)
        helper = SemanticArchive(self.archive.connection, mapper)
        result = helper.enrich_snapshot(self.sid)
        self.assertEqual(result["status"], "failed")
        self.assertIn("decoder failure", result["error"])
        self.assertEqual(self.archive.connection.execute("SELECT count(*) FROM report_event_meanings").fetchone()[0], 0)
        self.assertEqual(self.archive.connection.execute("SELECT count(*) FROM report_id_meanings").fetchone()[0], 0)
        self.assertEqual(self.archive.connection.execute("SELECT status FROM report_semantic_runs").fetchone()[0], "failed")
        self.assertEqual(self.archive.original_bytes(self.sid), self.raw)
        self.assertTrue(self.archive.verify()["ok"])

    def test_schema_and_enrichment_do_not_commit_outer_transaction(self):
        c = self.archive.connection
        c.execute("BEGIN")
        helper = SemanticArchive(c, self.mapper)
        helper.enrich_snapshot(self.sid)
        self.assertTrue(c.in_transaction)
        c.rollback()
        self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name='report_semantic_meta'").fetchone())
        self.assertEqual(self.archive.original_bytes(self.sid), self.raw)

    def test_read_only_stats_does_not_create_tables_and_schema_write_is_rejected(self):
        with ReportArchive(self.archive.db_path, read_only=True) as reader:
            self.assertEqual(semantic_stats(reader.connection), {"available": False})
            with self.assertRaises(sqlite3.OperationalError):
                ensure_schema(reader.connection)
            self.assertEqual(semantic_stats(reader.connection), {"available": False})

    def test_sources_for_registered_version_cannot_change(self):
        SemanticArchive(self.archive.connection, self.mapper)
        changed = SemanticMapper("fixture-v1", {"profile": "different"}, lookup, decode)
        with self.assertRaisesRegex(ValueError, "different sources"):
            SemanticArchive(self.archive.connection, changed)

    def test_current_views_follow_best_raw_snapshot(self):
        newer = payload()
        newer["data"]["eventDataList"].append({"eventId": 211, "intList": []})
        next_sid = self.archive.import_bytes(json.dumps(newer).encode())["snapshot_ids"][0]
        helper = SemanticArchive(self.archive.connection, self.mapper)
        helper.backfill()
        current = self.archive.connection.execute("SELECT DISTINCT snapshot_id FROM report_current_event_meanings").fetchall()
        self.assertEqual([r[0] for r in current], [next_sid])
        self.assertEqual(self.archive.connection.execute("SELECT count(*) FROM report_current_event_meanings").fetchone()[0], 3)

    def test_context_uses_real_army_ids_and_marks_ambiguous_hero_ids(self):
        value = payload()
        raw = copy.deepcopy(value["data"]["battleSettlement"]["attacker"]["army_member"][0])
        raw["index"] = 3
        value["data"]["battleSettlement"]["attacker"]["army_member"].append(raw)
        ctx = build_context(value, "123", lookup)
        self.assertNotIn("1", ctx["teams"])
        team = ctx["teams"]["168720940801"]
        self.assertNotIn("4012", team["heroes"])
        self.assertEqual(len(team["hero_candidates"]["4012"]), 2)
        self.assertEqual(ctx["heroes_by_side_position"]["attacker"]["2"]["id"], 4012)

    def test_contextual_lookup_uses_snapshot_scenario_and_is_available_to_events(self):
        value = payload(789)
        value["runtimeIdentity"] = {"scenarioId": "18"}
        value["data"]["scenarioId"] = 7
        sid = self.archive.import_bytes(json.dumps(value).encode())["snapshot_ids"][0]
        seen = []
        def contextual(kind, value, context):
            seen.append(context["scenario_id"])
            return {**lookup(kind, value), "name_zh": "场景" + str(context["scenario_id"]), "mapped": True,
                    "status": "mapped", "source": {"scenario_id": context["scenario_id"]}}
        def contextual_event(event, context):
            self.assertEqual(context["id_lookup"]("skill", 300401)["name_zh"], "场景18")
            return decode(event, context)
        def forbidden_fallback(kind, value):
            raise AssertionError("Contextual lookups must not fall back to another scenario")
        mapper = SemanticMapper("scenario-aware", {}, forbidden_fallback, contextual_event,
                                context_id_lookup=contextual)
        result = SemanticArchive(self.archive.connection, mapper).enrich_snapshot(sid)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(set(seen), {"18"})
        names = self.archive.connection.execute("SELECT DISTINCT name_zh FROM report_id_meanings WHERE snapshot_id=?", (sid,)).fetchall()
        self.assertEqual([r[0] for r in names], ["场景18"])
        event = self.archive.connection.execute("SELECT summary_zh FROM report_event_meanings WHERE snapshot_id=? AND sequence=0", (sid,)).fetchone()
        self.assertIn("场景18", event[0])

    def test_sourced_book_advanced_skill_and_explicit_skin_paths_only(self):
        # Verified design-table consumers:
        # BattleDef1369:2190-2200 / SimpleBattleKillInfoRender6058:570 (warbook),
        # ArmsAdvancedHelper1037:622,676 (advanced-arms skill),
        # AppearanceHelper0885:133,518 (skin -> appearance_appearance).
        value = payload(901)
        hero = value["data"]["battleSettlement"]["attacker"]["army_member"][0]
        hero["warbooks"] = {"idList": [11, 12]}
        hero["armsAdvancedSkillIdList"] = [1]
        hero["heroSkinInfo"] = {"skinId": 96040}
        hero["skills"][0]["skillSkinInfo"] = {"skinId": 140004}
        value["data"]["unknown"].update(idList=[11], skinId=999)
        sid = self.archive.import_bytes(json.dumps(value).encode())["snapshot_ids"][0]
        mapper = SemanticMapper("explicit-arrays", {}, lookup, decode)
        result = SemanticArchive(self.archive.connection, mapper).enrich_snapshot(sid)
        self.assertEqual(result["status"], "ready")
        rows = {r[0]: (r[1], json.loads(r[2])) for r in self.archive.connection.execute(
            "SELECT pointer,kind,raw_value_json FROM report_id_meanings WHERE snapshot_id=?", (sid,))}
        base = "/data/battleSettlement/attacker/army_member/0/"
        self.assertEqual(rows[base + "warbooks/idList/0"], ("warbook", 11))
        self.assertEqual(rows[base + "warbooks/idList/1"], ("warbook", 12))
        self.assertEqual(rows[base + "armsAdvancedSkillIdList/0"], ("advanced_arms_skill", 1))
        self.assertEqual(rows[base + "heroSkinInfo/skinId"], ("skin", 96040))
        self.assertEqual(rows[base + "skills/0/skillSkinInfo/skinId"], ("skin", 140004))
        self.assertNotIn("/data/unknown/idList/0", rows)
        self.assertNotIn("/data/unknown/skinId", rows)

    def test_other_connection_can_commit_raw_import_during_decoding(self):
        """WAL read snapshots must not be held across the CPU mapping phase."""
        phases = []
        committed = []
        with ReportArchive(self.archive.db_path) as writer:
            def concurrent_decode(event, context):
                phases.append(self.archive.connection.in_transaction)
                if not committed:
                    result = writer.import_bytes(json.dumps(payload(456)).encode())
                    self.assertEqual(result["status"], "imported")
                    committed.append(result["snapshot_ids"][0])
                return decode(event, context)

            mapper = SemanticMapper("concurrent-raw", {}, lookup, concurrent_decode)
            result = SemanticArchive(self.archive.connection, mapper).enrich_snapshot(self.sid)
            self.assertEqual(result["status"], "ready", result)
            self.assertEqual(phases, [False, False])
            self.assertFalse(self.archive.connection.in_transaction)
            self.assertEqual(writer.connection.execute(
                "SELECT status FROM report_semantic_runs WHERE snapshot_id=? AND version=?",
                (self.sid, mapper.version)).fetchone()[0], "ready")
            self.assertEqual(self.archive.connection.execute("SELECT count(*) FROM report_snapshots").fetchone()[0], 2)
            self.assertEqual(self.archive.original_bytes(self.sid), self.raw)
            self.assertTrue(self.archive.verify()["ok"])

    def test_enrichment_does_not_commit_an_existing_outer_write_transaction(self):
        c = self.archive.connection
        helper = SemanticArchive(c, self.mapper)
        c.execute("CREATE TABLE caller_owned(value TEXT)")
        c.commit()
        observer = sqlite3.connect(str(self.archive.db_path))
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO caller_owned VALUES('uncommitted')")
            result = helper.enrich_snapshot(self.sid)
            self.assertEqual(result["status"], "ready", result)
            self.assertTrue(c.in_transaction)
            self.assertEqual(observer.execute("SELECT count(*) FROM caller_owned").fetchone()[0], 0)
            self.assertEqual(observer.execute("SELECT count(*) FROM report_semantic_runs").fetchone()[0], 0)
            c.rollback()
            self.assertEqual(c.execute("SELECT count(*) FROM caller_owned").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM report_semantic_runs").fetchone()[0], 0)
            self.assertEqual(self.archive.original_bytes(self.sid), self.raw)
        finally:
            observer.close()

    def test_concurrent_same_version_ready_result_is_rechecked_before_writing(self):
        other_finished = []
        with ReportArchive(self.archive.db_path) as other:
            other_mapper = SemanticMapper("concurrent-same-version", {}, lookup, decode)
            other_helper = SemanticArchive(other.connection, other_mapper)

            def raced_decode(event, context):
                if not other_finished:
                    result = other_helper.enrich_snapshot(self.sid)
                    self.assertEqual(result["status"], "ready", result)
                    other_finished.append(True)
                return decode(event, context)

            mapper = SemanticMapper("concurrent-same-version", {}, lookup, raced_decode)
            result = SemanticArchive(self.archive.connection, mapper).enrich_snapshot(self.sid)
            self.assertEqual(result["status"], "duplicate", result)
            self.assertEqual(self.archive.connection.execute(
                "SELECT count(*) FROM report_event_meanings WHERE version=?", (mapper.version,)).fetchone()[0], 2)
            self.assertFalse(self.archive.connection.in_transaction)

    def test_failure_record_uses_fresh_writer_scope_after_other_raw_commit(self):
        committed = []
        with ReportArchive(self.archive.db_path) as writer:
            def fail_after_other_commit(event, context):
                if not committed:
                    result = writer.import_bytes(json.dumps(payload(456)).encode())
                    self.assertEqual(result["status"], "imported")
                    committed.append(True)
                raise RuntimeError("mapping failed after independent raw commit")

            mapper = SemanticMapper("concurrent-failure", {}, lookup, fail_after_other_commit)
            result = SemanticArchive(self.archive.connection, mapper).enrich_snapshot(self.sid)
            self.assertEqual(result["status"], "failed")
            self.assertIn("mapping failed after independent raw commit", result["error"])
            self.assertNotIn("failure_record_error", result)
            self.assertEqual(writer.connection.execute(
                "SELECT status FROM report_semantic_runs WHERE snapshot_id=? AND version=?",
                (self.sid, mapper.version)).fetchone()[0], "failed")
            self.assertEqual(self.archive.connection.execute("SELECT count(*) FROM report_snapshots").fetchone()[0], 2)
            self.assertEqual(self.archive.original_bytes(self.sid), self.raw)
            self.assertFalse(self.archive.connection.in_transaction)


if __name__ == "__main__":
    unittest.main()
