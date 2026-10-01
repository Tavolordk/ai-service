from __future__ import annotations

import re
import threading
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .context_builder import ContextBuilder, ContextFact, ProfileSnapshot


@dataclass(frozen=True, slots=True)
class FastPathResult:
    text: str
    evidence: list[dict[str, Any]]
    intent: str
    route: str


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return " ".join(text.lower().strip().split())


def _first_nonempty(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or singular + "s")


class FastQueryRouter:
    """Resuelve preguntas factuales frecuentes sin invocar al LLM.

    Está diseñado para el contrato actual de SPM: `profile.data`, `profile.addresses`
    y el grafo opcional (`nodes`/`links`). Si la pregunta exige inferencia, hipótesis,
    explicación causal o comparación compleja, retorna None y deja el trabajo a Qwen.
    """

    _ANALYTIC_TERMS = (
        "analiza", "analizar", "analisis", "patron", "patrones", "hipotesis",
        "infiere", "inferir", "deduce", "deducir", "explica por que", "por que podria",
        "riesgo", "sospech", "relevante", "prioriza", "correlacion", "conclusion",
        "que significa", "interpret", "posible linea", "lineas de investigacion",
        "contradiccion", "inconsistencia entre", "discrepancia entre", "compara",
    )

    _SUMMARY_TERMS = (
        "resumen", "resume", "resumeme", "sintetiza", "sintesis", "perfil general",
        "datos principales", "datos mas importantes", "informacion principal",
    )

    _COUNT_TERMS = ("cuantos", "cuantas", "cantidad", "numero de", "total de", "numero")

    _IDENTIFIER_ALIASES = {
        "curp": ("curp",),
        "rfc": ("rfc",),
        "cuip": ("cuip",),
        "cib": ("cib",),
    }

    _SIMPLE_DATA_FIELDS = {
        "fecha de nacimiento": ("fecha nacimiento", "fecha_nacimiento", "nacimiento", "fecha nac"),
        "sexo": ("sexo", "genero", "género"),
        "nacionalidad": ("nacionalidad",),
        "alias": ("alias", "apodo"),
        "teléfono": ("telefono", "teléfono", "celular", "movil", "móvil"),
        "correo": ("correo", "email", "e-mail"),
    }

    def __init__(self, context_builder: ContextBuilder, *, max_list_items: int = 12) -> None:
        self.context_builder = context_builder
        self.max_list_items = max(3, max_list_items)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._routes: Counter[str] = Counter()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "llmRequestsAvoided": self._hits,
                "routes": dict(self._routes),
            }

    def _record_hit(self, route: str) -> None:
        with self._lock:
            self._hits += 1
            self._routes[route] += 1

    def _record_miss(self) -> None:
        with self._lock:
            self._misses += 1

    @staticmethod
    def _data_entries(profile: dict[str, Any]) -> list[dict[str, Any]]:
        rows = profile.get("data") or []
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _addresses(profile: dict[str, Any]) -> list[dict[str, Any]]:
        rows = profile.get("addresses") or []
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _entry_key(row: dict[str, Any]) -> str:
        return _norm(" ".join(str(row.get(key) or "") for key in ("dataType", "code", "name", "label")))

    @classmethod
    def _find_data(cls, profile: dict[str, Any], aliases: tuple[str, ...]) -> list[tuple[str, dict[str, Any]]]:
        matches: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for row in cls._data_entries(profile):
            key = cls._entry_key(row)
            if not any(_norm(alias) in key for alias in aliases):
                continue
            value = str(row.get("value") or "").strip()
            if not value or _norm(value) in seen:
                continue
            seen.add(_norm(value))
            matches.append((value, row))
        return matches

    @classmethod
    def _first_data(cls, profile: dict[str, Any], aliases: tuple[str, ...]) -> tuple[str, dict[str, Any]] | None:
        # Ruta de lookup O(n) con salida temprana: no construye listas ni recorre miles
        # de registros después de encontrar el dato solicitado.
        normalized_aliases = tuple(_norm(alias) for alias in aliases)
        for row in cls._data_entries(profile):
            key = cls._entry_key(row)
            if not any(alias in key for alias in normalized_aliases):
                continue
            value = str(row.get("value") or "").strip()
            if value:
                return value, row
        return None

    @classmethod
    def _identity(cls, profile: dict[str, Any]) -> dict[str, Any]:
        nombre = cls._first_data(profile, ("nombre", "nombres", "name"))
        paterno = cls._first_data(profile, ("apellido paterno", "primer apellido", "paterno"))
        materno = cls._first_data(profile, ("apellido materno", "segundo apellido", "materno"))
        sexo = cls._first_data(profile, ("sexo", "genero", "género"))
        nacimiento = cls._first_data(profile, ("fecha nacimiento", "fecha_nacimiento", "nacimiento"))

        pieces = [item[0] for item in (nombre, paterno, materno) if item]
        return {
            "full_name": " ".join(pieces).strip() or None,
            "nombre": nombre,
            "paterno": paterno,
            "materno": materno,
            "sexo": sexo,
            "nacimiento": nacimiento,
        }

    @staticmethod
    def _format_address(address: dict[str, Any]) -> str:
        street = _first_nonempty(address.get("street"))
        ext = _first_nonempty(address.get("exteriorNumber"))
        interior = _first_nonempty(address.get("interiorNumber"))
        neighborhood = _first_nonempty(address.get("neighborhood"))
        municipality = _first_nonempty(address.get("municipality"))
        state = _first_nonempty(address.get("state"))
        postal = _first_nonempty(address.get("postalCode"))
        addr_type = _first_nonempty(address.get("type"))

        first = " ".join(part for part in (street, ext) if part)
        if interior:
            first = f"{first}, int. {interior}" if first else f"Int. {interior}"
        parts = [part for part in (first, neighborhood, municipality, state) if part]
        text = ", ".join(parts)
        if postal:
            text += (", " if text else "") + f"C.P. {postal}"
        if addr_type and text:
            return f"{addr_type}: {text}"
        return text or addr_type or "Domicilio sin detalle disponible"

    @staticmethod
    def _graph_nodes(graph: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(graph, dict):
            return []
        return [row for row in (graph.get("nodes") or []) if isinstance(row, dict)]

    @staticmethod
    def _graph_links(graph: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(graph, dict):
            return []
        return [row for row in (graph.get("links") or []) if isinstance(row, dict)]

    @classmethod
    def _nodes_of_kind(cls, graph: dict[str, Any] | None, kind: str) -> list[dict[str, Any]]:
        aliases = {
            "vehicle": ("vehiculo", "vehicle", "auto", "automovil"),
            "weapon": ("arma", "weapon"),
            "person": ("persona", "person"),
        }[kind]
        out: list[dict[str, Any]] = []
        for node in cls._graph_nodes(graph):
            node_type = _norm(node.get("type"))
            if any(alias in node_type for alias in aliases):
                out.append(node)
        return out

    @staticmethod
    def _node_label(node: dict[str, Any]) -> str:
        title = str(node.get("title") or "").strip()
        subtitle = str(node.get("subtitle") or "").strip()
        if title and subtitle and _norm(subtitle) not in _norm(title):
            return f"{title} ({subtitle})"
        return title or subtitle or str(node.get("id") or "Elemento")

    @classmethod
    def _source_counts(cls, profile: dict[str, Any]) -> Counter[str]:
        counts: Counter[str] = Counter()
        for row in [*cls._data_entries(profile), *cls._addresses(profile)]:
            origins = row.get("origins") or []
            if not isinstance(origins, list):
                continue
            for origin in origins:
                if not isinstance(origin, dict):
                    continue
                code = _first_nonempty(origin.get("sourceCode"), origin.get("source"), origin.get("system"))
                if code:
                    counts[code.strip()] += 1
        return counts

    @staticmethod
    def _evidence(path: str, value: Any, section: str, score: int = 100) -> dict[str, Any]:
        return {"path": path, "value": value, "section": section, "score": score}

    @staticmethod
    def _fact_evidence(fact: ContextFact, score: int = 100) -> dict[str, Any]:
        return {"path": fact.path, "value": fact.value, "section": fact.section, "score": score}

    @staticmethod
    def _is_count_question(q: str) -> bool:
        return any(term in q for term in FastQueryRouter._COUNT_TERMS)

    @staticmethod
    def _is_analytic(q: str) -> bool:
        return any(term in q for term in FastQueryRouter._ANALYTIC_TERMS)

    def _answer_identifier(self, profile: dict[str, Any], q: str) -> FastPathResult | None:
        for label, aliases in self._IDENTIFIER_ALIASES.items():
            if not any(alias in q for alias in aliases):
                continue
            found = self._first_data(profile, aliases)
            if not found:
                return FastPathResult(
                    text=f"No hay un {label.upper()} disponible en el perfil consolidado.",
                    evidence=[], intent="identity", route=f"identifier:{label}:missing",
                )
            value, row = found
            return FastPathResult(
                text=f"El {label.upper()} registrado es {value}.",
                evidence=[self._evidence(f"$.profile.data[{row.get('dataId', label)}]", value, "identifiers")],
                intent="identity", route=f"identifier:{label}",
            )
        return None


    def _answer_simple_data_field(self, profile: dict[str, Any], q: str) -> FastPathResult | None:
        for label, aliases in self._SIMPLE_DATA_FIELDS.items():
            if not any(_norm(alias) in q for alias in aliases):
                continue
            matches = self._find_data(profile, aliases)
            if not matches:
                return FastPathResult(
                    text=f"No hay un dato de {label} disponible en el perfil consolidado.",
                    evidence=[], intent="identity", route=f"field:{label}:missing",
                )
            values = [value for value, _ in matches[: self.max_list_items]]
            if len(values) == 1:
                text = f"El dato registrado de {label} es {values[0]}."
            else:
                text = f"Se encontraron {len(matches)} valores para {label}: " + "; ".join(values) + "."
            evidence = [
                self._evidence(f"$.profile.data[{row.get('dataId', label)}]", value, "identity")
                for value, row in matches[: self.max_list_items]
            ]
            return FastPathResult(text=text, evidence=evidence, intent="identity", route=f"field:{label}")
        return None

    def _answer_identity(self, profile: dict[str, Any], q: str) -> FastPathResult | None:
        if not any(term in q for term in ("nombre", "quien es", "quién es", "identidad")):
            return None
        identity = self._identity(profile)
        full_name = identity["full_name"]
        if not full_name:
            return None
        evidence: list[dict[str, Any]] = []
        for label in ("nombre", "paterno", "materno"):
            item = identity.get(label)
            if item:
                value, row = item
                evidence.append(self._evidence(f"$.profile.data[{row.get('dataId', label)}]", value, "identity"))
        return FastPathResult(
            text=f"El nombre registrado en el perfil es {full_name}.",
            evidence=evidence,
            intent="identity",
            route="identity:name",
        )

    def _answer_addresses(self, profile: dict[str, Any], q: str) -> FastPathResult | None:
        if not any(term in q for term in ("domicilio", "domicilios", "direccion", "dirección", "ubicacion", "ubicación", "residencia", "vive")):
            return None
        addresses = self._addresses(profile)
        if self._is_count_question(q):
            text = f"El perfil tiene {len(addresses)} {_plural(len(addresses), 'domicilio')} registrado{'' if len(addresses) == 1 else 's'}."
        elif not addresses:
            text = "No hay domicilios registrados en el perfil consolidado."
        else:
            formatted = [self._format_address(row) for row in addresses[: self.max_list_items]]
            lines = [f"Domicilios registrados: {len(addresses)}."] + [f"{idx + 1}. {value}" for idx, value in enumerate(formatted)]
            if len(addresses) > len(formatted):
                lines.append(f"Hay {len(addresses) - len(formatted)} domicilio(s) adicional(es).")
            text = "\n".join(lines)
        evidence = [
            self._evidence(f"$.profile.addresses[{idx}]", self._format_address(row), "addresses")
            for idx, row in enumerate(addresses[: self.max_list_items])
        ]
        return FastPathResult(text=text, evidence=evidence, intent="addresses", route="addresses")

    def _answer_graph_kind(self, graph: dict[str, Any] | None, q: str, *, kind: str, label: str, intent: str) -> FastPathResult | None:
        terms = {
            "vehicle": ("vehiculo", "vehículos", "vehículo", "vehiculos", "carro", "carros", "auto", "autos", "placa", "vin", "niv"),
            "weapon": ("arma", "armas", "weapon", "calibre"),
            "person": ("persona", "personas"),
        }[kind]
        if not any(term in q for term in terms):
            return None
        nodes = self._nodes_of_kind(graph, kind)
        if self._is_count_question(q):
            text = f"Se identificaron {len(nodes)} {_plural(len(nodes), label)} en el grafo actual."
        elif not nodes:
            text = f"No se identificaron {_plural(2, label)} en el grafo actual."
        else:
            labels = [self._node_label(node) for node in nodes[: self.max_list_items]]
            lines = [f"Se identificaron {len(nodes)} {_plural(len(nodes), label)}:"] + [f"{idx + 1}. {value}" for idx, value in enumerate(labels)]
            if len(nodes) > len(labels):
                lines.append(f"Hay {len(nodes) - len(labels)} elemento(s) adicional(es) no listados para mantener la respuesta breve.")
            text = "\n".join(lines)
        evidence = [
            self._evidence(f"$.graph.nodes[{idx}]", self._node_label(node), intent)
            for idx, node in enumerate(nodes[: self.max_list_items])
        ]
        return FastPathResult(text=text, evidence=evidence, intent=intent, route=f"graph:{kind}")

    def _answer_relation_count(self, graph: dict[str, Any] | None, q: str) -> FastPathResult | None:
        relation_terms = ("vinculo", "vínculo", "vinculos", "vínculos", "relacion", "relación", "relaciones", "conexiones", "links")
        if not any(term in q for term in relation_terms) or not self._is_count_question(q):
            return None
        links = self._graph_links(graph)
        return FastPathResult(
            text=f"El grafo actual contiene {len(links)} {_plural(len(links), 'vínculo')} entre sus nodos.",
            evidence=[self._evidence("$.graph.links", len(links), "relations")],
            intent="relations",
            route="graph:relation-count",
        )

    def _answer_related_count(self, graph: dict[str, Any] | None, q: str) -> FastPathResult | None:
        if not self._is_count_question(q):
            return None
        if not any(term in q for term in ("elementos relacionados", "elementos", "nodos", "relacionados")):
            return None
        nodes = self._graph_nodes(graph)
        selected_id = str((graph or {}).get("selectedNodeId") or "") if isinstance(graph, dict) else ""
        related = len(nodes)
        if selected_id and any(str(node.get("id") or "") == selected_id for node in nodes):
            related = max(0, related - 1)
        return FastPathResult(
            text=f"Hay {related} {_plural(related, 'elemento relacionado', 'elementos relacionados')} en el grafo actual.",
            evidence=[self._evidence("$.graph.nodes", related, "relations")],
            intent="relations",
            route="graph:related-count",
        )

    def _answer_sources(self, profile: dict[str, Any], q: str) -> FastPathResult | None:
        if not any(term in q for term in ("fuente", "fuentes", "origen", "origenes", "orígenes", "sistema", "sistemas")):
            return None
        counts = self._source_counts(profile)
        if self._is_count_question(q):
            total = sum(counts.values())
            text = f"El perfil contiene {total} referencias de origen distribuidas en {len(counts)} {_plural(len(counts), 'fuente')} distinta{'' if len(counts) == 1 else 's'}."
        elif not counts:
            text = "No se encontraron fuentes de origen en los datos recibidos del perfil."
        else:
            lines = [f"Fuentes registradas: {len(counts)}."]
            lines.extend(f"- {source}: {count} referencia(s)" for source, count in counts.most_common(self.max_list_items))
            text = "\n".join(lines)
        evidence = [self._evidence("$.profile.origins", f"{source}: {count}", "sources") for source, count in counts.most_common(self.max_list_items)]
        return FastPathResult(text=text, evidence=evidence, intent="sources", route="sources")

    def _summary(self, profile: dict[str, Any], graph: dict[str, Any] | None) -> FastPathResult:
        identity = self._identity(profile)
        full_name = identity["full_name"] or "sin nombre consolidado disponible"
        identifier_parts: list[str] = []
        evidence: list[dict[str, Any]] = []
        for label, aliases in self._IDENTIFIER_ALIASES.items():
            found = self._first_data(profile, aliases)
            if found:
                value, row = found
                identifier_parts.append(f"{label.upper()}: {value}")
                evidence.append(self._evidence(f"$.profile.data[{row.get('dataId', label)}]", value, "identifiers"))

        addresses = self._addresses(profile)
        vehicles = self._nodes_of_kind(graph, "vehicle")
        weapons = self._nodes_of_kind(graph, "weapon")
        links = self._graph_links(graph)
        source_counts = self._source_counts(profile)

        lines = [f"Resumen del perfil de {full_name}."]
        if identifier_parts:
            lines.append("Identificadores: " + "; ".join(identifier_parts) + ".")
        if addresses:
            lines.append(f"Domicilios registrados: {len(addresses)}. Principal: {self._format_address(addresses[0])}.")
            evidence.append(self._evidence("$.profile.addresses[0]", self._format_address(addresses[0]), "addresses"))
        else:
            lines.append("Domicilios registrados: 0.")

        if graph is not None:
            lines.append(f"Grafo actual: {len(vehicles)} vehículo(s), {len(weapons)} arma(s) y {len(links)} vínculo(s).")
            evidence.extend([
                self._evidence("$.graph.vehicles.count", len(vehicles), "vehicles"),
                self._evidence("$.graph.weapons.count", len(weapons), "weapons"),
                self._evidence("$.graph.links.count", len(links), "relations"),
            ])
            if vehicles:
                labels = ", ".join(self._node_label(node) for node in vehicles[:4])
                lines.append(f"Vehículos visibles: {labels}{'…' if len(vehicles) > 4 else '.'}")

        if source_counts:
            summary = ", ".join(f"{name} ({count})" for name, count in source_counts.most_common(5))
            lines.append(f"Fuentes de origen: {summary}.")
            evidence.extend(self._evidence("$.profile.origins", f"{name}: {count}", "sources") for name, count in source_counts.most_common(5))

        # El resumen rápido no ejecuta inferencias de discrepancias: esa tarea se reserva
        # al modo analítico con Qwen para no introducir conclusiones automáticas dudosas.
        lines.append("Discrepancias: este resumen rápido no realiza inferencias; solicita un análisis si necesitas comparar fuentes o detectar inconsistencias.")

        return FastPathResult(
            text="\n".join(lines),
            evidence=evidence[:36],
            intent="summary",
            route="summary:deterministic",
        )

    def try_answer(
        self,
        *,
        profile: dict[str, Any],
        profile_id: str,
        question: str,
        graph: dict[str, Any] | None,
        selected_node: dict[str, Any] | None,
        mode: str,
        thinking: bool,
        allow_summary: bool = True,
    ) -> FastPathResult | None:
        q = _norm(question)

        # `deep` siempre significa análisis por LLM. Las preguntas analíticas también.
        if mode == "deep" or self._is_analytic(q):
            self._record_miss()
            return None

        # Preguntas muy largas suelen contener condiciones que un extractor literal no debe simplificar.
        if len(q) > 500 and not any(term in q for term in self._SUMMARY_TERMS):
            self._record_miss()
            return None

        result: FastPathResult | None = None
        if allow_summary and any(term in q for term in self._SUMMARY_TERMS):
            result = self._summary(profile, graph)
        if result is None:
            result = self._answer_identifier(profile, q)
        if result is None:
            result = self._answer_simple_data_field(profile, q)
        if result is None:
            result = self._answer_identity(profile, q)
        if result is None:
            result = self._answer_addresses(profile, q)
        if result is None:
            result = self._answer_graph_kind(graph, q, kind="vehicle", label="vehículo", intent="vehicles")
        if result is None:
            result = self._answer_graph_kind(graph, q, kind="weapon", label="arma", intent="weapons")
        if result is None and self._is_count_question(q):
            result = self._answer_graph_kind(graph, q, kind="person", label="persona", intent="relations")
        if result is None:
            result = self._answer_relation_count(graph, q)
        if result is None:
            result = self._answer_related_count(graph, q)
        if result is None:
            result = self._answer_sources(profile, q)

        if result is None:
            self._record_miss()
            return None

        self._record_hit(result.route)
        return result
