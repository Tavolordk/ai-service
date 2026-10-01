#!/usr/bin/env bash
set -euo pipefail
docker compose up -d ollama
docker compose exec ollama ollama pull qwen3:4b
docker compose exec ollama ollama list
