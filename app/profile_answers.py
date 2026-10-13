"""Consultas documentales en español sobre el perfil consolidado.

Opera sobre los valores REALES de profile.data (code/value/origins), no sobre
nombres de propiedades extraídos del JSON. No utiliza Ollama para hechos.
No infiere características físicas, empleo vigente ni antecedentes.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ProfileAnswer:
    text: str
    evidence: list[dict[str, Any]]
    route: str


# Identificadores textuales exactos; todos los valores proceden del JSON.
_FIELDS = (
    ("CUIP", "CUIP"),
    ("CURP", "CURP"),
    ("RFC", "RFC"),
    ("CIB", "CIB"),
    ("NOMBRE", "Nombre"),
    ("APELLIDO_PATERNO", "Apellido paterno"),
    ("APELLIDO_MATERNO", "Apellido materno"),
    ("FECHA_NACIMIENTO", "Fecha de nacimiento"),
    ("SEXO", "Sexo registrado"),
    ("ESTADO_CIVIL", "Estado civil"),
    ("NACIONALIDAD", "Nacionalidad"),
    ("EMPLOYMENT_DEPENDENCIA", "Dependencia"),
    ("EMPLOYMENT_CORPORACION", "Corporación"),
    ("EMPLOYMENT_AREA", "Área de trabajo"),
    ("EMPLOYMENT_ENTIDAD_ADSCRIPCION", "Entidad de adscripción"),
    ("EMPLOYMENT_MUNICIPIO_ADSCRIPCION", "Municipio de adscripción"),
    ("EMPLOYMENT_PUESTO", "Puesto"),
    ("EMPLOYMENT_NIVEL_MANDO", "Nivel de mando"),
    ("EMPLOYMENT_ESTATUS_ADSCRIPCION", "Estatus de adscripción registrado"),
    ("EMPLOYMENT_ESTATUS_PERSONA", "Estatus de la persona registrado"),
    ("EMPLOYMENT_FECHA_INGRESO", "Fecha de ingreso registrada"),
    ("EMPLOYMENT_FECHA_ACTUALIZA_PERSONA", "Fecha de actualización laboral"),
    ("COLOR_PIEL", "Color de piel registrado"),
    ("TEZ", "Tez registrada"),
    ("ESTATURA", "Estatura registrada"),
    ("PESO", "Peso registrado"),
    ("COLOR_OJOS", "Color de ojos registrado"),
    ("COLOR_CABELLO", "Color de cabello registrado"),
)
_LABELS = dict(_FIELDS)
_EMPLOYMENT = tuple(code for code, _ in _FIELDS if code.startswith("EMPLOYMENT_"))
_IDENTITY = ("NOMBRE", "APELLIDO_PATERNO", "APELLIDO_MATERNO", "FECHA_NACIMIENTO", "SEXO", "ESTADO_CIVIL", "NACIONALIDAD")
_IDENTIFIERS = ("CUIP", "CURP", "RFC", "CIB")

_SOURCES = {
    "RNPSP": "Registro Nacional de Personal de Seguridad Pública (RNPSP)",
    "REPUVE": "Registro Público Vehicular (REPUVE)",
    "VRYR": "Registro Nacional de Vehículos Robados y Recuperados (VRYR)",
}


def _plain(text: Any) -> str:
    s = unicodedata.normalize("NFD", str(text or "").casefold())
    return " ".join("".join(c for c in s if unicodedata.category(c) != "Mn").split())


def _code(value: Any) -> str:
    raw = str(value or "").strip()
    # Incluye claves camelCase y delimitadores habituales sin transformar valores.
    raw = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", raw)
    return re.sub(r"[^A-Z0-9]+", "_", raw.upper()).strip("_")


def _has(q: str, *terms: str) -> bool:
    return any(re.search(r"(?<!\w)" + re.escape(_plain(t)) + r"(?!\w)", q) for t in terms)


def _mentions(q: str, *terms: str) -> bool:
    return any(_plain(t) in q for t in terms)


def _local_today() -> date:
    try:
        return datetime.now(ZoneInfo("America/Mexico_City")).date()
    except (KeyError, OSError):
        return date.today()


def _profile_rows(profile: dict[str, Any]) -> dict[str, list[tuple[str, dict[str, Any], int]]]:
    out: dict[str, list[tuple[str, dict[str, Any], int]]] = {}
    rows = profile.get("data") if isinstance(profile, dict) else None
    if not isinstance(rows, list):
        return out
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        code = _code(row.get("code") or row.get("name") or row.get("label"))
        if not code:
            continue
        v = row.get("value")
        if v is None or isinstance(v, (dict, list)):
            continue
        value = str(v).strip()
        if not value:
            continue
        unique = out.setdefault(code, [])
        if not any(_plain(existing) == _plain(value) for existing, _, _ in unique):
            unique.append((value, row, index))
    return out


def _evidence(code: str, value: str, row: dict[str, Any], index: int) -> dict[str, Any]:
    sources: list[str] = []
    for origin in row.get("origins") or []:
        if isinstance(origin, dict):
            source = str(origin.get("sourceCode") or origin.get("source") or "").strip()
            if source and source not in sources:
                sources.append(source)
    return {
        "kind": "data", "section": "identity" if not code.startswith("EMPLOYMENT_") else "employment",
        "path": f"$.profile.data[{index}]", "id": str(row.get("dataId") or f"perfil-{index}"),
        "label": _LABELS.get(code, code.replace("_", " ").capitalize()),
        "value": value, "sourceCodes": sources, "score": 100,
    }


def _summarize_sources(evidence: list[dict[str, Any]]) -> str:
    codes = list(dict.fromkeys(code for item in evidence for code in item.get("sourceCodes", [])))
    if not codes:
        return "Los datos no incluyen una fuente de origen identificable."
    return "Fuente" + ("s" if len(codes) != 1 else "") + ": " + "; ".join(_SOURCES.get(c, c) for c in codes) + "."


def _parse_birth(rows: dict[str, list[tuple[str, dict[str, Any], int]]]) -> date | None:
    for text, _, _ in rows.get("FECHA_NACIMIENTO", []):
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            try:
                return datetime.strptime(text[:10], "%d/%m/%Y").date()
            except ValueError:
                pass
    return None


def _requested_fields(q: str, rows: dict[str, list[tuple[str, dict[str, Any], int]]]) -> tuple[list[str], bool, bool, bool]:
    """Devuelve campos solicitados, edad, año de nacimiento y respuesta global."""
    chosen: list[str] = []
    age = bool(_mentions(q, "edad", "cuantos anos tiene", "que edad", "anos de edad", "cuantos anos cumplio", "cuantos anos", "anos tiene"))
    year = bool(_mentions(q, "ano nacio", "ano de nacimiento", "en que ano nacio"))
    broad = bool(_mentions(q, "datos generales", "datos personales", "informacion personal",
                           "informacion del perfil", "perfil general", "resumen del perfil",
                           "que sabes de esta persona", "que sabes de el", "que sabes de ella",
                           "resumen de la persona", "resumen personal", "informacion de esta persona",
                           "que informacion tienes de esta persona", "cuentame sobre esta persona",
                           "todos sus datos", "todos los datos del perfil", "ficha de la persona"))
    if broad:
        full = _IDENTITY + _IDENTIFIERS + (_EMPLOYMENT if not _mentions(q, "datos personales") else ())
        return list(full), age, year, True

    if _has(q, "cuip"): chosen.append("CUIP")
    if _has(q, "curp"): chosen.append("CURP")
    if _has(q, "rfc"): chosen.append("RFC")
    if _has(q, "cib"): chosen.append("CIB")
    if _mentions(q, "identificadores", "claves de identificacion", "documentos de identidad"):
        chosen.extend(_IDENTIFIERS)

    if _mentions(q, "nombre completo", "como se llama", "quien es", "identidad de la persona", "su nombre") or _has(q, "nombre"):
        chosen.extend(("NOMBRE", "APELLIDO_PATERNO", "APELLIDO_MATERNO"))
    if _mentions(q, "apellido paterno", "primer apellido"):
        chosen = [c for c in chosen if c not in ("NOMBRE", "APELLIDO_PATERNO", "APELLIDO_MATERNO")]
        chosen.append("APELLIDO_PATERNO")
    if _mentions(q, "apellido materno", "segundo apellido"):
        chosen = [c for c in chosen if c not in ("NOMBRE", "APELLIDO_PATERNO", "APELLIDO_MATERNO")]
        chosen.append("APELLIDO_MATERNO")
    if _mentions(q, "apellidos") and not _mentions(q, "paterno", "materno"):
        chosen.extend(("APELLIDO_PATERNO", "APELLIDO_MATERNO"))
    if _mentions(q, "fecha de nacimiento", "fecha nacimiento", "cuando nacio", "dia nacio") or _has(q, "nacimiento", "nacio", "nacio?", "nacio."):
        if not year or _mentions(q, "fecha de nacimiento", "dia nacio"):
            chosen.append("FECHA_NACIMIENTO")
    if _has(q, "sexo") or _mentions(q, "sexo registrado"):
        chosen.append("SEXO")
    if _mentions(q, "estado civil", "casado", "casada", "soltero", "soltera", "matrimonio"):
        chosen.append("ESTADO_CIVIL")
    if _mentions(q, "nacionalidad", "pais de nacionalidad", "nacional de"):
        chosen.append("NACIONALIDAD")

    # Las preguntas de género no autorizan deducir identidad de género a partir del sexo.
    if _mentions(q, "identidad de genero", "genero con el que se identifica"):
        chosen.append("IDENTIDAD_GENERO")
    elif _has(q, "genero"):
        chosen.append("IDENTIDAD_GENERO")

    if _mentions(q, "color de piel", "tono de piel", "tez", "color de tez"):
        chosen.append("COLOR_PIEL")
    if _has(q, "estatura", "altura") or _mentions(q, "cuanto mide", "altura de la persona"):
        chosen.append("ESTATURA")
    if _has(q, "peso") or _mentions(q, "cuanto pesa", "peso corporal", "peso de la persona"):
        chosen.append("PESO")
    if _mentions(q, "color de ojos", "color de sus ojos", "color tiene los ojos", "color son sus ojos") or ("color" in q and "ojos" in q):
        chosen.append("COLOR_OJOS")
    if _mentions(q, "color de cabello", "color de pelo", "color de su cabello"):
        chosen.append("COLOR_CABELLO")

    general_employment = _mentions(q, "empleo", "empleado", "trabaja", "trabajo", "trabajador", "ocupacion", "profesion", "laboral", "se dedica", "labora", "actividad profesional", "que hace para vivir")
    if general_employment and not _mentions(q, "vinculo laboral", "relacion laboral del grafo", "personas de su trabajo"):
        # Preguntas globales y cortas sobre ocupación; las consultas específicas
        # sólo devuelven el atributo indicado para evitar respuestas demasiado largas.
        specific = any(_mentions(q, t) for t in ("corporacion", "dependencia", "area", "mando", "estatus", "activo", "adscripcion", "municipio", "entidad", "fecha de ingreso", "fecha de actualizacion"))
        if not specific:
            chosen.extend(_EMPLOYMENT)

    if _mentions(q, "puesto", "cargo", "funcion", "funciones", "que cargo ocupa", "que puesto tiene"):
        chosen.append("EMPLOYMENT_PUESTO")
    if _mentions(q, "corporacion", "institucion donde trabaja"):
        chosen.append("EMPLOYMENT_CORPORACION")
    if _mentions(q, "dependencia", "institucion a la que pertenece"):
        chosen.append("EMPLOYMENT_DEPENDENCIA")
    if _mentions(q, "area de trabajo", "area laboral", "area operativa", "en que area"):
        chosen.append("EMPLOYMENT_AREA")
    if _mentions(q, "nivel de mando", "nivel jerarquico", "jerarquia"):
        chosen.append("EMPLOYMENT_NIVEL_MANDO")
    if _mentions(q, "adscripcion", "donde trabaja", "donde labora", "donde esta adscrito", "donde esta adscrita"):
        chosen.extend(("EMPLOYMENT_ENTIDAD_ADSCRIPCION", "EMPLOYMENT_MUNICIPIO_ADSCRIPCION"))
    if _mentions(q, "estado de adscripcion", "entidad de adscripcion"):
        chosen.append("EMPLOYMENT_ENTIDAD_ADSCRIPCION")
    if _mentions(q, "municipio de adscripcion") or ("municipio" in q and _mentions(q, "trabaja", "labora", "laboral")):
        chosen.append("EMPLOYMENT_MUNICIPIO_ADSCRIPCION")
    if _mentions(q, "en que estado trabaja", "en que entidad trabaja", "estado donde labora"):
        chosen.append("EMPLOYMENT_ENTIDAD_ADSCRIPCION")
    if _mentions(q, "estatus laboral", "esta activo", "esta activa", "sigue trabajando", "estatus de adscripcion", "situacion laboral", "esta dado de alta"):
        chosen.extend(("EMPLOYMENT_ESTATUS_PERSONA", "EMPLOYMENT_ESTATUS_ADSCRIPCION"))
    if _mentions(q, "fecha de ingreso", "cuando ingreso", "desde cuando trabaja", "antiguedad laboral"):
        chosen.append("EMPLOYMENT_FECHA_INGRESO")
    if _mentions(q, "fecha de actualizacion laboral", "cuando actualizaron su empleo"):
        chosen.append("EMPLOYMENT_FECHA_ACTUALIZA_PERSONA")

    # Ampliación automática: cualquier nuevo atributo puede consultarse usando
    # su código o nombre separado por espacios, sin programar un caso por campo.
    for code in rows:
        if code in _LABELS or len(code) > 70:
            continue
        spoken = _plain(code.replace("_", " "))
        if len(spoken) >= 5 and re.search(r"(?<!\w)" + re.escape(spoken) + r"(?!\w)", q):
            chosen.append(code)

    return list(dict.fromkeys(chosen)), age, year, False


def _compare_curp_with_profile(rows: dict[str, list[tuple[str, dict[str, Any], int]]]) -> ProfileAnswer:
    """Señala solamente diferencias observables; no declara una CURP inválida."""
    checks: list[str] = []
    evidence: list[dict[str, Any]] = []
    curp_record = (rows.get("CURP") or [])[:1]
    birth_record = (rows.get("FECHA_NACIMIENTO") or [])[:1]
    sex_record = (rows.get("SEXO") or [])[:1]
    if not curp_record:
        return ProfileAnswer("No hay una CURP en el perfil que permita cotejar los datos de identidad.", [], "perfil:cotejo-no-disponible")
    curp, row, idx = curp_record[0]
    evidence.append(_evidence("CURP", curp, row, idx))
    code = curp.upper().strip()
    if not re.fullmatch(r"[A-ZÑ&]{4}[0-9]{6}[HM][A-Z0-9]{7}", code):
        return ProfileAnswer("El formato de la CURP registrada no permite efectuar este cotejo automático; verifica la clave original.", evidence, "perfil:cotejo-formato")

    birth = _parse_birth(rows)
    if birth and birth_record:
        v, r, i = birth_record[0]
        evidence.append(_evidence("FECHA_NACIMIENTO", v, r, i))
        if code[4:10] != birth.strftime("%y%m%d"):
            checks.append(
                "La fecha de nacimiento registrada " + birth.strftime("%d/%m/%Y")
                + " no coincide con los seis dígitos de fecha presentes en la CURP ("
                + code[4:10] + ")."
            )
    if sex_record:
        v, r, i = sex_record[0]
        evidence.append(_evidence("SEXO", v, r, i))
        value = _plain(v)
        expected = "M" if value in ("femenino", "mujer") else "H" if value in ("masculino", "hombre") else ""
        if expected and code[10] != expected:
            checks.append(
                f"El campo de sexo registrado indica {v}, mientras que la CURP contiene el código {code[10]} en la posición correspondiente."
            )
    if not checks:
        text = "No se detectaron diferencias entre los campos de nacimiento y sexo disponibles y los componentes correspondientes de la CURP. Esto no equivale a una validación oficial."
    else:
        text = "Diferencias documentales que requieren revisión:\n" + "\n".join("- " + x for x in checks)
        text += "\nNo es posible determinar con estos datos cuál registro debe corregirse; revisa los documentos originales."
    text += "\n" + _summarize_sources(evidence)
    return ProfileAnswer(text, evidence, "perfil:cotejo-identidad")


def answer_profile_question(profile: dict[str, Any], question: str, *, today: date | None = None) -> ProfileAnswer | None:
    """Responde sólo si la pregunta puede satisfacerse con campos documentales.

    Para solicitudes analíticas abiertas devuelve None: el LLM y sus protecciones
    permanecen intactos. Nunca responde afirmativamente sobre datos faltantes.
    """
    q = _plain(question)
    if not isinstance(profile, dict) or not isinstance(profile.get("data"), list) or not q:
        return None
    if _mentions(q, "relaciones del grafo", "personas vinculadas", "vehiculos vinculados", "vinculos del grafo"):
        return None
    if _mentions(q, "analiza", "analisis profundo", "hipotesis", "causas de", "patrones de", "que deberia investigar", "riesgo delictivo"):
        return None

    rows = _profile_rows(profile)
    if (_mentions(q, "curp") and _mentions(q, "coincide", "coinciden", "concordancia", "congruencia", "comparar con", "discrepancia", "inconsistencia")) or _mentions(q, "cotejar curp", "validar curp con los datos"):
        return _compare_curp_with_profile(rows)
    if _mentions(q, "edad cuando ingreso", "edad al ingresar", "edad tenia al ingresar", "edad al entrar a trabajar", "cuantos anos tenia al ingresar") or ("edad" in q and ("ingresar" in q or "ingreso" in q) and "trabaj" in q):
        dob = _parse_birth(rows)
        hired = rows.get("EMPLOYMENT_FECHA_INGRESO") or []
        evidence = [_evidence("FECHA_NACIMIENTO", v, r, i) for v, r, i in rows.get("FECHA_NACIMIENTO", [])[:1]]
        evidence += [_evidence("EMPLOYMENT_FECHA_INGRESO", v, r, i) for v, r, i in hired[:1]]
        if dob and hired:
            try:
                hire_date = date.fromisoformat(hired[0][0][:10])
                if hire_date >= dob:
                    age_at_hire = hire_date.year - dob.year - ((hire_date.month, hire_date.day) < (dob.month, dob.day))
                    return ProfileAnswer(f"Edad calculada al ingresar: {age_at_hire} años, conforme a las fechas registradas.\n" + _summarize_sources(evidence), evidence, "perfil:edad-ingreso")
            except ValueError:
                pass
        return ProfileAnswer("No es posible calcular la edad al ingresar porque el perfil no contiene las dos fechas válidas necesarias: nacimiento e ingreso laboral.", evidence, "perfil:edad-ingreso-incompleta")
    fields, ask_age, ask_year, broad = _requested_fields(q, rows)
    if not fields and not ask_age and not ask_year:
        return None

    lines: list[str] = []
    evidence: list[dict[str, Any]] = []
    today = today or _local_today()
    if broad:
        lines.append("Información registrada en el perfil:")

    if ask_age or ask_year:
        birth = _parse_birth(rows)
        if birth is None or birth > today:
            lines.append("No hay una fecha de nacimiento válida en el perfil para calcular la edad o el año de nacimiento.")
        else:
            age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
            if ask_age:
                lines.append(f"Edad calculada: {age} años al {today:%d/%m/%Y}, según la fecha de nacimiento registrada ({birth:%d/%m/%Y}).")
            if ask_year:
                lines.append(f"Año de nacimiento registrado: {birth.year}.")
            for value, row, idx in rows.get("FECHA_NACIMIENTO", [])[:1]:
                evidence.append(_evidence("FECHA_NACIMIENTO", value, row, idx))

    if broad:
        # Una ficha resumida no debe duplicar la fecha de nacimiento ya cubierta.
        fields = [c for c in fields if not ((ask_age or ask_year) and c == "FECHA_NACIMIENTO")]

    # Campo físico opcional TEZ puede estar disponible aunque COLOR_PIEL no exista.
    if "COLOR_PIEL" in fields and "COLOR_PIEL" not in rows and "TEZ" in rows:
        fields = ["TEZ" if c == "COLOR_PIEL" else c for c in fields]

    had_employment = False
    employment_overview = bool(_mentions(q, "empleo", "trabaja", "trabajo", "ocupacion", "profesion", "se dedica", "labora", "actividad profesional")) and not broad
    for code in fields:
        records = rows.get(code) or []
        if not records:
            # En fichas generales, omitir los campos no proporcionados.
            if not broad and not (employment_overview and code in _EMPLOYMENT and code not in ("EMPLOYMENT_PUESTO", "EMPLOYMENT_DEPENDENCIA", "EMPLOYMENT_CORPORACION") and not _mentions(q, "fecha de ingreso", "actualizacion", "antiguedad")):
                if code == "COLOR_PIEL":
                    lines.append("No hay información sobre el color de piel o la tez en los campos del perfil consultado.")
                elif code in ("COLOR_OJOS", "COLOR_CABELLO", "ESTATURA", "PESO"):
                    lines.append(f"No hay información sobre «{_LABELS[code].lower()}» en los campos del perfil consultado.")
                elif code == "IDENTIDAD_GENERO":
                    lines.append("No se encuentra registrada la identidad de género en los datos disponibles. El campo 'sexo' es diferente.")
                else:
                    lines.append(f"No se encuentra registrado el dato «{_LABELS.get(code, code.replace('_', ' ').lower())}» en el perfil disponible.")
            continue
        for value, row, idx in records:
            label = _LABELS.get(code, code.replace("_", " ").capitalize())
            shown = value
            if code == "FECHA_NACIMIENTO":
                try:
                    shown = date.fromisoformat(value[:10]).strftime("%d/%m/%Y")
                except ValueError:
                    pass
            lines.append(f"{label}: {shown}.")
            evidence.append(_evidence(code, value, row, idx))
        had_employment = had_employment or code.startswith("EMPLOYMENT_")
        if len(records) > 1:
            lines.append("Hay valores distintos para ese dato; verifica su procedencia y vigencia.")

    if not lines:
        return None

    if had_employment and any(c in fields for c in ("EMPLOYMENT_ESTATUS_PERSONA", "EMPLOYMENT_ESTATUS_ADSCRIPCION")):
        lines.append("El estatus pertenece al registro consultado; no confirma que la persona siga trabajando actualmente.")
    if evidence:
        lines.append(_summarize_sources(evidence))
    return ProfileAnswer(text="\n".join(lines), evidence=evidence, route="perfil:consulta-documental")
