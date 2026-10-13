"""Pruebas de integración del parser real ijson (incluido en la imagen Docker).

Los tests SAX falsos que no requieren esa dependencia viven en test_large_scale.py.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from app import json_stream

pytestmark = pytest.mark.skipif(json_stream.ijson is None, reason="ijson no disponible en entorno de pruebas")


def test_real_ijson_parser_paths_and_graph(tmp_path: Path):
    source = tmp_path / 'source.json'
    source.write_text(json.dumps({
        'profile': {'data': [{'dataType': 'CURP', 'value': 'TEST12345'}]},
        'graph': {'nodes': [{'id':'N0','title':'NODO FICTICIO'}],
                  'links': [{'source':'N0', 'target':'N1', 'type':'CONTACTO'},
                            {'source':'N0', 'target':'N2', 'type':'VEHICULO'}]},
    }, ensure_ascii=False), encoding='utf-8')
    db = tmp_path / 'source.db'
    total = json_stream.index_json_to_sqlite(source, db, batch_size=2)
    assert total > 8
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 2
        assert conn.execute("SELECT value FROM facts WHERE path='$.profile.data[0].value'").fetchone()[0] == 'TEST12345'
        assert conn.execute("SELECT degree FROM graph_degree WHERE node_id='N0'").fetchone()[0] == 2
        assert conn.execute("SELECT source FROM case_fields WHERE attribute='CURP'").fetchone()[0] == 'sin fuente explícita'


def test_real_ijson_rejects_malformed_json_without_successful_index(tmp_path: Path):
    source=tmp_path/'bad.json'
    source.write_text('{"links": [ {"source":"A"}')
    with pytest.raises(Exception):
        json_stream.index_json_to_sqlite(source, tmp_path/'bad.db')
