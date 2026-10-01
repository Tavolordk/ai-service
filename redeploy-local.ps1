$ErrorActionPreference = "Stop"

$ComposeFile = ".\docker-compose.local.yml"
$EnvFile = ".\.env"

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & docker compose -f $ComposeFile --env-file $EnvFile @Args
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose falló (exit=$LASTEXITCODE): $($Args -join ' ')"
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker no está disponible en PATH. Abre Docker Desktop y vuelve a ejecutar."
}
if (-not (Test-Path $ComposeFile)) {
    throw "No existe $ComposeFile. Ejecuta este script desde la raíz del proyecto."
}
if (-not (Test-Path $EnvFile)) {
    Copy-Item ".\.env.example" $EnvFile
    Write-Host "Se creó .env desde .env.example"
}

Write-Host "Usando EXCLUSIVAMENTE: $ComposeFile" -ForegroundColor Cyan
Write-Host "No se usará compose.yaml aunque exista en la carpeta." -ForegroundColor DarkGray

# Elimina sólo contenedores locales conocidos que puedan haber quedado de un compose anterior.
$known = @("profile-intelligence-api", "spm-ollama-local")
$existing = @(& docker ps -a --format "{{.Names}}")
foreach ($name in $known) {
    if ($existing -contains $name) {
        Write-Host "Eliminando contenedor local previo: $name"
        & docker rm -f $name | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "No se pudo eliminar $name" }
    }
}

Invoke-Compose build --no-cache profile-intelligence-api
Invoke-Compose up -d --force-recreate
Invoke-Compose ps

Write-Host ""
Write-Host "Health: http://localhost:3651/health" -ForegroundColor Green
Write-Host "Docs:   http://localhost:3651/docs" -ForegroundColor Green
Write-Host ""
Write-Host "IMPORTANTE: si qwen3:4b no está instalado, ejecuta .\setup-local-model.ps1 o restaura el modelo offline." -ForegroundColor Yellow
