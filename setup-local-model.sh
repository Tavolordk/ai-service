#!/usr/bin/env bash
set -euo pipefail
compose=(docker compose -f docker-compose.local.yml --env-file .env)
[[ -f .env ]] || cp .env.example .env
"${compose[@]}" up -d ollama
for model in qwen3:1.7b qwen3:4b; do
  "${compose[@]}" exec -T ollama ollama pull "$model"
done
"${compose[@]}" exec -T ollama ollama list
