"""Compactación de mensajes por líneas completas, sin destruir evidencia documental.

No se confunden bytes con tokens: el estimador es conservador, no un tokenizador
exacto del modelo. Ollama decide el consumo real de la ventana de contexto.
"""
from __future__ import annotations

import math

MARKER = "CONTEXTO VERIFICADO Y COMPACTADO:\n"
PRIORITY_HEADER = "[EVIDENCIA PRIORITARIA CON FUENTES]"


def prompt_token_ceiling(messages: list[dict[str, str]]) -> int:
    """Estimación con reserva para chat template y tokens especiales.

    Aproximamos a 2 bytes UTF-8 por token en textos mixtos español/identificadores.
    Es un estimador, no una cota matemática ni medición de Qwen.
    """
    return 160 + sum(math.ceil(len(str(msg.get("content") or "").encode("utf-8")) / 2) + 20 for msg in messages)


def _context_lines(content: str) -> tuple[str, list[str]]:
    head, _, context = content.partition(MARKER)
    return head + MARKER, [line.strip() for line in context.splitlines() if line.strip()]


def _rebuild(head: str, lines: list[str], directive: str) -> str:
    return head + ("\n".join(lines) if lines else "[sin evidencia disponible]") + ("\n\n" + directive if directive else "")


def fit_messages(messages: list[dict[str, str]], num_ctx: int, num_predict: int) -> list[dict[str, str]]:
    """Reserva salida; elimina historial antes que evidencia y nunca corta un hecho.

    Las líneas de [EVIDENCIA PRIORITARIA CON FUENTES] se preservan primero.
    El marcador de control /think o /no_think se conserva en su posición final.
    """
    result = [dict(message) for message in messages]
    budget = num_ctx - num_predict
    if budget < 320:
        raise ValueError("La ventana de contexto no deja espacio suficiente para el prompt")

    # Una conversación histórica no debe desplazar hechos actuales.
    while prompt_token_ceiling(result) > budget and len(result) > 2:
        non_system = next((i for i, m in enumerate(result[:-1]) if m.get("role") != "system"), None)
        if non_system is None:
            break
        result.pop(non_system)

    if prompt_token_ceiling(result) <= budget:
        return result

    for msg in reversed(result):
        if msg.get("role") != "user" or MARKER not in str(msg.get("content") or ""):
            continue
        content = str(msg["content"])
        directive = next((d for d in ("/no_think", "/think") if content.rstrip().endswith(d)), "")
        if directive:
            content = content.rstrip()[:-len(directive)].rstrip()
        head, lines = _context_lines(content)
        prioritized = []
        others = []
        is_priority = False
        for line in lines:
            if line == PRIORITY_HEADER:
                is_priority = True
                prioritized.append(line)
                continue
            if line.startswith("[") and line.endswith("]"):
                is_priority = False
            (prioritized if is_priority else others).append(line)

        # Orden documental: datos críticos primero y contexto original después.
        retained: list[str] = []
        for line in [*prioritized, *others]:
            candidate = retained + [line]
            msg["content"] = _rebuild(head, candidate, directive)
            if prompt_token_ceiling(result) <= budget:
                retained.append(line)
        msg["content"] = _rebuild(head, retained, directive)
        break

    if prompt_token_ceiling(result) > budget:
        # Compatibilidad para invocaciones directas sin Context Builder (warmup,
        # herramientas auxiliares). Reducir por palabras, no en mitad de un UTF-8.
        for msg in reversed(result):
            if msg.get("role") != "user" or MARKER in str(msg.get("content") or ""):
                continue
            content = str(msg.get("content") or "")
            directive = next((d for d in ("/no_think", "/think") if content.rstrip().endswith(d)), "")
            prefix = content.rstrip()[:-len(directive)].rstrip() if directive else content
            words = prefix.split()
            while words and prompt_token_ceiling(result) > budget:
                words.pop()
                msg["content"] = " ".join(words) + ("\n\n" + directive if directive else "")
            break

    if prompt_token_ceiling(result) > budget:
        # No amputar la pregunta ni el sistema. Fallar explícitamente es mejor
        # que pedir al LLM que conteste basándose en campos incompletos.
        raise ValueError("AI_LLM_NUM_CTX insuficiente para sistema y pregunta sin recortar evidencia")
    return result
