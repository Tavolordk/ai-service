from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import ijson


SCALAR_EVENTS = {"string", "number", "boolean", "null"}


def _to_text(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def index_json_to_sqlite(source: Path, database: Path, *, batch_size: int = 5000) -> int:
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        database.unlink()

    conn = sqlite3.connect(database)
    total = 0
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-65536")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE facts USING fts5(path, value, tokenize='unicode61 remove_diacritics 2')"
            )
            fts = True
        except sqlite3.OperationalError:
            conn.execute("CREATE TABLE facts (path TEXT NOT NULL, value TEXT NOT NULL)")
            conn.execute("CREATE INDEX idx_facts_path ON facts(path)")
            fts = False

        batch: list[tuple[str, str]] = []
        with source.open("rb") as fh:
            for prefix, event, value in ijson.parse(fh):
                if event not in SCALAR_EVENTS:
                    continue
                path = prefix or "$"
                batch.append((path, _to_text(value)))
                if len(batch) >= batch_size:
                    conn.executemany("INSERT INTO facts(path, value) VALUES (?, ?)", batch)
                    total += len(batch)
                    batch.clear()
            if batch:
                conn.executemany("INSERT INTO facts(path, value) VALUES (?, ?)", batch)
                total += len(batch)

        conn.execute("INSERT INTO meta(key,value) VALUES('fts',?)", ("1" if fts else "0",))
        conn.execute("INSERT INTO meta(key,value) VALUES('facts',?)", (str(total),))
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
    conn = sqlite3.connect(database)
    try:
        return {row[0]: row[1] for row in conn.execute("SELECT key,value FROM meta")}
    finally:
        conn.close()
