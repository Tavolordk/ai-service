"""Comprobación limitada y determinista de identificadores citados por el LLM.

No interpreta criminalidad ni sustituye revisión humana. Bloquea fechas/placas que
no constan en los hechos proporcionados a Ollama y devoluciones meta en inglés.
"""
from __future__ import annotations

import re

_DATE_ISO = re.compile(r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b")
_DATE_MX = re.compile(r"\b\d{1,2}[/\-]\d{1,2}[/\-](?:19|20)\d{2}\b")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_PLATE = re.compile(r"(?<![\w-])[A-Z]{2,4}-?\d{3,6}[A-Z]{0,2}(?![\w-])")


def _body(messages: list[dict[str, str]]) -> str:
    return "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")


def ungrounded_identifiers(text: str, messages: list[dict[str, str]]) -> list[str]:
    """Reporta años, fechas numéricas y placas no presentes en la evidencia textual.

    No intenta validar nombres o ubicaciones arbitrarios: el filtro es explícito,
    conservador y mantiene los formatos del frontend intactos.
    """
    supplied = _body(messages).upper()
    answer = text.upper()
    failures: list[str] = []

    for matcher in (_DATE_ISO, _DATE_MX):
        for value in matcher.findall(answer):
            if value not in supplied and value not in failures:
                failures.append(value)

    # Un año no respaldado también detecta fechas parafraseadas (p. ej. 'en 1985').
    supplied_years = set(_YEAR.findall(supplied))
    for year in _YEAR.findall(answer):
        if year not in supplied_years and year not in failures:
            failures.append(year)

    # Sólo placas de formato definido; no confundir palabras o encabezados normales.
    supplied_plates = set(_PLATE.findall(supplied))
    for plate in _PLATE.findall(answer):
        if plate not in supplied_plates and plate not in failures:
            failures.append(plate)
    return failures


def documented_pairs(messages: list[dict[str, str]]) -> list[tuple[str, str]]:
    """Identifica pares A/B que el frontend incluye explícitamente como discrepancias."""
    supplied = _body(messages)
    pairs: list[tuple[str, str]] = []
    for line in supplied.splitlines():
        if not any(marker in line for marker in ("inconsistenciasDocumentales[", "comparacionDocumental[")):
            continue
        first = re.search(r"\b(?:valorFuenteA|valor1)=([^;]+)", line)
        second = re.search(r"\b(?:valorFuenteB|valor2)=([^;]+)", line)
        if first and second:
            pairs.append((first.group(1).strip(), second.group(1).strip()))
    return pairs


def missing_documented_pairs(text: str, messages: list[dict[str, str]]) -> list[tuple[str, str]]:
    """En cotejos documentales exige citar AMBOS valores de cada discrepancia."""
    user_questions = " ".join(str(m.get("content") or "").split("CONTEXTO VERIFICADO Y COMPACTADO:")[0] for m in messages if m.get("role") == "user").lower()
    if not any(part in user_questions for part in ("discrepan", "diferenc", "contradic", "inconsisten", "compar", "contrast")):
        return []
    answer = text.casefold()
    return [(a, b) for a, b in documented_pairs(messages) if a.casefold() not in answer or b.casefold() not in answer]



def missing_documented_sources(text: str, messages: list[dict[str, str]]) -> list[str]:
    """En cotejos exige atribuir cada lado al registro que lo proporcionó."""
    questions = " ".join(str(m.get("content") or "").split("CONTEXTO VERIFICADO Y COMPACTADO:")[0] for m in messages if m.get("role") == "user").lower()
    if not any(t in questions for t in ("discrepan", "diferenc", "contradic", "inconsisten", "compar", "contrast")):
        return []
    missing = []
    for line in _body(messages).splitlines():
        if not any(marker in line for marker in ("inconsistenciasDocumentales[", "comparacionDocumental[")):
            continue
        for label in (("fuente1", "fuente2") if "comparacionDocumental[" in line else ("fuenteA", "fuenteB")):
            found = re.search(r"\b" + label + r"=([^;]+)", line)
            if found and found.group(1).strip().casefold() not in text.casefold():
                missing.append(found.group(1).strip())
    return list(dict.fromkeys(missing))


def safe_evidence_fallback(messages: list[dict[str, str]]) -> str:
    """Rescate documental legible a partir de evidencia recibida por el LLM.

    No llama a otro modelo y no reconstruye registros no enviados.
    """
    content = _body(messages)
    compact = [line for line in content.splitlines() if "$.comparacionDocumental[" in line]
    if compact:
        parts = ["Hechos documentados y diferencias encontradas:"]
        for line in compact[:6]:
            values = re.findall(r"\bvalor(\d+)=([^;]+);\s*fuente\1=([^;]+)", line)
            label = re.search(r"\bcampo=([^;]+)", line)
            subject = re.search(r"\bentidad=([^;]+)", line)
            if not values:
                continue
            field = label.group(1).strip() if label else "Atributo"
            entity = subject.group(1).strip() if subject else ""
            variants = "; ".join(f"{v.strip()} (fuente: {source.strip()})" for _, v, source in values)
            parts.append(f"- {field} ({entity}): {variants}.")
        if len(parts) > 1:
            addresses = [line[2:] for line in content.splitlines() if line.startswith("- domicilio ")]
            links = [line[2:] for line in content.splitlines() if line.startswith("- vínculo ")]
            if addresses:
                parts.append("Domicilios documentados (la simultaneidad no está comprobada):")
                parts.extend("- " + line for line in addresses[:2])
            if links:
                parts.append("Vínculos expresamente registrados (no acreditan parentesco ni delito):")
                parts.extend("- " + line for line in links[:4])
            parts.extend([
                "Hipótesis no confirmadas: puede tratarse de un cambio histórico o un error de captura, pero no hay pruebas para atribuir una causa.",
                "Información pendiente: verificar documentos originales, vigencia de registros y actualizaciones de cada fuente.",
                "Conclusión: las diferencias no acreditan conductas ilícitas ni permiten escoger una versión como definitiva.",
            ])
            return "\n".join(parts)

    priority: list[str] = []
    in_priority = False
    for line in content.splitlines():
        if line == "[EVIDENCIA PRIORITARIA CON FUENTES]":
            in_priority = True
            continue
        if in_priority and line.startswith("[") and line.endswith("]"):
            in_priority = False
        if in_priority and line.startswith("- "):
            priority.append(line)
    priority.sort(key=lambda line: ("inconsistenciasDocumentales[" not in line, "FECHA DE NACIMIENTO" not in line.upper(), "licensePlate=" not in line))
    if not priority:
        priority = [line for line in content.splitlines() if line.startswith("- ")]
    if not priority:
        return "No se dispone de evidencia suficiente para producir una respuesta verificable."
    return ("No fue posible verificar automáticamente la respuesta generada. "
            "Estos son únicamente los registros disponibles, sin inferencias:\n"
            + "\n".join(priority[:9])
            + "\nLas diferencias requieren corroboración documental; no prueban por sí mismas un hecho ilícito.")
