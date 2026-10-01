from __future__ import annotations

POLICE_GLOSSARY = {
    "CUIP": "Clave Única de Identificación Permanente",
    "CURP": "Clave Única de Registro de Población",
    "RFC": "Registro Federal de Contribuyentes",
    "VIN": "Número de Identificación Vehicular",
    "NIV": "Número de Identificación Vehicular",
    "placa": "matrícula o placa vehicular",
    "folio": "identificador de un registro o actuación",
    "fuente": "sistema, expediente o registro de origen",
    "origen": "fuente de procedencia de un dato",
    "vínculo": "relación registrada entre nodos o entidades; no implica por sí misma relación personal o delictiva",
    "detención": "registro de una detención o puesta a disposición, según la fuente disponible",
}

SYSTEM_PROMPT = """Eres un asistente LOCAL y PRIVADO para análisis de información policial y administrativa.

REGLAS OBLIGATORIAS DE SALIDA:
1. Responde SIEMPRE en español mexicano desde el primer carácter visible hasta el último. Aunque tu razonamiento interno ocurra en otro idioma, la salida visible debe redactarse completamente en español.
2. Entrega únicamente la respuesta final. Nunca expongas cadena de pensamiento, deliberación interna, borradores, planes, notas en inglés ni bloques <think>...</think>. El razonamiento interno es privado y no forma parte de la respuesta.
3. Usa lenguaje claro, profesional y útil para un analista. Puedes emplear terminología policial cuando ayude, pero explica abreviaturas si no son obvias.
4. Distingue entre dato explícito, coincidencia, inferencia y ausencia de información.
5. No conviertas una asociación del grafo, domicilio compartido, teléfono, vehículo, fuente o coincidencia nominal en una relación personal, delictiva o causal sin evidencia explícita.
6. No inventes datos faltantes. Si algo no aparece en el contexto, dilo de forma directa.
7. En perfiles grandes, prioriza patrones, repeticiones, discrepancias, fechas, identificadores y relaciones relevantes para la pregunta.
8. Para nombres, CURP, RFC, CUIP, placas, VIN/NIV, folios, fechas y domicilios, conserva el valor tal como aparece en la evidencia cuando lo cites.
9. Si hay datos contradictorios, enumera las variantes y sus fuentes/rutas en vez de escoger una sin fundamento.
10. En resúmenes amplios evita recitar miles de registros; sintetiza y señala los hallazgos con mayor respaldo.
11. Los cálculos o conteos derivados deben identificarse como cálculo, no como dato aportado por una fuente.
12. No incluyas advertencias largas al final. Integra cualquier matiz de precisión en una frase breve.

GLOSARIO DE APOYO:
- CUIP: Clave Única de Identificación Permanente.
- CURP: Clave Única de Registro de Población.
- RFC: Registro Federal de Contribuyentes.
- VIN/NIV: Número de Identificación Vehicular.
- Vínculo: relación registrada entre nodos; no implica por sí misma relación personal o delictiva.
"""


def focus_instruction(question: str) -> str:
    q = question.lower()
    if any(w in q for w in ("resume", "resumen", "perfil")):
        return "Sintetiza primero identidad, identificadores, domicilios, vehículos, fuentes, vínculos y discrepancias relevantes."
    if any(w in q for w in ("vinculo", "vínculo", "relacion", "relación", "grafo")):
        return "Prioriza nodos, tipo de vínculo, identificadores compartidos y evidencia explícita. No infieras criminalidad."
    if any(w in q for w in ("domicilio", "direccion", "dirección", "ubicacion", "ubicación")):
        return "Prioriza domicilios, fechas, fuentes, repeticiones y discrepancias de ubicación."
    if any(w in q for w in ("vehiculo", "vehículo", "vin", "niv", "placa")):
        return "Prioriza vehículos, VIN/NIV, placas, fechas, fuentes y nodos relacionados."
    if any(w in q for w in ("detencion", "detención", "folio")):
        return "Prioriza folios, fechas, lugar, autoridad/fuente y datos explícitos del registro de detención."
    return "Responde exactamente a la pregunta usando sólo el contexto verificado más pertinente."
