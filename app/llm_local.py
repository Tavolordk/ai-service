from __future__ import annotations

import http.client
import json
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

from .config import Settings


class LocalLlmError(RuntimeError):
    pass


class _VisibleContentFilter:
    """Elimina bloques <think>...</think> aunque lleguen partidos en varios chunks."""

    OPEN = '<think>'
    CLOSE = '</think>'

    def __init__(self) -> None:
        self.buffer = ''
        self.inside_think = False

    def feed(self, chunk: str) -> str:
        self.buffer += chunk
        visible: list[str] = []

        while self.buffer:
            lowered = self.buffer.lower()

            if self.inside_think:
                close_index = lowered.find(self.CLOSE)
                if close_index >= 0:
                    self.buffer = self.buffer[close_index + len(self.CLOSE):]
                    self.inside_think = False
                    continue
                keep = min(len(self.buffer), len(self.CLOSE) - 1)
                self.buffer = self.buffer[-keep:] if keep else ''
                break

            open_index = lowered.find(self.OPEN)
            if open_index >= 0:
                if open_index:
                    visible.append(self.buffer[:open_index])
                self.buffer = self.buffer[open_index + len(self.OPEN):]
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

        return ''.join(visible)

    def flush(self) -> str:
        if self.inside_think:
            self.buffer = ''
            return ''
        visible = self.buffer
        self.buffer = ''
        return visible


@dataclass(frozen=True)
class LocalLlmStatus:
    reachable: bool
    model_available: bool
    detail: str


class LocalOllamaClient:
    """Cliente mínimo para Ollama local.

    No usa SDKs ni clientes externos. El host se valida contra una allowlist y
    sólo se acepta HTTP local/interno. Esto evita que un cambio accidental de URL
    convierta el servicio en un canal de salida de datos sensibles.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        parsed = urlparse(settings.llm_base_url)
        if parsed.scheme != 'http':
            raise ValueError('AI_LLM_BASE_URL debe usar http para el runtime local.')
        if not parsed.hostname or parsed.hostname not in settings.llm_allowed_hosts:
            raise ValueError('AI_LLM_BASE_URL apunta a un host no permitido.')
        self.host = parsed.hostname
        self.port = parsed.port or 80
        base_path = parsed.path.rstrip('/')
        self.base_path = base_path

    def _connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(
            self.host,
            self.port,
            timeout=self.settings.llm_timeout_seconds,
        )

    def status(self) -> LocalLlmStatus:
        if not self.settings.llm_enabled:
            return LocalLlmStatus(False, False, 'LLM local deshabilitado por configuración.')
        conn: http.client.HTTPConnection | None = None
        try:
            conn = self._connection()
            conn.request('GET', f'{self.base_path}/api/tags')
            response = conn.getresponse()
            body = response.read()
            if response.status != 200:
                return LocalLlmStatus(False, False, f'Ollama respondió HTTP {response.status}.')
            payload = json.loads(body.decode('utf-8'))
            names = {
                str(item.get('name', '')).strip()
                for item in payload.get('models', [])
                if isinstance(item, dict)
            }
            model_available = self.settings.llm_model in names or any(
                name.split(':', 1)[0] == self.settings.llm_model.split(':', 1)[0]
                and self.settings.llm_model.endswith(':latest')
                for name in names
            )
            return LocalLlmStatus(
                True,
                model_available,
                'Modelo local disponible.' if model_available else f'Modelo {self.settings.llm_model} no instalado.',
            )
        except Exception as exc:  # status nunca expone payloads
            return LocalLlmStatus(False, False, f'Runtime local no disponible: {type(exc).__name__}.')
        finally:
            if conn:
                conn.close()

    def stream_chat(self, messages: list[dict[str, str]], *, thinking: bool = False) -> Iterable[str]:
        if not self.settings.llm_enabled:
            raise LocalLlmError('LLM local deshabilitado.')

        runtime_messages = [dict(message) for message in messages]
        directive = '/think' if thinking else '/no_think'
        for index in range(len(runtime_messages) - 1, -1, -1):
            if runtime_messages[index].get('role') == 'user':
                content = str(runtime_messages[index].get('content') or '').rstrip()
                runtime_messages[index]['content'] = f'{content}\n\n{directive}'
                break

        payload = json.dumps(
            {
                'model': self.settings.llm_model,
                'messages': runtime_messages,
                'stream': True,
                'think': thinking,
                'keep_alive': '10m',
                'options': {
                    'temperature': self.settings.llm_temperature,
                    'top_p': 0.9,
                    'seed': 17,
                    'num_predict': self.settings.llm_max_predict,
                    'num_ctx': 16384,
                    'repeat_penalty': 1.05,
                },
            },
            ensure_ascii=False,
        ).encode('utf-8')

        conn = self._connection()
        try:
            conn.request(
                'POST',
                f'{self.base_path}/api/chat',
                body=payload,
                headers={
                    'Content-Type': 'application/json; charset=utf-8',
                    'Accept': 'application/x-ndjson',
                    'Connection': 'close',
                },
            )
            response = conn.getresponse()
            if response.status != 200:
                error_body = response.read(2048).decode('utf-8', errors='replace')
                if 'not found' in error_body.lower():
                    raise LocalLlmError(
                        f'El modelo local {self.settings.llm_model} no está instalado.'
                    )
                raise LocalLlmError(f'El runtime local respondió HTTP {response.status}.')

            visible_filter = _VisibleContentFilter()

            while True:
                raw = response.readline()
                if not raw:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw.decode('utf-8'))
                except json.JSONDecodeError as exc:
                    raise LocalLlmError('Respuesta inválida del runtime local.') from exc

                if item.get('error'):
                    raise LocalLlmError(str(item.get('error'))[:300])

                # El razonamiento interno de Qwen3 (message.thinking) es privado.

                # Sólo se transmite message.content al cliente.

                message = item.get('message') or {}

                # message.thinking se descarta deliberadamente.
                # Sólo message.content puede salir al frontend.
                content = message.get('content')
                if content:
                    visible = visible_filter.feed(str(content))
                    if visible:
                        yield visible

                if item.get('done'):
                    tail = visible_filter.flush()
                    if tail:
                        yield tail
                    break
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise LocalLlmError(f'No fue posible consultar el modelo local: {type(exc).__name__}.') from exc
        finally:
            conn.close()
