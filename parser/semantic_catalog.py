"""Load source-backed dictionaries once per process for derived Chinese meanings."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config/semantics.json"
ID_LOOKUP_CACHE_SIZE = 8192


def _path(value: str, base: Path = ROOT) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def _bounded_id_lookup(catalog, maxsize: int = ID_LOOKUP_CACHE_SIZE):
    """Reuse immutable lookup work without sharing mutable annotation results.

    Types are part of the key because bool/int and numeric/string IDs may have
    different validation outcomes or original JSON representations. Unsupported
    unhashable inputs still reach the catalog's normal validation path.
    """
    @lru_cache(maxsize=maxsize)
    def cached(kind, value_type, value, scenario_type, scenario_id):
        # Decouple the cached value from mutable objects owned by a catalog.
        return deepcopy(catalog.lookup(kind, value, scenario_id=scenario_id))

    def lookup(kind, value, scenario_id=None):
        key = (kind, type(value), value, type(scenario_id), scenario_id)
        try:
            hash(key)
        except TypeError:
            return deepcopy(catalog.lookup(kind, value, scenario_id=scenario_id))
        # Sources, descriptions and raw invalid values may be nested. A shallow
        # copy would let an annotation consumer corrupt later cache hits.
        return deepcopy(cached(*key))

    lookup.cache_info = cached.cache_info
    lookup.cache_clear = cached.cache_clear
    return lookup


@lru_cache(maxsize=4)
def build_default_mapper(config_path: str | Path = DEFAULT_CONFIG):
    config_path = Path(config_path)
    if not config_path.is_file():
        return None
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not config.get("enabled", True):
        return None
    from .id_semantics import load_id_catalog
    from .event_semantics import load_event_catalog, decode_event
    from .report_semantics import SemanticMapper

    ids = load_id_catalog(cache_path=_path(config["id_catalog"]))
    event_path = _path(config["event_catalog"])
    runtime_path = _path(config["runtime_capture"]) if config.get("runtime_capture") else None
    runtime = None
    runtime_hash = None
    if runtime_path:
        raw = runtime_path.read_bytes()
        runtime_hash = hashlib.sha256(raw).hexdigest()
        document = json.loads(raw)
        if document.get("serializationIssues"):
            raise ValueError("Runtime dictionary capture has serialization issues")
        payload = document.get("payload", document)
        runtime = dict(payload["event_catalog"])
        runtime["source"] = {"path": str(runtime_path), "sha256": runtime_hash,
                             "captured_at": payload.get("capturedAt"),
                             "runtime_identity": payload.get("runtimeIdentity")}
    events = None
    if event_path.is_file():
        cached = json.loads(event_path.read_text(encoding="utf-8"))
        digest = cached.get("digest")
        body = {key: value for key, value in cached.items() if key != "digest"}
        if digest != hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest():
            raise ValueError("Event dictionary content hash mismatch")
        if runtime_hash is None or runtime_hash in json.dumps(cached, ensure_ascii=True):
            events = cached
    if events is None:
        events = load_event_catalog(runtime_catalog=runtime)
    module_paths = [Path(__file__), ROOT / "parser/id_semantics.py",
                    ROOT / "parser/event_semantics.py", ROOT / "parser/report_semantics.py"]
    sources = {"id_catalog_version": ids.version, "id_sources": ids.source_manifest,
               "event_catalog_digest": events["digest"], "event_sources": events["source_files"],
               "runtime_capture": runtime.get("source") if runtime else None,
               "modules": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in module_paths}}
    version = hashlib.sha256(json.dumps(sources, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    lookup = _bounded_id_lookup(ids)
    return SemanticMapper(
        version=version, sources=sources,
        id_lookup=lookup,
        context_id_lookup=lambda kind, value, context: lookup(kind, value, scenario_id=context.get("scenario_id")),
        event_decode=lambda event, context: decode_event(event, events, id_resolver=context["id_lookup"], context=context),
    )
