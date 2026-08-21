# Profile Intelligence API 2.1 — chat preciso

Microservicio local para analizar perfiles/grafos y conversar sobre ellos con un modelo generativo **local**, sin cambiar ni reenviar los requests/responses de las APIs de negocio existentes.

## Qué cambia en 2.1

El endpoint determinista sigue existiendo, pero el chat del frontend ahora usa:

```text
POST /api/v1/intelligence/chat/stream
```

La respuesta llega por streaming, por lo que el texto aparece progresivamente como en un chatbot. La conversación mantiene hasta 8 mensajes recientes en memoria del navegador y cada pregunta vuelve a construir el contexto a partir del perfil, sus orígenes y el grafo visible.


### Selección de contexto por intención

Antes de llamar a Qwen se crea un `QueryPlan`. El objetivo es que el modelo no reciba datos que no necesita para una pregunta puntual.

Ejemplos:

- `Dame la primera dirección` -> una sola evidencia de dirección.
- `¿Y la segunda?` -> usa el turno anterior sólo para resolver que sigue hablando de direcciones y recupera únicamente la segunda.
- `¿Cuál es su fecha de nacimiento?` -> sólo el campo equivalente, aun si el código recibido es `FECHANACIMIENTO`.
- `¿Qué reporta SRC1?` -> sólo campos/direcciones cuyo `origin.sourceCode` sea `SRC1`.
- `¿Qué relación tiene el nodo seleccionado?` -> nodo seleccionado + aristas directas + nodos conectados directamente.

El historial sirve para resolver referencias conversacionales, no se concatena indiscriminadamente a la búsqueda de evidencia.

## Modelo local

La configuración incluida usa:

- Ollama 0.32.9 como runtime local.
- Qwen3 4B (`qwen3:4b`) como modelo conversacional.
- `think=false` para obtener una respuesta directa en vez de mostrar razonamiento interno.

El modelo de Ollama ocupa aproximadamente 2.5 GB. Puede trabajar por CPU; con GPU compatible la generación será más rápida.

## Instalación inicial del modelo en Windows

Desde `ai-service` ejecuta **una sola vez**:

```powershell
.\setup-local-model.cmd
```

O con PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup-local-model.ps1
```

El script hace lo siguiente:

1. detiene la versión previa sin borrar el volumen de modelos;
2. inicia únicamente Ollama;
3. le habilita salida de red temporalmente;
4. descarga `qwen3:4b`;
5. vuelve a quitar esa salida de red;
6. levanta Ollama + la API de inteligencia.

La descarga inicial necesita Internet **sólo para obtener los pesos del modelo**. En ese momento la API que recibe perfiles todavía no está levantada y no se envían datos del sistema.

Después de esa descarga, el arranque normal es:

```powershell
docker compose up -d --build
```

## Comprobar

```powershell
curl.exe http://127.0.0.1:8080/health
```

Respuesta esperada:

```json
{"status":"ok","mode":"local-private-chat","model":"qwen3:4b"}
```

Comprobar específicamente el modelo:

```powershell
curl.exe http://127.0.0.1:8080/health/llm
```

Debe indicar:

```json
{
  "status": "ok",
  "runtimeReachable": true,
  "modelAvailable": true,
  "model": "qwen3:4b"
}
```

Si `modelAvailable` aparece en `false`, ejecuta nuevamente el script de instalación del modelo.

## Privacidad por diseño

- No se usa OpenAI, Gemini, Claude, Hugging Face Inference API, AWS Bedrock, Azure AI ni otro servicio de IA remoto.
- Ollama **no publica el puerto 11434 hacia Windows ni hacia la LAN**; sólo la API de inteligencia puede verlo en una red Docker interna.
- La API de inteligencia sólo se publica en `127.0.0.1:8080`.
- El cliente LLM valida `AI_LLM_BASE_URL` contra una allowlist. En Compose sólo se permite el hostname interno `ollama`.
- No hay SDK cloud, telemetría ni clientes HTTP de terceros. La conexión local a Ollama usa `http.client` de la biblioteca estándar de Python.
- Uvicorn arranca con `--no-access-log` y la aplicación no registra prompts ni payloads.
- La API rechaza `Authorization` y `Cookie`; el JWT del sistema principal no debe llegar a IA.
- CORS está limitado a los puertos locales configurados y `allow_credentials=false`.
- Las respuestas llevan `Cache-Control: no-store`.
- Swagger/OpenAPI están apagados por defecto.
- La conversación no se persiste en base de datos, archivo, localStorage ni volumen. El frontend conserva el historial únicamente en memoria mientras la pantalla está abierta.

## Grounding / exactitud

El modelo **no recibe libertad para consultar Internet ni completar el perfil con conocimiento general**. Antes de cada pregunta la API construye un contexto verificable con:

- datos del perfil y sus códigos de fuente;
- direcciones y fuentes que las reportan;
- registros de origen;
- nodos que Angular ya está mostrando;
- aristas visibles del grafo;
- nodo seleccionado;
- hasta 8 mensajes previos de la conversación.

Se selecciona la evidencia más relevante a la pregunta y se le asignan marcadores `[E1]`, `[E2]`, etc. El prompt obliga al modelo a citar esos marcadores en afirmaciones factuales. El frontend permite desplegar la evidencia detrás de cada respuesta.

Si la respuesta no está en los datos, la instrucción es contestar que **no aparece en las fuentes proporcionadas** en lugar de inventarla.

La entrada de datos también se trata como contenido no confiable: cualquier texto que parezca una instrucción dentro de un nombre, domicilio, fuente o nodo se considera dato y no prompt.

## Fallback

Si Ollama no está arriba o el modelo no está instalado:

```text
chat -> analizador determinista local
```

El perfil y el grafo continúan funcionando y no se llama a ningún proveedor externo.

## Endpoints independientes del gateway

```text
GET  /health
GET  /health/llm
POST /api/v1/intelligence/profile/analyze
POST /api/v1/intelligence/profile/answer      # compatibilidad/fallback determinista
POST /api/v1/intelligence/chat                # respuesta completa
POST /api/v1/intelligence/chat/stream         # chatbot incremental (frontend)
POST /api/v1/intelligence/graph/analyze
```

Ninguno se integra al gateway actual.

## Variables principales

- `AI_ALLOWED_ORIGINS`
- `AI_ALLOWED_HOSTS`
- `AI_ENABLE_DOCS=false`
- `AI_MAX_CONTENT_LENGTH=5242880`
- `AI_LLM_ENABLED=true`
- `AI_LLM_BASE_URL=http://ollama:11434`
- `AI_LLM_ALLOWED_HOSTS=ollama`
- `AI_LLM_MODEL=qwen3:4b`
- `AI_LLM_TEMPERATURE=0.15`
- `AI_LLM_MAX_PREDICT=900`
- `AI_LLM_MAX_EVIDENCE=72`
- `AI_LLM_HISTORY_MESSAGES=8`
