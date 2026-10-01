from app.ollama_client import _ThinkingContentFilter, _VisibleContentFilter, _looks_spanish_enough


def test_filter_removes_think_even_when_split():
    f = _VisibleContentFilter()
    chunks = ["<thi", "nk>private reasoning", "</th", "ink>Respuesta final"]
    visible = "".join(f.feed(c) for c in chunks) + f.flush()
    assert visible == "Respuesta final"


def test_filter_leaves_normal_text():
    f = _VisibleContentFilter()
    visible = f.feed("Hola en español") + f.flush()
    assert visible == "Hola en español"


def test_thinking_filter_extracts_private_block_for_compatibility():
    f = _ThinkingContentFilter()
    chunks = ["antes<thi", "nk>private reasoning", "</th", "ink>Respuesta final"]
    reasoning = "".join(f.feed(c) for c in chunks) + f.flush()
    assert reasoning == "private reasoning"


def test_spanish_heuristic_rejects_typical_english_reasoning():
    text = "Okay, the user wants a summary of the profile based on the data and context provided."
    assert _looks_spanish_enough(text) is False


def test_spanish_heuristic_accepts_spanish_answer():
    text = "El perfil corresponde a una persona con datos de identidad, domicilio y fuentes registradas."
    assert _looks_spanish_enough(text) is True
