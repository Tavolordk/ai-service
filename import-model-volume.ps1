param(
    [Parameter(Mandatory=$true)]
    [string]$Archive
)

$ErrorActionPreference = "Stop"
$ComposeFile = ".\docker-compose.local.yml"
$EnvFile = ".\.env"

if (-not (Test-Path $Archive)) { throw "No existe el archivo: $Archive" }

& docker compose -f $ComposeFile --env-file $EnvFile up -d ollama
if ($LASTEXITCODE -ne 0) { throw "No pudo iniciar Ollama." }

$container = "spm-ollama-local"
$tmp = Join-Path $env:TEMP ("ollama-models-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    tar -xf $Archive -C $tmp
    if ($LASTEXITCODE -ne 0) { throw "No se pudo extraer $Archive" }

    $models = Get-ChildItem -Path $tmp -Directory -Recurse | Where-Object { $_.Name -eq "models" } | Select-Object -First 1
    if (-not $models) {
        # También aceptamos que el tar contenga directamente manifests/ y blobs/.
        if ((Test-Path (Join-Path $tmp "manifests")) -and (Test-Path (Join-Path $tmp "blobs"))) {
            $modelsPath = $tmp
        } else {
            throw "El tar no contiene una carpeta models ni manifests/blobs de Ollama."
        }
    } else {
        $modelsPath = $models.FullName
    }

    & docker exec $container sh -lc "mkdir -p /root/.ollama/models"
    if ($LASTEXITCODE -ne 0) { throw "No se pudo preparar /root/.ollama/models" }
    & docker cp ((Join-Path $modelsPath ".")) "${container}:/root/.ollama/models/"
    if ($LASTEXITCODE -ne 0) { throw "docker cp falló" }
    & docker restart $container | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "No se pudo reiniciar Ollama" }

    Write-Host "Modelos disponibles después de importar:" -ForegroundColor Green
    & docker exec $container ollama list
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
