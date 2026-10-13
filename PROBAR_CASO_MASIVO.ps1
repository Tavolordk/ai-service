param(
    [string]$JsonFile = '.\caso_masivo_ficticio.json',
    [string]$CaseId = 'PRUEBA_MASIVA',
    [string]$ApiBase = 'http://localhost:3651'
)
$ErrorActionPreference = 'Stop'
$path = (Resolve-Path -LiteralPath $JsonFile).Path
if ($CaseId -notmatch '^[A-Za-z0-9_.-]{1,120}$') { throw 'CaseId inválido' }
$base = $ApiBase.TrimEnd('/')
Write-Host "Archivo: $path" -ForegroundColor Cyan
Write-Host "Bytes: $((Get-Item -LiteralPath $path).Length)" -ForegroundColor Cyan
Write-Host '1/4 Subiendo archivo EN STREAMING al índice (sin JSON inline)...' -ForegroundColor Cyan
$clock = [System.Diagnostics.Stopwatch]::StartNew()
& curl.exe --fail-with-body -sS -X POST -H 'Content-Type: application/json' --data-binary "@$path" "$base/api/v1/cases?caseId=$CaseId"
if ($LASTEXITCODE -ne 0) { throw "Carga falló. Código CURL: $LASTEXITCODE" }
$clock.Stop()
Write-Host "`nTiempo de carga e indexación: $([math]::Round($clock.Elapsed.TotalSeconds,2)) s" -ForegroundColor Cyan
Write-Host '2/4 Leyendo contadores del índice...' -ForegroundColor Cyan
$meta = Invoke-RestMethod -Uri "$base/api/v1/cases/$CaseId"
$meta | ConvertTo-Json -Depth 8
Write-Host '3/4 Consultando únicamente 5 vínculos del nodo CENTRO...' -ForegroundColor Cyan
$clock.Restart()
$links = Invoke-RestMethod -Uri "$base/api/v1/cases/$CaseId/links?nodeId=CENTRO&limit=5"
$clock.Stop()
$links | ConvertTo-Json -Depth 8
Write-Host "Tiempo consulta adyacencia: $([math]::Round($clock.Elapsed.TotalSeconds,3)) s" -ForegroundColor Cyan
if ($links.nextEdgeId) {
    Write-Host 'Siguiente página por cursor (sin OFFSET profundo)...' -ForegroundColor Cyan
    Invoke-RestMethod -Uri "$base/api/v1/cases/$CaseId/links?nodeId=CENTRO&limit=5&afterEdgeId=$($links.nextEdgeId)" | ConvertTo-Json -Depth 4
}
Write-Host '4/4 Chat con caseId: NO reenvía el JSON grande...' -ForegroundColor Cyan
$body = @{ profile = @{profileId='PERFIL-SIMULADO'}; caseId=$CaseId; selectedNode=@{id='CENTRO'};
          question='¿Cuántos vínculos tiene este nodo?'; mode='auto'; thinking=$false; history=@() } | ConvertTo-Json -Depth 12
$clock.Restart()
$result = Invoke-RestMethod -Uri "$base/api/v1/intelligence/chat" -Method Post -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
$clock.Stop()
$result | ConvertTo-Json -Depth 10
Write-Host "Tiempo chat: $([math]::Round($clock.Elapsed.TotalSeconds,3)) s" -ForegroundColor Green
