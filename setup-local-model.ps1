$ErrorActionPreference = "Stop"

$ComposeFile = ".\docker-compose.local.yml"
$EnvFile = ".\.env"
$Model = "qwen3:4b"

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
Invoke-Compose up -d ollama

Write-Host "Modelos actualmente instalados:" -ForegroundColor Cyan
$list = & docker compose -f $ComposeFile --env-file $EnvFile exec -T ollama ollama list
if ($LASTEXITCODE -ne 0) {
    throw "No fue posible consultar Ollama dentro del contenedor."
}
$list | Write-Host

if (($list -join "`n") -match [regex]::Escape($Model)) {
    Write-Host "$Model ya está instalado. No se descargará de nuevo." -ForegroundColor Green
    exit 0
}

Write-Host "El modelo $Model NO está instalado." -ForegroundColor Yellow
Write-Host "Intentando descargarlo desde registry.ollama.ai..." -ForegroundColor Yellow
& docker compose -f $ComposeFile --env-file $EnvFile exec -T ollama ollama pull $Model
if ($LASTEXITCODE -ne 0) {
    Write-Host "" 
    Write-Host "ERROR: no se pudo descargar $Model." -ForegroundColor Red
    Write-Host "Tu red/DNS corporativo está bloqueando registry.ollama.ai." -ForegroundColor Red
    Write-Host "La API puede construirse, pero el chatbot no responderá hasta que el modelo exista en el volumen spm-ollama-models." -ForegroundColor Yellow
    Write-Host "Si tienes el modelo en otro equipo/servidor, usa import-model-volume.ps1 después de copiar un tar de /root/.ollama/models." -ForegroundColor Yellow
    exit 20
}

Write-Host "Modelo instalado correctamente:" -ForegroundColor Green
Invoke-Compose exec -T ollama ollama list
