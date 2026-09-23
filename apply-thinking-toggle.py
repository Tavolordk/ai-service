from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()
FILES = {
    "models": ROOT / "app" / "models.py",
    "llm": ROOT / "app" / "llm_local.py",
    "grounded": ROOT / "app" / "grounded_chat.py",
}


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def read(path: Path) -> str:
    if not path.exists():
        fail(f"No existe {path}. Ejecuta este script desde la raíz de ai-service.")
    return path.read_text(encoding="utf-8")


def patch_models(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    m = re.search(r"(?ms)^class\s+ChatRequest\b.*?(?=^class\s+ChatResponse\b)", text)
    if not m:
        fail("No encontré la clase ChatRequest en app/models.py")

    block = m.group(0)
    if re.search(r"(?m)^\s*thinking\s*:\s*bool\b", block):
        notes.append("models.py: thinking ya existía")
        return text, notes

    history = re.search(r"(?m)^(\s*)history\s*:[^\n]+$", block)
    if history:
        indent = history.group(1)
        pos = history.end()
        block = block[:pos] + f"\n{indent}thinking: bool = False" + block[pos:]
    else:
        # Fallback seguro: insertar antes del final del bloque de la clase.
        lines = block.rstrip("\n").splitlines()
        indent = "    "
        lines.append(f"{indent}thinking: bool = False")
        block = "\n".join(lines) + "\n"

    text = text[:m.start()] + block + text[m.end():]
    notes.append("models.py: agregado thinking: bool = False a ChatRequest")
    return text, notes


def _stream_method_bounds(text: str) -> tuple[int, int]:
    m = re.search(r"(?m)^\s*def\s+stream_chat\s*\(", text)
    if not m:
        fail("No encontré def stream_chat(...) en app/llm_local.py")
    start = m.start()
    indent = len(m.group(0)) - len(m.group(0).lstrip())
    # siguiente def/class al mismo o menor nivel
    tail = text[m.end():]
    end = len(text)
    for nxt in re.finditer(r"(?m)^(\s*)(?:def|class)\s+", tail):
        nxt_indent = len(nxt.group(1))
        if nxt_indent <= indent:
            end = m.end() + nxt.start()
            break
    return start, end


def patch_llm(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    start, end = _stream_method_bounds(text)
    method = text[start:end]

    # 1) parámetro thinking en la firma. Trabajamos sólo hasta el primer ':' de la firma.
    sig = re.search(r"(?ms)^([ \t]*)def\s+stream_chat\s*\((.*?)\)\s*(?:->\s*[^:\n]+)?\s*:", method)
    if not sig:
        fail("Encontré stream_chat, pero no pude interpretar su firma en app/llm_local.py")

    params = sig.group(2)
    if re.search(r"\bthinking\s*:", params):
        notes.append("llm_local.py: parámetro thinking ya existía")
    else:
        stripped = params.rstrip()
        suffix_ws = params[len(stripped):]
        if "\n" in params:
            # Firma multilínea: mantener estilo e insertar antes del cierre.
            base_indent = sig.group(1)
            param_indent_match = re.search(r"\n([ \t]+)\S", params)
            param_indent = param_indent_match.group(1) if param_indent_match else base_indent + "    "
            if stripped.endswith(","):
                new_params = stripped + f"\n{param_indent}thinking: bool = False," + suffix_ws
            else:
                new_params = stripped + f",\n{param_indent}thinking: bool = False," + suffix_ws
        else:
            if stripped:
                new_params = stripped + ", *, thinking: bool = False" + suffix_ws
            else:
                new_params = "*, thinking: bool = False" + suffix_ws
        method = method[:sig.start(2)] + new_params + method[sig.end(2):]
        notes.append("llm_local.py: agregado parámetro thinking a stream_chat")

    # Recalcular el bloque tras cambiar firma.
    # 2) hacer dinámico think.
    if re.search(r"['\"]think['\"]\s*:\s*thinking\b", method):
        notes.append("llm_local.py: think ya era dinámico")
    else:
        method2, count = re.subn(
            r"(?m)(['\"]think['\"]\s*:\s*)False\b",
            r"\1thinking",
            method,
            count=1,
        )
        if count == 0:
            # Si no existe think, insertarlo después de stream=True dentro del payload.
            method2, count = re.subn(
                r"(?m)^(\s*)(['\"]stream['\"]\s*:\s*True\s*,?)\s*$",
                lambda m: f"{m.group(1)}{m.group(2)}\n{m.group(1)}'think': thinking,",
                method,
                count=1,
            )
        if count == 0:
            fail("No pude localizar 'think': False ni 'stream': True dentro de stream_chat")
        method = method2
        notes.append("llm_local.py: Ollama ahora recibe think=thinking por petición")

    # 3) verificar que no se transmita message.thinking.
    if re.search(r"message\.get\(\s*['\"]thinking['\"]", method) or re.search(r"\[['\"]thinking['\"]\]", method):
        fail(
            "stream_chat todavía lee message.thinking. No lo modifiqué automáticamente para evitar romper tu parser; "
            "revisa ese bloque antes de desplegar."
        )

    # Debe existir lectura de content; si no, abortar.
    if not (re.search(r"message\.get\(\s*['\"]content['\"]", method) or re.search(r"\[['\"]content['\"]\]", method)):
        fail("No pude confirmar que stream_chat transmita message.content; no escribiré cambios inseguros.")

    # Comentario de seguridad, sólo una vez.
    if "message.thinking" not in method and "razonamiento interno" not in method.lower():
        marker = re.search(r"(?m)^(\s*)message\s*=.*$", method)
        if marker:
            indent = marker.group(1)
            comment = (
                f"{indent}# El razonamiento interno de Qwen3 (message.thinking) es privado.\n"
                f"{indent}# Sólo se transmite message.content al cliente.\n"
            )
            method = method[:marker.start()] + comment + method[marker.start():]

    text = text[:start] + method + text[end:]
    return text, notes


def patch_grounded(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []

    # 1) pasar el flag desde request a Ollama.
    if "stream_chat(messages, thinking=request.thinking)" in text:
        notes.append("grounded_chat.py: stream_chat ya recibe request.thinking")
    else:
        text2, count = re.subn(
            r"self\.llm\.stream_chat\(\s*messages\s*\)",
            "self.llm.stream_chat(messages, thinking=request.thinking)",
            text,
        )
        if count == 0:
            fail("No encontré self.llm.stream_chat(messages) en app/grounded_chat.py")
        text = text2
        notes.append(f"grounded_chat.py: thinking conectado a Ollama ({count} llamada(s))")

    # 2) reforzar español + salida final sin depender de numeración de reglas.
    spanish_rule = "Responde SIEMPRE en español mexicano"
    if spanish_rule in text:
        notes.append("grounded_chat.py: reglas de español/privacidad ya existían")
    else:
        marker = "FOCO DE ESTA PREGUNTA:"
        idx = text.find(marker)
        if idx == -1:
            fail("No encontré 'FOCO DE ESTA PREGUNTA:' para insertar reglas de salida")
        rules = (
            "REGLAS DE SALIDA OBLIGATORIAS:\n"
            "- Responde SIEMPRE en español mexicano, aunque el razonamiento interno del modelo ocurra en otro idioma.\n"
            "- Entrega únicamente la respuesta final al usuario. Nunca expongas cadena de pensamiento, deliberación interna, borradores ni etiquetas <think>.\n"
        )
        text = text[:idx] + rules + text[idx:]
        notes.append("grounded_chat.py: reforzada respuesta final en español y sin cadena de pensamiento")

    return text, notes


def main() -> None:
    original = {key: read(path) for key, path in FILES.items()}

    models, n1 = patch_models(original["models"])
    llm, n2 = patch_llm(original["llm"])
    grounded, n3 = patch_grounded(original["grounded"])

    changed = {
        "models": models,
        "llm": llm,
        "grounded": grounded,
    }

    # Escribir backups y cambios sólo después de validar todos los patrones.
    for key, path in FILES.items():
        backup = path.with_suffix(path.suffix + ".bak-thinking-toggle")
        if not backup.exists():
            shutil.copy2(path, backup)
        if original[key] != changed[key]:
            path.write_text(changed[key], encoding="utf-8", newline="")

    print("\nCambios aplicados/verificados:")
    for note in n1 + n2 + n3:
        print(f"  - {note}")

    print("\nValidando sintaxis Python...")
    cmd = [sys.executable, "-m", "py_compile", *(str(p) for p in FILES.values())]
    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        fail("La validación sintáctica falló. Restaura los .bak-thinking-toggle antes de continuar.")

    print("OK: sintaxis válida.")
    print("\nAhora revisa el diff:")
    print("  git diff -- app/models.py app/llm_local.py app/grounded_chat.py")
    print("\nLuego, si el diff se ve correcto:")
    print('  git add app/models.py app/llm_local.py app/grounded_chat.py')
    print('  git commit -m "feat: toggle de razonamiento Qwen3 por petición"')
    print('  git push')


if __name__ == "__main__":
    main()
