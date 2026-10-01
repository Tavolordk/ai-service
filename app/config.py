from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "si", "sí"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _csv(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("AI_APP_NAME", "SPM IA Agent API")
    app_version: str = os.getenv("AI_APP_VERSION", "4.0.0-hybrid-router")
    host: str = os.getenv("AI_HOST", "0.0.0.0")
    port: int = _int("AI_PORT", 8080)
    enable_docs: bool = _bool("AI_ENABLE_DOCS", True)

    allowed_origins: tuple[str, ...] = _csv(
        "AI_ALLOWED_ORIGINS",
        "http://localhost:4200,http://localhost:4202,http://localhost:3650,"
        "http://127.0.0.1:4200,http://127.0.0.1:4202,http://127.0.0.1:3650",
    )
    allowed_hosts: tuple[str, ...] = _csv(
        "AI_ALLOWED_HOSTS",
        "localhost,127.0.0.1,profile-intelligence-api",
    )

    llm_enabled: bool = _bool("AI_LLM_ENABLED", True)
    llm_base_url: str = os.getenv("AI_LLM_BASE_URL", "http://ollama:11434").rstrip("/")
    llm_model: str = os.getenv("AI_LLM_MODEL", "qwen3:4b")
    llm_allowed_hosts: tuple[str, ...] = _csv("AI_LLM_ALLOWED_HOSTS", "ollama,localhost,127.0.0.1")
    llm_temperature: float = _float("AI_LLM_TEMPERATURE", 0.10)
    llm_num_ctx: int = _int("AI_LLM_NUM_CTX", 12288)
    llm_max_predict: int = _int("AI_LLM_MAX_PREDICT", 2400)
    # AI_LLM_TIMEOUT_SECONDS se conserva por compatibilidad con despliegues anteriores.
    llm_timeout_seconds: int = _int("AI_LLM_TIMEOUT_SECONDS", 1200)
    llm_connect_timeout_seconds: int = _int("AI_LLM_CONNECT_TIMEOUT_SECONDS", 30)
    llm_first_token_timeout_seconds: int = _int("AI_LLM_FIRST_TOKEN_TIMEOUT_SECONDS", 1200)
    llm_stream_idle_timeout_seconds: int = _int("AI_LLM_STREAM_IDLE_TIMEOUT_SECONDS", 600)
    llm_final_timeout_seconds: int = _int("AI_LLM_FINAL_TIMEOUT_SECONDS", 1800)
    llm_history_messages: int = _int("AI_LLM_HISTORY_MESSAGES", 6)
    llm_max_evidence: int = _int("AI_LLM_MAX_EVIDENCE", 72)
    llm_keep_alive: str = os.getenv("AI_LLM_KEEP_ALIVE", "30m")

    # Router híbrido: resuelve consultas factuales y resúmenes frecuentes sin invocar Qwen.
    fast_path_enabled: bool = _bool("AI_FAST_PATH_ENABLED", True)
    fast_summary_enabled: bool = _bool("AI_FAST_SUMMARY_ENABLED", True)
    fast_max_list_items: int = _int("AI_FAST_MAX_LIST_ITEMS", 12)

    # Presupuestos compactos: el JSON completo se reduce antes de llegar al LLM.
    context_max_chars: int = _int("AI_CONTEXT_MAX_CHARS", 14000)
    context_deep_max_chars: int = _int("AI_CONTEXT_DEEP_MAX_CHARS", 28000)
    context_max_facts_per_section: int = _int("AI_CONTEXT_MAX_FACTS_PER_SECTION", 28)
    context_cache_entries: int = _int("AI_CONTEXT_CACHE_ENTRIES", 128)
    context_cache_ttl_seconds: int = _int("AI_CONTEXT_CACHE_TTL_SECONDS", 3600)
    max_inline_content_length: int = _int("AI_MAX_CONTENT_LENGTH", 5_242_880)
    max_case_bytes: int = _int("AI_MAX_CASE_BYTES", 1_073_741_824)
    deep_batch_chars: int = _int("AI_DEEP_BATCH_CHARS", 42000)
    deep_max_chunks: int = _int("AI_DEEP_MAX_CHUNKS", 5000)

    case_data_dir: str = os.getenv("AI_CASE_DATA_DIR", "/data/cases")


settings = Settings()
