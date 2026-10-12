"""4.1.2 — contratos de comparación documental, no alucinación y modo deep."""
import json
import sys
import types
from dataclasses import replace
from pathlib import Path

from app.config import settings
from app.documentary_compare import (compare_documentary_values, compact_documentary_context,
                                    documentary_report, straightforward_comparison,
                                    direct_conflicting_fact)
from app.evidence_guard import (documented_pairs, missing_documented_pairs,
                               missing_documented_sources, safe_evidence_fallback,
                               ungrounded_identifiers)
from app.token_budget import fit_messages, prompt_token_ceiling

FIXTURE = Path(__file__).parent / "fixtures" / "perfil_consolidado_ficticio.json"


def fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_two_real_conflicts_exact_sources_and_vin():
    conflicts = compare_documentary_values(fixture()["profile"])
    assert len(conflicts) == 2
    assert conflicts[0].label == "FECHA DE NACIMIENTO"
    assert [(v.value, v.source) for v in conflicts[0].values] == [
        ("1990-05-16", "RENAPO_SIMULADO"),
        ("1990-05-18", "CAPTURA_INTERNA_SIMULADA")]
    assert conflicts[1].subject == "vin-ficticio-001"
    assert [(v.value, v.source) for v in conflicts[1].values] == [
        ("ABC123A", "REPUVE_SIMULADO"),
        ("ABC128A", "REGISTRO_VEHICULAR_SIMULADO")]
    assert "GRO456B" not in documentary_report(conflicts, "Compara la fecha y las placas")


def test_same_date_format_not_false_conflict_and_different_vin_not_associated():
    profile = {"data": [
        {"dataType": "FECHA DE NACIMIENTO", "value": "1990-05-16"},
        {"dataType": "FECHA DE NACIMIENTO", "value": "16/05/1990"},
    ], "vehicles": [
        {"vin": "VIN-UNO", "licensePlate": "AAA123"},
        {"vin": "VIN-DOS", "licensePlate": "BBB234"},
    ]}
    assert compare_documentary_values(profile) == []


def test_compact_context_preserves_pairs_sources_addresses_and_graph():
    payload = fixture()
    q = "Analiza el perfil, contrastando fecha de nacimiento, placas, domicilios y relaciones"
    context = compact_documentary_context(payload["profile"], payload["graph"], q, mode="deep")
    assert len(context) < 3500
    for expected in ("1990-05-16", "1990-05-18", "ABC123A", "ABC128A",
                     "RENAPO_SIMULADO", "CAPTURA_INTERNA_SIMULADA", "REPUVE_SIMULADO",
                     "REGISTRO_VEHICULAR_SIMULADO", "domicilio", "vínculo"):
        assert expected in context
    messages = [
        {"role": "system", "content": "Analista: responde en español, con evidencia y sin inventar."},
        {"role": "user", "content": f"PREGUNTA: {q}\nCONTEXTO VERIFICADO Y COMPACTADO:\n{context}\n/no_think"},
    ]
    fitted = fit_messages(messages, 4096, 500)
    assert prompt_token_ceiling(fitted) + 500 <= 4096
    assert documented_pairs(fitted) == [("1990-05-16", "1990-05-18"), ("ABC123A", "ABC128A")]
    assert "Domicilios documentados" in safe_evidence_fallback(fitted)
    assert "Vínculos expresamente registrados" in safe_evidence_fallback(fitted)
    assert len(safe_evidence_fallback(fitted)) < 2500


def test_grounded_output_keeps_guard_but_rejects_unverified_claims():
    p = fixture()
    context = compact_documentary_context(p["profile"], p["graph"], "Compara fecha y placas")
    msgs = [{"role": "user", "content": "PREGUNTA: Compara fecha y placas\nCONTEXTO VERIFICADO Y COMPACTADO:\n" + context}]
    grounded = ("Fecha: 1990-05-16 según RENAPO_SIMULADO y 1990-05-18 según "
                "CAPTURA_INTERNA_SIMULADA. Placas: ABC123A de REPUVE_SIMULADO "
                "y ABC128A de REGISTRO_VEHICULAR_SIMULADO.")
    assert not missing_documented_pairs(grounded, msgs)
    assert not missing_documented_sources(grounded, msgs)
    assert not ungrounded_identifiers(grounded, msgs)
    assert ungrounded_identifiers("La placa DEF123Z fue documentada", msgs)
    assert missing_documented_sources("Son ABC123A y ABC128A, 1990-05-16 y 1990-05-18.", msgs)
    fallback = safe_evidence_fallback(msgs)
    assert "Hipótesis no confirmadas" in fallback
    assert all(x in fallback for x in ("ABC123A", "ABC128A", "1990-05-16", "1990-05-18"))


def test_simple_comparison_vs_real_interpretation_routing():
    diffs = compare_documentary_values(fixture()["profile"])
    assert straightforward_comparison("Analiza diferencias documentadas entre nacimiento y placas", "auto", False)
    assert not straightforward_comparison("Explica por qué hay diferencias y plantea hipótesis", "auto", False)
    assert not straightforward_comparison("Compara diferencias", "deep", False)
    assert direct_conflicting_fact("¿Cuál es la fecha de nacimiento?", diffs)
    assert direct_conflicting_fact("¿Qué placas tiene el mismo VIN?", diffs)
    assert not direct_conflicting_fact("¿Cuántos vehículos tiene?", diffs)


def test_comparator_intercepts_contradictory_birthdate_before_fast_router(monkeypatch):
    # El entorno de CI sin ijson no ejecuta ingestión; se carga la interfaz para
    # comprobar routing sin modificar código ni la dependencia real de producción.
    if "ijson" not in sys.modules:
        monkeypatch.setitem(sys.modules, "ijson", types.ModuleType("ijson"))
    from app.analyzer import PoliceAnalyzer
    from app.models import ChatRequest

    class NeverCallLlm:
        def stream_chat_events(self, *a, **kw):
            raise AssertionError("No debe invocarse Qwen en cotejos documentales")

    payload = fixture()
    cfg = replace(settings, fast_path_enabled=True, llm_num_ctx=4096, llm_max_predict=500)
    analyzer = PoliceAnalyzer(cfg, NeverCallLlm(), None)
    for question in ("¿Cuál es la fecha de nacimiento?", "Compara las diferencias de fecha y placas"):
        resp = analyzer.stream_inline(ChatRequest.model_validate({**payload, "question": question}))
        answer = "".join(item.text for item in resp.chunks)
        assert resp.model == "SPM Document Comparator"
        assert resp.route == "documentary:comparison"
        assert "1990-05-16" in answer and "1990-05-18" in answer
    # El contrato viejo sigue sirviendo para respuestas sencillas no ambiguas.
    resp = analyzer.stream_inline(ChatRequest.model_validate({**payload, "question": "¿Cuántos vehículos hay relacionados en el grafo?"}))
    assert resp.model == "SPM Fast Router"


def test_deep_uses_compact_context_and_correct_model(monkeypatch):
    if "ijson" not in sys.modules:
        monkeypatch.setitem(sys.modules, "ijson", types.ModuleType("ijson"))
    from app.analyzer import PoliceAnalyzer
    from app.models import ChatRequest
    from app.ollama_client import LlmStreamChunk

    class FakeLlm:
        def __init__(self):
            self.messages = None
            self.kwargs = None
        def stream_chat_events(self, messages, **kwargs):
            self.messages = messages
            self.kwargs = kwargs
            yield LlmStreamChunk("content", "Hechos documentados verificados.")

    payload = fixture()
    payload.update(question=("Analiza el perfil consolidado, contrastando fechas de nacimiento, "
                             "vehículos, domicilios y relaciones. Separa hechos, hipótesis y datos insuficientes."), mode="deep")
    client = FakeLlm()
    analyzer = PoliceAnalyzer(replace(settings, llm_model_deep="qwen3:4b", llm_num_ctx=4096), client, None)
    result = analyzer.stream_inline(ChatRequest.model_validate(payload))
    assert result.model == "qwen3:4b"
    list(result.chunks)
    assert client.kwargs["model"] == "qwen3:4b"
    text = client.messages[-1]["content"]
    assert len(text) < 4500
    assert "1990-05-16" in text and "1990-05-18" in text
    assert "ABC123A" in text and "ABC128A" in text
    assert "[EVIDENCIA PRIORITARIA CON FUENTES]" in text
    assert "domicilio" in text and "vínculo" in text
