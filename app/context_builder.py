from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

_WORD = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9_-]{2,}")

_SECTION_LABELS = {
    "identity": "IDENTIDAD",
    "identifiers": "IDENTIFICADORES",
    "addresses": "DOMICILIOS",
    "vehicles": "VEHÍCULOS",
    "weapons": "ARMAS",
    "relations": "VÍNCULOS Y RELACIONES",
    "sources": "FUENTES Y ORÍGENES",
    "events": "EVENTOS / REGISTROS",
    "discrepancies": "DISCREPANCIAS",
    "other": "OTROS DATOS RELEVANTES",
}

_SECTION_KEYWORDS = {
    "identity": (
        "nombre", "name", "apellido", "sexo", "genero", "género", "nacimiento", "fecha_nac", "edad",
    ),
    "identifiers": (
        "curp", "rfc", "cuip", "identificador", "identifier", "profileid", "perfilid", "cib",
    ),
    "addresses": (
        "domicilio", "direccion", "dirección", "address", "calle", "colonia", "municipio", "estado",
        "localidad", "codigo_postal", "código_postal", "postal", "cp",
    ),
    "vehicles": (
        "vehiculo", "vehículo", "vehicle", "vin", "niv", "placa", "automovil", "automóvil", "marca_veh",
        "modelo_veh",
    ),
    "weapons": (
        "arma", "weapon", "calibre", "matricula_arma", "matrícula_arma",
    ),
    "relations": (
        "vinculo", "vínculo", "relacion", "relación", "relationship", "edge", "grafo", "graph", "nodo", "node",
        "parent", "padre", "propietario", "asociado", "asociación",
    ),
    "sources": (
        "fuente", "source", "origen", "origin", "sistema", "provenance", "recordid", "source_record",
    ),
    "events": (
        "detencion", "detención", "folio", "fecha", "evento", "event", "autoridad", "expediente",
    ),
    "discrepancies": (
        "discrepancia", "diferencia", "inconsistencia", "conflicto", "variante", "mismatch", "difference",
    ),
}

_INTENT_TERMS = {
    "summary": ("resume", "resumen", "resumir", "sintetiza", "sintetizar", "perfil general"),
    "identity": ("nombre", "quien es", "quién es", "identidad", "curp", "rfc", "cuip", "identificador"),
    "addresses": ("domicilio", "direccion", "dirección", "ubicacion", "ubicación", "vive", "residencia"),
    "vehicles": ("vehiculo", "vehículo", "carro", "auto", "vin", "niv", "placa", "repuve"),
    "weapons": ("arma", "armas", "calibre"),
    "relations": ("vinculo", "vínculo", "relacion", "relación", "grafo", "nodo", "conecta", "asocia"),
    "sources": ("fuente", "fuentes", "origen", "orígenes", "registro", "registros"),
    "events": ("detencion", "detención", "folio", "evento", "fecha", "autoridad"),
    "discrepancies": ("discrepancia", "diferencia", "inconsistencia", "contradic", "conflicto"),
}


def _normalize(text: str) -> str:
    return " ".join(text.lower().strip().split())


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _WORD.findall(text)}


def classify_intent(question: str) -> str:
    q = _normalize(question)
    for intent in ("summary", "vehicles", "addresses", "relations", "identity", "weapons", "discrepancies", "sources", "events"):
        if any(term in q for term in _INTENT_TERMS[intent]):
            return intent
    return "general"


def _classify_path(path: str, value: str) -> str:
    haystack = _normalize(f"{path} {value[:160]}")
    # Priorizamos categorías concretas antes de fuentes/eventos para que, por ejemplo,
    # un VIN proveniente de REPUVE siga siendo un dato vehicular.
    order = ("identifiers", "identity", "addresses", "vehicles", "weapons", "relations", "discrepancies", "events", "sources")
    for section in order:
        if any(keyword in haystack for keyword in _SECTION_KEYWORDS[section]):
            return section
    return "other"


def _flatten(value: Any, path: str = "$.profile", *, max_items: int = 30000) -> list[tuple[str, str]]:
    # Recorrido por anchura: en JSON enormes captura primero datos de alto nivel de TODAS
    # las secciones antes de hundirse en miles de orígenes/registros repetidos.
    rows: list[tuple[str, str]] = []
    queue = deque([(path, value)])
    while queue and len(rows) < max_items:
        current_path, current = queue.popleft()
        if isinstance(current, dict):
            for key, child in current.items():
                queue.append((f"{current_path}.{key}", child))
        elif isinstance(current, list):
            for index, child in enumerate(current):
                queue.append((f"{current_path}[{index}]", child))
        elif current is not None:
            text = str(current).strip()
            if text:
                rows.append((current_path, text))
    return rows


@dataclass(frozen=True, slots=True)
class ContextFact:
    path: str
    value: str
    section: str


@dataclass(frozen=True, slots=True)
class ProfileSnapshot:
    profile_id: str
    fingerprint: str
    sections: dict[str, tuple[ContextFact, ...]]
    total_facts: int
    built_at: float


@dataclass(frozen=True, slots=True)
class ContextSelection:
    intent: str
    context: str
    evidence: list[dict[str, Any]]
    cache_hit: bool
    source_facts: int
    selected_facts: int


class ProfileContextCache:
    def __init__(self, *, max_entries: int = 128, ttl_seconds: int = 3600) -> None:
        self.max_entries = max(1, max_entries)
        self.ttl_seconds = max(60, ttl_seconds)
        self._items: OrderedDict[str, ProfileSnapshot] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def fingerprint(profile: dict[str, Any]) -> str:
        raw = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.blake2b(raw, digest_size=12).hexdigest()

    def get(self, key: str) -> ProfileSnapshot | None:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self._misses += 1
                return None
            if now - item.built_at > self.ttl_seconds:
                self._items.pop(key, None)
                self._misses += 1
                return None
            self._items.move_to_end(key)
            self._hits += 1
            return item

    def put(self, key: str, snapshot: ProfileSnapshot) -> None:
        with self._lock:
            self._items[key] = snapshot
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._items), "hits": self._hits, "misses": self._misses}


class ContextBuilder:
    """Reduce JSON estructurado a evidencia útil antes de involucrar al LLM."""

    def __init__(
        self,
        *,
        max_chars: int = 14000,
        deep_max_chars: int = 28000,
        max_facts_per_section: int = 28,
        cache_entries: int = 128,
        cache_ttl_seconds: int = 3600,
    ) -> None:
        self.max_chars = max(4000, max_chars)
        self.deep_max_chars = max(self.max_chars, deep_max_chars)
        self.max_facts_per_section = max(8, max_facts_per_section)
        self.cache = ProfileContextCache(max_entries=cache_entries, ttl_seconds=cache_ttl_seconds)

    def cache_stats(self) -> dict[str, int]:
        return self.cache.stats()

    def _build_snapshot(self, profile: dict[str, Any], profile_id: str, fingerprint: str) -> ProfileSnapshot:
        section_rows: dict[str, list[ContextFact]] = {key: [] for key in _SECTION_LABELS}
        seen: dict[str, set[str]] = {key: set() for key in _SECTION_LABELS}
        rows = _flatten(profile)
        for path, value in rows:
            section = _classify_path(path, value)
            # Deduplicar el mismo valor dentro de la misma categoría evita repetir docenas
            # de veces la misma fuente/domicilio sin perder la primera ruta verificable.
            dedupe_key = _normalize(value)
            if dedupe_key in seen[section]:
                continue
            seen[section].add(dedupe_key)
            section_rows[section].append(ContextFact(path=path, value=value, section=section))

        return ProfileSnapshot(
            profile_id=profile_id,
            fingerprint=fingerprint,
            sections={key: tuple(values) for key, values in section_rows.items()},
            total_facts=len(rows),
            built_at=time.monotonic(),
        )

    def snapshot(self, profile: dict[str, Any], profile_id: str) -> tuple[ProfileSnapshot, bool]:
        fingerprint = self.cache.fingerprint(profile)
        cache_key = f"{profile_id}:{fingerprint}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached, True
        snapshot = self._build_snapshot(profile, profile_id, fingerprint)
        self.cache.put(cache_key, snapshot)
        return snapshot, False

    @staticmethod
    def _section_order(intent: str) -> tuple[str, ...]:
        mapping = {
            "summary": ("identity", "identifiers", "addresses", "vehicles", "weapons", "relations", "sources", "discrepancies", "events"),
            "identity": ("identity", "identifiers", "addresses", "sources", "discrepancies"),
            "addresses": ("identity", "identifiers", "addresses", "sources", "relations", "discrepancies"),
            "vehicles": ("identity", "identifiers", "vehicles", "relations", "sources", "discrepancies"),
            "weapons": ("identity", "identifiers", "weapons", "relations", "sources", "discrepancies"),
            "relations": ("identity", "identifiers", "relations", "vehicles", "addresses", "weapons", "sources", "discrepancies"),
            "sources": ("identity", "identifiers", "sources", "vehicles", "addresses", "relations", "discrepancies"),
            "events": ("identity", "identifiers", "events", "addresses", "sources", "relations", "discrepancies"),
            "discrepancies": ("identity", "identifiers", "discrepancies", "sources", "addresses", "vehicles", "relations"),
            "general": ("identity", "identifiers", "addresses", "vehicles", "relations", "sources", "events", "discrepancies", "other"),
        }
        return mapping.get(intent, mapping["general"])

    @staticmethod
    def _rank(facts: tuple[ContextFact, ...] | list[ContextFact], question: str) -> list[ContextFact]:
        qtokens = _tokens(question)
        scored: list[tuple[int, int, ContextFact]] = []
        for index, fact in enumerate(facts):
            bag = _tokens(f"{fact.path} {fact.value}")
            score = len(qtokens & bag) * 20
            if any(token in _normalize(fact.path) for token in qtokens):
                score += 8
            scored.append((score, -index, fact))
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [fact for _, _, fact in scored]

    def select(
        self,
        *,
        profile: dict[str, Any],
        profile_id: str,
        question: str,
        mode: str,
        selected_node: dict[str, Any] | None = None,
        graph: dict[str, Any] | None = None,
    ) -> ContextSelection:
        intent = classify_intent(question)
        snapshot, cache_hit = self.snapshot(profile, profile_id)
        char_budget = self.deep_max_chars if mode == "deep" else self.max_chars

        parts: list[str] = [
            f"PERFIL_ID: {profile_id}",
            f"INTENCIÓN DETECTADA: {intent}",
            f"HECHOS ESTRUCTURADOS DISPONIBLES: {snapshot.total_facts}",
        ]
        evidence: list[dict[str, Any]] = []
        used_chars = sum(len(part) + 1 for part in parts)
        selected_count = 0

        section_order = self._section_order(intent)
        for section in section_order:
            facts = self._rank(snapshot.sections.get(section, ()), question)
            if not facts:
                continue
            # En resúmenes se reparte el presupuesto; en una pregunta concreta damos más
            # espacio a su sección principal sin volver al JSON bruto.
            cap = self.max_facts_per_section
            if intent != "summary" and section in {intent, "identifiers", "identity"}:
                cap = int(cap * 1.5)
            section_lines: list[str] = []
            for fact in facts[:cap]:
                line = f"- {fact.path}: {fact.value}"
                if used_chars + len(line) + len(_SECTION_LABELS[section]) + 4 > char_budget:
                    break
                section_lines.append(line)
                used_chars += len(line) + 1
                selected_count += 1
                evidence.append({"path": fact.path, "value": fact.value, "score": 0, "section": section})
            if section_lines:
                parts.append(f"\n[{_SECTION_LABELS[section]}]")
                parts.extend(section_lines)
            if used_chars >= char_budget:
                break

        # Nodo seleccionado tiene prioridad absoluta porque representa el foco explícito del usuario.
        if selected_node and used_chars < char_budget:
            node_facts = [ContextFact(path, value, "relations") for path, value in _flatten(selected_node, "$.selectedNode", max_items=1500)]
            node_lines: list[str] = []
            for fact in self._rank(node_facts, question)[:24]:
                line = f"- {fact.path}: {fact.value}"
                if used_chars + len(line) + 24 > char_budget:
                    break
                node_lines.append(line)
                used_chars += len(line) + 1
                evidence.append({"path": fact.path, "value": fact.value, "score": 100, "section": "selectedNode"})
                selected_count += 1
            if node_lines:
                parts.append("\n[NODO SELECCIONADO]")
                parts.extend(node_lines)

        # El grafo sólo entra cuando aporta valor a la intención; evita mandar cientos de nodos
        # para preguntas simples de identidad/domicilio.
        include_graph = graph and (intent in {"summary", "vehicles", "weapons", "relations", "general"} or mode == "deep")
        if include_graph and used_chars < char_budget:
            graph_rows = _flatten(graph, "$.graph", max_items=12000)
            graph_facts = [ContextFact(path, value, _classify_path(path, value)) for path, value in graph_rows]
            graph_lines: list[str] = []
            graph_cap = 80 if mode == "deep" else 36
            for fact in self._rank(graph_facts, question)[:graph_cap]:
                line = f"- {fact.path}: {fact.value}"
                if used_chars + len(line) + 16 > char_budget:
                    break
                graph_lines.append(line)
                used_chars += len(line) + 1
                evidence.append({"path": fact.path, "value": fact.value, "score": 50, "section": "graph"})
                selected_count += 1
            if graph_lines:
                parts.append("\n[GRAFO RELEVANTE]")
                parts.extend(graph_lines)

        return ContextSelection(
            intent=intent,
            context="\n".join(parts)[:char_budget],
            evidence=evidence,
            cache_hit=cache_hit,
            source_facts=snapshot.total_facts,
            selected_facts=selected_count,
        )
