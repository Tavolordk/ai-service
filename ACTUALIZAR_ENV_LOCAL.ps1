$ErrorActionPreference = "Stop"

$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "No existe .env en esta carpeta. Se creará a partir de .env.example." -ForegroundColor Yellow
    Copy-Item (Join-Path $PSScriptRoot ".env.example") $envFile
}

$content = Get-Content $envFile -Raw

if ($content -match '(?m)^AI_APP_VERSION=.*$') {
    $content = [regex]::Replace($content, '(?m)^AI_APP_VERSION=.*$', 'AI_APP_VERSION=4.0.4-reasoning-recovery')
} else {
    $content += "`r`nAI_APP_VERSION=4.0.4-reasoning-recovery`r`n"
}

if ($content -match '(?m)^AI_VERSION=.*$') {
    $content = [regex]::Replace($content, '(?m)^AI_VERSION=.*$', 'AI_VERSION=4.0.4-reasoning-recovery')
} else {
    $content += "AI_VERSION=4.0.4-reasoning-recovery`r`n"
}

Set-Content -Path $envFile -Value $content -Encoding UTF8
Write-Host "Versiones actualizadas en .env sin modificar el resto de tu configuración." -ForegroundColor Green
