SPM IA Agent API - 4.0.4-reasoning-recovery

Este ZIP es un overlay para el proyecto ai-service actual.

1. Haz una copia de seguridad de tu proyecto actual.
2. Descomprime este ZIP SOBRE la raíz del proyecto actual y acepta reemplazar archivos.
3. NO reemplaza tu .env.
4. Desde PowerShell, en la raíz del proyecto, ejecuta:

   .\ACTUALIZAR_ENV_LOCAL.ps1
   docker compose -f .\docker-compose.local.yml --env-file .\.env build --no-cache profile-intelligence-api
   docker compose -f .\docker-compose.local.yml --env-file .\.env up -d --force-recreate profile-intelligence-api

5. Verifica:

   Invoke-RestMethod http://localhost:3651/
   Invoke-RestMethod http://localhost:3651/health

La versión esperada es 4.0.4-reasoning-recovery.

Cambio principal:
- Qwen ya no transmite message.content directamente al frontend mientras genera.
- La salida se retiene y se valida antes de publicarse.
- Se bloquean fugas de deliberación del tipo "Okay, the user...", "Let me...", "I need to...".
- Si la salida no está suficientemente en español o contiene deliberación, se regenera sin reutilizar el borrador contaminado.
- Si dos intentos siguen siendo inválidos, la API falla de forma cerrada y NO expone el razonamiento.
- El canal message.thinking/message.reasoning continúa siendo privado.
