from __future__ import annotations

import http.client
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from .config import Settings


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


class LocalOllamaClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
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
    ) -> bytes:
        runtime_messages = [dict(m) for m in messages]
        directive = "/think" if thinking else "/no_think"

        language_lock = (
            "IDIOMA ÚNICO DE GENERACIÓN: español mexicano. "
            "Si generas thinking/reasoning privado, razona textualmente EXCLUSIVAMENTE "
            "en español mexicano desde el primer token; no uses inglés, no traduzcas "
            "después y no alternes idiomas. La respuesta final también debe estar "
            "íntegramente en español mexicano. "
            "La deliberación interna jamás debe copiarse a la respuesta visible."
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
                    "\n\nINSTRUCCIÓN DE THINKING: razona en español mexicano desde el primer token "
                    "del canal privado de razonamiento. No uses inglés y no copies esa deliberación "
                    "al campo de respuesta visible."
                    if thinking
                    else ""
                )
                runtime_messages[index]["content"] = (
                    f"{content}{reasoning_instruction}\n\n{directive}"
                )
                break

        payload = {
            "model": self.settings.llm_model,
            "messages": runtime_messages,
            "stream": stream,
            "think": thinking,
            "keep_alive": self.settings.llm_keep_alive,
            "options": {
                "temperature": self.settings.llm_temperature,
                "num_ctx": max(
                    2048,
                    min(num_ctx or self.settings.llm_num_ctx, self.settings.llm_num_ctx),
                ),
                "num_predict": max(
                    256,
                    min(
                        num_predict or self.settings.llm_max_predict,
                        self.settings.llm_max_predict,
                    ),
                ),
            },
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def stream_chat_events(
        self,
        messages: list[dict[str, str]],
        *,
        thinking: bool = False,
        num_ctx: int | None = None,
        num_predict: int | None = None,
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

        try:
            conn = http.client.HTTPConnection(
                self.host,
                self.port,
                timeout=self.settings.llm_first_token_timeout_seconds,
            )
            conn.request(
                "POST",
                "/api/chat",
                body=self._payload(
                    messages,
                    thinking=thinking,
                    stream=True,
                    num_ctx=num_ctx,
                    num_predict=num_predict,
                ),
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

        # Si la respuesta quedó truncada pero el texto visible es limpio, la usamos como
        # borrador para completar. Si contenía deliberación, jamás la realimentamos.
        needs_regeneration = not final_text or done_reason == "length"
        if needs_regeneration:
            safe_draft = final_text if final_text and done_reason == "length" else ""
            regenerated = self._regenerate_final_spanish(
                messages,
                draft=safe_draft,
                strict=True,
                hard=False,
                num_ctx=num_ctx,
                num_predict=num_predict,
            )
            final_text = _extract_publishable_suffix(regenerated)

        # Segunda barrera: sólo si no logramos rescatar una respuesta final limpia.
        if not final_text:
            regenerated = self._regenerate_final_spanish(
                messages,
                draft="",
                strict=True,
                hard=True,
                num_ctx=num_ctx,
                num_predict=num_predict,
            )
            final_text = _extract_publishable_suffix(regenerated)

        # Último recurso: no exponemos deliberación. Este error sólo se alcanza cuando
        # tampoco existe un sufijo final en español que pueda rescatarse.
        if not final_text:
            raise LocalLlmError(
                "El modelo local respondió, pero no fue posible separar una respuesta final "
                "en español de su deliberación interna."
            )

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
        ).strip()

    def _chat_once_no_think(
        self,
        messages: list[dict[str, str]],
        *,
        num_ctx: int | None = None,
        num_predict: int | None = None,
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
    ) -> Iterable[str]:
        """Compatibilidad interna: expone sólo la respuesta final ya reconciliada."""
        full = ""
        for chunk in self.stream_chat_events(
            messages,
            thinking=thinking,
            num_ctx=num_ctx,
            num_predict=num_predict,
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
    ) -> str:
        return "".join(
            self.stream_chat(
                messages,
                thinking=thinking,
                num_ctx=num_ctx,
                num_predict=num_predict,
            )
        ).strip()
