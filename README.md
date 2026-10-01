# SPM IA Agent API — 4.0.0 hybrid-router

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
  `-- análisis/patrones/hipótesis/etc. --> Context Builder --> Qwen3:4b
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

Preguntas como `analiza patrones`, `propón hipótesis`, `compara inconsistencias`, `qué relación es más relevante` o el modo `deep` siguen usando Qwen3:4b.

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
spm-profile-intelligence-api:4.0.0-hybrid-router
```

## Pruebas

```text
25 passed
```

Incluyen clasificación de contexto, streaming, filtros de razonamiento, presupuestos de runtime y rutas rápidas de CURP, domicilios, vehículos, fuentes, resumen y preguntas analíticas que deben caer al LLM.

## Sobre entrenar un modelo pequeño

Esta versión ataca primero el problema que más tiempo desperdicia: usar un LLM para recuperar datos que ya están estructurados. Un modelo pequeño especializado puede añadirse después para redacción más libre de resúmenes, pero no es necesario para obtener respuesta inmediata en consultas factuales. Mantener esta separación también reduce el riesgo de que un modelo invente un CURP, RFC, conteo o domicilio que la API puede leer exactamente.
