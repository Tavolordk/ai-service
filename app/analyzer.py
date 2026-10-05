from __future__ import annotations

from collections.abc import Iterable
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
    items = history[-max_messages:]
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
    ) -> list[dict[str, str]]:
        system = (
            SYSTEM_PROMPT
            + "\nFOCO DE ESTA PREGUNTA:\n"
            + focus_instruction(question)
            + "\n\nOPTIMIZACIÓN DE CONTEXTO: el contexto ya fue reducido de forma determinista. "
              "No pidas el JSON original ni supongas que faltan datos sólo porque no se incluyeron campos irrelevantes. "
              "Si una sección indica ausencia, respétala; si la evidencia no permite afirmar algo, dilo."
        )
        user = (
            f"PREGUNTA:\n{question}\n\n"
            f"INTENCIÓN: {intent}\n\n"
            f"CONTEXTO VERIFICADO Y COMPACTADO:\n{context or '[sin evidencia pertinente]'}"
            "\n\nIDIOMA OBLIGATORIO: toda generación textual, incluido cualquier canal privado de thinking/reasoning, debe producirse en español mexicano desde el primer token. "
            "SALIDA VISIBLE: responde únicamente en español mexicano, con frases completas. "
            "No muestres ni traduzcas el razonamiento interno; entrega sólo la respuesta final solicitada. "
            "No repitas todos los registros si basta con sintetizarlos."
        )
        return [
            {"role": "system", "content": system},
            *_history_messages(history, self.settings.llm_history_messages),
            {"role": "user", "content": user},
        ]

    def _runtime(self, *, intent: str, mode: str, thinking: bool) -> RuntimeBudget:
        return choose_runtime_budget(
            intent=intent,
            mode=mode,
            thinking=thinking,
            max_num_ctx=self.settings.llm_num_ctx,
            max_predict=self.settings.llm_max_predict,
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
                )
            except LocalLlmError:
                break
            if summary:
                summaries.append({"path": f"deep.batch[{idx}]", "value": summary, "score": 0})
        return summaries or evidence[: self.settings.llm_max_evidence]

    def stream_inline(self, request: ChatRequest) -> AnalysisStream:
        profile = request.profile.model_dump(mode="json")

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

        selection = self.context_builder.select(
            profile=profile,
            profile_id=request.profile.profileId,
            question=request.question,
            mode=request.mode,
            selected_node=request.selectedNode,
            graph=request.graph,
        )

        context = selection.context
        evidence = list(selection.evidence)

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

        runtime = self._runtime(intent=selection.intent, mode=request.mode, thinking=request.thinking)
        messages = self._messages(
            request.question,
            context,
            request.history,
            intent=selection.intent,
        )
        return AnalysisStream(
            evidence=evidence,
            chunks=self.llm.stream_chat_events(
                messages,
                thinking=request.thinking,
                num_ctx=runtime.num_ctx,
                num_predict=runtime.num_predict,
            ),
            mode="local-llm",
            model=self.settings.llm_model,
            thinking=request.thinking,
            route=f"llm:{selection.intent}",
        )

    def stream_case(self, case_id: str, request: CaseChatRequest) -> AnalysisStream:
        intent = classify_intent(request.question)
        multiplier = 8 if request.mode == "deep" else 2 if request.mode == "auto" else 1
        evidence = self._case_evidence(case_id, request.question, multiplier=multiplier)
        if request.mode == "deep":
            evidence = self._deep_reduce(request.question, evidence)
        max_chars = self.settings.context_deep_max_chars if request.mode == "deep" else self.settings.context_max_chars
        context = _evidence_text(evidence, max_chars)
        runtime = self._runtime(intent=intent, mode=request.mode, thinking=request.thinking)
        messages = self._messages(request.question, context, request.history, intent=intent)
        return AnalysisStream(
            evidence=evidence,
            chunks=self.llm.stream_chat_events(
                messages,
                thinking=request.thinking,
                num_ctx=runtime.num_ctx,
                num_predict=runtime.num_predict,
            ),
            mode="local-llm",
            model=self.settings.llm_model,
            thinking=request.thinking,
            route=f"case-llm:{intent}",
        )
