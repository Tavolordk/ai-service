"""Indexación incremental de JSON gigantes en SQLite/FTS5 y adyacencias.

Sólo mantiene en RAM el lote de escritura, la pila de profundidad JSON y
un nodo/vínculo de grafo a la vez. No construye un dict del archivo completo.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

try:
    import ijson
except ImportError:  # La imagen Docker instala ijson; permite probar helpers sin red.
    ijson = None

SCALAR_EVENTS = {"string", "number", "boolean", "null"}
_ARRAY_ENTRY = re.compile(r"(?:^|\.)(?:links|edges|vinculos|vínculos)\[\d+\]$", re.I)
_RECORD_ENTRY = re.compile(r"(?:^|\.)(data|vehicles|addresses)\[\d+\]$", re.I)
_NODE_ENTRY = re.compile(r"(?:^|\.)(?:nodes|nodos)\[\d+\]$", re.I)
_ENDPOINT_SOURCE = {"source", "sourceid", "from", "fromid", "src", "sourcelinkedid", "source_linked_id", "origin", "origen"}
_ENDPOINT_TARGET = {"target", "targetid", "to", "toid", "dst", "targetlinkedid", "target_linked_id", "destination", "destino"}
_EDGE_TYPE = {"type", "relation", "relationship", "label", "tipo_vinculo", "tipovinculo"}


def _to_text(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass(slots=True)
class _Frame:
    kind: str
    path: str
    next_index: int = 0
    key: str = ""


def _child_path(stack: list[_Frame]) -> str:
    if not stack:
        return "$"
    parent = stack[-1]
    if parent.kind == "array":
        path = f"{parent.path}[{parent.next_index}]"
        parent.next_index += 1
        return path
    return f"{parent.path}.{parent.key}"


def concrete_events(events: Iterator[tuple[str, str, Any]]) -> Iterator[tuple[str, str, Any]]:
    """Transforma prefijos ijson con `.item` en rutas reales `[n]`."""
    stack: list[_Frame] = []
    for _prefix, event, value in events:
        if event == "map_key":
            if stack:
                stack[-1].key = str(value)
        elif event in ("start_map", "start_array"):
            path = _child_path(stack)
            stack.append(_Frame("array" if event == "start_array" else "map", path))
            yield path, event, value
        elif event in ("end_map", "end_array"):
            if stack:
                yield stack[-1].path, event, value
                stack.pop()
        elif event in SCALAR_EVENTS:
            yield _child_path(stack), event, value


def _field(path: str, base: str) -> str:
    return path[len(base) + 1:].lower().replace("_", "") if path.startswith(base + ".") else ""


def _set_member(target: dict[str, str], relative: str, value: str, *, kind: str) -> None:
    if len(target) > 16 or len(value) > 512:
        return
    field = relative.split(".")[0]
    leaf = relative.split(".")[-1]
    if kind == "link":
        if field in {x.replace("_", "") for x in _ENDPOINT_SOURCE} and leaf in (field, "id", "nodeid"):
            target.setdefault("source", value)
        elif field in {x.replace("_", "") for x in _ENDPOINT_TARGET} and leaf in (field, "id", "nodeid"):
            target.setdefault("target", value)
        elif field in {x.replace("_", "") for x in _EDGE_TYPE}:
            target.setdefault("relation", value)
    elif kind == "node":
        if field in ("id", "nodeid", "key"):
            target.setdefault("id", value)
        elif field in ("title", "label", "name", "nombre"):
            target.setdefault("label", value)
        elif field in ("type", "kind", "tipo"):
            target.setdefault("type", value)


def _create_schema(conn: sqlite3.Connection, *, max_value_chars: int) -> None:
    conn.execute("PRAGMA journal_mode=DELETE")  # Sin sidecars: swap atómico del directorio.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32768")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    # No degradar silenciosamente a LIKE sobre millones de registros.
    conn.execute("CREATE VIRTUAL TABLE facts USING fts5(path UNINDEXED, value, tokenize='unicode61 remove_diacritics 2')")
    conn.execute("CREATE TABLE graph_edges (edge_id INTEGER PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL, relation TEXT NOT NULL, path TEXT NOT NULL)")
    conn.execute("CREATE TABLE graph_adj (node_id TEXT NOT NULL, edge_id INTEGER NOT NULL)")
    conn.execute("CREATE TABLE graph_nodes (node_id TEXT PRIMARY KEY, label TEXT NOT NULL, node_type TEXT NOT NULL, path TEXT NOT NULL)")
    conn.execute("CREATE TABLE case_fields (kind TEXT NOT NULL, attribute TEXT NOT NULL, subject TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL, reference TEXT NOT NULL, observed_at TEXT NOT NULL, path TEXT NOT NULL)")


def index_json_to_sqlite(source: Path, database: Path, *, batch_size: int = 5000,
                         max_value_chars: int = 1024) -> int:
    """Crea base de datos nueva. Publicar sólo al terminar indexación válida.

    El conteo incluye todos los escalares; los valores excesivamente largos
    se truncan SOLO en el índice, nunca en el JSON de origen.
    """
    if ijson is None:
        raise RuntimeError("ijson no está instalado (requerido para JSON grandes)")
    database.parent.mkdir(parents=True, exist_ok=True)
    database.unlink(missing_ok=True)
    conn = sqlite3.connect(database)
    total = 0
    edges_count = 0
    nodes_count = 0
    clipped = 0
    facts: list[tuple[str, str]] = []
    edges: list[tuple[int, str, str, str, str]] = []
    adjacency: list[tuple[str, int]] = []
    nodes: list[tuple[str, str, str, str]] = []
    fields: list[tuple[str, str, str, str, str, str, str, str]] = []
    active_record: tuple[str, str, dict[str, str]] | None = None
    active_link: tuple[str, dict[str, str]] | None = None
    active_node: tuple[str, dict[str, str]] | None = None

    def flush() -> None:
        if facts:
            conn.executemany("INSERT INTO facts(path,value) VALUES(?,?)", facts)
            facts.clear()
        if edges:
            conn.executemany("INSERT INTO graph_edges(edge_id,source,target,relation,path) VALUES(?,?,?,?,?)", edges)
            edges.clear()
        if adjacency:
            conn.executemany("INSERT INTO graph_adj(node_id,edge_id) VALUES(?,?)", adjacency)
            adjacency.clear()
        if nodes:
            conn.executemany("INSERT OR REPLACE INTO graph_nodes(node_id,label,node_type,path) VALUES(?,?,?,?)", nodes)
            nodes.clear()
        if fields:
            conn.executemany("INSERT INTO case_fields(kind,attribute,subject,value,source,reference,observed_at,path) VALUES(?,?,?,?,?,?,?,?)", fields)
            fields.clear()

    try:
        _create_schema(conn, max_value_chars=max_value_chars)
        with source.open("rb") as handle:
            for path, event, value in concrete_events(ijson.parse(handle)):
                if event == "start_map":
                    if _ARRAY_ENTRY.search(path):
                        active_link = (path, {})
                    elif _NODE_ENTRY.search(path):
                        active_node = (path, {})
                    else:
                        match = _RECORD_ENTRY.search(path)
                        if match:
                            active_record = (path, match.group(1).lower(), {})
                elif event == "end_map":
                    if active_link is not None and path == active_link[0]:
                        obj = active_link[1]
                        if obj.get("source") and obj.get("target"):
                            edges_count += 1
                            edges.append((edges_count, obj["source"], obj["target"], obj.get("relation", "SIN TIPO"), path))
                            adjacency.append((obj["source"], edges_count))
                            if obj["target"] != obj["source"]:
                                adjacency.append((obj["target"], edges_count))
                        active_link = None
                    if active_record is not None and path == active_record[0]:
                        base, kind, record = active_record
                        origin = record.get("sourcecode") or record.get("source") or "sin fuente explícita"
                        reference = record.get("recordreference", "")
                        observed = record.get("date", "")
                        if kind == "data" and record.get("value") and (record.get("datatype") or record.get("code")):
                            fields.append(("data", (record.get("datatype") or record.get("code") or "").upper(),
                                           base.rsplit(".data[", 1)[0], record["value"], origin, reference, observed, base))
                        elif kind == "vehicles" and record.get("vin") and record.get("plate"):
                            fields.append(("vehicle", "PLACA", record["vin"].upper(), record["plate"], origin, reference, observed, base))
                        active_record = None
                    if active_node is not None and path == active_node[0]:
                        obj = active_node[1]
                        if obj.get("id"):
                            nodes.append((obj["id"], obj.get("label", obj["id"]), obj.get("type", ""), path))
                            nodes_count += 1
                        active_node = None
                elif event in SCALAR_EVENTS:
                    text = _to_text(value)
                    if active_link is not None:
                        _set_member(active_link[1], _field(path, active_link[0]), text, kind="link")
                    if active_node is not None:
                        _set_member(active_node[1], _field(path, active_node[0]), text, kind="node")
                    if active_record is not None:
                        relative = _field(path, active_record[0])
                        record = active_record[2]
                        if relative in ("datatype", "code", "value", "vin", "niv", "licenseplate", "placa"):
                            key = "vin" if relative == "niv" else "plate" if relative in ("licenseplate", "placa") else relative
                            record[key] = text[:max_value_chars]
                        elif "origins[" in relative:
                            if relative.endswith((".sourcecode", ".source")):
                                record.setdefault("sourcecode", text[:120])
                            elif relative.endswith(".recordreference"):
                                record.setdefault("recordreference", text[:120])
                            elif relative.endswith(".date"):
                                record.setdefault("date", text[:40])
                    if len(text) > max_value_chars:
                        clipped += 1
                    facts.append((path, text[:max_value_chars]))
                    total += 1
                if len(facts) >= batch_size or len(edges) >= batch_size or len(adjacency) >= batch_size or len(nodes) >= batch_size or len(fields) >= batch_size:
                    flush()
        flush()
        # CREATE INDEX al FINAL: para millones de links reduce costo de inserción.
        conn.execute("CREATE INDEX idx_adj_node_edge ON graph_adj(node_id,edge_id)")
        conn.execute("CREATE INDEX idx_fields_attr_subj ON case_fields(attribute,subject)")
        conn.execute("CREATE TABLE graph_degree (node_id TEXT PRIMARY KEY, degree INTEGER NOT NULL)")
        conn.execute("INSERT INTO graph_degree SELECT node_id, count(*) FROM graph_adj GROUP BY node_id")
        for key, val in {"fts": "1", "facts": str(total), "edges": str(edges_count),
                         "nodes": str(nodes_count), "clippedFacts": str(clipped), "schema": "2"}.items():
            conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", (key, val))
        conn.commit()
        return total
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_meta(database: Path) -> dict[str, str]:
    if not database.exists():
        return {}
    with sqlite3.connect(database) as conn:
        return dict(conn.execute("SELECT key,value FROM meta"))
