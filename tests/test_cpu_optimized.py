import json
import pytest
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
    pytest.importorskip("ijson")
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
    pytest.importorskip("ijson")
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
