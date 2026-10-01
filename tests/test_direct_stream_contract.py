from app.ollama_client import LlmStreamChunk, _looks_spanish_enough


def test_replace_chunk_is_supported():
    chunk = LlmStreamChunk("replace", "Esta es la respuesta final completa en español.")
    assert chunk.kind == "replace"
    assert chunk.text.startswith("Esta")


def test_language_guard_rejects_obvious_english():
    text = "The user wants a summary of the profile and the data provided in the context with sources and relationships."
    assert not _looks_spanish_enough(text)


def test_language_guard_accepts_spanish():
    text = "El perfil corresponde a una persona con identificadores, domicilio y vínculos registrados en las fuentes consultadas."
    assert _looks_spanish_enough(text)
