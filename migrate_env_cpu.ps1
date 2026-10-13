param([string]$EnvFile = '.\.env')
$ErrorActionPreference = 'Stop'
if (-not (Test-Path $EnvFile)) {
    if (-not (Test-Path '.\.env.example')) { throw 'No existe .env ni .env.example' }
    Copy-Item '.\.env.example' $EnvFile
}
$values = [ordered]@{
    AI_APP_VERSION='4.2.0-large-scale'
    AI_VERSION='4.2.0-large-scale'
    AI_LLM_MODEL='qwen3:4b'
    AI_LLM_MODEL_FAST='qwen3:1.7b'
    AI_LLM_MODEL_DEEP='qwen3:4b'
    AI_LLM_THINKING_DEFAULT='false'
    AI_LLM_THINKING_MAX_PREDICT='1200'
    AI_FORCE_SPANISH_THINKING='false'
    AI_LLM_NUM_CTX='4096'
    AI_LLM_MAX_PREDICT='500'
    AI_CONTEXT_MAX_CHARS='6000'
    AI_CONTEXT_DEEP_MAX_CHARS='12000'
    AI_LLM_MAX_EVIDENCE='30'
    AI_LLM_HISTORY_MESSAGES='2'
    AI_LLM_WARMUP='true'
    AI_CASE_INDEX_VALUE_CHARS='1024'
    AI_CASE_SEARCH_MAX_RESULTS='200'
}
$full = [System.IO.Path]::GetFullPath($EnvFile)
$content = [System.IO.File]::ReadAllText($full)
$lines = New-Object 'System.Collections.Generic.List[string]'
$lines.AddRange([string[]]($content -split '\r?\n'))
foreach ($key in $values.Keys) {
    $value = $values[$key]
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match ('^\s*' + [regex]::Escape($key) + '\s*=')) {
            $lines[$i] = "$key=$value"
            $found = $true
            break
        }
    }
    if (-not $found) { $lines.Add("$key=$value") }
}
$backup = "$full.bak-cpu-optimized"
Copy-Item $full $backup -Force
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($full, ($lines -join "`r`n"), $utf8)
Write-Host "Actualizado $full; copia de seguridad en $backup"
