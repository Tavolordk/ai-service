"""Cotejo documental determinista: nunca convierte una diferencia en culpabilidad.

Se comparan valores del MISMO atributo y, para placas, del MISMO VIN/NIV
explícito. Los registros de procedencia se mantienen unidos al valor. No se
utiliza el LLM para calcular diferencias, fechas o relaciones entre entidades.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any


def _text(value: Any, maxlen: int = 140) -> str:
    return " ".join(str(value if value is not None else "").split())[:maxlen]


def _normalize(value: Any) -> str:
    raw = unicodedata.normalize("NFD", _text(value).casefold())
    return " ".join("".join(ch for ch in raw if unicodedata.category(ch) != "Mn").split())


def _same_value(value: str) -> str:
    value = _normalize(value)
    # Normalización para evitar falsas diferencias por formato de fecha.
    iso = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", value)
    mx = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", value)
    if mx:
        return f"{int(mx[3]):04}-{int(mx[2]):02}-{int(mx[1]):02}"
    if iso:
        return f"{int(iso[1]):04}-{int(iso[2]):02}-{int(iso[3]):02}"
    return value


@dataclass(frozen=True, slots=True)
class DocumentValue:
    value: str
    source: str
    reference: str
    observed_at: str
    path: str

    def compact(self) -> str:
        extra = f", ref. {self.reference}" if self.reference else ""
        timestamp = f", registro {self.observed_at}" if self.observed_at else ""
        return f"{self.value} (fuente: {self.source}{extra}{timestamp}; ruta: {self.path})"


@dataclass(frozen=True, slots=True)
class DocumentaryDifference:
    label: str
    subject: str
    values: tuple[DocumentValue, ...]


def _source_entries(item: dict[str, Any]) -> list[tuple[str, str, str]]:
    origins = item.get("origins") or []
    if not isinstance(origins, list) or not origins:
        return [("sin fuente explícita", "", "")]
    entries: list[tuple[str, str, str]] = []
    for record in origins[:3]:
        if isinstance(record, dict):
            entries.append((
                _text(record.get("sourceCode") or record.get("source") or record.get("system") or "sin fuente explícita", 65),
                _text(record.get("recordReference") or "", 55),
                _text(record.get("date") or "", 24),
            ))
    return entries or [("sin fuente explícita", "", "")]


def compare_documentary_values(profile: dict[str, Any], *, max_groups: int = 12) -> list[DocumentaryDifference]:
    """No presupone que valores distintos representen errores ni historial."""
    records: dict[tuple[str, str], list[DocumentValue]] = defaultdict(list)
    display: dict[tuple[str, str], str] = {}

    for i, item in enumerate((profile.get("data") or [])[:30000]):
        if not isinstance(item, dict):
            continue
        label = _text(item.get("dataType") or item.get("code") or "", 65)
        value = _text(item.get("value"), 140)
        if not label or not value:
            continue
        key = ("profile", _normalize(label))
        display[key] = label
        for src, ref, date in _source_entries(item):
            records[key].append(DocumentValue(value, src, ref, date, f"$.profile.data[{i}]"))

    # Únicamente un identificador vehicular explícito permite agrupar placas.
    # Nunca cotejamos dos vehículos por coincidir en marca o color.
    for i, item in enumerate((profile.get("vehicles") or [])[:10000]):
        if not isinstance(item, dict):
            continue
        vin = _text(item.get("vin") or item.get("niv") or "", 120)
        plate = _text(item.get("licensePlate") or item.get("placa") or "", 50)
        if not vin or not plate:
            continue
        key = ("vehicle", _normalize(vin))
        display[key] = "PLACA VEHICULAR"
        for src, ref, date in _source_entries(item):
            records[key].append(DocumentValue(plate, src, ref, date, f"$.profile.vehicles[{i}]"))

    differences: list[DocumentaryDifference] = []
    for (type_, subject), all_rows in records.items():
        distinct: dict[str, DocumentValue] = {}
        for row in all_rows:
            # Preserva todas las procedencias únicas. No duplica una misma cita.
            signature = (_same_value(row.value), row.source, row.reference, row.observed_at, row.path)
            distinct.setdefault(str(signature), row)
        rows = list(distinct.values())
        if len({_same_value(row.value) for row in rows}) < 2:
            continue
        values = tuple(rows[:8])
        title = display[(type_, subject)]
        differences.append(DocumentaryDifference(
            label=title, subject=subject if type_ == "vehicle" else "perfil consolidado",
            values=values,
        ))
    # Menos datos al modelo: fechas, VIN/placas e identificadores primero.
    def sort_order(item: DocumentaryDifference) -> int:
        label = _normalize(item.label)
        if "nacimiento" in label or "fecha" in label:
            return 0
        if "placa" in label or "vin" in label:
            return 1
        if any(x in label for x in ("curp", "rfc", "cuip", "identificador")):
            return 2
        return 3
    differences.sort(key=sort_order)
    return differences[:max_groups]


def _relevance(conflict: DocumentaryDifference, question: str) -> int:
    q = _normalize(question)
    label = _normalize(conflict.label)
    relevance = 0
    if any(t in q for t in ("nacimiento", "fecha", "edad")) and any(t in label for t in ("nacimiento", "fecha")):
        relevance += 20
    if any(t in q for t in ("placa", "vin", "vehicul")) and "placa" in label:
        relevance += 20
    if any(t in q for t in ("curp", "rfc", "cuip")) and any(t in label for t in ("curp", "rfc", "cuip")):
        relevance += 20
    return relevance


def select_differences(differences: list[DocumentaryDifference], question: str, *, limit: int = 5) -> list[DocumentaryDifference]:
    scored = sorted(enumerate(differences), key=lambda x: (-_relevance(x[1], question), x[0]))
    matched = [c for _, c in scored if _relevance(c, question)]
    return (matched if matched else [c for _, c in scored])[:limit]


def _address_rows(profile: dict[str, Any], limit: int = 2) -> list[str]:
    rows = []
    for i, item in enumerate((profile.get("addresses") or [])[:limit]):
        if not isinstance(item, dict):
            continue
        parts = [item.get(key) for key in ("type", "street", "exteriorNumber", "neighborhood", "municipality", "state", "postalCode")]
        address = ", ".join(_text(v, 55) for v in parts if v not in (None, ""))
        source = _source_entries(item)[0][0]
        if address:
            rows.append(f"- domicilio {i + 1}: {address}; fuente: {source}; ruta: $.profile.addresses[{i}]")
    return rows


def _graph_rows(graph: dict[str, Any] | None, limit: int = 5) -> list[str]:
    if not isinstance(graph, dict):
        return []
    nodes = {str(x.get("id")): x for x in (graph.get("nodes") or []) if isinstance(x, dict)}
    rows = []
    for i, link in enumerate((graph.get("links") or [])[:limit]):
        if not isinstance(link, dict):
            continue
        source_id = str(link.get("source") or link.get("sourceId") or link.get("from") or "")
        target_id = str(link.get("target") or link.get("targetId") or link.get("to") or "")
        def node_text(key: str) -> str:
            data = nodes.get(key) or {}
            return _text(data.get("title") or data.get("label") or key, 65)
        if source_id and target_id:
            relation = _text(link.get("type") or link.get("label") or link.get("relation") or "tipo no especificado", 60)
            rows.append(f"- vínculo {i + 1}: {node_text(source_id)} --[{relation}]--> {node_text(target_id)}; ruta: $.graph.links[{i}]")
    return rows


def compact_documentary_context(profile: dict[str, Any], graph: dict[str, Any] | None,
                                question: str, *, mode: str = "auto",
                                selected_node: dict[str, Any] | None = None) -> str:
    """Máximo 5 contrastes y unos pocos datos auxiliares, sin enviar el JSON crudo."""
    diffs = select_differences(compare_documentary_values(profile), question, limit=5 if mode == "deep" else 3)
    lines = ["[EVIDENCIA PRIORITARIA CON FUENTES]"]
    if isinstance(selected_node, dict) and selected_node:
        parts = [f"{key}={_text(selected_node[key], 100)}" for key in
                 ("id", "type", "title", "subtitle", "label") if selected_node.get(key) not in (None, "")]
        if parts:
            lines.append("- $.selectedNode " + "; ".join(parts))
    for i, diff in enumerate(diffs):
        # Encabezado propio para que validadores puedan contrastar ambos lados.
        vals = diff.values
        lines.append(f"- $.comparacionDocumental[{i}] campo={_text(diff.label)}; entidad={_text(diff.subject)}; "
                     + "; ".join(f"valor{j + 1}={v.value}; fuente{j + 1}={v.source}" for j, v in enumerate(vals)))
        for val in vals:
            lines.append(f"- {val.compact()}")
    if mode == "deep" or any(t in _normalize(question) for t in ("domicilio", "ubicacion", "direccion")):
        lines.extend(_address_rows(profile, 2 if mode != "deep" else 3))
    if graph and (mode == "deep" or any(t in _normalize(question) for t in ("vinculo", "relacion", "grafo"))):
        lines.extend(_graph_rows(graph, 5))
    lines.append("[LÍMITES DOCUMENTALES]")
    lines.append("- Los registros no demuestran por sí solos cuál valor es vigente ni la causa de la diferencia.")
    lines.append("- Una vinculación de grafo no implica delito, parentesco ni culpabilidad.")
    return "\n".join(lines)


def documentary_report(differences: list[DocumentaryDifference], question: str, *, deep: bool = False,
                       profile: dict[str, Any] | None = None, graph: dict[str, Any] | None = None) -> str:
    selected = select_differences(differences, question)
    if not selected:
        return "No se identificaron valores contradictorios del mismo atributo en los registros comparables disponibles. Esto no acredita consistencia de fuentes ausentes."
    parts = ["Hechos documentados y discrepancias identificadas:"]
    for i, diff in enumerate(selected, 1):
        values = list(diff.values)
        entries = "; ".join(v.compact() for v in values)
        detail = f"{i}. {diff.label} ({diff.subject}): {entries}."
        parts.append(detail)
    if deep:
        if profile and _address_rows(profile):
            parts.append("Domicilios registrados (no se infiere que sean simultáneos):")
            parts.extend(_address_rows(profile))
        if graph and _graph_rows(graph):
            parts.append("Vínculos expresamente registrados (sin inferencias personales):")
            parts.extend(_graph_rows(graph))
        parts.append("Hipótesis no confirmadas: las diferencias podrían obedecer a actualización histórica o captura diferente; no hay evidencia suficiente para escoger alguna explicación.")
        parts.append("Datos insuficientes: documentos originales, fechas de vigencia y criterios de actualización de las fuentes.")
    else:
        parts.append("Interpretación: se trata de valores distintos documentados para un mismo atributo o identificador, no de una causa comprobada.")
    parts.append("Conclusión: se requiere corroboración documental y temporal; ninguna diferencia prueba por sí misma una conducta ilícita.")
    return "\n".join(parts)


def straightforward_comparison(question: str, mode: str, thinking: bool) -> bool:
    """Comparación de hechos: resoluble sin inferencia generativa ni costo de CPU."""
    if mode == "deep" or thinking:
        return False
    q = _normalize(question)
    asks_compare = any(t in q for t in ("discrepan", "diferenc", "inconsisten", "contradic", "compara", "contrasta"))
    asks_interpretation = any(t in q for t in ("hipotesis", "infiere", "inferir", "por que", "causa", "patron", "riesgo", "criminal", "profund", "investiga"))
    return asks_compare and not asks_interpretation


def direct_conflicting_fact(question: str, differences: list[DocumentaryDifference]) -> bool:
    """Evita devolver sólo la primera fecha/placa cuando las fuentes difieren."""
    q = _normalize(question)
    ask_value = any(term in q for term in ("cual", "que fecha", "que placa", "indica", "registrad", "aparece"))
    if not ask_value:
        return False
    for difference in differences:
        label = _normalize(difference.label)
        if "nacimiento" in label and "nacimiento" in q:
            return True
        if "placa" in label and any(t in q for t in ("placa", "matricula")):
            return True
        if any(t in label for t in ("curp", "rfc", "cuip")) and label in q:
            return True
    return False
