#!/usr/bin/env python3
"""Parche reproducible para Tavolordk/ai-service main (4.0.4-reasoning-recovery).
Uso: python cpu_optimize_4_1_0.py /ruta/a/ai-service
Modifica sólo los archivos verificados, sin tocar .env ni datos de casos.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import difflib
import ast

p=argparse.ArgumentParser()
p.add_argument('repo', nargs='?', default='.')
p.add_argument('--check', action='store_true', help='Valida y muestra diff sin escribir')
a=p.parse_args()
root=Path(a.repo).resolve()
changes={}

def read(path):
    f=root/path
    if not f.exists(): raise RuntimeError(f'Falta {path}; verifica la rama base')
    if path not in changes: changes[path]=[f.read_text(encoding='utf-8'),None]
    return changes[path][1] if changes[path][1] is not None else changes[path][0]

def put(path,content):
    if path not in changes:
        f=root/path
        changes[path]=[f.read_text(encoding='utf-8') if f.exists() else '',None]
    changes[path][1]=content

def rep(path,before,after,expected=1):
    src=read(path)
    n=src.count(before)
    if n!=expected: raise RuntimeError(f'{path}: se esperaban {expected} coincidencias, encontradas {n} de: {before[:85]!r}')
    put(path,src.replace(before,after))

def insert(path,anchor,text): rep(path,anchor,text+anchor)

# 1. Configuración centralizada y retrocompatibilidad cuando sólo está AI_LLM_MODEL.
c='app/config.py'
rep(c,'"4.0.4-reasoning-recovery"','"4.1.0-cpu-optimized"')
rep(c,'    llm_model: str = os.getenv("AI_LLM_MODEL", "qwen3:4b")\n', '''    llm_model: str = os.getenv("AI_LLM_MODEL", "qwen3:4b")
    llm_model_fast: str = os.getenv("AI_LLM_MODEL_FAST", "").strip()
    llm_model_deep: str = os.getenv("AI_LLM_MODEL_DEEP", "").strip()
    llm_thinking_default: bool = _bool("AI_LLM_THINKING_DEFAULT", False)
    llm_thinking_max_predict: int = _int("AI_LLM_THINKING_MAX_PREDICT", 1200)
    force_spanish_thinking: bool = _bool("AI_FORCE_SPANISH_THINKING", False)
    llm_num_thread: int | None = int(os.environ["AI_LLM_NUM_THREAD"]) if os.getenv("AI_LLM_NUM_THREAD", "").strip().isdigit() and int(os.environ["AI_LLM_NUM_THREAD"]) > 0 else None
    llm_warmup: bool = _bool("AI_LLM_WARMUP", True)
''')
for before,after in [('12288','4096'),('2400','500'),('"AI_LLM_HISTORY_MESSAGES", 6','"AI_LLM_HISTORY_MESSAGES", 2'),('"AI_LLM_MAX_EVIDENCE", 72','"AI_LLM_MAX_EVIDENCE", 30'),('"AI_CONTEXT_MAX_CHARS", 14000','"AI_CONTEXT_MAX_CHARS", 6000'),('"AI_CONTEXT_DEEP_MAX_CHARS", 28000','"AI_CONTEXT_DEEP_MAX_CHARS", 12000')]: rep(c,before,after)
# El método debe quedar DENTRO de Settings, antes de settings = Settings().
insert(c,'\nsettings = Settings()\n','''
    def model_for_mode(self, mode: str) -> str:
        """Mantiene AI_LLM_MODEL si no se configuró un modelo para el modo."""
        return (self.llm_model_deep if mode == "deep" else self.llm_model_fast) or self.llm_model
''')

# 2. Defaults de las solicitudes; no cambia nombre/campo del contrato JSON.
rep('app/models.py','from pydantic import BaseModel, ConfigDict, Field, field_validator','from pydantic import BaseModel, ConfigDict, Field, field_validator\nfrom .config import settings')
rep('app/models.py','    thinking: bool = False','    thinking: bool = Field(default_factory=lambda: settings.llm_thinking_default)',2)

# 3. Presupuesto conservador de contexto; se recorta evidencia y se preserva SYSTEM.
put('app/token_budget.py', '''"""Límite previo de tokens. Qwen tokeniza bytes BPE; bytes UTF-8 es cota conservadora."""
from __future__ import annotations


def prompt_token_ceiling(messages: list[dict[str, str]]) -> int:
    # Reserva para delimitadores/roles y plantillas de chat de Ollama.
    return 128 + sum(len(str(msg.get("content") or "").encode("utf-8")) + 16 for msg in messages)


def _cut_utf8(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[:max(0, max_bytes)].decode("utf-8", errors="ignore")


def fit_messages(messages: list[dict[str, str]], num_ctx: int, num_predict: int) -> list[dict[str, str]]:
    """Conserva system; reduce evidencia, después historial y pregunta sólo si es necesario.

    `num_predict` también se reserva en la ventana para que la salida tenga espacio.
    Se usa una sobreestimación UTF-8, no un promedio de chars/token.
    """
    result = [dict(m) for m in messages]
    budget = num_ctx - num_predict
    def excess() -> int:
        return max(0, prompt_token_ceiling(result) - budget)

    # Primera prioridad: no enviar evidencia que desborda el contexto.
    marker = "CONTEXTO VERIFICADO Y COMPACTADO:\\n"
    for msg in result:
        if not excess(): break
        content = str(msg.get("content") or "")
        if marker not in content: continue
        start, evidence = content.split(marker, 1)
        suffix_marker = "\\n\\nIDIOMA OBLIGATORIO:"
        body, sep, suffix = evidence.partition(suffix_marker)
        keep = max(0, len(body.encode("utf-8")) - excess())
        msg["content"] = start + marker + _cut_utf8(body, keep) + (sep + suffix if sep else "")

    # Después reducir primero las partes antiguas de la conversación.
    while excess() and len(result) > 2:
        result.pop(1)

    # Casos extremos: la pregunta no cabe. Nunca truncar la instrucción de sistema.
    if excess():
        for msg in reversed(result):
            if msg.get("role") == "system": continue
            content = str(msg.get("content") or "")
            msg["content"] = _cut_utf8(content, len(content.encode("utf-8")) - excess())
            if not excess(): break

    if excess():
        raise ValueError("AI_LLM_NUM_CTX insuficiente para las instrucciones de sistema")
    return result
''')

# 4. Eliminar coerción textual de razonamiento; mantener Español FINAL.
rep('app/police_domain.py','1. IDIOMA ÚNICO DE TODA LA GENERACIÓN: español mexicano. Si el motor genera un canal privado `thinking` o `reasoning`, esa deliberación textual también debe producirse EXCLUSIVAMENTE en español mexicano desde su primer token. No uses inglés en el razonamiento textual ni cambies de idioma a mitad del proceso.','1. IDIOMA DE LA RESPUESTA VISIBLE: español mexicano, desde el primer hasta el último carácter visible.')

# 5. Cliente Ollama: modelos, thinking, opciones CPU, métricas y warmup.
c='app/ollama_client.py'
rep(c,'import http.client\nimport json\nimport re','import http.client\nimport json\nimport re\nimport logging\nimport threading')
rep(c,'from .config import Settings','from .config import Settings\nfrom .token_budget import fit_messages')
insert(c,'class LocalOllamaClient:', '''_LOG = logging.getLogger(__name__)


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


''')
rep(c,'        self.settings = settings\n        parsed = urlparse(settings.llm_base_url)','        self.settings = settings\n        self.stats = LlmStats()\n        parsed = urlparse(settings.llm_base_url)')
rep(c,'        num_predict: int | None = None,\n    ) -> bytes:', '        num_predict: int | None = None,\n        model: str | None = None,\n    ) -> bytes:',1)
start='''        language_lock = (
            "IDIOMA ÚNICO DE GENERACIÓN: español mexicano. "
            "Si generas thinking/reasoning privado, razona textualmente EXCLUSIVAMENTE "
            "en español mexicano desde el primer token; no uses inglés, no traduzcas "
            "después y no alternes idiomas. La respuesta final también debe estar "
            "íntegramente en español mexicano. "
            "La deliberación interna jamás debe copiarse a la respuesta visible."
        )'''
rep(c,start,'''        language_lock = (
            "Responde exclusivamente en español mexicano y sin mostrar razonamiento interno."
            if not self.settings.force_spanish_thinking else
            "Responde en español mexicano. Si razonas, usa español mexicano también. "
            "Nunca expongas la deliberación interna."
        )''')
start='''                reasoning_instruction = (
                    "\\n\\nINSTRUCCIÓN DE THINKING: razona en español mexicano desde el primer token "
                    "del canal privado de razonamiento. No uses inglés y no copies esa deliberación "
                    "al campo de respuesta visible."
                    if thinking
                    else ""
                )'''
rep(c,start,'''                reasoning_instruction = (
                    "\\n\\nINSTRUCCIÓN DE THINKING: razona en español mexicano."
                    if thinking and self.settings.force_spanish_thinking else ""
                )''')
rep(c,'''        payload = {
            "model": self.settings.llm_model,
            "messages": runtime_messages,''','''        chosen_model = model or self.settings.llm_model
        predict_limit = (self.settings.llm_thinking_max_predict if thinking
                         else self.settings.llm_max_predict)
        effective_predict = max(1, min(num_predict if num_predict is not None else predict_limit, predict_limit))
        effective_ctx = max(256, min(num_ctx if num_ctx is not None else self.settings.llm_num_ctx, self.settings.llm_num_ctx))
        runtime_messages = fit_messages(runtime_messages, effective_ctx, effective_predict)
        options = {
            "temperature": self.settings.llm_temperature,
            "num_ctx": effective_ctx,
            "num_predict": effective_predict,
        }
        if self.settings.llm_num_thread is not None:
            options["num_thread"] = self.settings.llm_num_thread
        payload = {
            "model": chosen_model,
            "messages": runtime_messages,''')
rep(c,'''            "options": {
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
            },''','''            "options": options,''')
# Llave model en stream_chat_events
rep(c,'        num_predict: int | None = None,\n    ) -> Iterable[LlmStreamChunk]:','        num_predict: int | None = None,\n        model: str | None = None,\n    ) -> Iterable[LlmStreamChunk]:',1)
rep(c,'        received_first_chunk = False\n','        received_first_chunk = False\n        chosen_model = model or self.settings.llm_model\n',1)
rep(c,'                    num_predict=num_predict,\n                ),\n                headers={"Content-Type": "application/json"},','                    num_predict=num_predict,\n                    model=chosen_model,\n                ),\n                headers={"Content-Type": "application/json"},',1)
rep(c,'                if item.get("done"):\n                    done_reason', '                if item.get("done"):\n                    self.stats.observe(chosen_model, item)\n                    done_reason',1)
rep(c,'                num_predict=num_predict,\n            )\n            final_text = _extract_publishable_suffix(regenerated)', '                num_predict=num_predict,\n                model=chosen_model,\n            )\n            final_text = _extract_publishable_suffix(regenerated)',2)
# Regeneración y llamada no-stream
rep(c,'        num_predict: int | None = None,\n    ) -> str:\n        final_messages', '        num_predict: int | None = None,\n        model: str | None = None,\n    ) -> str:\n        final_messages',1)
rep(c,'            num_predict=final_predict,\n        ).strip()', '            num_predict=final_predict,\n            model=model,\n        ).strip()',1)
rep(c,'        num_predict: int | None = None,\n    ) -> str:\n        """Generación final directa', '        num_predict: int | None = None,\n        model: str | None = None,\n        count_metrics: bool = True,\n    ) -> str:\n        """Generación final directa',1)
rep(c,'                    num_predict=num_predict,\n                ),\n                headers={"Content-Type": "application/json"},','                    num_predict=num_predict,\n                    model=model,\n                ),\n                headers={"Content-Type": "application/json"},',1)
rep(c,'            # Ignorar explícitamente cualquier campo privado también en la regeneración final.', '            if count_metrics:\n                self.stats.observe(model or self.settings.llm_model, item)\n            # Ignorar explícitamente cualquier campo privado también en la regeneración final.',1)
# stream_chat y chat agregan modelo (compatibilidad por keyword)
rep(c,'        num_predict: int | None = None,\n    ) -> Iterable[str]:','        num_predict: int | None = None,\n        model: str | None = None,\n    ) -> Iterable[str]:',1)
rep(c,'            num_predict=num_predict,\n        ):\n            if chunk.kind', '            num_predict=num_predict,\n            model=model,\n        ):\n            if chunk.kind',1)
rep(c,'        num_predict: int | None = None,\n    ) -> str:\n        return "".join(', '        num_predict: int | None = None,\n        model: str | None = None,\n    ) -> str:\n        return "".join(',1)
rep(c,'                num_predict=num_predict,\n            )\n        ).strip()', '                num_predict=num_predict,\n                model=model,\n            )\n        ).strip()',1)
put(c,read(c)+'''

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
''')

# 6. Analizador: modelo según el modo, no cambia SSE ni fast path.
c='app/analyzer.py'
rep(c,'from .runtime_policy import RuntimeBudget, choose_runtime_budget','from .runtime_policy import RuntimeBudget, choose_runtime_budget\nfrom .token_budget import fit_messages')
rep(c,'    items = history[-max_messages:]','    items = history[-max_messages:] if max_messages > 0 else []')
rep(c,'        intent: str,\n    ) -> list[dict[str, str]]:', '        intent: str,\n        num_ctx: int | None = None,\n        num_predict: int | None = None,\n    ) -> list[dict[str, str]]:',1)
rep(c,'"\\n\\nIDIOMA OBLIGATORIO: toda generación textual, incluido cualquier canal privado de thinking/reasoning, debe producirse en español mexicano desde el primer token. "','"\\n\\nIDIOMA OBLIGATORIO: la respuesta visible debe producirse íntegramente en español mexicano. "')
rep(c,'''        return [
            {"role": "system", "content": system},
            *_history_messages(history, self.settings.llm_history_messages),
            {"role": "user", "content": user},
        ]''','''        messages = [
            {"role": "system", "content": system},
            *_history_messages(history, self.settings.llm_history_messages),
            {"role": "user", "content": user},
        ]
        if num_ctx is not None:
            return fit_messages(messages, num_ctx, num_predict or self.settings.llm_max_predict)
        return messages''')
rep(c,'            max_predict=self.settings.llm_max_predict,','            max_predict=(self.settings.llm_thinking_max_predict if thinking else self.settings.llm_max_predict),')
rep(c,'                    num_predict=min(650, self.settings.llm_max_predict),','                    num_predict=min(650, self.settings.llm_max_predict),\n                    model=self.settings.model_for_mode("deep"),')
rep(c,'            intent=selection.intent,\n        )','            intent=selection.intent,\n            num_ctx=runtime.num_ctx,\n            num_predict=runtime.num_predict,\n        )',1)
rep(c,'                num_predict=runtime.num_predict,\n            ),\n            mode="local-llm",\n            model=self.settings.llm_model,','                num_predict=runtime.num_predict,\n                model=self.settings.model_for_mode(request.mode),\n            ),\n            mode="local-llm",\n            model=self.settings.model_for_mode(request.mode),',1)
rep(c,'        messages = self._messages(request.question, context, request.history, intent=intent)','        messages = self._messages(request.question, context, request.history, intent=intent, num_ctx=runtime.num_ctx, num_predict=runtime.num_predict)',1)
rep(c,'                num_predict=runtime.num_predict,\n            ),\n            mode="local-llm",\n            model=self.settings.llm_model,','                num_predict=runtime.num_predict,\n                model=self.settings.model_for_mode(request.mode),\n            ),\n            mode="local-llm",\n            model=self.settings.model_for_mode(request.mode),',1)

# 7. Warmup daemon y health extendido (ninguna modificación del contrato de chat).
c='app/main.py'
rep(c,'import json\nfrom collections.abc import Iterable','import json\nfrom collections.abc import Iterable\nfrom threading import Thread')
insert(c,'\ndef _sse(event: str, data: dict) -> str:', '''
@app.on_event("startup")
def _schedule_warmup() -> None:
    if settings.llm_warmup and settings.llm_enabled:
        Thread(target=llm.warmup, name="ollama-warmup", daemon=True).start()

''')
rep(c,'        "maxContextWindow": settings.llm_num_ctx,','        "maxContextWindow": settings.llm_num_ctx,\n        "llmStats": llm.stats.snapshot(),')

# 8. Entorno y Docker.
c='.env.example'
rep(c,'4.0.4-reasoning-recovery','4.1.0-cpu-optimized',2)
rep(c,'AI_LLM_MODEL=qwen3:4b\n','''AI_LLM_MODEL=qwen3:4b
AI_LLM_MODEL_FAST=qwen3:1.7b
AI_LLM_MODEL_DEEP=qwen3:4b
AI_LLM_THINKING_DEFAULT=false
AI_LLM_THINKING_MAX_PREDICT=1200
AI_FORCE_SPANISH_THINKING=false
AI_LLM_NUM_THREAD=
AI_LLM_WARMUP=true
''')
for before,after in [('AI_LLM_NUM_CTX=12288','AI_LLM_NUM_CTX=4096'),('AI_LLM_MAX_PREDICT=2400','AI_LLM_MAX_PREDICT=500'),('AI_LLM_HISTORY_MESSAGES=6','AI_LLM_HISTORY_MESSAGES=2'),('AI_LLM_MAX_EVIDENCE=72','AI_LLM_MAX_EVIDENCE=30'),('AI_CONTEXT_MAX_CHARS=14000','AI_CONTEXT_MAX_CHARS=6000'),('AI_CONTEXT_DEEP_MAX_CHARS=28000','AI_CONTEXT_DEEP_MAX_CHARS=12000')]:rep(c,before,after)
put('VERSION','4.1.0-cpu-optimized\n')
for c in ('docker-compose.local.yml','docker-compose.yml'):
    rep(c,'spm-profile-intelligence-api:4.0.4-reasoning-recovery','spm-profile-intelligence-api:4.1.0-cpu-optimized')
    rep(c,'      OLLAMA_KEEP_ALIVE: ${OLLAMA_KEEP_ALIVE:-30m}','''      OLLAMA_KEEP_ALIVE: ${OLLAMA_KEEP_ALIVE:-30m}
      OLLAMA_NUM_PARALLEL: "1"
      OLLAMA_MAX_LOADED_MODELS: "2"
      OLLAMA_FLASH_ATTENTION: "1"
      OLLAMA_KV_CACHE_TYPE: "q8_0"''')
c='compose.yaml'
rep(c,'AI_LLM_MAX_PREDICT: "900"','AI_LLM_MAX_PREDICT: "${AI_LLM_MAX_PREDICT:-500}"')
rep(c,'AI_LLM_MAX_EVIDENCE: "72"','AI_LLM_MAX_EVIDENCE: "${AI_LLM_MAX_EVIDENCE:-30}"')
rep(c,'AI_LLM_HISTORY_MESSAGES: "8"','AI_LLM_HISTORY_MESSAGES: "${AI_LLM_HISTORY_MESSAGES:-2}"')
insert(c,'      AI_LLM_MODEL: "${AI_LLM_MODEL}"', '''      AI_LLM_MODEL_FAST: "${AI_LLM_MODEL_FAST:-}"
      AI_LLM_MODEL_DEEP: "${AI_LLM_MODEL_DEEP:-}"
      AI_LLM_NUM_CTX: "${AI_LLM_NUM_CTX:-4096}"
      AI_LLM_THINKING_DEFAULT: "${AI_LLM_THINKING_DEFAULT:-false}"
      AI_LLM_THINKING_MAX_PREDICT: "${AI_LLM_THINKING_MAX_PREDICT:-1200}"
      AI_FORCE_SPANISH_THINKING: "${AI_FORCE_SPANISH_THINKING:-false}"
      AI_LLM_NUM_THREAD: "${AI_LLM_NUM_THREAD:-}"
      AI_LLM_WARMUP: "${AI_LLM_WARMUP:-true}"
''')

# 9. Scripts de carga de modelos.
c='setup-local-model.ps1'
rep(c,'$Model = "qwen3:4b"','$Models = @("qwen3:1.7b", "qwen3:4b")')
start='''if (($list -join "`n") -match [regex]::Escape($Model)) {
    Write-Host "$Model ya está instalado. No se descargará de nuevo." -ForegroundColor Green
    exit 0
}

Write-Host "El modelo $Model NO está instalado." -ForegroundColor Yellow
Write-Host "Intentando descargarlo desde registry.ollama.ai..." -ForegroundColor Yellow
& docker compose -f $ComposeFile --env-file $EnvFile exec -T ollama ollama pull $Model
if ($LASTEXITCODE -ne 0) {
    Write-Host "" 
    Write-Host "ERROR: no se pudo descargar $Model." -ForegroundColor Red
    Write-Host "Tu red/DNS corporativo está bloqueando registry.ollama.ai." -ForegroundColor Red
    Write-Host "La API puede construirse, pero el chatbot no responderá hasta que el modelo exista en el volumen spm-ollama-models." -ForegroundColor Yellow
    Write-Host "Si tienes el modelo en otro equipo/servidor, usa import-model-volume.ps1 después de copiar un tar de /root/.ollama/models." -ForegroundColor Yellow
    exit 20
}
'''
rep(c,start,'''foreach ($Model in $Models) {
    if (($list -join "`n") -match [regex]::Escape($Model)) {
        Write-Host "$Model ya está instalado." -ForegroundColor Green
        continue
    }
    Write-Host "Descargando $Model desde registry.ollama.ai..." -ForegroundColor Yellow
    & docker compose -f $ComposeFile --env-file $EnvFile exec -T ollama ollama pull $Model
    if ($LASTEXITCODE -ne 0) {
        Write-Host "No se pudo descargar $Model. Revisa la conectividad o usa import-model-volume.ps1." -ForegroundColor Red
        exit 20
    }
}
''')
put('setup-local-model.sh','''#!/usr/bin/env bash
set -euo pipefail
compose=(docker compose -f docker-compose.local.yml --env-file .env)
[[ -f .env ]] || cp .env.example .env
"${compose[@]}" up -d ollama
for model in qwen3:1.7b qwen3:4b; do
  "${compose[@]}" exec -T ollama ollama pull "$model"
done
"${compose[@]}" exec -T ollama ollama list
''')
put('setup-local-model.cmd','''@echo off
setlocal
REM Usa los mismos nombres de servicio/volumen que docker-compose.local.yml.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-local-model.ps1"
exit /b %ERRORLEVEL%
''')

# 10. Actualizar tests existentes que verificaban el bloqueo de idioma antiguo.
c='tests/test_ollama_runtime_payload.py'
rep(c,'def test_payload_forces_spanish_in_private_thinking_channel():','def test_payload_does_not_force_spanish_private_thinking_by_default():')
rep(c,'    assert "thinking/reasoning" in system\n    assert "español mexicano" in system\n    assert "instrucción de thinking" in user\n    assert "/think" in user','    assert "exclusivamente en español mexicano" in system\n    assert "instrucción de thinking" not in user\n    assert "/think" in user')
c='tests/test_reasoning_output_guard.py'
rep(c,'def test_payload_keeps_spanish_lock_in_thinking_mode():','def test_payload_keeps_final_spanish_only_in_thinking_mode():')
rep(c,'    assert "thinking/reasoning" in system\n    assert "español mexicano" in system\n    assert "instrucción de thinking" in user\n    assert "/think" in user','    assert "exclusivamente en español mexicano" in system\n    assert "instrucción de thinking" not in user\n    assert "/think" in user')

put('tests/test_cpu_optimized.py', '''import json
from dataclasses import replace

from app.config import settings
from app.models import ChatRequest, ProfileInput
from app.ollama_client import LlmStats, LocalOllamaClient
from app.token_budget import prompt_token_ceiling


def payload(cfg, thinking=False, model=None, num_ctx=None, num_predict=None):
    return json.loads(LocalOllamaClient(cfg)._payload(
        [{"role": "system", "content": "Responde en español."},
         {"role": "user", "content": "Analiza lo observado. " + "EVIDENCIA MUY LARGA " * 2000}],
        thinking=thinking, stream=True, model=model, num_ctx=num_ctx, num_predict=num_predict,
    ))


def test_thinking_is_off_by_default_and_question_contract_unchanged():
    cfg = replace(settings, llm_thinking_default=False)
    assert cfg.llm_thinking_default is False
    assert ChatRequest(profile=ProfileInput(profileId="1"), question="Explica").thinking is False
    assert payload(cfg)["think"] is False


def test_explicit_thinking_has_its_own_cap():
    cfg = replace(settings, llm_max_predict=500, llm_thinking_max_predict=1200)
    request = payload(cfg, thinking=True, num_predict=2500)
    assert request["think"] is True
    assert request["options"]["num_predict"] == 1200
    assert "/think" in request["messages"][-1]["content"]


def test_fast_deep_model_and_legacy_fallback():
    cfg = replace(settings, llm_model="my:legacy", llm_model_fast="qwen3:1.7b", llm_model_deep="qwen3:4b")
    assert cfg.model_for_mode("auto") == "qwen3:1.7b"
    assert cfg.model_for_mode("deep") == "qwen3:4b"
    assert payload(cfg, model=cfg.model_for_mode("auto"))["model"] == "qwen3:1.7b"
    old = replace(cfg, llm_model_fast="", llm_model_deep="")
    assert old.model_for_mode("auto") == old.model_for_mode("deep") == "my:legacy"


def test_prompt_fits_ctx_and_keeps_system_instruction():
    cfg = replace(settings, llm_num_ctx=4096, llm_max_predict=500)
    obj = payload(cfg)
    assert obj["options"]["num_ctx"] == 4096
    assert prompt_token_ceiling(obj["messages"]) + obj["options"]["num_predict"] <= 4096
    assert "Responde en español" in obj["messages"][0]["content"]
    assert len(obj["messages"][-1]["content"]) < 20000


def test_analyzer_routes_analytical_auto_to_fast_and_deep_to_large():
    from app.analyzer import PoliceAnalyzer
    from app.models import ChatRequest
    from app.ollama_client import LlmStreamChunk

    class FakeLlm:
        def __init__(self): self.calls = []
        def stream_chat_events(self, messages, **kw):
            self.calls.append(kw)
            yield LlmStreamChunk("content", "Datos disponibles en español.")

    cfg = replace(settings, fast_path_enabled=False,
        llm_model_fast="qwen3:1.7b", llm_model_deep="qwen3:4b", llm_num_ctx=4096)
    fake = FakeLlm()
    analyzer = PoliceAnalyzer(cfg, fake, None)
    for mode, expected in (("auto", "qwen3:1.7b"), ("deep", "qwen3:4b")):
        result = analyzer.stream_inline(ChatRequest(
            profile=ProfileInput(profileId="p-1"),
            question="Analiza patrones complejos en las relaciones observadas.", mode=mode,
        ))
        assert result.model == expected
        list(result.chunks)
        assert fake.calls[-1]["model"] == expected


def test_analyzer_thinking_budget_uses_separate_cap():
    from app.analyzer import PoliceAnalyzer
    cfg = replace(settings, llm_max_predict=500, llm_thinking_max_predict=1200)
    analyzer = PoliceAnalyzer(cfg, object(), None)
    assert analyzer._runtime(intent="relations", mode="deep", thinking=True).num_predict == 1200
    assert analyzer._runtime(intent="relations", mode="deep", thinking=False).num_predict <= 500


def test_num_thread_only_when_configured_and_stats_average():
    cfg = replace(settings, llm_num_thread=6)
    assert payload(cfg)["options"]["num_thread"] == 6
    assert "num_thread" not in payload(replace(cfg, llm_num_thread=None))["options"]
    stats = LlmStats()
    stats.observe("qwen3:1.7b", {"done": True, "prompt_eval_count": 40, "prompt_eval_duration": 2_000_000_000,
        "eval_count": 10, "eval_duration": 1_000_000_000, "load_duration": 5_000_000})
    obj = stats.snapshot()
    assert obj["calls"] == 1
    assert obj["byModel"]["qwen3:1.7b"]["avgEvalTokensPerSecond"] == 10.0
''')

# 11. Documento (sólo sección nueva + corregir cabecera sobre modelos).
c='README.md'
rep(c,'# SPM IA Agent API — 4.0.2 hybrid-router + razonamiento en español','# SPM IA Agent API — 4.1.0-cpu-optimized')
rep(c,'spm-profile-intelligence-api:4.0.2-spanish-reasoning','spm-profile-intelligence-api:4.1.0-cpu-optimized')
rep(c,'## Pruebas\n\n```text\n25 passed\n```','## Pruebas (referencia histórica 4.0.2)\n\nLa versión anterior documentaba 25 tests; ejecutar de nuevo la suite sobre esta rama antes de publicar.')
rep(c,'`-- análisis/patrones/hipótesis/etc. --> Context Builder --> Qwen3:4b','`-- análisis/patrones/hipótesis/etc. --> Context Builder --> FAST 1.7b / DEEP 4b')
rep(c,'Preguntas como `analiza patrones`, `propón hipótesis`, `compara inconsistencias`, `qué relación es más relevante` o el modo `deep` siguen usando Qwen3:4b.','Preguntas analíticas normales usan el modelo FAST (1.7b); `mode=deep` usa el modelo DEEP (4b).')
rep(c,'## 4.0.2 — idioma del razonamiento\n\nCuando `thinking=true`, la API agrega un bloqueo de idioma en el mensaje de sistema y justo antes de `/think` para solicitar que el canal privado textual `thinking/reasoning` de Qwen se genere en español mexicano desde el primer token. Ese canal continúa siendo privado y nunca se expone por SSE al frontend.\n\nImportante: esta configuración controla la salida textual de razonamiento que el modelo genera a través de Ollama. No existe una forma de imponer o verificar el "idioma" de representaciones internas no textuales del modelo. La respuesta visible final sí continúa validándose para mantenerse en español.','El forzado de idioma en el razonamiento privado está desactivado por defecto (`AI_FORCE_SPANISH_THINKING=false`). La respuesta final visible continúa validándose en español antes de entregarse al frontend.')
put(c,read(c)+'''

## Modo CPU (4.1.0)

No requiere GPU. El Fast Router, Context Builder, SSE, caché y contrato con frontend se conservan.
El modo `auto`/`quick` analítico usa FAST; `deep` usa DEEP. Si `AI_LLM_MODEL_FAST` o
`AI_LLM_MODEL_DEEP` quedan vacíos, se reutiliza `AI_LLM_MODEL` (compatibilidad).

```env
AI_APP_VERSION=4.1.0-cpu-optimized
AI_LLM_MODEL=qwen3:4b
AI_LLM_MODEL_FAST=qwen3:1.7b
AI_LLM_MODEL_DEEP=qwen3:4b
AI_LLM_THINKING_DEFAULT=false
AI_LLM_THINKING_MAX_PREDICT=1200
AI_FORCE_SPANISH_THINKING=false
AI_LLM_NUM_CTX=4096
AI_LLM_MAX_PREDICT=500
AI_CONTEXT_MAX_CHARS=6000
AI_CONTEXT_DEEP_MAX_CHARS=12000
AI_LLM_MAX_EVIDENCE=30
AI_LLM_HISTORY_MESSAGES=2
AI_LLM_NUM_THREAD=
AI_LLM_WARMUP=true
```

`AI_LLM_NUM_THREAD` vacío permite que Ollama decida. Si lo defines, usa el número de
**núcleos FÍSICOS** accesibles al contenedor (no hilos lógicos). Con WSL2 valida
`docker info` y los límites de CPU/RAM de Docker Desktop. `num_predict=1200` se aplica
*solo* al modo `thinking=true` explícito; sin thinking, máximo 500.
El prompt se limita con una cota conservadora basada en bytes UTF-8, se prioriza
recortar evidencia, nunca las instrucciones de sistema.

En `docker-compose.local.yml` Ollama usa 1 solicitud paralela, máximo 2 modelos
cargados, flash attention y caché KV q8_0. Confirma que tu versión de Ollama
soporta estas opciones; la memoria suficiente para ambos modelos depende del equipo.

Los scripts `setup-local-model.ps1`, `.sh` o `.cmd` instalan ambos pesos sin borrar
`spm-ollama-models`. Asegúrate de transferir estos valores a tu `.env` existente;
`.env.example` y los defaults de código **no sobrescriben** entradas existentes en `.env`.

```powershell
.\\setup-local-model.ps1
docker compose -f .\\docker-compose.local.yml --env-file .\\.env build --no-cache profile-intelligence-api
docker compose -f .\\docker-compose.local.yml --env-file .\\.env up -d --force-recreate
curl.exe http://localhost:3651/health
docker exec -it spm-ollama-local ollama run qwen3:4b --verbose
```

`/health.llmStats` da `calls` y `byModel[modelo]`, con promedios de
`prompt_eval_count`, `prompt_eval_duration`, `eval_count`, `eval_duration`,
`load_duration`, `promptTokensPerSecond` y `evalTokensPerSecond`. Las duraciones
son nanosegundos como las devuelve Ollama. Warm-up no cuenta en promedios.
Compara la mediana y promedio de duración por pregunta equivalente antes/después;
una caída en `eval_count`, `prompt_eval_count` y `load_duration` normalmente reduce
latencia; `tokens/s` debe mantenerse o mejorar. Los contadores se reinician con la API.

Para pruebas locales:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q tests/test_cpu_optimized.py tests/test_ollama_runtime_payload.py tests/test_reasoning_output_guard.py tests/test_context_builder.py tests/test_runtime_policy.py
```
''')

# Todas las validaciones terminan antes de tocar cualquier archivo.
for path, (_, content) in changes.items():
    if content is not None and path.endswith('.py'):
        ast.parse(content, filename=path)
for path,(before,after) in sorted(changes.items()):
    if after is None or before==after: continue
    diff=list(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile='a/'+path,tofile='b/'+path))
    print(''.join(diff))
if not a.check:
    for path,(before,after) in changes.items():
        if after is not None and before!=after:
            file=root/path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(after,encoding='utf-8')
    print('PARCHE APLICADO: '+', '.join(sorted(k for k,(b,v) in changes.items() if v is not None and v!=b)))
else: print('Validación correcta; no se escribió ningún archivo.')
