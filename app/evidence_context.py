"""Selección documental prioritaria a partir del JSON ya enviado por el frontend.

Ajuste puntual previo al recorte: preserva valores y procedencia juntos, no infiere
relaciones nuevas y no sustituye el Context Builder ni modifica la caché.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any


def _norm(value: Any) -> str:
    s = unicodedata.normalize("NFD", str(value or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _clean(value: Any, max_chars: int = 130) -> str:
    # Evita inyección de líneas por datos externos, conservando contenido original.
    return " ".join(str(value if value is not None else "").split())[:max_chars]


def _origins(item: dict[str, Any]) -> str:
    origins = item.get("origins") or []
    if not isinstance(origins, list):
        return "sin fuente explícita"
    out = []
    for record in origins[:3]:
        if not isinstance(record, dict):
            continue
        source = _clean(record.get("sourceCode") or record.get("source") or "fuente sin nombre", 55)
        ref = _clean(record.get("recordReference") or "", 40)
        date = _clean(record.get("date") or "", 24)
        out.append(source + (f" ({ref})" if ref else "") + (f" [{date}]" if date else ""))
    return ", ".join(out) if out else "sin fuente explícita"


def priority_evidence(profile: dict[str, Any], question: str, mode: str) -> list[str]:
    q = _norm(question)
    want_conflicts = any(t in q for t in ("discrepan", "diferenc", "contradic", "inconsisten", "compar", "fuente", "documentad"))
    want_birth = want_conflicts or any(t in q for t in ("nacim", "edad", "fecha"))
    want_vehicle = want_conflicts or any(t in q for t in ("vin", "niv", "plac", "vehicul", "automovil", "carro"))
    want_identity = any(t in q for t in ("identidad", "nombre", "curp", "rfc", "cuip")) or mode == "deep"
    if not (want_conflicts or want_birth or want_vehicle or want_identity or mode == "deep"):
        return []

    lines: list[str] = []
    data = profile.get("data")
    if isinstance(data, list):
        for index, row in enumerate(data):
            if not isinstance(row, dict) or row.get("value") in (None, ""):
                continue
            label = _norm(row.get("dataType") or row.get("code") or "")
            if (want_birth and ("nacim" in label or "fecha" in label)) or (want_vehicle and any(t in label for t in ("vin", "niv", "plac", "vehicul"))) or (want_identity and any(t in label for t in ("nombre", "apellido", "curp", "rfc", "cuip"))):
                lines.append(f"- $.profile.data[{index}] {_clean(row.get('dataType') or row.get('code'), 40)} = {_clean(row['value'])}; fuente: {_origins(row)}")

    vehicles = profile.get("vehicles") or []
    if want_vehicle and isinstance(vehicles, list):
        for index, vehicle in enumerate(vehicles[:15]):
            if not isinstance(vehicle, dict):
                continue
            fields = [f"{key}={_clean(vehicle[key])}" for key in ("vin", "niv", "licensePlate", "placa", "brand", "model", "year", "status") if vehicle.get(key) not in (None, "")]
            if fields:
                lines.append(f"- $.profile.vehicles[{index}] " + "; ".join(fields) + "; fuente: " + _origins(vehicle))

    discrepancies = profile.get("inconsistenciasDocumentales") or profile.get("discrepancies") or []
    if want_conflicts and isinstance(discrepancies, list):
        for index, item in enumerate(discrepancies[:12]):
            if not isinstance(item, dict):
                continue
            fields = [(key, _clean(val, 150)) for key, val in item.items() if val not in (None, "")]
            # Campos del propio registro, no conclusiones generadas por la API.
            if fields:
                lines.append(f"- $.profile.inconsistenciasDocumentales[{index}] " + "; ".join(f"{key}={val}" for key, val in fields))

    # Mantener registros completos, sin repetir la misma línea de evidencia.
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        if line not in seen:
            result.append(line)
            seen.add(line)
    if want_conflicts:
        # Primero el cotejo explícito, luego ambas procedencias y finalmente
        # los demás datos. Con presupuestos pequeños no se pierde un par A/B.
        result.sort(key=lambda line: (
            0 if "inconsistenciasDocumentales[" in line else
            1 if "FECHA DE NACIMIENTO" in line else
            2 if "licensePlate=" in line else 3
        ))
    return result[:35]


def merge_context(context: str, priority: list[str]) -> str:
    if not priority:
        return context
    return "[EVIDENCIA PRIORITARIA CON FUENTES]\n" + "\n".join(priority) + "\n\n" + context
