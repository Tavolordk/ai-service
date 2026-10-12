# Prueba REAL del API SPM IA 4.1.2, con perfil y grafo ficticios.
# Windows PowerShell 5.1 / PowerShell 7. No modifica la API, BD ni volúmenes.
# Uso: .\PROBAR_IA_SPM_COMPLETA.ps1
# Opcional: .\PROBAR_IA_SPM_COMPLETA.ps1 -Llm  (incluir Qwen 1.7B)
# Opcional: .\PROBAR_IA_SPM_COMPLETA.ps1 -Deep (Qwen 1.7B y 4B)
param(
    [string]$BaseUrl = 'http://localhost:3651',
    [switch]$Deep,
    [switch]$Llm
)
$ErrorActionPreference = 'Stop'
$PSDefaultParameterValues['*:ErrorAction'] = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$fixture = Join-Path $scriptDir 'tests/fixtures/perfil_consolidado_ficticio.json'
if (-not (Test-Path $fixture)) { throw "No existe el JSON de prueba: $fixture" }

Write-Host "=== Health inicial ===" -ForegroundColor Cyan
$initial = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 30
$initial | ConvertTo-Json -Depth 10
if ($initial.status -ne 'ok') { throw 'La API no está saludable.' }

function Invoke-Prueba {
    param([string]$Titulo,[string]$Pregunta,[string]$Modo = 'auto')
    # Objeto reconstruido para evitar reutilizar historial/respuestas accidentalmente.
    $payload = Get-Content -LiteralPath $fixture -Raw -Encoding UTF8 | ConvertFrom-Json
    $payload.question = $Pregunta
    $payload.mode = $Modo
    $payload.thinking = $false
    $json = $payload | ConvertTo-Json -Depth 40 -Compress
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    Write-Host "`n=== $Titulo ===" -ForegroundColor Yellow
    Write-Host "Pregunta: $Pregunta"
    try {
        $r = Invoke-RestMethod -Uri "$BaseUrl/api/v1/intelligence/chat" `
             -Method Post -ContentType 'application/json; charset=utf-8' `
             -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 1200
        $watch.Stop()
        Write-Host ("Modo: {0} | Modelo: {1} | Thinking: {2} | Segundos: {3:N2}" -f $r.mode,$r.model,$r.thinking,$watch.Elapsed.TotalSeconds) -ForegroundColor Green
        Write-Host ('Respuesta: ' + $r.text)
        Write-Host ("Evidencias: {0}" -f @($r.evidence).Count)
        return [pscustomobject]@{ Prueba=$Titulo; Modelo=$r.model; Modo=$r.mode; Segundos=[math]::Round($watch.Elapsed.TotalSeconds,2); Texto=$r.text }
    }
    catch {
        $watch.Stop()
        Write-Warning "Error en ${Titulo}: $($_.Exception.Message)"
        throw
    }
}

$results = @()
$results += Invoke-Prueba -Titulo '1 - Identificador (FAST PATH)' -Pregunta '¿Cuál es la CURP registrada?'
$results += Invoke-Prueba -Titulo '2 - Domicilios (FAST PATH)' -Pregunta 'Enumera sus domicilios registrados.'
$results += Invoke-Prueba -Titulo '3 - Vehículos (FAST PATH)' -Pregunta '¿Cuántos vehículos hay relacionados en el grafo?'
$results += Invoke-Prueba -Titulo '4 - Vínculos (FAST PATH)' -Pregunta '¿Cuántas relaciones hay entre los nodos del grafo?'
$results += Invoke-Prueba -Titulo '5 - Fuentes (FAST PATH)' -Pregunta '¿Qué fuentes de origen están registradas?'
$results += Invoke-Prueba -Titulo '6 - Comparación documental (SIN OLLAMA)' -Pregunta 'Analiza las diferencias documentadas entre las fuentes de fecha de nacimiento y las dos placas para el mismo VIN. Cita los valores y las fuentes, no inventes hechos y distingue discrepancias de cambios históricos.'
if ($Llm -or $Deep) {
    $results += Invoke-Prueba -Titulo '7 - Interpretación de discrepancias (QWEN FAST)' -Pregunta 'Explica posibles causas de las diferencias documentadas entre fechas de nacimiento y placas del mismo VIN, plantea hipótesis por verificar y evita atribuir delitos.'
}
if ($Deep) {
    $results += Invoke-Prueba -Titulo '8 - Investigación profunda (QWEN DEEP)' -Modo 'deep' -Pregunta 'Analiza el perfil consolidado, contrastando fecha de nacimiento, vehículos, domicilios y relaciones. Separa hechos documentados, discrepancias, hipótesis y datos insuficientes. Evita inferencias de criminalidad o culpabilidad.'
}

Write-Host "`n=== Resumen de tiempos ===" -ForegroundColor Cyan
$results | Select-Object Prueba,Modo,Modelo,Segundos | Format-Table -AutoSize

Write-Host "`n=== Métricas de Ollama / Fast Router ===" -ForegroundColor Cyan
$health = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 30
$health.fastPath | ConvertTo-Json -Depth 12
$health.llmStats | ConvertTo-Json -Depth 12
Write-Host "`nNota: los registros y las personas son completamente ficticios." -ForegroundColor DarkGray
