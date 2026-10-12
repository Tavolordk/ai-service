@echo off
setlocal
REM Usa los mismos nombres de servicio/volumen que docker-compose.local.yml.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-local-model.ps1"
exit /b %ERRORLEVEL%
