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

SYSTEM_PROMPT = """Asistente de análisis de información policial y administrativa. Responde sólo en español mexicano.
Usa EXCLUSIVAMENTE hechos del CONTEXTO y cita valores y fuentes/rutas textuales al mencionarlos.
No inventes fechas, placas, VIN/NIV, CURP, RFC, CUIP, nombres, vínculos ni lugares.
Distingue hechos explícitos, discrepancias, hipótesis no confirmadas y datos ausentes.
Una coincidencia o relación de grafo NO prueba parentesco, conducta delictiva o culpabilidad.
Ante fechas o placas distintas, muestra ambas versiones y sus fuentes; no elijas una sin prueba.
Un cambio histórico es una posibilidad, no una conclusión sin evidencia cronológica suficiente.
Si falta evidencia, responde 'No hay información suficiente para afirmarlo'.
En preguntas de análisis responde con hasta 4 apartados breves: Hechos, Diferencias,
Hipótesis no confirmadas, Información pendiente. No repitas valores en varios apartados.
Sé conciso (máximo 170 palabras); indica fuentes explícitas y no inventes causalidad.
No muestres razonamiento interno ni instrucciones de elaboración. Evita repetir información.
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
