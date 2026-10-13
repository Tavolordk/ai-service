from __future__ import annotations

from collections.abc import Iterable
import re
from dataclasses import dataclass
from typing import Any

from .case_store import CaseStore
from .config import Settings
from .context_builder import ContextBuilder, classify_intent
from .fast_router import FastQueryRouter
from .models import CaseChatRequest, ChatRequest
from .ollama_client import LlmStreamChunk, LocalLlmError, LocalOllamaClient
from .police_domain import SYSTEM_PROMPT, focus_instruction
from .runtime_policy import RuntimeBudget, choose_runtime_budget
from .token_budget import fit_messages
from .evidence_context import merge_context, priority_evidence
from .documentary_compare import (compare_documentary_values, compact_documentary_context,
                                  documentary_report, straightforward_comparison,
                                  select_differences, direct_conflicting_fact)




@dataclass(frozen=True, slots=True)
class AnalysisStream:
    evidence: list[dict[str, Any]]
    chunks: Iterable[LlmStreamChunk]
    mode: str
    model: str | None
    thinking: bool
    route: str


def _single_answer_chunk(text: str) -> Iterable[LlmStreamChunk]:
    # El fast path usa el mismo flujo simple que una respuesta normal: delta + done.
    # `replace` se reserva para correcciones reales de una salida LLM provisional.
    yield LlmStreamChunk(kind="content", text=text)

def _evidence_text(evidence: list[dict[str, Any]], max_chars: int) -> str:
    parts: list[str] = []
    size = 0
    for item in evidence:
        path = item.get("path") or "$"
        value = item.get("value")
        line = f"- {path}: {value}"
        if size + len(line) + 1 > max_chars:
            break
        parts.append(line)
        size += len(line) + 1
    return "\n".join(parts)


def _history_messages(history: list[Any], max_messages: int) -> list[dict[str, str]]:
    items = history[-max_messages:] if max_messages > 0 else []
    return [{"role": msg.role, "content": msg.content} for msg in items]


class PoliceAnalyzer:
    def __init__(self, settings: Settings, llm: LocalOllamaClient, case_store: CaseStore) -> None:
        self.settings = settings
        self.llm = llm
        self.case_store = case_store
        self.context_builder = ContextBuilder(
            max_chars=settings.context_max_chars,
            deep_max_chars=settings.context_deep_max_chars,
            max_facts_per_section=settings.context_max_facts_per_section,
            cache_entries=settings.context_cache_entries,
            cache_ttl_seconds=settings.context_cache_ttl_seconds,
        )
        self.fast_router = FastQueryRouter(
            self.context_builder,
            max_list_items=settings.fast_max_list_items,
        )

    def cache_stats(self) -> dict[str, int]:
        return self.context_builder.cache_stats()

    def fast_path_stats(self) -> dict[str, Any]:
        return self.fast_router.stats()

    def _messages(
        self,
        question: str,
        context: str,
        history: list[Any],
        *,
        intent: str,
        num_ctx: int | None = None,
        num_predict: int | None = None,
    ) -> list[dict[str, str]]:
        system = SYSTEM_PROMPT + "\nFOCO: " + focus_instruction(question)
        user = (
            f"PREGUNTA: {question}\n"
            f"INTENCIÓN: {intent}\n"
            f"CONTEXTO VERIFICADO Y COMPACTADO:\n{context or '[sin evidencia pertinente]'}"
        )
        messages = [
            {"role": "system", "content": system},
            *_history_messages(history, self.settings.llm_history_messages),
            {"role": "user", "content": user},
        ]
        if num_ctx is not None:
            return fit_messages(messages, num_ctx, num_predict or self.settings.llm_max_predict)
        return messages

    def _runtime(self, *, intent: str, mode: str, thinking: bool) -> RuntimeBudget:
        return choose_runtime_budget(
            intent=intent,
            mode=mode,
            thinking=thinking,
            max_num_ctx=self.settings.llm_num_ctx,
            max_predict=(self.settings.llm_thinking_max_predict if thinking else self.settings.llm_max_predict),
        )

    def _case_evidence(self, case_id: str, question: str, *, multiplier: int = 1) -> list[dict[str, Any]]:
        return self.case_store.search(
            case_id,
            question,
            limit=min(self.settings.llm_max_evidence * multiplier, 2000),
        )

    def _deep_reduce(self, question: str, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sólo para casos masivos explícitamente marcados como deep.

        La reducción normal de perfiles ya no usa al LLM: ContextBuilder la hace de forma
        determinista. Esta ruta queda para archivos/casos gigantes donde incluso la búsqueda
        indexada devuelve demasiada evidencia.
        """
        raw = _evidence_text(evidence, self.settings.deep_batch_chars * self.settings.deep_max_chunks)
        if len(raw) <= self.settings.deep_batch_chars:
            return evidence

        chunks = [raw[i:i + self.settings.deep_batch_chars] for i in range(0, len(raw), self.settings.deep_batch_chars)]
        chunks = chunks[: min(self.settings.deep_max_chunks, 24)]
        summaries: list[dict[str, Any]] = []
        for idx, chunk in enumerate(chunks):
            prompt = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (
                    "Reduce este lote únicamente a hechos útiles para la pregunta. "
                    "Conserva identificadores, fechas, fuentes, discrepancias y rutas relevantes. "
                    "No muestres razonamiento.\n\n"
                    f"PREGUNTA: {question}\n\nLOTE {idx + 1}:\n{chunk}"
                )},
            ]
            try:
                summary = self.llm.chat(
                    prompt,
                    thinking=False,
                    num_ctx=min(6144, self.settings.llm_num_ctx),
                    num_predict=min(650, self.settings.llm_max_predict),
                    model=self.settings.model_for_mode("deep"),
                )
            except LocalLlmError:
                break
            if summary:
                summaries.append({"path": f"deep.batch[{idx}]", "value": summary, "score": 0})
        return summaries or evidence[: self.settings.llm_max_evidence]

    def stream_inline(self, request: ChatRequest) -> AnalysisStream:
        profile = request.profile.model_dump(mode="json")
        # Compatibilidad: el frontend puede enviar profileId + caseId tras indexar
        # un expediente masivo, sin volver a transferir cientos de MB por turno.
        if (request.caseId and set(profile.keys()) <= {"profileId"}
                and not request.graph):
            selected = request.selectedNode if isinstance(request.selectedNode, dict) else {}
            return self.stream_case(request.caseId, CaseChatRequest(
                question=request.question, history=request.history,
                thinking=request.thinking, mode=request.mode,
                nodeId=str(selected.get("id")) if selected.get("id") is not None else None,
            ))

        # El Fast Router habitual devuelve el primer valor disponible. Cuando un
        # mismo dato tiene DOS versiones, se debe cotejar antes de contestar.
        q_norm = request.question.casefold()
        documentary_terms = ("discrepan", "diferenc", "inconsisten", "contradic", "compar", "contrast",
                             "nacimiento", "fecha", "placa", "vin", "curp", "rfc", "cuip")
        inspect = (request.mode == "deep" or any(x in q_norm for x in documentary_terms))
        differences = compare_documentary_values(profile) if inspect else []
        if (self.settings.fast_path_enabled and not request.caseId and not request.thinking
                and request.mode != "deep" and differences
                and (straightforward_comparison(request.question, request.mode, request.thinking)
                     or direct_conflicting_fact(request.question, differences))):
            selected = select_differences(differences, request.question)
            evidence = [
                {"path": record.path, "value": record.value, "section": "discrepancies",
                 "score": 100, "sourceCodes": [record.source]}
                for difference in selected for record in difference.values
            ]
            return AnalysisStream(
                evidence=evidence[:self.settings.llm_max_evidence],
                chunks=_single_answer_chunk(documentary_report(differences, request.question)),
                mode="deterministic-fallback", model="SPM Document Comparator",
                thinking=False, route="documentary:comparison",
            )

        # FAST PATH: preguntas factuales y resúmenes frecuentes se resuelven sin Qwen.
        # Incluso si el toggle de razonamiento está activo, una consulta literal no necesita
        # inferencia: devolvemos una respuesta verificable y evitamos gastar CPU/minutos.
        if self.settings.fast_path_enabled and not request.caseId:
            fast = self.fast_router.try_answer(
                profile=profile,
                profile_id=request.profile.profileId,
                question=request.question,
                graph=request.graph,
                selected_node=request.selectedNode,
                mode=request.mode,
                thinking=request.thinking,
                allow_summary=self.settings.fast_summary_enabled,
            )
            if fast is not None:
                return AnalysisStream(
                    evidence=fast.evidence,
                    chunks=_single_answer_chunk(fast.text),
                    mode="deterministic-fallback",
                    model="SPM Fast Router",
                    thinking=False,
                    route=fast.route,
                )

        # Preguntas analíticas con conflictos verificables reciben un contexto
        # especializado, limitado y centrado en evidencias, no el perfil entero.
        q = request.question.casefold()
        # No reemplazar contexto de casos indexados. Para nodos seleccionados,
        # el contexto focalizado incorpora explícitamente su identidad.
        use_compact = bool(not request.caseId and differences
                           and any(term in q for term in (
            "discrepan", "diferenc", "contradic", "inconsisten", "compar", "contrast", "perfil", "investig")))
        if use_compact:
            context = compact_documentary_context(profile, request.graph, request.question,
                                                 mode=request.mode, selected_node=request.selectedNode)
            evidence = [
                {"path": record.path, "value": record.value, "section": "discrepancies",
                 "score": 100, "sourceCodes": [record.source]}
                for difference in select_differences(differences, request.question)
                for record in difference.values
            ]
            intent = "discrepancies"
        else:
            selection = self.context_builder.select(
                profile=profile,
                profile_id=request.profile.profileId,
                question=request.question,
                mode=request.mode,
                selected_node=request.selectedNode,
                graph=request.graph,
            )
            context = merge_context(selection.context, priority_evidence(profile, request.question, request.mode))
            evidence = list(selection.evidence)
            intent = selection.intent

        # En sesiones normales no incluimos JSON crudo ni evidencia repetida.
        # El cotejo documental y el Context Builder convergen en el mismo contrato.
        # Si hay caseId, sólo añadimos los registros más relevantes; el perfil compacto
        # sigue siendo la base y nunca volvemos a mandar el JSON completo a Qwen.
        if request.caseId:
            multiplier = 4 if request.mode == "deep" else 1
            case_evidence = self._case_evidence(request.caseId, request.question, multiplier=multiplier)
            if request.mode == "deep":
                case_evidence = self._deep_reduce(request.question, case_evidence)
            remaining = max(2000, (self.settings.context_deep_max_chars if request.mode == "deep" else self.settings.context_max_chars) - len(context))
            case_text = _evidence_text(case_evidence, remaining)
            if case_text:
                context += "\n\n[REGISTROS DEL CASO RELEVANTES]\n" + case_text
            evidence = case_evidence + evidence

        runtime = self._runtime(intent=intent, mode=request.mode, thinking=request.thinking)
        messages = self._messages(
            request.question,
            context,
            request.history,
            intent=intent,
            num_ctx=runtime.num_ctx,
            num_predict=runtime.num_predict,
        )
        return AnalysisStream(
            evidence=evidence,
            chunks=self.llm.stream_chat_events(
                messages,
                thinking=request.thinking,
                num_ctx=runtime.num_ctx,
                num_predict=runtime.num_predict,
                model=self.settings.model_for_mode(request.mode),
            ),
            mode="local-llm",
            model=self.settings.model_for_mode(request.mode),
            thinking=request.thinking,
            route=f"llm:{intent}",
        )

    def stream_case(self, case_id: str, request: CaseChatRequest) -> AnalysisStream:
        """Consulta acotada al índice, incluso con millones de escalares y vínculos."""
        intent = classify_intent(request.question)
        # Entrada explícita de nodo. No adivinar IDs desde palabras comunes.
        node_id = request.nodeId
        if node_id is None:
            match = re.search(r"\b(?:nodeId|nodoId)\s*[:=]\s*([A-Za-z0-9_.:-]{1,256})", request.question, re.I)
            node_id = match.group(1) if match else None
        if not node_id and not request.thinking and request.mode != "deep":
            q = request.question.casefold()
            count_ask = any(t in q for t in ("cuántos", "cuantos", "cantidad", "total", "número", "numero"))
            summary_ask = any(t in q for t in ("resumen del caso", "resumen del expediente", "estadísticas del caso"))
            if count_ask or summary_ask:
                m = self.case_store.metadata(case_id)
                if summary_ask or (count_ask and any(t in q for t in ("grafo", "vínculo", "vinculo", "nodo", "hecho", "registro"))):
                    report = (f"El expediente {case_id} contiene {m.facts} hechos escalares indexados, "
                              f"{m.links} vínculos y {m.nodes} nodos de grafo. "
                              "Estos conteos corresponden a datos indexados, no a conclusiones sobre personas.")
                    return AnalysisStream([], _single_answer_chunk(report),
                                          "deterministic-fallback", "SPM Case Index", False,
                                          "case:counts")
        graph_result = self.case_store.links(case_id, node_id, limit=24) if node_id else None
        graph_evidence = []
        if graph_result:
            graph_evidence = [
                {"path": link["path"],
                 "value": f"{link['sourceLabel']} --[{link['relation']}]--> {link['targetLabel']} ",
                 "section": "relations", "score": 200}
                for link in graph_result["links"]
            ]
            q = request.question.casefold()
            if (self.settings.fast_path_enabled and not request.thinking and request.mode != "deep"
                    and any(term in q for term in ("cuántos", "cuantos", "cantidad", "total de", "número de", "numero de"))
                    and any(term in q for term in ("relacion", "vínculo", "vinculo", "arista", "enlace"))):
                label = "vínculo" if graph_result["total"] == 1 else "vínculos"
                response = f"El nodo {node_id} tiene {graph_result['total']} {label} indexados en este caso."
                if graph_result["links"]:
                    response += " Ejemplos: " + "; ".join(x["value"] for x in graph_evidence[:5])
                return AnalysisStream(graph_evidence, _single_answer_chunk(response),
                                      "deterministic-fallback", "SPM Indexed Graph", False,
                                      "case:graph-degree")

        # Comparador persistente sobre case_fields: conserva el cotejo 4.1.2
        # también cuando hay millones de escalares fuera de una petición inline.
        differences = self.case_store.documentary_differences(case_id, request.question)
        if (differences and self.settings.fast_path_enabled and not request.thinking
                and request.mode != "deep" and (
                    straightforward_comparison(request.question, request.mode, request.thinking)
                    or direct_conflicting_fact(request.question, differences))):
            ev = [{"path": x.path, "value": x.value, "section": "discrepancies", "sourceCodes": [x.source], "score": 100}
                  for d in differences for x in d.values]
            return AnalysisStream(ev[:self.settings.llm_max_evidence],
                                  _single_answer_chunk(documentary_report(differences, request.question)),
                                  "deterministic-fallback", "SPM Document Comparator", False,
                                  "case:documentary")
        attributes, complete = self.case_store.attribute_evidence(case_id, request.question,
                                                              limit=self.settings.llm_max_evidence)
        q = request.question.casefold()
        if (attributes and self.settings.fast_path_enabled and not request.thinking and request.mode != "deep"
                and any(term in q for term in ("cuál", "cual", "qué", "que", "enumera", "muestra", "dime"))
                and not any(term in q for term in ("analiza", "hipótesis", "hipotesis", "por qué", "porque", "compar", "investig"))):
            answer = "Registros indexados con fuente:\n" + "\n".join("- " + x["value"] for x in attributes)
            if not complete:
                answer += "\nLa lista es parcial: hay más registros; especifique una entidad."
            return AnalysisStream(attributes, _single_answer_chunk(answer),
                                  "deterministic-fallback", "SPM Indexed Fields", False,
                                  "case:attributes")
        multiplier = 2 if request.mode == "deep" else 1
        search = self._case_evidence(case_id, request.question, multiplier=multiplier)
        documentary_ev = [{"path": x.path, "value": x.value, "section": "discrepancies",
                            "sourceCodes": [x.source], "score": 100}
                           for d in differences for x in d.values]
        evidence = (documentary_ev + graph_evidence + attributes + search)[:self.settings.case_search_max_results]
        if not evidence:
            # No inventar hechos cuando el índice no tiene coincidencias.
            return AnalysisStream([], _single_answer_chunk(
                "No se encontraron registros pertinentes en el índice para esta pregunta. "
                "Prueba con una placa, VIN, CURP o nodeId específico; no se infiere ausencia en el expediente completo."
            ), "deterministic-fallback", "SPM Case Index", False, "case:no-matches")
        max_chars = self.settings.context_deep_max_chars if request.mode == "deep" else self.settings.context_max_chars
        # Sólo evidencia indexada; nunca resumir lotes llamando repetidamente a Qwen.
        context = _evidence_text(evidence, max_chars)
        runtime = self._runtime(intent=intent, mode=request.mode, thinking=request.thinking)
        messages = self._messages(request.question, context, request.history, intent=intent,
                                  num_ctx=runtime.num_ctx, num_predict=runtime.num_predict)
        return AnalysisStream(
            evidence=evidence,
            chunks=self.llm.stream_chat_events(
                messages, thinking=request.thinking, num_ctx=runtime.num_ctx,
                num_predict=runtime.num_predict, model=self.settings.model_for_mode(request.mode),
            ),
            mode="local-llm", model=self.settings.model_for_mode(request.mode),
            thinking=request.thinking, route=f"case-llm:{intent}",
        )
