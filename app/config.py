from __future__ import annotations

import os
from dataclasses import dataclass


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    value = os.getenv(name, default)
    return tuple(item.strip() for item in value.split(',') if item.strip())


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


@dataclass(frozen=True)
class Settings:
    allowed_origins: tuple[str, ...] = _csv_env(
        'AI_ALLOWED_ORIGINS',
        'http://localhost:4200,http://127.0.0.1:4200,http://localhost:4202,http://127.0.0.1:4202,http://localhost:4204,http://127.0.0.1:4204',
    )
    allowed_hosts: tuple[str, ...] = _csv_env(
        'AI_ALLOWED_HOSTS',
        'localhost,127.0.0.1,testserver',
    )
    enable_docs: bool = _bool_env('AI_ENABLE_DOCS', False)
    max_content_length: int = int(os.getenv('AI_MAX_CONTENT_LENGTH', str(5 * 1024 * 1024)))

    # El único destino HTTP permitido para inferencia es un runtime local explícito.
    llm_enabled: bool = _bool_env('AI_LLM_ENABLED', True)
    llm_base_url: str = os.getenv('AI_LLM_BASE_URL', 'http://ollama:11434').strip()
    llm_allowed_hosts: tuple[str, ...] = _csv_env(
        'AI_LLM_ALLOWED_HOSTS',
        'ollama,localhost,127.0.0.1,host.docker.internal',
    )
    llm_model: str = os.getenv('AI_LLM_MODEL', 'qwen3:4b').strip()
    llm_timeout_seconds: int = int(os.getenv('AI_LLM_TIMEOUT_SECONDS', '180'))
    llm_temperature: float = float(os.getenv('AI_LLM_TEMPERATURE', '0.15'))
    llm_max_predict: int = int(os.getenv('AI_LLM_MAX_PREDICT', '900'))
    llm_max_evidence: int = int(os.getenv('AI_LLM_MAX_EVIDENCE', '72'))
    llm_history_messages: int = int(os.getenv('AI_LLM_HISTORY_MESSAGES', '8'))


settings = Settings()
