from pathlib import Path
import py_compile

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "app" / "models.py"
LLM = ROOT / "app" / "llm_local.py"
CHAT = ROOT / "app" / "grounded_chat.py"

for p in (MODELS, LLM, CHAT):
    if not p.exists():
        raise SystemExit(f"No existe: {p}")

def replace_once(text, old, new, label):
    if new in text:
        print(f"[OK] {label} ya aplicado")
        return text
    if old not in text:
        raise SystemExit(f"[ERROR] No se encontró patrón para: {label}")
    print(f"[APPLY] {label}")
    return text.replace(old, new, 1)

# models.py
text = MODELS.read_text(encoding="utf-8")
text = replace_once(
    text,
    "    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=12)\n",
    "    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=12)\n    thinking: bool = False\n",
    "ChatRequest.thinking",
)
MODELS.write_text(text, encoding="utf-8")

# llm_local.py
text = LLM.read_text(encoding="utf-8")

helper = '''class _VisibleContentFilter:
    """Elimina bloques <think>...</think> aunque lleguen partidos en varios chunks."""

    OPEN = '<think>'
    CLOSE = '</think>'

    def __init__(self) -> None:
        self.buffer = ''
        self.inside_think = False

    def feed(self, chunk: str) -> str:
        self.buffer += chunk
        visible: list[str] = []

        while self.buffer:
            lowered = self.buffer.lower()

            if self.inside_think:
                close_index = lowered.find(self.CLOSE)
                if close_index >= 0:
                    self.buffer = self.buffer[close_index + len(self.CLOSE):]
                    self.inside_think = False
                    continue
                keep = min(len(self.buffer), len(self.CLOSE) - 1)
                self.buffer = self.buffer[-keep:] if keep else ''
                break

            open_index = lowered.find(self.OPEN)
            if open_index >= 0:
                if open_index:
                    visible.append(self.buffer[:open_index])
                self.buffer = self.buffer[open_index + len(self.OPEN):]
                self.inside_think = True
                continue

            hold = 0
            max_suffix = min(len(self.buffer), len(self.OPEN) - 1)
            for size in range(1, max_suffix + 1):
                if self.OPEN.startswith(self.buffer[-size:].lower()):
                    hold = size

            emit_until = len(self.buffer) - hold
            if emit_until > 0:
                visible.append(self.buffer[:emit_until])
                self.buffer = self.buffer[emit_until:]
            break

        return ''.join(visible)

    def flush(self) -> str:
        if self.inside_think:
            self.buffer = ''
            return ''
        visible = self.buffer
        self.buffer = ''
        return visible


'''

if "class _VisibleContentFilter:" not in text:
    text = replace_once(
        text,
        "class LocalLlmError(RuntimeError):\n    pass\n\n\n",
        "class LocalLlmError(RuntimeError):\n    pass\n\n\n" + helper,
        "filtro de <think>",
    )

text = replace_once(
    text,
    "    def stream_chat(self, messages: list[dict[str, str]]) -> Iterable[str]:\n",
    "    def stream_chat(self, messages: list[dict[str, str]], *, thinking: bool = False) -> Iterable[str]:\n",
    "stream_chat(thinking)",
)

if "runtime_messages = [dict(message) for message in messages]" not in text:
    old = "        if not self.settings.llm_enabled:\n            raise LocalLlmError('LLM local deshabilitado.')\n\n"
    new = (
        "        if not self.settings.llm_enabled:\n"
        "            raise LocalLlmError('LLM local deshabilitado.')\n\n"
        "        runtime_messages = [dict(message) for message in messages]\n"
        "        directive = '/think' if thinking else '/no_think'\n"
        "        for index in range(len(runtime_messages) - 1, -1, -1):\n"
        "            if runtime_messages[index].get('role') == 'user':\n"
        "                content = str(runtime_messages[index].get('content') or '').rstrip()\n"
        "                runtime_messages[index]['content'] = f'{content}\\n\\n{directive}'\n"
        "                break\n\n"
    )
    text = replace_once(text, old, new, "directiva /think /no_think")

text = text.replace("'messages': messages,", "'messages': runtime_messages,")
text = text.replace("'think': False,", "'think': thinking,")

if "visible_filter = _VisibleContentFilter()" not in text:
    text = replace_once(
        text,
        "            while True:\n",
        "            visible_filter = _VisibleContentFilter()\n\n            while True:\n",
        "filtro visible en streaming",
    )

old_block = (
    "                message = item.get('message') or {}\n"
    "                content = message.get('content')\n"
    "                if content:\n"
    "                    yield str(content)\n\n"
    "                if item.get('done'):\n"
    "                    break\n"
)
new_block = (
    "                message = item.get('message') or {}\n\n"
    "                # message.thinking se descarta deliberadamente.\n"
    "                # Sólo message.content puede salir al frontend.\n"
    "                content = message.get('content')\n"
    "                if content:\n"
    "                    visible = visible_filter.feed(str(content))\n"
    "                    if visible:\n"
    "                        yield visible\n\n"
    "                if item.get('done'):\n"
    "                    tail = visible_filter.flush()\n"
    "                    if tail:\n"
    "                        yield tail\n"
    "                    break\n"
)
if "visible = visible_filter.feed" not in text:
    text = replace_once(text, old_block, new_block, "emitir sólo content visible")

LLM.write_text(text, encoding="utf-8")

# grounded_chat.py
text = CHAT.read_text(encoding="utf-8")
text = text.replace(
    "Eres un asistente de análisis LOCAL y PRIVADO. Hablas en español mexicano claro, natural y ameno, como si le estuvieras contando al usuario el contexto de una persona de forma conversacional, pero sin perder precisión.",
    "Eres un asistente de análisis LOCAL y PRIVADO. TODA salida visible debe estar exclusivamente en español mexicano claro, natural y ameno, como si le estuvieras contando al usuario el contexto de una persona de forma conversacional, pero sin perder precisión."
)

if "17. IDIOMA OBLIGATORIO:" not in text:
    text = replace_once(
        text,
        "16. Evita cerrar cada respuesta con advertencias largas. Si necesitas un matiz de precisión, intégralo en una sola frase natural.\n",
        "16. Evita cerrar cada respuesta con advertencias largas. Si necesitas un matiz de precisión, intégralo en una sola frase natural.\n"
        "17. IDIOMA OBLIGATORIO: responde en español desde el primer carácter visible hasta el último. No escribas análisis, planes, razonamientos, prefacios ni notas en inglés. Nunca muestres cadena de pensamiento, deliberación interna ni bloques <think>...</think>; entrega sólo la respuesta final para el usuario.\n",
        "regla obligatoria de español",
    )

text = replace_once(
    text,
    "            for chunk in self.llm.stream_chat(messages):\n",
    "            for chunk in self.llm.stream_chat(messages, thinking=request.thinking):\n",
    "pasar thinking a Ollama",
)
CHAT.write_text(text, encoding="utf-8")

for p in (MODELS, LLM, CHAT):
    py_compile.compile(str(p), doraise=True)

print("OK: cambios aplicados y sintaxis válida.")
