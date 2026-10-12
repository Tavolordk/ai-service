from __future__ import annotations

import http.client
import json
import re
import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from .config import Settings
from .token_budget import fit_messages
from .evidence_guard import ungrounded_identifiers, missing_documented_pairs, missing_documented_sources, safe_evidence_fallback


class LocalLlmError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LlmStreamChunk:
    kind: Literal["reasoning", "content", "replace"]
    text: str


class _ThinkingContentFilter:
    """Extrae bloques <think>...</think> incluso si las etiquetas llegan partidas."""

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self.buffer = ""
        self.inside_think = False

    def feed(self, chunk: str) -> str:
        self.buffer += chunk
        reasoning: list[str] = []
        while self.buffer:
            lowered = self.buffer.lower()
            if self.inside_think:
                close_idx = lowered.find(self.CLOSE)
                if close_idx >= 0:
                    if close_idx:
                        reasoning.append(self.buffer[:close_idx])
                    self.buffer = self.buffer[close_idx + len(self.CLOSE):]
                    self.inside_think = False
                    continue

                hold = 0
                max_suffix = min(len(self.buffer), len(self.CLOSE) - 1)
                for size in range(1, max_suffix + 1):
                    if self.CLOSE.startswith(self.buffer[-size:].lower()):
                        hold = size
                emit_until = len(self.buffer) - hold
                if emit_until > 0:
                    reasoning.append(self.buffer[:emit_until])
                    self.buffer = self.buffer[emit_until:]
                break

            open_idx = lowered.find(self.OPEN)
            if open_idx >= 0:
                self.buffer = self.buffer[open_idx + len(self.OPEN):]
                self.inside_think = True
                continue

            hold = 0
            max_suffix = min(len(self.buffer), len(self.OPEN) - 1)
            for size in range(1, max_suffix + 1):
                if self.OPEN.startswith(self.buffer[-size:].lower()):
                    hold = size
            self.buffer = self.buffer[-hold:] if hold else ""
            break

        return "".join(reasoning)

    def flush(self) -> str:
        reasoning = self.buffer if self.inside_think else ""
        self.buffer = ""
        self.inside_think = False
        return reasoning


class _VisibleContentFilter:
    """Elimina bloques <think>...</think> incluso si llegan partidos entre chunks."""

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self.buffer = ""
        self.inside_think = False

    def feed(self, chunk: str) -> str:
        self.buffer += chunk
        visible: list[str] = []
        while self.buffer:
            lowered = self.buffer.lower()
            if self.inside_think:
                close_idx = lowered.find(self.CLOSE)
                if close_idx >= 0:
                    self.buffer = self.buffer[close_idx + len(self.CLOSE):]
                    self.inside_think = False
                    continue
                keep = min(len(self.buffer), len(self.CLOSE) - 1)
                self.buffer = self.buffer[-keep:] if keep else ""
                break

            open_idx = lowered.find(self.OPEN)
            if open_idx >= 0:
                if open_idx:
                    visible.append(self.buffer[:open_idx])
                self.buffer = self.buffer[open_idx + len(self.OPEN):]
                self.inside_think = True
                continue

            hold = 0
            max_suffix = min(len(self.buffer), len(self.OPEN) - 1)
            for size in range(1, max_suffix + 1):
                if self.OPEN.startswith(self.buffer[-size:].lower()):
                    hold = size
            emit_until = len(self.buffer) - hold
            if emit_until > 0:
                visible.append(self.buffer[:emit_until])
                self.buffer = self.buffer[emit_until:]
            break
        return "".join(visible)

    def flush(self) -> str:
        if self.inside_think:
            self.buffer = ""
            return ""
        visible = self.buffer
        self.buffer = ""
        return visible


_ENGLISH_MARKERS = {
    "the", "and", "user", "profile", "address", "first", "need", "data", "given",
    "there", "which", "based", "summary", "context", "vehicle", "sources", "relationship",
    "this", "that", "with", "from", "for", "into", "also", "should", "would", "could",
    "okay", "wait", "draft", "rephrase", "check", "make", "sure",
}

_SPANISH_MARKERS = {
    "el", "la", "los", "las", "y", "de", "del", "en", "un", "una", "perfil", "domicilio",
    "datos", "fuente", "fuentes", "vehículo", "vehículos", "vínculo", "vínculos", "nombre",
    "identidad", "dirección", "registro", "registros", "no", "se", "con", "para", "por",
    "persona", "respuesta", "información", "informacion",
}

# Frases típicas de deliberación que Qwen puede colocar incorrectamente en message.content.
# No se intenta "traducir" esta deliberación: se bloquea y se regenera una respuesta final limpia.
_REASONING_LEAK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"\bokay[,.:;]?\s+(?:so\s+)?the user\b",
        r"\bthe user\s+(?:wants|asked|asks|is asking|needs|said|says)\b",
        r"\bso if the context\b",
        r"\bif the context\b",
        r"\bas an AI\b",
        r"\blet me\s+(?:check|draft|rephrase|think|review|analy[sz]e|verify|look|write)\b",
        r"\bi\s+(?:need|should|have)\s+to\b",
        r"\bwait[,.:;]",
        r"\bcheck(?:ing)?\s+the\s+(?:context|profile|data)\b",
        r"\bthe profile id\b",
        r"\bmake sure to\b",
        r"\bstart with\b",
        r"\bthe previous response\b",
        r"\bthe user's (?:rules|instruction|instructions|request)\b",
        r"\bel usuario\s+(?:quiere|pidió|pide|preguntó|pregunta)\b",
        r"\bvoy a\s+(?:revisar|redactar|comprobar|analizar|verificar)\b",
        r"\bdebo\s+(?:revisar|redactar|comprobar|analizar|verificar)\b",
        r"\bdéjame\s+(?:revisar|redactar|comprobar|analizar|verificar)\b",
    )
)


def _contains_reasoning_leak(text: str) -> bool:
    """Detecta deliberación/meta-instrucciones que nunca deben llegar al frontend."""
    if not text or not text.strip():
        return False
    return any(pattern.search(text) for pattern in _REASONING_LEAK_PATTERNS)


def _looks_spanish_enough(text: str) -> bool:
    """Heurística conservadora para evitar publicar una respuesta predominantemente en inglés."""
    if _contains_reasoning_leak(text):
        return False

    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", text.lower())
    if len(tokens) < 6:
        return True

    english = sum(token in _ENGLISH_MARKERS for token in tokens)
    spanish = sum(token in _SPANISH_MARKERS for token in tokens)

    # Cuatro o más marcadores ingleses con poco soporte español suele indicar fuga de CoT.
    if english >= 4 and spanish < english:
        return False
    return True


def _is_publishable_final(text: str) -> bool:
    return bool(text.strip()) and _looks_spanish_enough(text) and not _contains_reasoning_leak(text)


def _extract_publishable_suffix(text: str) -> str:
    """Rescata la respuesta final cuando Qwen antepone deliberación en message.content.

    El contenido nunca se publica mientras se genera. Si el texto completo contiene
    frases meta como ``Okay, the user...`` o ``Let me...``, se buscan límites de
    párrafo/línea y se conserva el sufijo más largo que ya sea una respuesta
    publicable en español. Esto evita convertir una fuga recuperable en un error 500/SSE.
    """
    value = (text or "").strip()
    if not value:
        return ""
    if _is_publishable_final(value):
        return value

    # Encabezados que a veces genera el modelo antes de su deliberación visible.
    value = re.sub(
        r"^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:an[aá]lisis|razonamiento)(?:\*\*)?\s*[:\-]?\s*",
        "",
        value,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    if _is_publishable_final(value):
        return value

    boundaries: set[int] = {0}
    # La respuesta final de Qwen normalmente comienza después de un salto de línea/párrafo.
    for match in re.finditer(r"\n+", value):
        boundaries.add(match.end())
    # También contemplamos transiciones en una misma línea: "Let me draft: Resumen...".
    for match in re.finditer(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÜÑ])", value):
        boundaries.add(match.end())
    for match in re.finditer(r"[:;]\s+(?=[A-ZÁÉÍÓÚÜÑ])", value):
        boundaries.add(match.end())

    # Elegimos el primer sufijo válido: conserva la mayor cantidad de respuesta final.
    for start in sorted(boundaries):
        candidate = value[start:].strip()
        if _is_publishable_final(candidate):
            return candidate

    return ""


def _chunk_visible_text(text: str, size: int = 180) -> Iterable[str]:
    """Entrega la respuesta final validada en trozos cómodos sin cortar palabras."""
    remaining = text.strip()
    while remaining:
        if len(remaining) <= size:
            yield remaining
            return
        cut = remaining.rfind(" ", 0, size + 1)
        if cut < size // 2:
            cut = size
        yield remaining[:cut]
        remaining = remaining[cut:].lstrip()


_LOG = logging.getLogger(__name__)


class LlmStats:
    """Métricas acumuladas en memoria y seguras para múltiples solicitudes."""
    FIELDS = ("prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration", "load_duration")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_model: dict[str, dict[str, float]] = {}

    def observe(self, model: str, item: dict) -> None:
        if not item.get("done") or "eval_count" not in item:
            return
        values = {key: max(0, int(item.get(key) or 0)) for key in self.FIELDS}
        prompt_tps = values["prompt_eval_count"] * 1e9 / values["prompt_eval_duration"] if values["prompt_eval_duration"] else 0.0
        eval_tps = values["eval_count"] * 1e9 / values["eval_duration"] if values["eval_duration"] else 0.0
        with self._lock:
            record = self._by_model.setdefault(model, {"calls": 0, **{key: 0.0 for key in (*self.FIELDS, "promptTokensPerSecond", "evalTokensPerSecond")}})
            record["calls"] += 1
            for key, value in values.items(): record[key] += value
            record["promptTokensPerSecond"] += prompt_tps
            record["evalTokensPerSecond"] += eval_tps
        _LOG.info("llm_model=%s prompt_eval_count=%s prompt_eval_duration=%s eval_count=%s eval_duration=%s load_duration=%s prompt_tps=%.2f eval_tps=%.2f", model, *(values[k] for k in self.FIELDS), prompt_tps, eval_tps)

    def snapshot(self) -> dict:
        with self._lock:
            by_model = {name: dict(record) for name, record in self._by_model.items()}
        averages = {}
        for name, row in by_model.items():
            n = row.pop("calls")
            averages[name] = {"calls": n, **{("avg" + "".join(piece.title() for piece in key.split("_")) if "_" in key else "avg" + key[0].upper() + key[1:]): round(value/n, 2) for key, value in row.items()}}
        return {"calls": sum(row["calls"] for row in averages.values()), "byModel": averages}


class LocalOllamaClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stats = LlmStats()
        parsed = urlparse(settings.llm_base_url)
        if parsed.scheme != "http":
            raise LocalLlmError("AI_LLM_BASE_URL debe usar http dentro de la red local de Docker.")
        self.host = parsed.hostname or "ollama"
        self.port = parsed.port or 11434
        if self.host not in settings.llm_allowed_hosts:
            raise LocalLlmError(f"Host LLM no permitido: {self.host}")

    def _payload(
        self,
        messages: list[dict[str, str]],
        thinking: bool,
        stream: bool,
        *,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        model: str | None = None,
    ) -> bytes:
        runtime_messages = [dict(m) for m in messages]
        directive = "/think" if thinking else "/no_think"

        language_lock = (
            "Responde exclusivamente en español mexicano y sin mostrar razonamiento interno."
            if not self.settings.force_spanish_thinking else
            "Responde en español mexicano. Si razonas, usa español mexicano también. "
            "Nunca expongas la deliberación interna."
        )
        system_index = next(
            (i for i, message in enumerate(runtime_messages) if message.get("role") == "system"),
            None,
        )
        if system_index is None:
            runtime_messages.insert(0, {"role": "system", "content": language_lock})
        else:
            current_system = str(runtime_messages[system_index].get("content") or "").lstrip()
            runtime_messages[system_index]["content"] = f"{language_lock}\n\n{current_system}"

        for index in range(len(runtime_messages) - 1, -1, -1):
            if runtime_messages[index].get("role") == "user":
                content = str(runtime_messages[index].get("content") or "").rstrip()
                reasoning_instruction = (
                    "\n\nINSTRUCCIÓN DE THINKING: razona en español mexicano."
                    if thinking and self.settings.force_spanish_thinking else ""
                )
                runtime_messages[index]["content"] = (
                    f"{content}{reasoning_instruction}\n\n{directive}"
                )
                break

        chosen_model = model or self.settings.llm_model
        predict_limit = (self.settings.llm_thinking_max_predict if thinking
                         else self.settings.llm_max_predict)
        effective_predict = max(1, min(num_predict if num_predict is not None else predict_limit, predict_limit))
        effective_ctx = max(256, min(num_ctx if num_ctx is not None else self.settings.llm_num_ctx, self.settings.llm_num_ctx))
        try:
            runtime_messages = fit_messages(runtime_messages, effective_ctx, effective_predict)
        except ValueError as exc:
            raise LocalLlmError(str(exc)) from exc
        options = {
            "temperature": self.settings.llm_temperature,
            "num_ctx": effective_ctx,
            "num_predict": effective_predict,
        }
        if self.settings.llm_num_thread is not None:
            options["num_thread"] = self.settings.llm_num_thread
        payload = {
            "model": chosen_model,
            "messages": runtime_messages,
            "stream": stream,
            "think": thinking,
            "keep_alive": self.settings.llm_keep_alive,
            "options": options,
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def stream_chat_events(
        self,
        messages: list[dict[str, str]],
        *,
        thinking: bool = False,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        model: str | None = None,
    ) -> Iterable[LlmStreamChunk]:
        """Genera, valida y sólo entonces publica la respuesta.

        Importante: ningún fragmento de message.content se reenvía al frontend mientras
        Qwen sigue generando. Esto evita que una fuga tipo "Okay, the user..." alcance
        el SSE antes de que el backend pueda detectarla.
        """
        if not self.settings.llm_enabled:
            raise LocalLlmError("LLM local deshabilitado.")

        conn: http.client.HTTPConnection | None = None
        visible_filter = _VisibleContentFilter()
        tagged_thinking_filter = _ThinkingContentFilter()
        buffered_visible: list[str] = []
        done_reason: str | None = None
        received_first_chunk = False
        chosen_model = model or self.settings.llm_model

        try:
            request_body = self._payload(
                messages,
                thinking=thinking,
                stream=True,
                num_ctx=num_ctx,
                num_predict=num_predict,
                model=chosen_model,
            )
            # Validar contra los datos que REALMENTE recibió Ollama, no contra
            # el mensaje anterior a la compactación.
            supplied_messages = json.loads(request_body)["messages"]
            conn = http.client.HTTPConnection(
                self.host,
                self.port,
                timeout=self.settings.llm_first_token_timeout_seconds,
            )
            conn.request(
                "POST",
                "/api/chat",
                body=request_body,
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            if conn.sock is not None:
                conn.sock.settimeout(self.settings.llm_first_token_timeout_seconds)
            if response.status >= 400:
                body = response.read(2048).decode("utf-8", errors="replace")
                raise LocalLlmError(f"Ollama respondió HTTP {response.status}: {body}")

            while True:
                raw = response.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not received_first_chunk:
                    received_first_chunk = True
                    if conn.sock is not None:
                        conn.sock.settimeout(self.settings.llm_stream_idle_timeout_seconds)

                if item.get("error"):
                    raise LocalLlmError(str(item["error"])[:500])

                message = item.get("message") or {}

                # El canal privado se consume, pero nunca se publica.
                _ = message.get("thinking") or message.get("reasoning")

                content = message.get("content")
                if content:
                    content_text = str(content)
                    tagged_thinking_filter.feed(content_text)
                    visible = visible_filter.feed(content_text)
                    if visible:
                        buffered_visible.append(visible)

                if item.get("done"):
                    self.stats.observe(chosen_model, item)
                    done_reason = str(item.get("done_reason") or "") or None
                    tagged_thinking_filter.flush()
                    tail = visible_filter.flush()
                    if tail:
                        buffered_visible.append(tail)
                    break

        except TimeoutError as exc:
            stage = "durante el primer token" if not received_first_chunk else "durante el streaming"
            limit = (
                self.settings.llm_first_token_timeout_seconds
                if not received_first_chunk
                else self.settings.llm_stream_idle_timeout_seconds
            )
            raise LocalLlmError(
                f"Ollama excedió el tiempo de espera {stage} ({limit}s). "
                "El modelo sigue siendo local; prueba nuevamente o reduce el contexto enviado."
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise LocalLlmError(f"No fue posible consultar Ollama: {exc}") from exc
        finally:
            if conn:
                conn.close()

        raw_text = "".join(buffered_visible).strip()
        final_text = _extract_publishable_suffix(raw_text)

        # Ninguna regeneración por defecto. Antes se podían ejecutar 3 inferencias
        # seguidas, multiplicando el tiempo de CPU y generando nuevas alucinaciones.
        # Si la salida es errónea, utilizar evidencia extractiva sin volver a Qwen.
        if final_text:
            invalid_values = ungrounded_identifiers(final_text, supplied_messages)
            missing_pairs = missing_documented_pairs(final_text, supplied_messages)
            missing_sources = missing_documented_sources(final_text, supplied_messages)
            if invalid_values or missing_pairs or missing_sources:
                _LOG.warning(
                    "Respuesta LLM descartada por hechos no respaldados: modelo=%s "
                    "valores_no_verificados=%s pares_omitidos=%s fuentes_omitidas=%s",
                    chosen_model, len(invalid_values), len(missing_pairs), len(missing_sources),
                )
                final_text = ""

        if not final_text:
            _LOG.warning("Respuesta LLM no publicable; entregando evidencia documental: modelo=%s", chosen_model)
            final_text = safe_evidence_fallback(supplied_messages)
        elif done_reason == "length":
            # Recortar al último límite de oración, sin abrir una segunda inferencia.
            last_end = max(final_text.rfind(". "), final_text.rfind(".\n"))
            if last_end >= 40:
                final_text = final_text[:last_end + 1].strip()
            elif not final_text.rstrip().endswith((".", "!", "?")):
                final_text = safe_evidence_fallback(supplied_messages)

        for part in _chunk_visible_text(final_text):
            yield LlmStreamChunk("content", part)

    def _regenerate_final_spanish(
        self,
        messages: list[dict[str, str]],
        *,
        draft: str = "",
        strict: bool = False,
        hard: bool = False,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        model: str | None = None,
    ) -> str:
        final_messages = [dict(message) for message in messages]
        if final_messages and final_messages[0].get("role") == "system":
            final_messages[0]["content"] = (
                str(final_messages[0].get("content") or "")
                + "\n\nREGLA CRÍTICA DE ENTREGA: la respuesta que verá el usuario debe estar COMPLETA y "
                  "EXCLUSIVAMENTE en español mexicano. No muestres razonamiento, pasos internos, "
                  "borradores, traducciones del razonamiento ni texto en inglés. "
                  "Empieza directamente por la respuesta final."
            )

        if draft:
            final_messages.append({
                "role": "assistant",
                "content": draft[:12000],
            })

        extra = ""
        if strict:
            extra += (
                " No uses ninguna palabra o encabezado en inglés. "
                "No escribas frases meta como 'the user wants', 'let me', 'I need to', "
                "'voy a revisar', 'el usuario quiere' ni explicaciones sobre cómo elaborarás la respuesta."
            )
        if hard:
            extra += (
                " PROHIBIDO incluir una sección de análisis, razonamiento, planificación o borrador. "
                "No escribas 'Análisis', 'Razonamiento', 'Plan' ni comentarios previos. "
                "La primera frase debe ser parte de la respuesta final solicitada."
            )

        final_messages.append({
            "role": "user",
            "content": (
                "Entrega ahora únicamente la respuesta final solicitada originalmente. "
                "Redáctala de principio a fin en español mexicano, profesional y clara. "
                "Asegúrate de cerrar todas las frases y apartados; no la cortes a mitad de oración. "
                "No expliques tu proceso ni muestres razonamiento interno."
                + extra
            ),
        })

        final_predict = min(
            self.settings.llm_max_predict,
            max(num_predict or 0, 1600),
        )
        return self._chat_once_no_think(
            final_messages,
            num_ctx=num_ctx,
            num_predict=final_predict,
            model=model,
        ).strip()

    def _chat_once_no_think(
        self,
        messages: list[dict[str, str]],
        *,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        model: str | None = None,
        count_metrics: bool = True,
    ) -> str:
        """Generación final directa sin reentrar al validador de streaming."""
        conn: http.client.HTTPConnection | None = None
        try:
            conn = http.client.HTTPConnection(
                self.host,
                self.port,
                timeout=self.settings.llm_final_timeout_seconds,
            )
            conn.request(
                "POST",
                "/api/chat",
                body=self._payload(
                    messages,
                    thinking=False,
                    stream=False,
                    num_ctx=num_ctx,
                    num_predict=num_predict,
                    model=model,
                ),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            body = response.read().decode("utf-8", errors="replace")
            if response.status >= 400:
                raise LocalLlmError(f"Ollama respondió HTTP {response.status}: {body[:2048]}")
            try:
                item = json.loads(body)
            except json.JSONDecodeError as exc:
                raise LocalLlmError("Ollama devolvió una respuesta JSON inválida.") from exc
            if item.get("error"):
                raise LocalLlmError(str(item["error"])[:500])

            message = item.get("message") or {}
            if count_metrics:
                self.stats.observe(model or self.settings.llm_model, item)
            # Ignorar explícitamente cualquier campo privado también en la regeneración final.
            _ = message.get("thinking") or message.get("reasoning")
            content = str(message.get("content") or "")
            visible_filter = _VisibleContentFilter()
            visible = visible_filter.feed(content) + visible_filter.flush()
            return visible.strip()

        except TimeoutError as exc:
            raise LocalLlmError(
                f"Ollama excedió el tiempo de espera al generar la respuesta final "
                f"({self.settings.llm_final_timeout_seconds}s)."
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise LocalLlmError(f"No fue posible consultar Ollama: {exc}") from exc
        finally:
            if conn:
                conn.close()

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        thinking: bool = False,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        model: str | None = None,
    ) -> Iterable[str]:
        """Compatibilidad interna: expone sólo la respuesta final ya reconciliada."""
        full = ""
        for chunk in self.stream_chat_events(
            messages,
            thinking=thinking,
            num_ctx=num_ctx,
            num_predict=num_predict,
            model=model,
        ):
            if chunk.kind == "content":
                full += chunk.text
            elif chunk.kind == "replace":
                full = chunk.text
        if full:
            yield full

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        thinking: bool = False,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        model: str | None = None,
    ) -> str:
        return "".join(
            self.stream_chat(
                messages,
                thinking=thinking,
                num_ctx=num_ctx,
                num_predict=num_predict,
                model=model,
            )
        ).strip()


    def warmup(self) -> None:
        """Se invoca exclusivamente desde un thread daemon, nunca desde health/startup."""
        if not self.settings.llm_enabled or not self.settings.llm_warmup:
            return
        models = dict.fromkeys((self.settings.model_for_mode("auto"), self.settings.model_for_mode("deep")))
        for model in models:
            try:
                self._chat_once_no_think(
                    [{"role": "user", "content": "Di OK."}],
                    num_ctx=min(4096, self.settings.llm_num_ctx),
                    num_predict=4,
                    model=model,
                    count_metrics=False,
                )
                _LOG.info("LLM warmup completado: %s", model)
            except Exception as exc:
                _LOG.warning("LLM warmup fallido (%s): %s", model, type(exc).__name__)
