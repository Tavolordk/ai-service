from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from .config import Settings
from .json_stream import index_json_to_sqlite
from .models import CaseMetadata

_SAFE_CASE = re.compile(r"[^A-Za-z0-9_.-]+")
_TOKEN = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9_-]{2,}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_case_id(value: str) -> str:
    cleaned = _SAFE_CASE.sub("_", value.strip())[:120]
    if not cleaned:
        raise HTTPException(400, "caseId inválido")
    return cleaned


def _fts_query(question: str) -> str:
    seen: set[str] = set()
    terms: list[str] = []
    for token in _TOKEN.findall(question.lower()):
        if token in seen:
            continue
        seen.add(token)
        escaped = token.replace('"', '""')
        terms.append(f'"{escaped}"')
        if len(terms) >= 12:
            break
    return " OR ".join(terms)


class CaseStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = Path(settings.case_data_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self, case_id: str) -> tuple[str, Path, Path, Path]:
        safe = _safe_case_id(case_id)
        case_dir = self.root / safe
        return safe, case_dir / "source.json", case_dir / "case.db", case_dir / "metadata.json"

    async def ingest(self, case_id: str, request: Request) -> CaseMetadata:
        safe, source, database, metadata_file = self._paths(case_id)
        source.parent.mkdir(parents=True, exist_ok=True)
        tmp = source.with_suffix(".uploading")
        total = 0
        try:
            with tmp.open("wb") as fh:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > self.settings.max_case_bytes:
                        raise HTTPException(413, f"El caso excede AI_MAX_CASE_BYTES={self.settings.max_case_bytes}")
                    fh.write(chunk)
            if total == 0:
                raise HTTPException(400, "El cuerpo JSON está vacío")
            tmp.replace(source)
            try:
                facts = await asyncio.to_thread(index_json_to_sqlite, source, database)
            except Exception as exc:
                source.unlink(missing_ok=True)
                database.unlink(missing_ok=True)
                raise HTTPException(400, f"JSON inválido o no indexable: {exc}") from exc

            metadata = CaseMetadata(
                caseId=safe,
                bytes=total,
                facts=facts,
                indexedAt=_utc_now(),
                sourceFile=str(source),
                databaseFile=str(database),
            )
            metadata_file.write_text(metadata.model_dump_json(indent=2), encoding="utf-8")
            return metadata
        finally:
            tmp.unlink(missing_ok=True)

    def metadata(self, case_id: str) -> CaseMetadata:
        _, _, _, metadata_file = self._paths(case_id)
        if not metadata_file.exists():
            raise HTTPException(404, "Caso no encontrado")
        return CaseMetadata.model_validate_json(metadata_file.read_text(encoding="utf-8"))

    def delete(self, case_id: str) -> None:
        _, source, database, metadata_file = self._paths(case_id)
        if not metadata_file.exists():
            raise HTTPException(404, "Caso no encontrado")
        for path in (source, database, database.with_name(database.name + "-wal"), database.with_name(database.name + "-shm"), metadata_file):
            path.unlink(missing_ok=True)
        try:
            source.parent.rmdir()
        except OSError:
            pass

    def search(self, case_id: str, question: str, *, limit: int) -> list[dict[str, Any]]:
        _, _, database, metadata_file = self._paths(case_id)
        if not metadata_file.exists() or not database.exists():
            raise HTTPException(404, "Caso no encontrado")

        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        try:
            meta = {row[0]: row[1] for row in conn.execute("SELECT key,value FROM meta")}
            fts = meta.get("fts") == "1"
            if fts:
                match = _fts_query(question)
                if match:
                    try:
                        rows = conn.execute(
                            "SELECT path, value, bm25(facts) AS score FROM facts WHERE facts MATCH ? ORDER BY score LIMIT ?",
                            (match, limit),
                        ).fetchall()
                    except sqlite3.OperationalError:
                        rows = []
                else:
                    rows = conn.execute("SELECT path,value,0.0 AS score FROM facts LIMIT ?", (limit,)).fetchall()
            else:
                tokens = [t for t in _TOKEN.findall(question.lower())][:4]
                if tokens:
                    clauses = " OR ".join("lower(path || ' ' || value) LIKE ?" for _ in tokens)
                    params = [f"%{t}%" for t in tokens] + [limit]
                    rows = conn.execute(
                        f"SELECT path,value,0.0 AS score FROM facts WHERE {clauses} LIMIT ?", params
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT path,value,0.0 AS score FROM facts LIMIT ?", (limit,)).fetchall()

            return [
                {"path": row["path"], "value": row["value"], "score": float(row["score"] or 0.0)}
                for row in rows
            ]
        finally:
            conn.close()
