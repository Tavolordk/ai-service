import json
from dataclasses import replace

from app.config import settings
from app.ollama_client import LocalOllamaClient


def test_payload_uses_dynamic_runtime_budget_and_keep_alive():
    cfg = replace(settings, llm_num_ctx=12288, llm_max_predict=2400, llm_keep_alive="30m")
    client = LocalOllamaClient(cfg)
    payload = json.loads(client._payload(
        [{"role": "user", "content": "Resume el perfil"}],
        thinking=False,
        stream=True,
        num_ctx=4096,
        num_predict=650,
    ))
    assert payload["options"]["num_ctx"] == 4096
    assert payload["options"]["num_predict"] == 650
    assert payload["keep_alive"] == "30m"
    assert "/no_think" in payload["messages"][-1]["content"]
