import json

from parser.semantic_catalog import _bounded_id_lookup, build_default_mapper


class FakeCatalog:
    version = "fake-id-version"
    source_manifest = [{"path": "fake-id-source", "sha256": "fake-sha"}]

    def __init__(self):
        self.calls = []
        self.shared_source = [{"path": "source.lua", "nested": {"lines": [1, 2]}}]

    def lookup(self, kind, value, scenario_id=None):
        self.calls.append((kind, type(value), value, type(scenario_id), scenario_id))
        mapped = isinstance(value, int) and not isinstance(value, bool) and value > 0
        return {"kind": kind, "id": value, "name_zh": f"{kind}:{value}:{scenario_id}" if mapped else None,
                "mapped": mapped, "status": "mapped" if mapped else "unmapped",
                "source": self.shared_source, "verification": "test_source"}


def test_cache_separates_value_types_scenarios_and_kinds():
    catalog = FakeCatalog()
    lookup = _bounded_id_lookup(catalog)
    requests = [("hero", 1, 18), ("hero", True, 18), ("hero", "1", 18),
                ("hero", 1, "18"), ("hero", 1, 19), ("skill", 1, 18)]
    first = [lookup(*request) for request in requests]
    for request, expected in zip(requests, first):
        assert lookup(*request) == expected
    assert len(catalog.calls) == len(requests)
    assert first[0]["mapped"] is True
    assert first[1]["mapped"] is False and first[1]["id"] is True
    assert first[2]["mapped"] is False and first[2]["id"] == "1"
    assert lookup.cache_info().hits == len(requests)


def test_cached_results_and_catalog_sources_cannot_be_mutated_by_callers():
    catalog = FakeCatalog()
    lookup = _bounded_id_lookup(catalog)
    first = lookup("hero", 1, 18)
    first["name_zh"] = "changed"
    first["source"][0]["nested"]["lines"].append(999)
    assert lookup("hero", 1, 18)["source"][0]["nested"]["lines"] == [1, 2]
    assert lookup("hero", 1, 18)["name_zh"] == "hero:1:18"
    assert catalog.shared_source[0]["nested"]["lines"] == [1, 2]
    catalog.shared_source[0]["nested"]["lines"].append(3)
    assert lookup("hero", 1, 18)["source"][0]["nested"]["lines"] == [1, 2]


def test_unhashable_invalid_values_are_preserved_and_not_cached():
    catalog = FakeCatalog()
    lookup = _bounded_id_lookup(catalog)
    raw = {"unexpected": [1]}
    first = lookup("hero", raw, 18)
    first["id"]["unexpected"].append(2)
    assert raw == {"unexpected": [1]}
    assert lookup("hero", raw, 18)["id"] == raw
    assert len(catalog.calls) == 2
    assert lookup.cache_info().currsize == 0


def test_lookup_cache_has_a_fixed_bound_and_evicts_old_entries():
    catalog = FakeCatalog()
    lookup = _bounded_id_lookup(catalog, maxsize=2)
    lookup("hero", 1, 18)
    lookup("hero", 2, 18)
    lookup("hero", 3, 18)
    assert lookup.cache_info().currsize == 2
    lookup("hero", 1, 18)
    assert len(catalog.calls) == 4
    lookup.cache_clear()
    assert lookup.cache_info().currsize == 0


def test_factory_reuses_lookup_cache_for_report_context_without_real_catalog(tmp_path, monkeypatch):
    from parser import id_semantics, event_semantics
    catalog = FakeCatalog()
    monkeypatch.setattr(id_semantics, "load_id_catalog", lambda **kwargs: catalog)
    monkeypatch.setattr(event_semantics, "load_event_catalog", lambda **kwargs: {"digest": "event-digest", "source_files": []})
    monkeypatch.setattr(event_semantics, "decode_event", lambda event, events, id_resolver, context:
                        {"hero": id_resolver("hero", event["hero"]), "context": context["scenario_id"]})
    config = tmp_path / "semantics.json"
    config.write_text(json.dumps({"enabled": True, "id_catalog": str(tmp_path / "fake-ids.json"),
                                 "event_catalog": str(tmp_path / "missing-events.json")}), encoding="utf-8")
    build_default_mapper.cache_clear()
    try:
        mapper = build_default_mapper(config)
        context = {"scenario_id": "18"}
        lookup = lambda kind, value: mapper.context_id_lookup(kind, value, context)
        context["id_lookup"] = lookup
        first = mapper.event_decode({"hero": 7}, context)
        first["hero"]["source"][0]["nested"]["lines"].append(9)
        second = mapper.event_decode({"hero": 7}, context)
        assert second["hero"]["source"][0]["nested"]["lines"] == [1, 2]
        assert len(catalog.calls) == 1
        mapper.context_id_lookup("hero", 7, {"scenario_id": "19"})
        mapper.id_lookup("hero", 7)
        assert len(catalog.calls) == 3
        assert mapper.sources["id_catalog_version"] == catalog.version
        assert mapper.sources["modules"]
    finally:
        build_default_mapper.cache_clear()
