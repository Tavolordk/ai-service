import json
from dataclasses import replace

from app.config import settings
from app.ollama_client import (
    LocalOllamaClient,
    _contains_reasoning_leak,
    _is_publishable_final,
    _looks_spanish_enough,
)


def test_detects_english_reasoning_leak():
    text = (
        "**Análisis**\nOkay, the user wants a detailed summary of the profile. "
        "Let me check the context again. I need to structure this in Spanish."
    )
    assert _contains_reasoning_leak(text) is True
    assert _is_publishable_final(text) is False


def test_detects_short_meta_reasoning_that_old_heuristic_could_miss():
    text = "Okay, let me check."
    assert _contains_reasoning_leak(text) is True
    assert _looks_spanish_enough(text) is False


def test_accepts_normal_spanish_answer():
    text = (
        "El perfil registra información de identidad, dos domicilios y varios vehículos. "
        "Los datos provienen de las fuentes incluidas en el contexto proporcionado."
    )
    assert _contains_reasoning_leak(text) is False
    assert _is_publishable_final(text) is True


def test_payload_keeps_spanish_lock_and_no_think():
    cfg = replace(settings, llm_num_ctx=8192, llm_max_predict=1200)
    client = LocalOllamaClient(cfg)
    payload = json.loads(client._payload(
        [
            {"role": "system", "content": "Analiza el contexto."},
            {"role": "user", "content": "Describe el perfil."},
        ],
        thinking=False,
        stream=True,
        num_ctx=4096,
        num_predict=800,
    ))
    system = payload["messages"][0]["content"].lower()
    user = payload["messages"][-1]["content"].lower()
    assert "español mexicano" in system
    assert "/no_think" in user
    assert payload["think"] is False


def test_payload_keeps_spanish_lock_in_thinking_mode():
    cfg = replace(settings, llm_num_ctx=8192, llm_max_predict=1200)
    client = LocalOllamaClient(cfg)
    payload = json.loads(client._payload(
        [
            {"role": "system", "content": "Analiza el contexto."},
            {"role": "user", "content": "Encuentra patrones relevantes."},
        ],
        thinking=True,
        stream=True,
        num_ctx=4096,
        num_predict=800,
    ))
    system = payload["messages"][0]["content"].lower()
    user = payload["messages"][-1]["content"].lower()
    assert "thinking/reasoning" in system
    assert "español mexicano" in system
    assert "instrucción de thinking" in user
    assert "/think" in user
    assert payload["think"] is True


def test_extracts_spanish_final_after_english_reasoning():
    from app.ollama_client import _extract_publishable_suffix
    text = (
        "**Análisis**\nOkay, the user wants a detailed summary of the profile. "
        "Let me check the context.\n\n"
        "Resumen detallado del perfil: la persona cuenta con datos de identidad, "
        "domicilios registrados y vehículos asociados en las fuentes consultadas."
    )
    result = _extract_publishable_suffix(text)
    assert result.startswith("Resumen detallado del perfil")
    assert "Okay, the user" not in result
    assert "Let me check" not in result


def test_extracts_final_when_same_line_after_meta_reasoning():
    from app.ollama_client import _extract_publishable_suffix
    text = (
        "Let me draft: El perfil presenta información de identidad y domicilios "
        "registrados en las fuentes disponibles."
    )
    result = _extract_publishable_suffix(text)
    assert result.startswith("El perfil presenta")
