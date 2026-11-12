"""Source-backed Chinese labels for client configuration IDs.

These are additive interpretations. Callers must retain the original report
values. Historical client tables are labelled as such, and never promoted to
current-runtime verification merely because a lookup succeeded.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = ROOT / "capture/output/processed/decompiled_lua/battle_focus"
DEFAULT_CACHE_PATH = ROOT / "data/dictionaries/id_semantics.json"
FORMAT_VERSION = 1
HISTORICAL_DATE = "2026-05-25"

# The table names and name fields come from the client's Data modules. Equipment
# and horse effects explicitly use item_item[id].name in BattleLogParse.
TABLE_SPECS = {
    "heros_heros": ("hero", ("name",), ("biography",)),
    "skills_skills": ("skill", ("name",), ("description", "desc")),
    "skills_feature": ("feature", ("name",), ("description", "desc")),
    "skills_skillType": ("skill_type", ("name",), ("description", "desc")),
    "formation_formation": ("formation", ("name",), ("desc", "description")),
    "buff_buff": ("buff", ("name",), ("description", "desc")),
    "arms_advanced_arms_advanced": ("advanced_arms", ("Arms_name", "name"), ("description", "desc")),
    "arms_advanced_arms_advanced_skill": ("advanced_arms_skill", ("name",), ("description", "desc")),
    "arms_advanced_arms_advanced_effect": ("advanced_arms_effect", ("name",), ("description", "desc")),
    "item_item": ("item", ("name",), ("description", "desc")),
    "warbook_warbook": ("warbook", ("name",), ("description", "desc")),
    "warbook_group": ("warbook_group", ("name",), ("description", "desc")),
    "fightprop_BattleProp": ("property", ("Name", "name"), ("Description", "description")),
    "fightprop_BuffBattleProp": ("buff_property", ("Name", "name"), ("Description", "description")),
    "battle_reports_fightprop": ("report_property", ("fightprop_name",), ("description", "desc")),
    "battle_reports_tags": ("tag", ("tags_name",), ("description", "desc")),
    "appearance_appearance": ("skin", ("name",), ("desc", "description")),
    "appearance_appearance_cn": ("skin", ("name",), ("desc", "description")),
}
SOURCE_PATTERNS = ("*_heros_*.lua", "*_skills_*.lua", "*_formation_*.lua", "*_buff_*.lua",
                   "*_arms_advanced_*.lua", "*_item_*.lua", "*_warbook_*.lua", "*_fightprop_*.lua", "*_battle_reports_*.lua", "*_appearance_*.lua")
KIND_ALIASES = {"heroes": "hero", "skills": "skill", "formations": "formation",
                "equipment": "item", "horse": "item", "arms_advanced": "advanced_arms"}
ENUM_LABELS = {
    "CommonConst.ArmyType": ("arms", {"Peltast": "盾兵", "Archr": "弓兵", "SpearMan": "枪兵", "Cavalry": "骑兵"}),
    "CommonConst.HeroGroupType": ("group", {"WEI": "魏", "SHU": "蜀", "WU": "吴", "QUN": "群", "DEFAULT": "默认阵营", "NONE": "无阵营"}),
}


def _read_lua_string(text: str, start: int) -> tuple[str, int]:
    quote, data, i = text[start], bytearray(), start + 1
    escapes = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11}
    while i < len(text):
        ch = text[i]
        if ch == quote:
            return data.decode("utf-8", "surrogateescape"), i + 1
        if ch != "\\":
            data.extend(ch.encode("utf-8")); i += 1; continue
        i += 1
        if i >= len(text):
            break
        ch = text[i]
        if ch in escapes:
            data.append(escapes[ch]); i += 1
        elif ch.isascii() and ch.isdigit():
            match = re.match(r"[0-9]{1,3}", text[i:])
            number = int(match[0])
            if number > 255:
                raise ValueError("Lua decimal byte escape exceeds 255")
            data.append(number); i += len(match[0])
        elif ch == "x" and re.match(r"[0-9a-fA-F]{2}", text[i + 1:i + 3]):
            data.append(int(text[i + 1:i + 3], 16)); i += 3
        elif ch == "z":
            i += 1
            while i < len(text) and text[i].isspace(): i += 1
        elif ch in "\\\"'":
            data.extend(ch.encode()); i += 1
        elif ch in "\r\n":
            if ch == "\r" and i + 1 < len(text) and text[i + 1] == "\n": i += 1
            data.append(10); i += 1
        else:
            # Do not silently reinterpret an unsupported escape.
            data.extend(("\\" + ch).encode("utf-8")); i += 1
    raise ValueError("Unterminated Lua string")


def _brace_end(text: str, start: int) -> int:
    depth, i = 1, start + 1
    while i < len(text):
        if text[i] in "\"'":
            _, i = _read_lua_string(text, i); continue
        if text.startswith("--", i):
            end = text.find("\n", i); i = len(text) if end < 0 else end + 1; continue
        if text[i] == "{": depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0: return i
        i += 1
    raise ValueError("Unterminated Lua table")


def _scalar_fields(body: str) -> dict[str, Any]:
    """Read only top-level scalar assignments; nested names cannot overwrite them."""
    values, i = {}, 0
    assign = re.compile(r"([A-Za-z_]\w*)\s*=\s*")
    number = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
    while i < len(body):
        if body[i] in "\"'":
            _, i = _read_lua_string(body, i); continue
        if body[i] == "{": i = _brace_end(body, i) + 1; continue
        if body.startswith("--", i):
            end = body.find("\n", i); i = len(body) if end < 0 else end + 1; continue
        match = assign.match(body, i)
        if not match:
            i += 1; continue
        key, i = match[1], match.end()
        if i < len(body) and body[i] in "\"'":
            values[key], i = _read_lua_string(body, i)
        elif found := number.match(body, i):
            raw = found[0]
            values[key] = float(raw) if any(c in raw for c in ".eE") else int(raw)
            i = found.end()
        elif body.startswith("true", i): values[key], i = True, i + 4
        elif body.startswith("false", i): values[key], i = False, i + 5
        elif i < len(body) and body[i] == "{": i = _brace_end(body, i) + 1
    return values


def _records(text: str, translations: bool = False) -> Iterator[tuple[str, str, dict, int]]:
    key = r'\[\s*"([^"\\]+)"\s*\]|\.([A-Za-z_]\w*)' if translations else r"\[\s*(\d+)\s*\]"
    pattern = re.compile(r"(?m)^([A-Za-z_]\w*)(?:" + key + r")\s*=\s*(?:setmetatable\s*\(\s*)?\{")
    newlines = [m.start() for m in re.finditer("\n", text)]
    for match in pattern.finditer(text):
        brace = match.end() - 1
        record_id = next(v for v in match.groups()[1:] if v is not None)
        yield match[1], record_id, _scalar_fields(text[brace + 1:_brace_end(text, brace)]), bisect.bisect_right(newlines, match.start()) + 1


def _source(path: Path, text: str) -> dict:
    match = re.search(r"Scenario(\d+)", text[:1000])
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "chunk": text.splitlines()[0].lstrip("\ufeff") if text else "", "scenario_id": match[1] if match else None,
            "extracted_on": HISTORICAL_DATE, "verification": "historical_source"}


def _label(fields: dict, names: tuple[str, ...], translations: dict) -> tuple[str | None, list[dict]]:
    for name in names:
        value = fields.get(name)
        if isinstance(value, str) and value: return value, []
        hashed = fields.get(name + "_hash")
        if isinstance(hashed, str) and hashed in translations:
            entry = translations[hashed]
            return entry["text"], [entry["source"]]
    return None, []


class IdCatalog:
    def __init__(self, entries: list[dict] | None = None, source_manifest: list[dict] | None = None):
        self.entries = list(entries or [])
        self.source_manifest = list(source_manifest or [])
        self._index: dict[tuple[str, str], list[dict]] = {}
        self._refresh()

    def _refresh(self) -> None:
        self._index = {}
        for entry in self.entries:
            self._index.setdefault((entry["kind"], str(entry["id"])), []).append(entry)
        self.version = hashlib.sha256(json.dumps(self.to_dict(include_version=False), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def to_dict(self, include_version: bool = True) -> dict:
        result = {"format_version": FORMAT_VERSION, "entries": self.entries, "source_manifest": self.source_manifest}
        if include_version: result["version"] = self.version
        return result

    def lookup(self, kind: str, value: Any, scenario_id: Any = None) -> dict:
        result = {"kind": kind, "id": value, "name_zh": None, "mapped": False, "status": "unmapped", "source": [], "sources": [], "verification": "unmapped"}
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            result["status"] = "invalid_id"; return result
        key = str(value)
        if key.isascii() and key.isdigit(): key = str(int(key))
        candidates = self._index.get((KIND_ALIASES.get(kind, kind), key), [])
        scenario = None if scenario_id is None else str(scenario_id)
        runtime = [e for e in candidates if e.get("verification") == "current_runtime"
                   and (scenario is None or e.get("scenario_id") is None or str(e.get("scenario_id")) == scenario)]
        historical = [e for e in candidates if e.get("verification") != "current_runtime"]
        selected = runtime or historical
        if scenario is not None:
            exact = [e for e in selected if str(e.get("scenario_id")) == scenario]
            global_entries = [e for e in selected if e.get("scenario_id") is None]
            selected = exact or global_entries or selected
        names = {e.get("name_zh") for e in selected if e.get("name_zh")}
        if not names: return result
        if len(names) != 1:
            result.update(status="ambiguous_scenario", candidates=sorted(names)); return result
        name = next(iter(names))
        selected = [e for e in selected if e.get("name_zh") == name]
        source = [s for e in selected for s in e.get("source", [])]
        # Keep provenance stable while avoiding duplicate translation records.
        source = list({json.dumps(s, sort_keys=True, ensure_ascii=True): s for s in source}.values())
        descriptions = {e.get("description_zh") for e in selected if e.get("description_zh")}
        verification = "current_runtime" if runtime else "historical_source"
        result.update(mapped=True, name_zh=name, status="mapped", source=source, sources=source,
                      verification=verification, scenario_id=scenario,
                      description_zh=next(iter(descriptions)) if len(descriptions) == 1 else None)
        return result

    def merge_runtime(self, document: dict, source_path: str | Path = "<runtime>") -> "IdCatalog":
        payload = document.get("payload", document)
        if not isinstance(payload, dict): raise ValueError("Runtime dictionary payload must be an object")
        source_path = str(source_path)
        canonical = json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
        file = Path(source_path)
        raw = file.read_bytes() if file.is_file() else canonical
        manifest = {"path": source_path, "sha256": hashlib.sha256(raw).hexdigest(),
                    "digest_scope": "file_bytes" if file.is_file() else "canonical_document", "verification": "current_runtime",
                    "captured_at": payload.get("capturedAt", payload.get("captured_at")), "runtime_identity": payload.get("runtimeIdentity", payload.get("identity"))}
        identity = manifest["runtime_identity"] or {}
        scenario = identity.get("scenarioId") if isinstance(identity, dict) else None
        tables = payload.get("tables", payload.get("entries", {}))
        if not isinstance(tables, dict): raise ValueError("Runtime tables must be an object")
        # Replacing a previous runtime snapshot must not create false ambiguity.
        self.entries = [e for e in self.entries if e.get("verification") != "current_runtime"]
        self.source_manifest = [s for s in self.source_manifest if s.get("verification") != "current_runtime"]
        self.source_manifest.append(manifest)
        for kind, records in tables.items():
            canonical = KIND_ALIASES.get(kind, kind)
            if kind in TABLE_SPECS: canonical = TABLE_SPECS[kind][0]
            iterable = records.items() if isinstance(records, dict) else enumerate(records) if isinstance(records, list) else ()
            for key, record in iterable:
                if not isinstance(record, dict): continue
                value = record.get("id", record.get("ID", key))
                name = record.get("name_zh") or record.get("name") or record.get("Arms_name") or record.get("Name") or record.get("fightprop_name")
                if not isinstance(name, str) or not name: continue
                self.entries.append({"kind": canonical, "id": str(value), "name_zh": name,
                    "description_zh": record.get("description_zh", record.get("description", record.get("desc"))),
                    "scenario_id": str(scenario) if scenario is not None else None, "verification": "current_runtime",
                    "source": [{**manifest, "table": kind, "record_id": str(value), "field": "name"}]})
        enums = payload.get("enums", {})
        for table, (kind, labels) in ENUM_LABELS.items():
            values = enums.get(kind, enums.get(table, {})) if isinstance(enums, dict) else {}
            if not isinstance(values, dict): continue
            for symbol, value in values.items():
                if symbol in labels and isinstance(value, int) and not isinstance(value, bool):
                    self.entries.append({"kind": kind, "id": str(value), "name_zh": labels[symbol], "symbol": symbol,
                        "scenario_id": str(scenario) if scenario is not None else None, "verification": "current_runtime",
                        "source": [{**manifest, "table": table, "symbol": symbol}]})
        self._refresh()
        return self


def build_id_catalog(source_dir: str | Path = DEFAULT_SOURCE_DIR) -> IdCatalog:
    source_dir = Path(source_dir)
    translations, manifests = {}, {}
    for path in sorted(source_dir.glob("*_translate_*.lua")):
        text = path.read_text(encoding="utf-8-sig")
        src = _source(path, text); manifests[str(path.resolve())] = src
        for table, key, fields, line in _records(text, translations=True):
            if table in {"translate_prefab", "translate_excel_design", "translate_extra"} and isinstance(fields.get("message_cn"), str):
                translations[key] = {"text": fields["message_cn"], "source": {**src, "line": line, "table": table, "record_id": key, "field": "message_cn"}}
    entries = []
    paths = sorted({path for pattern in SOURCE_PATTERNS for path in source_dir.glob(pattern)})
    for path in paths:
        text = path.read_text(encoding="utf-8-sig")
        src = _source(path, text)
        used = False
        for table, value, fields, line in _records(text):
            if table not in TABLE_SPECS: continue
            kind, names, descriptions = TABLE_SPECS[table]
            name, name_sources = _label(fields, names, translations)
            if not name: continue
            description, description_sources = _label(fields, descriptions, translations)
            entries.append({"kind": kind, "id": value, "name_zh": name, "description_zh": description,
                "scenario_id": src["scenario_id"], "verification": "historical_source",
                "source": [{**src, "line": line, "table": table, "record_id": value, "field": names[0]}] + name_sources + description_sources})
            if table == "battle_reports_tags" and isinstance(fields.get("buff_tags"), str) and fields["buff_tags"]:
                entries.append({"kind": "state", "id": fields["buff_tags"], "name_zh": name,
                    "scenario_id": src["scenario_id"], "verification": "historical_source",
                    "source": [{**src, "line": line, "table": table, "record_id": value, "field": "buff_tags"}] + name_sources})
            used = True
        if used: manifests[str(path.resolve())] = src
    for path in sorted(source_dir.glob("*_CommonConst_*.lua")):
        text = path.read_text(encoding="utf-8-sig"); src = _source(path, text)
        for table, (kind, labels) in ENUM_LABELS.items():
            match = re.search(re.escape(table) + r"\s*=\s*\{", text)
            if not match: continue
            brace = match.end() - 1
            values = _scalar_fields(text[brace + 1:_brace_end(text, brace)])
            for symbol, value in values.items():
                if symbol not in labels or not isinstance(value, int): continue
                entries.append({"kind": kind, "id": str(value), "name_zh": labels[symbol], "symbol": symbol,
                    "scenario_id": None, "verification": "historical_source",
                    "source": [{**src, "line": text.count("\n", 0, match.start()) + 1, "table": table, "symbol": symbol}]})
                manifests[str(path.resolve())] = src
    return IdCatalog(entries, list(manifests.values()))


def save_id_catalog(catalog: IdCatalog, path: str | Path = DEFAULT_CACHE_PATH) -> Path:
    from capture.game_assets.export_battle_report_runtime import atomic_json
    path = Path(path)
    atomic_json(path, catalog.to_dict())
    return path


@lru_cache(maxsize=8)
def load_id_catalog(source_dir: str | Path = DEFAULT_SOURCE_DIR,
                    cache_path: str | Path | None = DEFAULT_CACHE_PATH,
                    runtime_path: str | Path | None = None) -> IdCatalog:
    if cache_path is not None and Path(cache_path).is_file():
        document = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        if document.get("format_version") != FORMAT_VERSION: raise ValueError("Unsupported ID dictionary format")
        catalog = IdCatalog(document.get("entries", []), document.get("source_manifest", []))
        if document.get("version") and document["version"] != catalog.version: raise ValueError("ID dictionary content hash mismatch")
    else:
        catalog = build_id_catalog(source_dir)
    if runtime_path is not None:
        document = json.loads(Path(runtime_path).read_text(encoding="utf-8"))
        catalog.merge_runtime(document, source_path=runtime_path)
    return catalog
