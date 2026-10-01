from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeBudget:
    num_ctx: int
    num_predict: int


def choose_runtime_budget(
    *,
    intent: str,
    mode: str,
    thinking: bool,
    max_num_ctx: int,
    max_predict: int,
) -> RuntimeBudget:
    """Presupuesto conservador para CPU: usa sólo lo necesario y respeta máximos configurables."""
    if mode == "deep":
        ctx, predict = 12288, 2200
    elif intent in {"identity", "addresses", "sources", "events"}:
        ctx, predict = 4096, 650
    elif intent in {"vehicles", "weapons"}:
        ctx, predict = 6144, 900
    elif intent == "summary":
        ctx, predict = 6144, 900
    elif intent in {"relations", "discrepancies"}:
        ctx, predict = 8192, 1400
    else:
        ctx, predict = 6144, 1000

    if mode == "quick":
        ctx = min(ctx, 4096)
        predict = min(predict, 650)

    if thinking:
        # El pensamiento necesita algo de margen, pero no duplicamos el contexto por defecto.
        ctx = max(ctx, 8192 if intent in {"summary", "relations", "discrepancies", "general"} else 6144)
        predict = max(predict, 1400 if intent in {"relations", "discrepancies"} else 1100)

    return RuntimeBudget(
        num_ctx=max(2048, min(ctx, max_num_ctx)),
        num_predict=max(256, min(predict, max_predict)),
    )
