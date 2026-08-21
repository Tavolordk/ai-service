# Dependencias directas, runtime y modelo

La solución se mantiene con componentes de licencia permisiva. No incluye SDKs de IA cloud.

| Componente | Uso | Versión/configuración | Licencia |
|---|---|---|---|
| FastAPI | API HTTP | 0.128.2 | MIT |
| Uvicorn | servidor ASGI | 0.48.0 | BSD-3-Clause |
| Pydantic | validación de payloads | 2.13.4 | MIT |
| NetworkX | análisis determinista de grafos | 3.6.1 | BSD-3-Clause |
| Ollama | runtime de inferencia local | imagen 0.32.9 | MIT |
| Qwen3 4B | modelo conversacional local | `qwen3:4b` / Q4_K_M | Apache-2.0 |

El cliente que conecta FastAPI con Ollama usa `http.client` de la biblioteca estándar de Python, por lo que no se agregó `requests`, `httpx`, SDK de Ollama ni SDK de un proveedor cloud.

## Nota de distribución

Los pesos de `qwen3:4b` **no se incluyen dentro de este ZIP**. El script `setup-local-model.*` los descarga al volumen Docker local del usuario. Esto evita redistribuir un archivo de varios GB dentro del proyecto.

Antes de una liberación formal se recomienda generar y archivar:

1. SBOM de la imagen final;
2. inventario de dependencias transitivas;
3. copia de las licencias correspondientes a la versión exacta desplegada;
4. digest de la imagen de Ollama y del modelo usado en producción.
