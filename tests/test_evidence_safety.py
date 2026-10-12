"""Regresión 4.1.1: hechos prioritarios, placas/fechas y no regeneración."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from app.config import settings
from app.evidence_context import merge_context, priority_evidence
from app.evidence_guard import (documented_pairs, missing_documented_pairs,
                                missing_documented_sources, safe_evidence_fallback, ungrounded_identifiers)
from app.ollama_client import LocalOllamaClient
from app.police_domain import SYSTEM_PROMPT
from app.token_budget import fit_messages, prompt_token_ceiling

FIXTURE = Path(__file__).parent / "fixtures" / "perfil_consolidado_ficticio.json"
QUESTION = ("Analiza las diferencias documentadas entre las fuentes de fecha de nacimiento "
            "y las dos placas para el mismo VIN. Cita valores y fuentes sin inventar hechos.")


def _messages():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    priority = priority_evidence(data["profile"], QUESTION, mode="auto")
    # Contexto secundario suficientemente grande para forzar compactación.
    context = merge_context("[OTROS DATOS]\n" + "- $.extra: abcdef123456789\n" * 600, priority)
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"PREGUNTA: {QUESTION}\nCONTEXTO VERIFICADO Y COMPACTADO:\n{context}\n\n/no_think"}]


def test_prioritized_documentary_facts_survive_ctx_4096():
    fitted = fit_messages(_messages(), 4096, 500)
    content = fitted[-1]["content"]
    for expected in ("1990-05-16", "1990-05-18", "ABC123A", "ABC128A",
                     "VIN-FICTICIO-001", "REPUVE_SIMULADO", "REGISTRO_VEHICULAR_SIMULADO",
                     "RENAPO_SIMULADO", "CAPTURA_INTERNA_SIMULADA"):
        assert expected in content
    assert content.endswith("/no_think")
    assert prompt_token_ceiling(fitted) + 500 <= 4096
    assert documented_pairs(fitted) == [("1990-05-16", "1990-05-18"), ("ABC123A", "ABC128A")]
    assert all("abcdef12345" not in l or "abcdef123456789" in l for l in content.splitlines())


def test_grounded_output_passes_and_fabricated_data_is_rejected():
    fitted = fit_messages(_messages(), 4096, 500)
    correct = ("Los registros consignan 1990-05-16 (RENAPO_SIMULADO) y 1990-05-18 "
               "(CAPTURA_INTERNA_SIMULADA). El VIN tiene ABC123A (REPUVE_SIMULADO) "
               "y ABC128A (REGISTRO_VEHICULAR_SIMULADO).")
    assert ungrounded_identifiers(correct, fitted) == []
    assert missing_documented_pairs(correct, fitted) == []
    assert missing_documented_sources(correct, fitted) == []
    assert missing_documented_sources("Las placas ABC123A y ABC128A difieren.", fitted)
    made_up = "El nacimiento fue en 1985 y las placas registradas eran ABC123X y XC-12345."
    assert {"1985", "ABC123X", "XC-12345"}.issubset(set(ungrounded_identifiers(made_up, fitted)))
    assert len(missing_documented_pairs(made_up, fitted)) == 2


def test_fallback_uses_only_received_complete_evidence():
    fitted = fit_messages(_messages(), 4096, 500)
    text = safe_evidence_fallback(fitted)
    assert "1990-05-16" in text and "1990-05-18" in text
    assert "ABC123A" in text and "ABC128A" in text
    assert "1985" not in text
    assert "fuenteA=RENAPO_SIMULADO" in text


def test_no_extra_inference_on_hallucination(monkeypatch):
    requests = []

    class Response:
        status = 200
        def __init__(self):
            self.lines = iter([
                json.dumps({"message": {"content": "La placa XC-12345 y la fecha 1985 son correctas."}, "done": False}).encode()+b"\n",
                json.dumps({"message": {}, "done": True, "done_reason": "stop", "eval_count": 20,
                            "prompt_eval_count": 500, "eval_duration": 1_000_000_000,
                            "prompt_eval_duration": 2_000_000_000}).encode()+b"\n",
            ])
        def readline(self): return next(self.lines, b"")
        def read(self, n=None): return b""

    class FakeSocket:
        def settimeout(self, seconds): pass

    class Connection:
        sock = FakeSocket()
        def __init__(self, *args, **kwargs): pass
        def request(self, method, path, body, headers): requests.append(json.loads(body))
        def getresponse(self): return Response()
        def close(self): pass

    monkeypatch.setattr("app.ollama_client.http.client.HTTPConnection", Connection)
    client = LocalOllamaClient(replace(settings, llm_num_ctx=4096, llm_max_predict=500))
    result = "".join(chunk.text for chunk in client.stream_chat_events(_messages(), model="qwen3:4b"))
    assert len(requests) == 1, "No se permiten regeneraciones automáticas extra"
    assert requests[0]["options"]["num_ctx"] == 4096
    assert requests[0]["think"] is False
    assert "1990-05-16" in result and "1990-05-18" in result
    assert "ABC123A" in result and "ABC128A" in result
    assert "XC-12345" not in result and "1985" not in result
    assert client.stats.snapshot()["calls"] == 1


def test_deep_english_meta_fragment_never_leaks(monkeypatch):
    from app.ollama_client import _extract_publishable_suffix
    assert not _extract_publishable_suffix('Si algo no aparece en el contexto, dilo de forma directa. So if the context')


def test_excessive_system_prompt_fails_instead_of_cutting_fields():
    from app.token_budget import fit_messages
    import pytest
    with pytest.raises(ValueError, match="insuficiente"):
        fit_messages([
            {"role": "system", "content": "X" * 10000},
            {"role": "user", "content": "CONTEXTO VERIFICADO Y COMPACTADO:\n- placa: ABC123A"},
        ], 4096, 500)
