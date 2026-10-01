$ComposeFile = ".\docker-compose.local.yml"
$EnvFile = ".\.env"

Write-Host "=== Docker ===" -ForegroundColor Cyan
docker version --format "Client={{.Client.Version}} Server={{.Server.Version}}"
Write-Host ""
Write-Host "=== Compose usado ===" -ForegroundColor Cyan
Write-Host $ComposeFile
& docker compose -f $ComposeFile --env-file $EnvFile config --services
Write-Host ""
Write-Host "=== Contenedores ===" -ForegroundColor Cyan
& docker compose -f $ComposeFile --env-file $EnvFile ps -a
Write-Host ""
Write-Host "=== Modelos Ollama ===" -ForegroundColor Cyan
& docker compose -f $ComposeFile --env-file $EnvFile exec -T ollama ollama list
Write-Host ""
Write-Host "=== Logs API (ultimas 80 lineas) ===" -ForegroundColor Cyan
& docker compose -f $ComposeFile --env-file $EnvFile logs --tail 80 profile-intelligence-api
Write-Host ""
Write-Host "=== Logs Ollama (ultimas 80 lineas) ===" -ForegroundColor Cyan
& docker compose -f $ComposeFile --env-file $EnvFile logs --tail 80 ollama
