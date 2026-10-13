"""Almacén persistente para grandes investigaciones: carga streaming, SQLite y grafo indexado."""
from __future__ import annotations

import asyncio
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from .config import Settings
from .json_stream import index_json_to_sqlite, read_meta
from .models import CaseMetadata

_SAFE_CASE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
_WORD = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9_-]{2,}")
_STOP = frozenset({"el", "la", "los", "las", "de", "del", "que", "son", "para", "por", "con", "entre", "sobre",
                   "una", "uno", "unos", "y", "o", "un", "sus", "se", "es", "en", "al", "cual", "cuantos", "cuantas",
                   "analiza", "analizar", "explica", "informacion", "perfil", "datos", "dame", "muestra", "nodo",
                   "nodos", "grafo", "relaciones", "vinculos", "vinculo", "vínculos", "vínculo", "comparar", "comparacion"})


def _safe_case_id(value: str) -> str:
    if not _SAFE_CASE.fullmatch(value) or value in (".", "..") or value.startswith("."):
        raise HTTPException(400, "caseId inválido: sólo letras, números, guion, punto y guion bajo")
    return value


def _terms(question: str) -> list[str]:
    tokens = []
    for term in _WORD.findall(question.casefold()):
        if term not in _STOP and (len(term) >= 3 or any(x.isdigit() for x in term)) and term not in tokens:
            tokens.append(term)
    # Identificadores, placas y nombres específicos tienen prioridad.
    tokens.sort(key=lambda t: (not any(c.isdigit() for c in t), -len(t)))
    return tokens[:5]


def _fts_query(question: str) -> str:
    terms = _terms(question)
    # OR preserva registros pertinentes aunque estén dispersos; nunca incluye texto del usuario sin comillas.
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


class CaseStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = Path(settings.case_data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._swap_lock = threading.RLock()

    def _paths(self, case_id: str) -> tuple[str, Path, Path, Path]:
        safe = _safe_case_id(case_id)
        folder = self.root / safe
        return safe, folder / "source.json", folder / "case.db", folder / "metadata.json"

    async def ingest(self, case_id: str, request: Request) -> CaseMetadata:
        """Indexa primero en carpeta aislada; conserva la versión anterior ante cualquier error."""
        safe, source, database, metadata_file = self._paths(case_id)
        # Evita mantener el JSON completo en RAM y respeta límite aun sin Content-Length.
        stage = Path(tempfile.mkdtemp(prefix=f".{safe}.stage-", dir=self.root))
        received = 0
        try:
            staging_source = stage / "source.json"
            with staging_source.open("wb") as handle:
                async for piece in request.stream():
                    received += len(piece)
                    if received > self.settings.max_case_bytes:
                        raise HTTPException(413, f"Límite de archivo AI_MAX_CASE_BYTES={self.settings.max_case_bytes}")
                    handle.write(piece)
            if not received:
                raise HTTPException(400, "El JSON está vacío")
            staging_database = stage / "case.db"
            try:
                facts = await asyncio.to_thread(
                    index_json_to_sqlite, staging_source, staging_database,
                    max_value_chars=self.settings.case_index_value_chars,
                )
            except OSError as exc:
                raise HTTPException(507, f"Error de almacenamiento al indexar: {exc}") from exc
            except Exception as exc:
                # No tocar el caso antiguo. ijson lanza excepción en JSON corrupto.
                raise HTTPException(400, f"No se pudo indexar el JSON: {type(exc).__name__}: {exc}") from exc
            indexed = read_meta(staging_database)
            metadata = CaseMetadata(
                caseId=safe, bytes=received, facts=facts,
                indexedAt=datetime.now(timezone.utc).isoformat(),
                sourceFile=str(source), databaseFile=str(database),
                links=int(indexed.get("edges", "0")), nodes=int(indexed.get("nodes", "0")),
                clippedFacts=int(indexed.get("clippedFacts", "0")), indexSchema=2,
            )
            (stage / "metadata.json").write_text(metadata.model_dump_json(indent=2), encoding="utf-8")
            previous = self.root / f".{safe}.backup-{uuid.uuid4().hex}"
            with self._swap_lock:
                had_previous = source.parent.is_dir()
                if had_previous:
                    source.parent.rename(previous)
                try:
                    stage.rename(source.parent)
                except Exception:
                    if had_previous:
                        previous.rename(source.parent)
                    raise
            if previous.exists():
                await asyncio.to_thread(shutil.rmtree, previous, True)
            return metadata
        finally:
            if stage.exists():
                await asyncio.to_thread(shutil.rmtree, stage, True)

    def metadata(self, case_id: str) -> CaseMetadata:
        _, _, _, meta_path = self._paths(case_id)
        with self._swap_lock:
            if not meta_path.is_file():
                raise HTTPException(404, "Caso no encontrado")
            return CaseMetadata.model_validate_json(meta_path.read_text(encoding="utf-8"))

    def delete(self, case_id: str) -> None:
        _, source, _, meta_file = self._paths(case_id)
        with self._swap_lock:
            if not meta_file.is_file():
                raise HTTPException(404, "Caso no encontrado")
            backup = self.root / f".{source.parent.name}.deleted-{uuid.uuid4().hex}"
            source.parent.rename(backup)
        shutil.rmtree(backup)

    def _database(self, case_id: str) -> Path:
        _, _, db, meta = self._paths(case_id)
        if not meta.is_file() or not db.is_file():
            raise HTTPException(404, "Caso no encontrado")
        return db

    def search(self, case_id: str, question: str, *, limit: int) -> list[dict[str, Any]]:
        """Búsqueda FTS5 indexada; no permite un barrido completo por pregunta vacía."""
        limit = max(1, min(limit, self.settings.case_search_max_results))
        match = _fts_query(question)
        if not match:
            return []
        with self._swap_lock:
            db = self._database(case_id)
            conn = sqlite3.connect(db, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            rows = conn.execute(
                "SELECT path,value,bm25(facts) AS score FROM facts WHERE facts MATCH ? ORDER BY score LIMIT ?",
                (match, limit),
            ).fetchall()
            return [{"path": row["path"], "value": row["value"], "score": float(row["score"] or 0)}
                    for row in rows]
        except sqlite3.OperationalError as exc:
            raise HTTPException(503, f"Índice FTS no disponible: {exc}") from exc
        finally:
            conn.close()

    def attribute_evidence(self, case_id: str, question: str, *, limit: int = 30) -> tuple[list[dict[str, Any]], bool]:
        """Datos y fuentes agrupados: evita devolver sólo el nombre del campo FTS."""
        q = question.casefold()
        labels = []
        if "curp" in q:
            labels.append("CURP")
        if "rfc" in q:
            labels.append("RFC")
        if "cuip" in q:
            labels.append("CUIP")
        if "nacimiento" in q:
            labels.extend(["FECHA DE NACIMIENTO", "FECHA_NACIMIENTO", "NACIMIENTO"])
        if "placa" in q:
            labels.append("PLACA")
        if not labels:
            return [], True
        limit = min(max(1, limit), self.settings.case_search_max_results)
        with self._swap_lock:
            db = self._database(case_id)
            con = sqlite3.connect(db)
        try:
            if read_meta(db).get("schema") != "2":
                return [], True
            placeholders = ",".join("?" for _ in labels)
            rows = con.execute(
                f"SELECT attribute,subject,value,source,reference,observed_at,path FROM case_fields "
                f"WHERE attribute IN ({placeholders}) ORDER BY attribute,subject LIMIT ?",
                (*labels, limit + 1),
            ).fetchall()
            evidence = []
            for attr,subject,value,source,reference,observed,path in rows[:limit]:
                evidence.append({"path":path, "section":"data", "score":100,
                                 "sourceCodes":[source],
                                 "value":f"{attr} ({subject}) = {value}; fuente: {source}"
                                         + (f"; ref: {reference}" if reference else "")
                                         + (f"; fecha: {observed}" if observed else "")})
            return evidence, len(rows) <= limit
        finally:
            con.close()

    def documentary_differences(self, case_id: str, question: str, *, limit: int = 5):
        """Cotejo indexado de atributos solicitados; NUNCA compara vehículos sin mismo VIN."""
        from .documentary_compare import DocumentValue, DocumentaryDifference, _same_value
        q = question.casefold()
        labels = []
        if any(t in q for t in ("nacimiento", "fecha", "edad")):
            labels.extend(["FECHA DE NACIMIENTO", "FECHA_NACIMIENTO", "NACIMIENTO", "DOB", "BIRTHDATE"])
        if any(t in q for t in ("placa", "vin", "niv", "vehículo", "vehiculo")):
            labels.append("PLACA")
        if "curp" in q:
            labels.append("CURP")
        if "rfc" in q:
            labels.append("RFC")
        if not labels:
            return []
        placeholders = ",".join("?" for _ in labels)
        with self._swap_lock:
            db = self._database(case_id)
            conn = sqlite3.connect(db, timeout=10)
        try:
            if read_meta(db).get("schema") != "2":
                return []
            # El índice (attribute,subject) evita revisar millones de vínculos.
            rows = conn.execute(
                f"SELECT kind,attribute,subject,value,source,reference,observed_at,path FROM case_fields "
                f"WHERE attribute IN ({placeholders}) ORDER BY attribute,subject LIMIT 10001", labels,
            ).fetchall()
            if len(rows) > 10000:
                raise HTTPException(422, "Más de 10000 registros para comparar: seleccione una entidad o atributo más específico; no es una comparación exhaustiva")
            groups: dict[tuple[str,str,str], list] = {}
            for kind, attr, subject, value, source, reference, observed, path in rows:
                groups.setdefault((kind,attr,subject), []).append(DocumentValue(
                    value=value, source=source, reference=reference, observed_at=observed, path=path))
            out = []
            for (kind,attr,subject), values in groups.items():
                if len({_same_value(r.value) for r in values}) > 1:
                    out.append(DocumentaryDifference(label=attr, subject=subject, values=tuple(values[:8])))
                    if len(out) >= limit:
                        break
            return out
        finally:
            conn.close()

    def links(self, case_id: str, node_id: str, *, limit: int = 30, offset: int = 0,
              after_edge_id: int | None = None) -> dict[str, Any]:
        """Consulta adyacencias exactas O(log N + resultados) mediante índices source/target."""
        node_id = node_id.strip()
        if not node_id or len(node_id) > 256:
            raise HTTPException(400, "nodeId requerido (máximo 256 caracteres)")
        limit = max(1, min(limit, self.settings.case_search_max_results))
        offset = max(0, min(offset, 1_000_000_000))
        if after_edge_id is not None and after_edge_id < 0:
            raise HTTPException(400, "afterEdgeId debe ser >= 0")
        with self._swap_lock:
            db = self._database(case_id)
            conn = sqlite3.connect(db, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            if read_meta(db).get("schema") != "2":
                raise HTTPException(409, "Índice de grafo no disponible: vuelve a cargar el caso para indexarlo")
            count_row = conn.execute("SELECT degree FROM graph_degree WHERE node_id=?", (node_id,)).fetchone()
            count = int(count_row[0]) if count_row else 0
            # Paginación por cursor evita OFFSET profundo O(offset) sobre millones.
            continuation = (" AND adj.edge_id > ?" if after_edge_id is not None else "")
            sql = """
                SELECT adj.edge_id,e.path,e.source,e.target,e.relation,
                       COALESCE(a.label,e.source) AS source_label,
                       COALESCE(b.label,e.target) AS target_label
                FROM graph_adj adj
                JOIN graph_edges e ON e.edge_id=adj.edge_id
                LEFT JOIN graph_nodes a ON a.node_id=e.source
                LEFT JOIN graph_nodes b ON b.node_id=e.target
                WHERE adj.node_id=?
                """ + continuation + " ORDER BY adj.edge_id LIMIT ? OFFSET ?"
            params = ([node_id, after_edge_id, limit, 0] if after_edge_id is not None
                      else [node_id, limit, offset])
            rows = conn.execute(sql, params).fetchall()
            items = [{"path": row["path"], "source": row["source"], "target": row["target"],
                      "relation": row["relation"], "sourceLabel": row["source_label"],
                      "targetLabel": row["target_label"]} for row in rows]
            last_id = int(rows[-1]["edge_id"]) if rows else None
            more = (bool(rows) and bool(conn.execute(
                "SELECT 1 FROM graph_adj WHERE node_id=? AND edge_id>? LIMIT 1", (node_id, last_id)
            ).fetchone())) if after_edge_id is not None else offset + len(items) < count
            return {"caseId": case_id, "nodeId": node_id, "total": count, "limit": limit,
                    "offset": offset, "links": items, "hasMore": more,
                    "nextEdgeId": last_id if more else None}
        except sqlite3.OperationalError as exc:
            raise HTTPException(503, f"Índice de relaciones no disponible: {exc}") from exc
        finally:
            conn.close()
