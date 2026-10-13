"""Pruebas sin Ollama y sin descargar dependencias durante la ejecución.

El fake de ijson reproduce eventos SAX de ijson.parse para fixtures pequeñas.
En Docker, requirements.txt instala ijson real para streaming de archivos grandes.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import json_stream
from app.case_store import CaseStore
from app.config import Settings
from app.models import ChatRequest
from app.request_limit import InlineBodyLimit


def _events(value, prefix=""):
    if isinstance(value, dict):
        yield prefix, "start_map", None
        for key, child in value.items():
            yield prefix, "map_key", key
            yield from _events(child, prefix + "." + key if prefix else key)
        yield prefix, "end_map", None
    elif isinstance(value, list):
        yield prefix, "start_array", None
        for child in value:
            yield from _events(child, prefix + ".item")
        yield prefix, "end_array", None
    elif value is None:
        yield prefix, "null", None
    elif isinstance(value, bool):
        yield prefix, "boolean", value
    elif isinstance(value, (int, float)):
        yield prefix, "number", value
    else:
        yield prefix, "string", value


@pytest.fixture
def fake_ijson(monkeypatch):
    monkeypatch.setattr(json_stream, "ijson", SimpleNamespace(parse=lambda handle: _events(json.load(handle))))


def _settings(tmp_path):
    return Settings(case_data_dir=str(tmp_path), max_case_bytes=100000000)


def _indexed(tmp_path, fake_ijson, amount=6):
    payload = {
        "profile": {"data": [{"dataType": "CURP", "value": "PRUEBA123"}]},
        "graph": {
            "nodes": [{"id": "CENTRO", "title": "Nodo central"}, {"id": "EXT1", "title": "Nodo vecino"}],
            "links": [{"source": "CENTRO", "target": "EXT1" if i == 0 else f"OTRO{i}",
                       "type": "RELACION_SIMULADA", "weight": i} for i in range(amount)],
        },
    }
    source = tmp_path / "source.json"
    database = tmp_path / "case.db"
    source.write_text(json.dumps(payload), encoding="utf-8")
    assert json_stream.index_json_to_sqlite(source, database, batch_size=3) > 5
    return source, database


def test_real_paths_and_graph_neighbors(tmp_path, fake_ijson):
    _indexed(tmp_path, fake_ijson)
    db = sqlite3.connect(tmp_path / "case.db")
    assert db.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 6
    assert db.execute("SELECT degree FROM graph_degree WHERE node_id='CENTRO'").fetchone()[0] == 6
    assert db.execute("SELECT label FROM graph_nodes WHERE node_id='EXT1'").fetchone()[0] == "Nodo vecino"
    assert db.execute("SELECT value FROM facts WHERE path='$.profile.data[0].value'").fetchone()[0] == "PRUEBA123"
    paths = [x[0] for x in db.execute("SELECT path FROM graph_edges ORDER BY edge_id")]
    assert paths[0] == "$.graph.links[0]" and paths[-1] == "$.graph.links[5]"
    db.close()


def test_pagination_constant_size_and_exact_neighbor(tmp_path, fake_ijson):
    _indexed(tmp_path, fake_ijson, amount=1050)
    from app.models import CaseMetadata
    import datetime
    source, db = tmp_path / 'source.json', tmp_path / 'case.db'
    case = tmp_path / 'CASE_01'
    case.mkdir()
    source.rename(case / source.name)
    db.rename(case / db.name)
    CaseMetadata(caseId="CASE_01", bytes=100, facts=1000, indexedAt="2026-01-01",
                 sourceFile=str(case / 'source.json'), databaseFile=str(case / 'case.db')).model_dump_json()
    (case / 'metadata.json').write_text(CaseMetadata(caseId="CASE_01", bytes=100, facts=1000,
        indexedAt="2026-01-01", sourceFile=str(case / 'source.json'),
        databaseFile=str(case / 'case.db')).model_dump_json())
    store = CaseStore(_settings(tmp_path))
    first = store.links('CASE_01','CENTRO',limit=20)
    assert first['total'] == 1050 and len(first['links']) == 20 and first['hasMore']
    assert first['links'][0]['sourceLabel'] == 'Nodo central'
    last = store.links('CASE_01','CENTRO',limit=20,offset=1040)
    assert len(last['links']) == 10 and not last['hasMore']
    assert store.links('CASE_01','EXT1')['total'] == 1
    matches = store.search('CASE_01','PRUEBA123',limit=3)
    assert matches and any('PRUEBA123' in x['value'] for x in matches)


def test_self_link_counted_once(tmp_path, fake_ijson):
    source = tmp_path / 'x.json'; db=tmp_path/'db.sqlite'
    source.write_text('{"links":[{"source":"A","target":"A","type":"contacto"}]}')
    json_stream.index_json_to_sqlite(source,db)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT degree FROM graph_degree WHERE node_id="A"').fetchone()[0] == 1


def test_old_case_preservation_on_invalid_json(tmp_path, fake_ijson):
    from app.models import CaseMetadata
    store = CaseStore(_settings(tmp_path))
    current = tmp_path/'Exp01'; current.mkdir()
    (current/'metadata.json').write_text(CaseMetadata(caseId='Exp01', bytes=1, facts=1,
       indexedAt='2026-01-01',sourceFile='old', databaseFile='old').model_dump_json())
    (current/'source.json').write_text('{"original": true}')
    class Upload:
        async def stream(self):
            yield b'{bad JSON'
    with pytest.raises(HTTPException) as error:
        asyncio.run(store.ingest('Exp01', Upload()))
    assert error.value.status_code == 400
    assert (current/'source.json').read_text() == '{"original": true}'
    assert not list(tmp_path.glob('.Exp01.stage-*'))


def test_valid_replace_atomic_and_metadata(tmp_path, fake_ijson):
    store = CaseStore(_settings(tmp_path))
    class Upload:
        async def stream(self):
            yield b'{"graph":{"links":[{"source":"A",'
            yield b'"target":"B","type":"TEST"}]}}'
    meta = asyncio.run(store.ingest('CASE_A', Upload()))
    assert meta.facts >= 3 and meta.links == 1 and meta.indexSchema == 2
    assert store.links('CASE_A','A')['total'] == 1
    assert store.metadata('CASE_A').links == 1
    assert not list(tmp_path.glob('.CASE_A.stage-*'))
    assert not list(tmp_path.glob('.CASE_A.backup-*'))


def test_caseid_stub_routes_to_indexed_store(monkeypatch, tmp_path):
    from app.analyzer import PoliceAnalyzer
    from app.ollama_client import LocalOllamaClient
    store = CaseStore(_settings(tmp_path))
    def fake_links(case_id, node_id, limit=30, offset=0):
        return {'total':900000,'links':[], 'caseId':case_id,'nodeId':node_id}
    monkeypatch.setattr(store,'links',fake_links)
    llm = LocalOllamaClient(_settings(tmp_path))
    ana = PoliceAnalyzer(_settings(tmp_path),llm,store)
    req=ChatRequest(profile={'profileId':'ABC'},caseId='CASO',question='¿Cuántos vínculos tiene este nodo?',
                    selectedNode={'id':'NODO-1'})
    ans=ana.stream_inline(req)
    assert ans.route == 'case:graph-degree'
    assert '900000' in ''.join(c.text for c in ans.chunks)


def test_inline_content_length_rejected_without_parsing():
    async def never_called(scope, receive, send):
        raise AssertionError('No debería parsear un JSON de 10 MB')
    sent = []
    async def send(message):
        sent.append(message)
    async def receive():
        return {"type":"http.request", "body":b"irrelevante", "more_body":False}
    scope={"type":"http","method":"POST", "path":"/api/v1/intelligence/chat",
           "headers":[(b"content-length",b"10000000")]}
    asyncio.run(InlineBodyLimit(never_called,max_bytes=5242880)(scope,receive,send))
    assert sent[0]['status'] == 413
    assert b'cases' in sent[1]['body']


def test_case_ids_cannot_escape_root(tmp_path):
    store=CaseStore(_settings(tmp_path))
    with pytest.raises(HTTPException): store._paths('../private')
    with pytest.raises(HTTPException): store._paths('.hidden')


def test_indexed_documentary_detects_only_same_subject_and_vin(tmp_path, fake_ijson):
    fixture = {
        'profile': {'data': [
            {'dataType': 'FECHA DE NACIMIENTO', 'value': '1990-05-16',
             'origins': [{'sourceCode': 'RENAPO_SIMULADO'}]},
            {'dataType': 'FECHA DE NACIMIENTO', 'value': '1990-05-18',
             'origins': [{'sourceCode': 'CAPTURA_SIMULADA'}]},
        ], 'vehicles': [
            {'vin': 'VIN001','licensePlate':'ABC123A','origins':[{'sourceCode':'REPUVE_SIMULADO'}]},
            {'vin': 'VIN001','licensePlate':'ABC128A','origins':[{'sourceCode':'OTRA_SIMULADA'}]},
            {'vin': 'VIN002','licensePlate':'GRO555B'},
        ]},
    }
    import asyncio
    class Upload:
        async def stream(self):
            yield json.dumps(fixture).encode()
    store = CaseStore(_settings(tmp_path))
    meta = asyncio.run(store.ingest('DEMO', Upload()))
    assert meta.links == 0
    diffs = store.documentary_differences('DEMO','Compara fecha de nacimiento y placas del mismo VIN')
    assert len(diffs) == 2
    output = [(x.label, [(v.value,v.source,v.path) for v in x.values]) for x in diffs]
    assert any(x[0] == 'FECHA DE NACIMIENTO' and {v[0] for v in x[1]} == {'1990-05-16','1990-05-18'} for x in output)
    assert any(x[0] == 'PLACA' and {v[0] for v in x[1]} == {'ABC123A','ABC128A'} for x in output)
    assert all('GRO555B' not in {v[0] for v in record[1]} for record in output)
    assert any('RENAPO_SIMULADO' == v[1] for record in output for v in record[1])


def test_cursor_pages_graph(tmp_path, fake_ijson):
    _indexed(tmp_path, fake_ijson, amount=52)
    from app.models import CaseMetadata
    case = tmp_path / 'M'
    case.mkdir()
    (tmp_path/'case.db').rename(case/'case.db')
    (case/'metadata.json').write_text(CaseMetadata(caseId='M',bytes=1,facts=100,indexedAt='2026-01-01',
                                 sourceFile='x',databaseFile='x').model_dump_json())
    store=CaseStore(_settings(tmp_path))
    ids=[]
    cursor=None
    while True:
        result=store.links('M','CENTRO',limit=13,after_edge_id=cursor)
        ids.extend(x['path'] for x in result['links'])
        if not result['hasMore']:
            break
        assert result['nextEdgeId']
        cursor=result['nextEdgeId']
    assert len(ids) == 52 and len(set(ids)) == 52


def test_true_streaming_limit_without_content_length():
    sent=[]
    async def receive_once():
        return {"type":"http.request", "body": b'x'*101, "more_body": False}
    async def receive_app(scope, receive, send):
        await receive()
    async def send(msg):
        sent.append(msg)
    asyncio.run(InlineBodyLimit(receive_app,max_bytes=100)(
        {"type":"http","method":"POST","path":"/api/v1/intelligence/chat", "headers":[]},
        receive_once,send))
    assert sent[0]['status'] == 413


def test_http_contract_case_upload_links_and_frontend_chat(tmp_path, fake_ijson, monkeypatch):
    from app import main
    store = CaseStore(_settings(tmp_path))
    monkeypatch.setattr(main, 'case_store', store)
    monkeypatch.setattr(main.analyzer, 'case_store', store)
    from dataclasses import replace
    monkeypatch.setattr(main, 'settings', replace(main.settings, llm_warmup=False))
    payload = {"profile": {"profileId": "P-1", "data":[{"dataType":"CURP","value":"FICTICIA-123",
                "origins":[{"sourceCode":"FUENTE_SIMULADA", "recordReference":"REF-001"}]}]},
               "graph":{"links":[{"source":"C","target":"E","type":"RELACION"}]}}
    with TestClient(main.app, base_url="http://localhost") as client:
        post = client.post('/api/v1/cases?caseId=CASO_DEMO', json=payload)
        assert post.status_code == 200, post.text
        assert post.json()['links'] == 1
        assert client.get('/api/v1/cases/CASO_DEMO').json()['indexSchema'] == 2
        refs=client.get('/api/v1/cases/CASO_DEMO/links?nodeId=C&limit=5')
        assert refs.status_code == 200 and refs.json()['total'] == 1
        searchable=client.get('/api/v1/cases/CASO_DEMO/search?q=FICTICIA-123')
        assert searchable.status_code == 200
        query={"profile":{"profileId":"P-1"}, "caseId":"CASO_DEMO", "selectedNode":{"id":"C"},
               "question":"¿Cuántos vínculos tiene este nodo?", "thinking":False}
        reply=client.post('/api/v1/intelligence/chat',json=query)
        assert reply.status_code == 200 and reply.json()['model'] == 'SPM Indexed Graph'
        assert '1 vínculo' in reply.json()['text']
        streamed=client.post('/api/v1/intelligence/chat/stream',json=query)
        assert streamed.status_code == 200 and 'event: done' in streamed.text
        identifiers=client.post('/api/v1/cases/CASO_DEMO/chat',json={"question":"¿Cuál es su CURP?"})
        assert identifiers.status_code == 200 and 'FICTICIA-123' in identifiers.json()['text']
        assert 'FUENTE_SIMULADA' in identifiers.json()['text']
