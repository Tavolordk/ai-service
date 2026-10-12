$ErrorActionPreference = "Stop"
$Compose = ".\docker-compose.local.yml"
$EnvFile = ".\.env"

if (-not (Test-Path $EnvFile)) { Copy-Item ".\.env.example" $EnvFile }

docker compose -f $Compose --env-file $EnvFile up -d ollama
docker compose -f $Compose --env-file $EnvFile build --no-cache profile-intelligence-api
docker compose -f $Compose --env-file $EnvFile up -d --force-recreate profile-intelligence-api

Write-Host "`n=== CONTENEDORES ===" -ForegroundColor Cyan
docker compose -f $Compose --env-file $EnvFile ps

Write-Host "`n=== MODELO ===" -ForegroundColor Cyan
docker compose -f $Compose --env-file $EnvFile exec -T ollama ollama list

Write-Host "`n=== HEALTH ===" -ForegroundColor Cyan
curl.exe http://localhost:3651/health

Write-Host "`n`nPara ver rendimiento en vivo:" -ForegroundColor Green
Write-Host "docker logs -f profile-intelligence-api"
Write-Host "Busca líneas que empiecen con [IA PERF]."
