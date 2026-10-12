#!/usr/bin/env python3
"""Limpieza SEGURA de una copia de distribución de ai-service, nunca sobre el original."""
from __future__ import annotations
import argparse
from pathlib import Path
import shutil
import sys

parser = argparse.ArgumentParser()
parser.add_argument('root')
args = parser.parse_args()
root = Path(args.root).resolve()
if not (root / '.ai-service-export-stage').exists():
    sys.exit('Protección: se permite limpiar únicamente el staging del constructor; tu proyecto original está a salvo.')
if not (root / 'app' / 'main.py').is_file() or not (root / 'VERSION').is_file():
    sys.exit('Directorio no válido: no es raíz del ai-service')
if (root / 'VERSION').read_text(encoding='utf-8').strip() != '4.1.0-cpu-optimized':
    sys.exit('Solo se limpia la distribución 4.1.0-cpu-optimized')

# Verificados contra importaciones del main/analyzer/rutas activas de 4.0.4.
OBSOLETE_FILES = {
    'README_REEMPLAZO.txt', 'ACTUALIZAR_ENV_LOCAL.ps1',
    'REDEPLOY_LOCAL_4.0.3.ps1', 'REDEPLOY_LOCAL_4.0.4.ps1',
    'apply-thinking-toggle.py', 'backend-thinking-toggle.patch',
    'fix-qwen-spanish-thinking.py', 'docker-compose.example.yml',
    'app/llm_local.py', 'app/grounded_chat.py',
    'tests/test_analyzer.py',
    '.env',  # Nunca distribuir secretos de entorno (en main está trackeado).
    'cpu_optimize_4_1_0.py',
}
for rel in sorted(OBSOLETE_FILES):
    path = root / rel
    if path.is_file():
        path.unlink()
        print('Quitado:', rel)

for dirname in ('.git', '.venv', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.idea', '.vscode', 'tars'):
    path = root / dirname
    if path.is_dir():
        shutil.rmtree(path)
        print('Quitado directorio:', dirname)

for item in sorted(root.rglob('*'), key=lambda p: len(p.parts), reverse=True):
    if any(part in {'.git', '.venv'} for part in item.relative_to(root).parts):
        continue
    if item.is_dir() and item.name in {'__pycache__', '.pytest_cache'}:
        shutil.rmtree(item, ignore_errors=True)
        continue
    if not item.is_file():
        continue
    if item.suffix in {'.pyc', '.pyo', '.pyd', '.tar', '.tgz'} or item.name.endswith(('.bak', '.bak-thinking-toggle', '.bak-cpu-optimized')):
        item.unlink()

# Los datos personales de perfiles/casos jamás deben salir en la distribución.
case_dir = root / 'data' / 'cases'
if case_dir.exists():
    for path in case_dir.iterdir():
        if path.name != '.gitkeep':
            if path.is_dir(): shutil.rmtree(path)
            else: path.unlink()

# Evitar crear imágenes Docker con claves locales por accidente.
dockerignore = root / '.dockerignore'
base = dockerignore.read_text(encoding='utf-8') if dockerignore.exists() else ''
for ignore in ('\n.env\n', '\n.env.*\n', '\n!.env.example\n', '\ntars/\n', '\n*.tar\n'):
    if ignore.strip() not in base.splitlines(): base += ignore
# Incluir *.env.example incluso bajo .env.*; las reglas se evalúan por orden.
dockerignore.write_text(base.rstrip() + '\n', encoding='utf-8')
(root / '.gitignore').write_text('''# Local/configuración y secretos
.env
.env.*
!.env.example

# Entorno local
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
.idea/
.vscode/

# Datos y binarios generados
data/cases/*
!data/cases/.gitkeep
tars/
*.tar
*.zip
*.bak*
''', encoding='utf-8')
print('Limpieza de distribución completada:', root)
