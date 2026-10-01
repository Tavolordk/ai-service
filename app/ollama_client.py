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
}
_SPANISH_MARKERS = {
    "el", "la", "los", "las", "y", "de", "del", "en", "un", "una", "perfil", "domicilio",
    "datos", "fuente", "fuentes", "vehículo", "vehículos", "vínculo", "vínculos", "nombre",
    "identidad", "dirección", "registro", "registros", "no", "se", "con", "para", "por",
}


def _looks_spanish_enough(text: str) -> bool:
    """Heurística conservadora para evitar publicar una respuesta predominantemente en inglés."""
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", text.lower())
    if len(tokens) < 6:
        return True
    english = sum(token in _ENGLISH_MARKERS for token in tokens)
    spanish = sum(token in _SPANISH_MARKERS for token in tokens)
    return english < 4 or spanish >= english


def _chunk_visible_text(text: str, size: int = 180) -> Iterable[str]:
    """Entrega la respuesta final en trozos cómodos sin cortar palabras cuando sea posible."""
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
        for index in range(len(runtime_messages) - 1, -1, -1):
            if runtime_messages[index].get("role") == "user":
                content = str(runtime_messages[index].get("content") or "").rstrip()
                runtime_messages[index]["content"] = f"{content}\n\n{directive}"
                break

        payload = {
            "model": self.settings.llm_model,
            "messages": runtime_messages,
            "stream": stream,
            "think": thinking,
            "keep_alive": self.settings.llm_keep_alive,
            "options": {
                "temperature": self.settings.llm_temperature,
                "num_ctx": max(2048, min(num_ctx or self.settings.llm_num_ctx, self.settings.llm_num_ctx)),
                "num_predict": max(256, min(num_predict or self.settings.llm_max_predict, self.settings.llm_max_predict)),
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
        if not self.settings.llm_enabled:
            raise LocalLlmError("LLM local deshabilitado.")

        conn: http.client.HTTPConnection | None = None
        visible_filter = _VisibleContentFilter()
        tagged_thinking_filter = _ThinkingContentFilter()
        buffered_visible: list[str] = []
        streamed_visible: list[str] = []
        done_reason: str | None = None

        try:
            # El modelo puede tardar bastante en prefijar un perfil grande, sobre todo en CPU.
            # Usamos un timeout amplio hasta obtener respuesta/primer token y luego uno de
            # inactividad entre fragmentos para no abortar una generación legítima.
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

            received_first_chunk = False
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

                # Qwen/Ollama puede entregar deliberación privada en thinking/reasoning.
                # Se consume para permitir que el modelo razone, pero JAMÁS se reenvía al cliente.
                _ = message.get("thinking") or message.get("reasoning")

                content = message.get("content")
                if content:
                    content_text = str(content)
                    # Consumimos también posibles <think>...</think> antiguos sin publicarlos.
                    tagged_thinking_filter.feed(content_text)
                    visible = visible_filter.feed(content_text)
                    if visible:
                        if thinking:
                            # En modo razonamiento retenemos la salida hasta poder validar idioma
                            # y finalización. El front sólo muestra el indicador "Razonando…".
                            buffered_visible.append(visible)
                        else:
                            streamed_visible.append(visible)
                            yield LlmStreamChunk("content", visible)

                if item.get("done"):
                    done_reason = str(item.get("done_reason") or "") or None
                    tagged_thinking_filter.flush()
                    tail = visible_filter.flush()
                    if tail:
                        if thinking:
                            buffered_visible.append(tail)
                        else:
                            streamed_visible.append(tail)
                            yield LlmStreamChunk("content", tail)
                    break
        except TimeoutError as exc:
            stage = "durante el primer token" if not locals().get("received_first_chunk", False) else "durante el streaming"
            limit = (
                self.settings.llm_first_token_timeout_seconds
                if not locals().get("received_first_chunk", False)
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

        if not thinking:
            direct_text = "".join(streamed_visible).strip()
            must_replace = (
                not direct_text
                or done_reason == "length"
                or not _looks_spanish_enough(direct_text)
            )
            if must_replace:
                corrected = self._regenerate_final_spanish(
                    messages,
                    draft=direct_text,
                    strict=True,
                    num_ctx=num_ctx,
                    num_predict=num_predict,
                )
                if corrected:
                    yield LlmStreamChunk("replace", corrected)
            return

        final_text = "".join(buffered_visible).strip()
        must_regenerate = (
            not final_text
            or done_reason == "length"
            or not _looks_spanish_enough(final_text)
        )

        if must_regenerate:
            final_text = self._regenerate_final_spanish(
                messages,
                draft=final_text,
                num_ctx=num_ctx,
                num_predict=num_predict,
            )

        # Última defensa: nunca publicar al usuario un bloque mayoritariamente en inglés.
        if final_text and not _looks_spanish_enough(final_text):
            final_text = self._regenerate_final_spanish(
                messages,
                draft=final_text,
                strict=True,
                num_ctx=num_ctx,
                num_predict=num_predict,
            )

        for part in _chunk_visible_text(final_text):
            yield LlmStreamChunk("content", part)

    def _regenerate_final_spanish(
        self,
        messages: list[dict[str, str]],
        *,
        draft: str = "",
        strict: bool = False,
        num_ctx: int | None = None,
        num_predict: int | None = None,
    ) -> str:
        final_messages = [dict(message) for message in messages]
        if final_messages and final_messages[0].get("role") == "system":
            final_messages[0]["content"] = (
                str(final_messages[0].get("content") or "")
                + "\n\nREGLA CRÍTICA DE ENTREGA: la respuesta que verá el usuario debe estar COMPLETA y "
                  "EXCLUSIVAMENTE en español mexicano. No muestres razonamiento, pasos internos, "
                  "borradores, traducciones del razonamiento ni texto en inglés."
            )
        if draft:
            # Sólo reutilizamos el borrador de RESPUESTA visible, nunca la deliberación privada.
            final_messages.append({
                "role": "assistant",
                "content": draft[:12000],
            })
        final_messages.append({
            "role": "user",
            "content": (
                "Entrega ahora únicamente la respuesta final solicitada originalmente. "
                "Redáctala de principio a fin en español mexicano, profesional y clara. "
                "Asegúrate de cerrar todas las frases y apartados; no la cortes a mitad de oración. "
                "No expliques tu proceso ni muestres razonamiento interno."
                + (" No uses ninguna palabra o encabezado en inglés." if strict else "")
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
