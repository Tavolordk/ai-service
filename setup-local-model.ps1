$ErrorActionPreference = 'Stop'

Write-Host 'Recreando servicios de IA sin borrar el volumen de modelos...' -ForegroundColor Cyan
docker compose down

Write-Host 'Iniciando Ollama local...' -ForegroundColor Cyan
docker compose up -d ollama

Write-Host 'Habilitando salida temporal SOLO para descargar el modelo...' -ForegroundColor Yellow
$alreadyConnected = docker inspect profile-intelligence-ollama --format '{{json .NetworkSettings.Networks}}' | Select-String '"bridge"'
if (-not $alreadyConnected) {
  docker network connect bridge profile-intelligence-ollama
}

try {
  Write-Host 'Descargando Qwen3 4B (Apache-2.0). Esto se hace una sola vez y NO envía perfiles.' -ForegroundColor Cyan
  docker exec profile-intelligence-ollama ollama pull qwen3:4b
}
finally {
  Write-Host 'Quitando de nuevo la salida temporal de Ollama...' -ForegroundColor Yellow
  docker network disconnect bridge profile-intelligence-ollama 2>$null
}

Write-Host 'Modelo instalado. Levantando la API privada...' -ForegroundColor Green
docker compose up -d --build --force-recreate

Write-Host 'Listo. Prueba:' -ForegroundColor Green
Write-Host '  curl.exe http://127.0.0.1:8080/health'
Write-Host '  curl.exe http://127.0.0.1:8080/health/llm'
