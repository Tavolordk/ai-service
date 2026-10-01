#!/usr/bin/env bash
set -euo pipefail
[ -f .env ] || cp .env.example .env
docker compose down --remove-orphans
docker compose build --no-cache profile-intelligence-api
docker compose up -d --force-recreate
docker compose ps
printf '\nHealth: http://localhost:3651/health\nDocs:   http://localhost:3651/docs\n'
