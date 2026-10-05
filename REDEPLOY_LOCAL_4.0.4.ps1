$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

& .\ACTUALIZAR_ENV_LOCAL.ps1
if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) {
    throw "No se pudo actualizar .env"
}

$compose = ".\docker-compose.local.yml"
$envFile = ".\.env"

Write-Host "Construyendo API 4.0.4 sin cache..." -ForegroundColor Cyan
docker compose -f $compose --env-file $envFile build --no-cache profile-intelligence-api
if ($LASTEXITCODE -ne 0) { throw "Falló docker compose build" }

Write-Host "Recreando sólo profile-intelligence-api..." -ForegroundColor Cyan
docker compose -f $compose --env-file $envFile up -d --no-deps --force-recreate profile-intelligence-api
if ($LASTEXITCODE -ne 0) { throw "Falló docker compose up" }

Write-Host "Estado:" -ForegroundColor Cyan
docker compose -f $compose --env-file $envFile ps

Write-Host "`nVerificando versión..." -ForegroundColor Cyan
$response = Invoke-RestMethod http://localhost:3651/
$response | ConvertTo-Json -Depth 5

if ($response.version -ne "4.0.4-reasoning-recovery") {
    Write-Warning "La API respondió versión '$($response.version)'. Revisa que .env y la imagen correspondan a 4.0.4."
} else {
    Write-Host "OK: API 4.0.4-reasoning-recovery desplegada." -ForegroundColor Green
}

Write-Host "`nLogs en vivo:" -ForegroundColor Yellow
Write-Host "docker compose -f .\docker-compose.local.yml --env-file .\.env logs -f --tail=200 profile-intelligence-api"
