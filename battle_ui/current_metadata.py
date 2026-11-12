"""Bounded current-report metadata reads through the JSON-pointer primary key.

Container rows store only {} / []; callers needing a small structured branch
must reconstruct its indexed children instead of loading the original payload.
"""
from __future__ import annotations

from collections import defaultdict
import json
import time

from .data import BattleQueryTimeout


_INDEX = "sqlite_autoindex_report_fields_1"
_NODE_COLUMNS = "pointer,parent_pointer,field_key,ordinal,node_type,value_json"
_DISCOVERY_LEAVES = {"scope": "/scope", "parent": "/parent", "children": "/row/childNum",
                     "combat_children": "/row/combatInfo/childNum"}


def _budget(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise BattleQueryTimeout("战报元数据查询超过时间预算，请缩小账号或日期范围。")


def json_path_pointer(path):
    """Convert the existing simple $.data.field paths (not arbitrary JSONPath)."""
    if not isinstance(path, str) or not path.startswith("$."):
        raise ValueError("Expected a simple $.field JSON path")
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in path[2:].split("."))


def _point(connection, snapshot_id, pointer):
    return connection.execute(
        f"SELECT {_NODE_COLUMNS} FROM report_fields INDEXED BY {_INDEX} WHERE snapshot_id=? AND pointer=?",
        (snapshot_id, pointer)).fetchone()


def read_indexed_subtree(connection, snapshot_id, pointer, *, max_nodes=512, deadline=None):
    """Return (reconstructed value, truncated); never read another branch/blob.

    max_nodes bounds retained rows including the root. A partial tree is marked
    explicitly; a sparse/truncated array is never silently renumbered.
    """
    if (not isinstance(pointer, str) or not pointer.startswith("/")
            or isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 1):
        raise ValueError("Invalid metadata subtree pointer or node bound")
    _budget(deadline)
    root = _point(connection, snapshot_id, pointer)
    if root is None:
        return None, False
    rows = [root]
    truncated = False
    if root["node_type"] in ("object", "array"):
        children = list(connection.execute(
            f"SELECT {_NODE_COLUMNS} FROM report_fields INDEXED BY {_INDEX} "
            "WHERE snapshot_id=? AND pointer>=? AND pointer<? ORDER BY pointer LIMIT ?",
            (snapshot_id, pointer + "/", pointer + "0", max_nodes)))
        truncated = len(children) >= max_nodes
        rows.extend(children[:max_nodes - 1])
    child_map = defaultdict(list)
    for row in rows[1:]:
        child_map[row["parent_pointer"]].append(row)

    def build(row, depth=0):
        nonlocal truncated
        _budget(deadline)
        if depth > 128:
            truncated = True
            return None
        kind = row["node_type"]
        if kind not in ("object", "array"):
            return json.loads(row["value_json"])
        children = sorted(child_map[row["pointer"]], key=lambda item: item["ordinal"])
        if kind == "object":
            return {child["field_key"]: build(child, depth + 1) for child in children}
        indices = []
        for child in children:
            try:
                index = int(child["field_key"])
            except (ValueError, TypeError):
                truncated = True
                return None
            if str(index) != child["field_key"] or index < 0:
                truncated = True
                return None
            indices.append(index)
        if indices != list(range(len(indices))):
            truncated = True
            return None
        return [build(child, depth + 1) for child in children]

    return build(root), truncated


def _discovery_entries(connection, snapshot_id, limit, deadline, root_kind=None):
    """Read top-level observation containers, skipping every large child body."""
    root_pointer = "/discovery"
    root = _point(connection, snapshot_id, root_pointer) if root_kind is None else None
    kind = root["node_type"] if root is not None else root_kind
    if kind not in ("array", "object"):
        return [], False
    entries = []
    if kind == "array":
        index = 0
        while limit is None or len(entries) < limit:
            _budget(deadline)
            node = _point(connection, snapshot_id, root_pointer + "/" + str(index))
            if node is None:
                break
            if node["node_type"] == "object":
                entries.append(node)
            index += 1
    else:
        # Dictionary observations use their original ordinal. Point seeks skip
        # descendant intervals, so a row's combat history is never traversed.
        cursor, upper = root_pointer + "/", root_pointer + "0"
        while True:
            _budget(deadline)
            node = connection.execute(
                f"SELECT {_NODE_COLUMNS} FROM report_fields INDEXED BY {_INDEX} "
                "WHERE snapshot_id=? AND pointer>=? AND pointer<? ORDER BY pointer LIMIT 1",
                (snapshot_id, cursor, upper)).fetchone()
            if node is None:
                break
            if node["parent_pointer"] == root_pointer:
                if node["node_type"] == "object":
                    entries.append(node)
                cursor = node["pointer"] + "\0"
            else:
                # The next node is inside an already visited observation.
                top = root_pointer + "/" + node["pointer"][len(root_pointer) + 1:].split("/", 1)[0]
                cursor = top + "0"
        entries.sort(key=lambda row: row["ordinal"])
        if limit is not None:
            entries = entries[:limit]
    # The caller's 129th object is deliberately retained as proof that the
    # daily view's first 128 observations were truncated.
    return entries, limit is not None and len(entries) >= limit


def _array_discovery(connection, snapshot_id, limit, deadline):
    """Read one observation and next-entry presence in one six-pointer probe."""
    observations, index = [], 0
    while limit is None or len(observations) < limit:
        _budget(deadline)
        pointer = "/discovery/" + str(index)
        following = "/discovery/" + str(index + 1)
        paths = [pointer, *(pointer + suffix for suffix in _DISCOVERY_LEAVES.values()), following]
        rows = {row["pointer"]: row for row in connection.execute(
            f"SELECT pointer,node_type,value_json FROM report_fields INDEXED BY {_INDEX} "
            "WHERE snapshot_id=? AND pointer IN (?,?,?,?,?,?)", (snapshot_id, *paths))}
        entry = rows.get(pointer)
        if entry is None:
            break
        if entry["node_type"] == "object":
            observations.append({"pointer": pointer, "index": str(index),
                **{name: json.loads(rows[pointer + suffix]["value_json"]) if pointer + suffix in rows else None
                   for name, suffix in _DISCOVERY_LEAVES.items()}})
        if following not in rows:
            break
        index += 1
    return observations, limit is not None and len(observations) >= limit


def read_current_metadata(connection, pointers, namespace=None, *, include_discovery=False,
                          discovery_limit=129, deadline=None):
    """Read current scalar nodes and optional small discovery leaves only.

    metadata preserves missing-vs-explicit-null through dictionary membership.
    discovery_limit=None supports consumers whose original view has no cap.
    The supplied connection owns its existing consistent read transaction.
    """
    if discovery_limit is not None and (isinstance(discovery_limit, bool)
            or not isinstance(discovery_limit, int) or discovery_limit < 1):
        raise ValueError("discovery_limit must be positive or None")
    requested_pointers = tuple(dict.fromkeys(pointers))
    if any(not isinstance(pointer, str) or not pointer.startswith("/") for pointer in requested_pointers):
        raise ValueError("Metadata pointers must be absolute JSON pointers")
    pointers = tuple(dict.fromkeys((*requested_pointers, *(["/discovery"] if include_discovery else []))))
    _budget(deadline)
    condition = " WHERE r.namespace=?" if namespace is not None else ""
    params = (namespace,) if namespace is not None else ()
    nodes = [dict(row) for row in connection.execute(
        "SELECT r.namespace,r.battle_uid,r.current_snapshot_id snapshot_id,s.completeness,s.source,s.event_count,s.capture_version "
        "FROM report_records r JOIN report_snapshots s ON s.id=r.current_snapshot_id AND s.report_id=r.id" + condition,
        params)]
    by_id = {}
    for node in nodes:
        node.update(metadata={}, discovery=[], discovery_truncated=False)
        by_id[node["snapshot_id"]] = node
    # Limit each statement's variable count without changing source priority.
    for offset in range(0, len(pointers), 200):
        _budget(deadline)
        chunk = pointers[offset:offset + 200]
        placeholders = ",".join("?" for _ in chunk)
        for row in connection.execute(
            "SELECT r.current_snapshot_id snapshot_id,f.pointer,f.value_json FROM report_records r "
            f"CROSS JOIN report_fields AS f INDEXED BY {_INDEX} "
            "ON f.snapshot_id=r.current_snapshot_id AND f.pointer IN (" + placeholders + ")" + condition,
            (*chunk, *params)):
            _budget(deadline)
            node = by_id.get(row["snapshot_id"])
            if node is not None:
                node["metadata"][row["pointer"]] = json.loads(row["value_json"])
    if include_discovery:
        for node in nodes:
            marker = node["metadata"].get("/discovery")
            kind = "array" if isinstance(marker, list) else "object" if isinstance(marker, dict) else "other"
            if "/discovery" not in requested_pointers:
                node["metadata"].pop("/discovery", None)
            if kind == "array":
                node["discovery"], node["discovery_truncated"] = _array_discovery(
                    connection, node["snapshot_id"], discovery_limit, deadline)
                continue
            entries, truncated = _discovery_entries(connection, node["snapshot_id"], discovery_limit, deadline, kind)
            node["discovery_truncated"] = truncated
            for entry in entries:
                _budget(deadline)
                paths = [entry["pointer"] + suffix for suffix in _DISCOVERY_LEAVES.values()]
                values = {row["pointer"]: json.loads(row["value_json"]) for row in connection.execute(
                    f"SELECT pointer,value_json FROM report_fields INDEXED BY {_INDEX} "
                    "WHERE snapshot_id=? AND pointer IN (?,?,?,?)", (node["snapshot_id"], *paths))}
                node["discovery"].append({"pointer": entry["pointer"], "index": entry["field_key"],
                    **{name: values.get(entry["pointer"] + suffix) for name, suffix in _DISCOVERY_LEAVES.items()}})
    return nodes
