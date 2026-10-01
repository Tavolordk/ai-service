from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.json_stream import iter_json_records, detect_entity_type, extract_identifiers


def test_stream_records():
    payload = {
        "persona": {"nombre": "EJEMPLO", "curp": "XXXX000000XXXXXX00"},
        "vehiculos": [
            {"vin": "VIN001", "placa": "ABC123", "marca": "JEEP"},
            {"niv": "VIN002", "placa": "XYZ987"},
        ],
        "vinculos": [{"tipo_vinculo": "Propietario", "source_linked_id": 123}],
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "case.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        records = list(iter_json_records(path))
    assert records
    types = [detect_entity_type({**r.get("parent", {}), **r.get("fields", {})}, r["path"]) for r in records]
    assert "persona" in types
    assert "vehiculo" in types
    assert "vinculo" in types


def test_identifiers():
    ids = extract_identifiers({"VIN": "1A", "placa": "ABC", "marca": "X"})
    assert "VIN" in ids
    assert "placa" in ids
