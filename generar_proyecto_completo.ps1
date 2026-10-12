<#
Construye ZIP COMPLETO del repositorio ai-service 4.0.4 -> 4.1.0 optimizado, sin borrar originales.
Requiere Git, Python 3.11+, conexión para clonar (o -RepoPath), y pytest para verificación.
Ejemplos:
  .\generar_proyecto_completo.ps1 -RepoPath 'D:\ai-service'
  .\generar_proyecto_completo.ps1 -Clone
  .\generar_proyecto_completo.ps1 -RepoPath 'D:\ai-service' -SkipTests
#>
[CmdletBinding()]
param(
  [string]$RepoPath = '',
  [switch]$Clone,
  [switch]$SkipTests,
  [string]$OutputZip = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$bundleDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OutputZip) { $OutputZip = Join-Path $bundleDir 'ai-service-4.1.0-cpu-optimized-COMPLETO.zip' }
$OutputZip = [System.IO.Path]::GetFullPath($OutputZip)
$work = Join-Path ([System.IO.Path]::GetTempPath()) ('ai-service-export-' + [guid]::NewGuid().ToString('N'))
$source = Join-Path $work 'repository'
$stage = Join-Path $work 'ai-service-4.1.0-cpu-optimized'
New-Item -ItemType Directory -Path $source, $stage -Force | Out-Null
try {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'Requiere Python instalado en PATH.' }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Requiere Git instalado en PATH.' }
    if ($Clone -or -not $RepoPath) {
        Write-Host 'Descargando repositorio completo desde GitHub...' -ForegroundColor Cyan
        & git clone --depth=1 --branch main https://github.com/Tavolordk/ai-service.git $source
        if ($LASTEXITCODE -ne 0) { throw 'No fue posible clonar ai-service.' }
    } else {
        $local = [System.IO.Path]::GetFullPath($RepoPath)
        if (-not (Test-Path (Join-Path $local 'app\main.py'))) { throw "Ruta de proyecto incorrecta: $local" }
        $source = $local
    }
    $version = (& git -C $source show 'HEAD:VERSION').Trim()
    if ($LASTEXITCODE -ne 0) { throw 'VERSION no existe en el commit de origen.' }
    if ($version -ne '4.0.4-reasoning-recovery') {
        throw "Versión de origen inesperada: $version. Se requiere 4.0.4-reasoning-recovery; no se sobrescribe nada."
    }
    # Exportar archivos TRACKED desde el commit. Evita copiar venv, estados locales y cambios ajenos.
    $tar = Join-Path $work 'source.tar'
    & git -C $source archive --format=tar -o $tar HEAD
    if ($LASTEXITCODE -ne 0) { throw 'git archive falló.' }
    & tar.exe -xf $tar -C $stage
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo extraer el archive de Git.' }

    Copy-Item (Join-Path $bundleDir 'cpu_optimize_4_1_0.py') $stage -Force
    Copy-Item (Join-Path $bundleDir 'migrate_env_cpu.ps1') $stage -Force
    & python (Join-Path $stage 'cpu_optimize_4_1_0.py') $stage --check *> (Join-Path $work 'preview_diff.txt')
    if ($LASTEXITCODE -ne 0) { Get-Content (Join-Path $work 'preview_diff.txt') -Tail 40; throw 'No pasó la prevalidación del parche.' }
    & python (Join-Path $stage 'cpu_optimize_4_1_0.py') $stage *> (Join-Path $work 'applied_diff.txt')
    if ($LASTEXITCODE -ne 0) { Get-Content (Join-Path $work 'applied_diff.txt') -Tail 40; throw 'No fue posible aplicar el parche.' }

    Set-Content -Path (Join-Path $stage '.ai-service-export-stage') -Value 'stage-only' -Encoding ASCII
    & python (Join-Path $bundleDir 'clean_distribution.py') $stage
    if ($LASTEXITCODE -ne 0) { throw 'Falló la limpieza.' }

    # Validar sintaxis SIN dejar __pycache__ dentro del ZIP.
    $syntax = @'
import ast, pathlib, sys
base=pathlib.Path(sys.argv[1]); files=list(base.rglob('*.py'))
for p in files: ast.parse(p.read_text(encoding='utf8'), filename=str(p))
print('Sintaxis correcta:',len(files),'archivos Python')
'@
    & python -c $syntax $stage
    if ($LASTEXITCODE -ne 0) { throw 'Existen errores de sintaxis.' }
    if (-not $SkipTests) {
        Write-Host 'Ejecutando suite de tests de la versión vigente...' -ForegroundColor Cyan
        Push-Location $stage
        try {
            & python -m pytest -q
            if ($LASTEXITCODE -ne 0) { throw 'Tests fallidos. No se construyó el ZIP; revisa log y dependencias.' }
        } finally { Pop-Location }
        & python (Join-Path $bundleDir 'clean_distribution.py') $stage
        if ($LASTEXITCODE -ne 0) { throw 'Falló limpieza post-test.' }
    }

    Remove-Item (Join-Path $stage '.ai-service-export-stage') -Force
    $guide = @'
AI-SERVICE 4.1.0 CPU OPTIMIZED
=============================
Paquete limpio. NO incluye .env (secreto), cachés, tars ni datos de perfiles/casos.

PowerShell desde la raíz:
  Copy-Item .env.example .env
  .\setup-local-model.ps1
  docker compose -f .\docker-compose.local.yml --env-file .\.env build --no-cache profile-intelligence-api
  docker compose -f .\docker-compose.local.yml --env-file .\.env up -d --force-recreate
  curl.exe http://localhost:3651/health
  docker exec -it spm-ollama-local ollama run qwen3:4b --verbose

Si migras desde una .env existente, copia tu .env local al nuevo proyecto y ejecuta:
  powershell.exe -ExecutionPolicy Bypass -File .\migrate_env_cpu.ps1
'@
    Set-Content -Path (Join-Path $stage 'INSTALACION_LOCAL.txt') -Value $guide -Encoding UTF8
    $parent = Split-Path -Parent $OutputZip
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory $parent -Force | Out-Null }
    if (Test-Path $OutputZip) { Remove-Item $OutputZip -Force }
    # Empaqueta carpeta raíz para extraer sin sobreescribir otro proyecto accidentalmente.
    Compress-Archive -LiteralPath $stage -DestinationPath $OutputZip -CompressionLevel Optimal
    Write-Host "ZIP COMPLETO: $OutputZip" -ForegroundColor Green
    $size = (Get-Item $OutputZip).Length
    Write-Host "Tamaño: $size bytes" -ForegroundColor Green
} finally {
    if (Test-Path $work) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
}
