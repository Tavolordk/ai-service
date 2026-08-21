@echo off
setlocal

echo Recreando servicios de IA sin borrar el volumen de modelos...
docker compose down

echo Iniciando Ollama local...
docker compose up -d ollama || exit /b 1

echo Habilitando salida temporal SOLO para descargar el modelo...
docker network connect bridge profile-intelligence-ollama >nul 2>&1

echo Descargando Qwen3 4B. Esto se hace una sola vez y NO envia perfiles.
docker exec profile-intelligence-ollama ollama pull qwen3:4b
set PULL_ERROR=%ERRORLEVEL%

echo Quitando de nuevo la salida temporal de Ollama...
docker network disconnect bridge profile-intelligence-ollama >nul 2>&1

if not "%PULL_ERROR%"=="0" (
  echo No fue posible descargar el modelo.
  exit /b %PULL_ERROR%
)

echo Levantando la API privada...
docker compose up -d --build --force-recreate || exit /b 1

echo.
echo Listo. Prueba:
echo   curl.exe http://127.0.0.1:8080/health
echo   curl.exe http://127.0.0.1:8080/health/llm
endlocal
