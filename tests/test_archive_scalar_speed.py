"""Optimized scalar indexing must match the prior standard-library reference."""
from __future__ import annotations

import copy
import json
import math
import random
from unittest.mock import patch

import pytest

import parser.report_archive as module
from parser.report_archive import ReportArchive, iter_fields
from parser.report_semantics import SemanticMapper


def reference_json(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def reference_fields(value, pointer="", parent=None, key=None, ordinal=0):
    """Frozen pre-optimization traversal, independent of production helpers."""
    if isinstance(value, dict):
        kind, scalar, node_json = "object", None, "{}"
    elif isinstance(value, list):
        kind, scalar, node_json = "array", None, "[]"
    elif value is None:
        kind, scalar, node_json = "null", None, "null"
    elif isinstance(value, bool):
        kind, scalar, node_json = "boolean", str(value).lower(), reference_json(value)
    elif isinstance(value, int):
        kind, scalar, node_json = "integer", str(value), reference_json(value)
    elif isinstance(value, float):
        kind, scalar, node_json = "number", reference_json(value), reference_json(value)
    elif isinstance(value, str):
        kind, scalar, node_json = "string", value, reference_json(value)
        scalar = scalar.encode("utf-8", "backslashreplace").decode("utf-8")
    else:
        raise TypeError(f"Unsupported JSON value at {pointer}: {type(value).__name__}")
    yield pointer, parent, key, ordinal, kind, node_json, scalar
    children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for index, (child_key, child) in enumerate(children):
        text_key = str(child_key)
        escaped = text_key.replace("~", "~0").replace("/", "~1")
        yield from reference_fields(child, pointer + "/" + escaped, pointer, text_key, index)


class DescribedInteger(int):
    def __str__(self):
        return "custom-integer-text"


class EncodedString(str):
    def encode(self, *args, **kwargs):
        return b"custom-encoding"


class DescribedFloat(float):
    def __repr__(self):
        return "custom-float-text"


@pytest.mark.parametrize("value", [None, True, False, 0, 1, -1, 2**128, -(2**256), 2**2000,
    0.0, -0.0, 1.25, 5e-324, 2.2250738585072014e-308, 1.7976931348623157e308,
    "", "ASCII", "\\/\"\n\r\t\b\f\x00", "张三😀", "\ud800", "\udfff", "x\ud800中\udfff",
    [], {}, DescribedInteger(7), EncodedString("unchanged-json"), DescribedFloat(1.25)])
def test_all_scalar_and_subclass_results_match_reference(value):
    assert list(iter_fields(value)) == list(reference_fields(value))


def test_containers_pointer_escaping_order_and_types_match_reference():
    value = {"": [True, 1, 1.0, "1", None, {}, []], "a/b~c": {"雪/😀~": [-0.0, 2**80]},
             "empty": "", "null": None, "control": "\x00\x1f\x7f", "surrogate": "\ud800\udfff"}
    expected = list(reference_fields(value))
    assert list(iter_fields(value)) == expected
    assert expected[1][0] == "/"
    assert any(row[0] == "/a~1b~0c/雪~1😀~0" for row in expected)


def test_deterministic_mixed_nested_values_match_every_field():
    generator = random.Random(20261109)
    scalars = [None, True, False, 0, -1, 1, 2**100, -0.0, 1.2345678901234567,
               "plain", "", "中文😀", "\ud800", "\n\r\x00", "/~"]
    def make(depth):
        if depth == 0 or generator.random() < .45:
            return generator.choice(scalars)
        if generator.random() < .5:
            return [make(depth - 1) for _ in range(generator.randrange(5))]
        return {f"key/{index}~{generator.choice(['', '中', '😀'])}": make(depth - 1)
                for index in range(generator.randrange(5))}
    for _ in range(400):
        value = make(5)
        assert list(iter_fields(value)) == list(reference_fields(value))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), object(), (1, 2)])
def test_invalid_values_keep_the_same_exception_and_message(value):
    errors = []
    for function in (reference_fields, iter_fields):
        with pytest.raises((ValueError, TypeError)) as error:
            list(function({"a/b~": [value]}))
        errors.append((type(error.value), str(error.value)))
    assert errors[0] == errors[1]


def payload():
    data = {"battleID": "123", "eventDataList": [{"eventId": 0, "intList": [0, False, None, 2**100], "tab": 0}],
        "battleSettlement": {"attacker": {"army_id": 99, "avatar_data": {"avatar_id": 1, "avatar_name": "甲"},
            "army_member": [{"heros_id": 100, "index": 1, "hero_lv": 0,
                "skills": [{"skillId": 200, "level": 0}]}]}, "defensive": {"army_member": []}},
        "unknown": {"a/b~": [None, False, True, 0, -0.0, 1.25, 2**128, "\ud800中\udfff", {}, []]}}
    return {"battleID": "123", "captureVersion": 2, "source": "all", "eventCount": 1,
        "data": data, "components": {name: copy.deepcopy(data) if name in ("all", "detail", "basic") else {}
            for name in ("basic", "detail", "all", "statics", "snapshots")},
        "componentStatus": {name: {"requested": False, "available": True, "complete": True}
            for name in ("basic", "detail", "all", "statics", "snapshots")},
        "serializationIssues": [], "captureComplete": True}


def mapper():
    def lookup(kind, value):
        return {"name_zh": "已映射" if value in (100, 200) else None,
                "mapped": value in (100, 200), "status": "mapped" if value in (100, 200) else "unmapped",
                "source": {"path": "fixture", "line": 1}, "raw_id": value}
    def decode(event, context):
        return {"event_name_zh": "事件", "summary_zh": "中文说明", "status": "mapped",
                "parameters": event["intList"], "source": {"path": "fixture", "line": 2}}
    return SemanticMapper("scalar-speed-fixture", {"fixture": True}, lookup, decode)


def test_import_and_chinese_projection_equal_reference_and_keep_original_bytes(tmp_path):
    raw = b"\xef\xbb\xbf" + json.dumps(payload(), ensure_ascii=True, indent=2, allow_nan=False).encode("ascii") + b"\r\n"
    contents = []
    for label, function in (("reference", reference_fields), ("optimized", iter_fields)):
        with ReportArchive(tmp_path / (label + ".db"), semantic_mapper=mapper()) as archive:
            with patch.object(module, "iter_fields", function):
                result = archive.import_bytes(raw, namespace="account")
                assert result["status"] == "imported" and result["semantics"]["failed"] == 0
                assert archive.original_bytes(result["snapshot_ids"][0]) == raw
                assert archive.verify()["ok"]
                assert archive.import_bytes(raw, namespace="account")["status"] == "duplicate"
            tables = {}
            for table in ("report_fields", "report_components", "report_events", "report_sides", "report_heroes",
                          "report_skills", "report_id_meanings", "report_event_meanings"):
                tables[table] = [tuple(row) for row in archive.connection.execute("SELECT * FROM " + table + " ORDER BY rowid")]
            contents.append(tables)
    assert contents[0] == contents[1]
