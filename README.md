# SPM IA Agent API — 4.1.2-precise-fast

Versión optimizada para equipos con pocos recursos. El cambio principal es que las preguntas simples ya **no llaman a Qwen**. La API primero intenta resolverlas de forma determinista usando el perfil estructurado y el grafo que ya envía el frontend; sólo las preguntas analíticas pasan al LLM.

## Flujo nuevo

```text
Pregunta
  |
  v
SPM Fast Router
  |-- CURP/RFC/CUIP/CIB -----------------> respuesta inmediata
  |-- nombre/fecha nacimiento/sexo ------> respuesta inmediata
  |-- domicilios ------------------------> respuesta inmediata
  |-- vehículos/armas/personas ----------> respuesta inmediata
  |-- conteos de nodos/vínculos ---------> respuesta inmediata
  |-- fuentes/orígenes ------------------> respuesta inmediata
  |-- resumen del perfil ----------------> resumen determinista rápido
  |
  `-- análisis/patrones/hipótesis/etc. --> Context Builder --> FAST 1.7b / DEEP 4b
```

El frontend actual no necesita cambios: las respuestas rápidas usan el modo ya soportado `deterministic-fallback` y se presentan como **Modo exacto local**.

## Preguntas que evitan Qwen

Ejemplos:

- `dame un resumen del perfil`
- `cuál es su CURP`
- `cuál es su RFC`
- `cuál es su nombre`
- `cuál es su fecha de nacimiento`
- `dame sus domicilios`
- `cuántos domicilios tiene`
- `qué vehículos tiene`
- `cuántos vehículos tiene relacionados`
- `qué armas aparecen`
- `cuántos vínculos hay`
- `cuántos elementos relacionados hay`
- `qué fuentes tiene`

Preguntas analíticas normales usan el modelo FAST (1.7b); `mode=deep` usa el modelo DEEP (4b).

Aunque el usuario tenga activado **Razonar**, una pregunta factual que el router puede responder exactamente no invoca al modelo ni muestra estado de razonamiento. Esto evita consumir CPU sin aportar valor.

## Rendimiento del router

Prueba sintética local con un perfil de ~1.27 MB y 9,000 registros auxiliares:

```text
dame un resumen del perfil            ~136 ms
cual es su curp                       ~0.3 ms
cuantos vehiculos tiene relacionados  ~0.2 ms
```

Estos números miden sólo el procesamiento de la API en el entorno de prueba; el tiempo real dependerá del equipo, tamaño del JSON y red. Lo importante es que esas rutas no esperan inferencia de Ollama.

## Timeout de respaldo

Se mantienen timeouts amplios para las preguntas que sí llegan al LLM:

```env
AI_LLM_TIMEOUT_SECONDS=1200
AI_LLM_CONNECT_TIMEOUT_SECONDS=30
AI_LLM_FIRST_TOKEN_TIMEOUT_SECONDS=1200
AI_LLM_STREAM_IDLE_TIMEOUT_SECONDS=600
AI_LLM_FINAL_TIMEOUT_SECONDS=1800
```

Por tanto, la optimización no depende de reducir el timeout: primero se evita Qwen cuando es innecesario y, cuando sí se necesita, queda margen para CPU lenta.

## Variables nuevas

```env
AI_FAST_PATH_ENABLED=true
AI_FAST_SUMMARY_ENABLED=true
AI_FAST_MAX_LIST_ITEMS=12
```

- `AI_FAST_PATH_ENABLED=false`: desactiva completamente el router y fuerza el comportamiento LLM anterior.
- `AI_FAST_SUMMARY_ENABLED=false`: los datos puntuales siguen siendo rápidos, pero `resumen del perfil` vuelve a Qwen.
- `AI_FAST_MAX_LIST_ITEMS`: máximo de elementos que lista una respuesta rápida.

## Context Builder y caché

Para preguntas analíticas se conserva la optimización 3.2.0:

- no se envía el JSON gigante en bruto;
- se clasifica la intención;
- se seleccionan sólo secciones relevantes;
- se deduplican datos repetidos;
- el snapshot del perfil queda en caché;
- el presupuesto de contexto/tokens se ajusta según la pregunta.

## Health

```powershell
curl.exe http://localhost:3651/health
```

Ahora incluye estadísticas del router:

```json
{
  "contextOptimization": true,
  "contextCache": {
    "entries": 1,
    "hits": 3,
    "misses": 1
  },
  "fastPath": {
    "hits": 12,
    "misses": 4,
    "llmRequestsAvoided": 12,
    "routes": {
      "summary:deterministic": 2,
      "identifier:curp": 4,
      "graph:vehicle": 6
    }
  }
}
```

`llmRequestsAvoided` permite comprobar cuántas consultas no gastaron inferencia.

## Levantar local

```powershell
docker compose -f .\docker-compose.local.yml --env-file .\.env down
docker compose -f .\docker-compose.local.yml --env-file .\.env build --no-cache profile-intelligence-api
docker compose -f .\docker-compose.local.yml --env-file .\.env up -d --force-recreate
```

Verifica:

```powershell
docker ps
curl.exe http://localhost:3651/health
```

La imagen esperada es:

```text
spm-profile-intelligence-api:4.0.2-spanish-reasoning
```

## Pruebas (referencia histórica 4.0.2)

La versión anterior documentaba 25 tests; ejecutar de nuevo la suite sobre esta rama antes de publicar.

Incluyen clasificación de contexto, streaming, filtros de razonamiento, presupuestos de runtime y rutas rápidas de CURP, domicilios, vehículos, fuentes, resumen y preguntas analíticas que deben caer al LLM.

## Sobre entrenar un modelo pequeño

Esta versión ataca primero el problema que más tiempo desperdicia: usar un LLM para recuperar datos que ya están estructurados. Un modelo pequeño especializado puede añadirse después para redacción más libre de resúmenes, pero no es necesario para obtener respuesta inmediata en consultas factuales. Mantener esta separación también reduce el riesgo de que un modelo invente un CURP, RFC, conteo o domicilio que la API puede leer exactamente.

El forzado de idioma en el razonamiento privado está desactivado por defecto (`AI_FORCE_SPANISH_THINKING=false`). La respuesta final visible continúa validándose en español antes de entregarse al frontend.


## Modo CPU (4.1.0)

No requiere GPU. El Fast Router, Context Builder, SSE, caché y contrato con frontend se conservan.
El modo `auto`/`quick` analítico usa FAST; `deep` usa DEEP. Si `AI_LLM_MODEL_FAST` o
`AI_LLM_MODEL_DEEP` quedan vacíos, se reutiliza `AI_LLM_MODEL` (compatibilidad).

```env
AI_APP_VERSION=4.1.2-precise-fast
AI_LLM_MODEL=qwen3:4b
AI_LLM_MODEL_FAST=qwen3:1.7b
AI_LLM_MODEL_DEEP=qwen3:4b
AI_LLM_THINKING_DEFAULT=false
AI_LLM_THINKING_MAX_PREDICT=1200
AI_FORCE_SPANISH_THINKING=false
AI_LLM_NUM_CTX=4096
AI_LLM_MAX_PREDICT=500
AI_CONTEXT_MAX_CHARS=6000
AI_CONTEXT_DEEP_MAX_CHARS=12000
AI_LLM_MAX_EVIDENCE=30
AI_LLM_HISTORY_MESSAGES=2
AI_LLM_NUM_THREAD=
AI_LLM_WARMUP=true
```

`AI_LLM_NUM_THREAD` vacío permite que Ollama decida. Si lo defines, usa el número de
**núcleos FÍSICOS** accesibles al contenedor (no hilos lógicos). Con WSL2 valida
`docker info` y los límites de CPU/RAM de Docker Desktop. `num_predict=1200` se aplica
*solo* al modo `thinking=true` explícito; sin thinking, máximo 500.
El prompt se ajusta mediante una estimación conservadora de tokens (aprox. 2 bytes UTF-8/token),
conservando líneas documentales completas y priorizando discrepancias, fechas, placas, VIN y fuentes.
No se utiliza un tokenizador exacto: el consumo real lo determina Ollama.

En `docker-compose.local.yml` Ollama usa 1 solicitud paralela, máximo 2 modelos
cargados, flash attention y caché KV q8_0. Confirma que tu versión de Ollama
soporta estas opciones; la memoria suficiente para ambos modelos depende del equipo.

Los scripts `setup-local-model.ps1`, `.sh` o `.cmd` instalan ambos pesos sin borrar
`spm-ollama-models`. Asegúrate de transferir estos valores a tu `.env` existente;
`.env.example` y los defaults de código **no sobrescriben** entradas existentes en `.env`.

```powershell
.\setup-local-model.ps1
docker compose -f .\docker-compose.local.yml --env-file .\.env build --no-cache profile-intelligence-api
docker compose -f .\docker-compose.local.yml --env-file .\.env up -d --force-recreate
curl.exe http://localhost:3651/health
docker exec -it spm-ollama-local ollama run qwen3:4b --verbose
```

`/health.llmStats` da `calls` y `byModel[modelo]`, con promedios de
`prompt_eval_count`, `prompt_eval_duration`, `eval_count`, `eval_duration`,
`load_duration`, `promptTokensPerSecond` y `evalTokensPerSecond`. Las duraciones
son nanosegundos como las devuelve Ollama. Warm-up no cuenta en promedios.
Compara la mediana y promedio de duración por pregunta equivalente antes/después;
una caída en `eval_count`, `prompt_eval_count` y `load_duration` normalmente reduce
latencia; `tokens/s` debe mantenerse o mejorar. Los contadores se reinician con la API.

Para pruebas locales:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q tests/test_cpu_optimized.py tests/test_ollama_runtime_payload.py tests/test_reasoning_output_guard.py tests/test_context_builder.py tests/test_runtime_policy.py
```


## 4.1.1 — Evidencia antes de inferencia (corrección de precisión)

El problema detectado en la prueba de discrepancias no era sólo del modelo:
la versión 4.1.0 eliminaba hechos completos mediante un corte por bytes al final
cuando se agotaba `num_ctx=4096`. Ahora:

- `app/evidence_context.py`: recupera registros completos de nacimiento, vehículos, VIN,
  placas, discrepancias explícitas y **su origen** desde el perfil original. Es un
  bloque de prioridad documental; no reemplaza el Context Builder existente.
- `app/token_budget.py`: estima el presupuesto de tokens, retira historial obsoleto
  antes que evidencia y selecciona **líneas completas**; preserva `/no_think` y `/think`.
  Si ni sistema ni pregunta caben, falla de forma explícita: nunca corta campos.
- `app/police_domain.py` y `app/analyzer.py`: prompt compacto sin instrucciones
  redundantes, para reservar más espacio a evidencia documental.
- `app/evidence_guard.py`: detecta años, fechas y placas no presentes en la evidencia
  que **se envió realmente al modelo**. En cotejos con `inconsistenciasDocumentales`
  obliga a mencionar los dos valores y sus fuentes.
- `app/ollama_client.py`: por defecto **no realiza regeneraciones**. Si la salida es
  inglesa, inventa identificadores, no atribuye fuentes o se trunca sin una oración
  completa, entrega líneas de evidencia verificadas de forma determinista. Mantiene
  el mismo contrato HTTP/SSE y los modelos FAST/DEEP; no afirma automáticamente
  que cualquier respuesta del LLM sea correcta.
- `OLLAMA_NO_CLOUD=1` en Docker Compose local (sin modificar volúmenes ni datos).

**Alcance:** el verificador estático protege los formatos de fechas y placas y
comprueba pares documentales explícitos. No verifica con certeza absoluta
nombres, domicilios, causalidad ni todas las afirmaciones en lenguaje natural.
Los hallazgos sensibles requieren siempre revisión por una persona analista.

Reproducción con el perfil ficticio incluido en `tests/fixtures`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\PROBAR_IA_SPM_COMPLETA.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\PROBAR_IA_SPM_COMPLETA.ps1 -Deep
```

El mismo escenario conserva ambas fechas `1990-05-16` y `1990-05-18`, el VIN
`VIN-FICTICIO-001`, las placas `ABC123A` y `ABC128A` y sus fuentes después del
recorte a `num_ctx=4096` con reserva de 500 tokens de salida.

Pruebas locales: **44 passed, 2 skipped** (2 necesitan `ijson`, que sí se instala
con `requirements.txt` en Docker). No se pudo ejecutar inferencia real de Ollama
en este entorno; repetir la prueba funcional en Docker Desktop antes de promover.


## 4.1.2 — Cotejo documental rápido y análisis focalizado

Para consultas de comparación documental sencilla en modos `auto` y `quick` con `thinking=false`, `SPM Document Comparator` compara atributos repetidos y placas con el mismo VIN explícito sin invocar Qwen. Es un cotejo de valores, **no una determinación de cuál es verdadero o vigente**. Para `deep`, hipótesis o interpretación causal, Qwen sigue disponible pero recibe evidencia compacta (valores, fuentes, domicilios y vínculos relevantes), sin duplicar todo el Context Builder.

Si la salida de Qwen falla los controles documentales, se emite un resumen estructurado basado únicamente en evidencia recibida. Se preservan `/api/v1/intelligence/chat`, `/stream`, SSE, JSON de respuestas y Fast Router original.

La velocidad real en CPU depende del equipo, tamaño de contexto y disponibilidad del modelo; las pruebas unitarias no representan una medición real de Ollama. Consulte `CAMBIOS_Y_DESPLIEGUE_4.1.2.txt` para despliegue local.
