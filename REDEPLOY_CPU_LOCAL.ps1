$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$compose = '.\docker-compose.local.yml'
$envFile = '.\.env'

Write-Host '1/5 Migrando variables locales sin perder otras configuraciones...' -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\migrate_env_cpu.ps1"
if ($LASTEXITCODE -ne 0) { throw 'No se pudo preparar .env' }

Write-Host '2/5 Verificando Compose...' -ForegroundColor Cyan
& docker compose -f $compose --env-file $envFile config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Compose invalido' }

Write-Host '3/5 Iniciando Ollama e instalando modelos faltantes...' -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\setup-local-model.ps1"
if ($LASTEXITCODE -ne 0) { throw 'Falló la instalación de modelos. Revisa conexión a registry.ollama.ai y el volumen existente.' }

Write-Host '4/5 Reconstruyendo la API sin borrar el volumen de modelos...' -ForegroundColor Cyan
& docker compose -f $compose --env-file $envFile build --no-cache profile-intelligence-api
if ($LASTEXITCODE -ne 0) { throw 'Falló la construcción Docker' }
& docker compose -f $compose --env-file $envFile up -d --force-recreate
if ($LASTEXITCODE -ne 0) { throw 'No se pudo levantar Docker' }

Write-Host '5/5 Estado y salud...' -ForegroundColor Cyan
& docker compose -f $compose --env-file $envFile ps
& docker exec spm-ollama-local ollama list
& curl.exe --fail http://localhost:3651/health
if ($LASTEXITCODE -ne 0) { Write-Warning 'Health aun no responde; consulta: docker logs --tail 150 profile-intelligence-api' }
Write-Host 'Redeploy local finalizado. Conservado: volumen spm-ollama-models y carpeta data/cases.' -ForegroundColor Green
