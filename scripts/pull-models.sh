#!/usr/bin/env bash
# pull-models.sh: downloads the models listed in .env (CHAT_MODELS and
# EMBEDDING_MODEL) into the running Ollama container. Safe to re-run;
# models already present are only checked for updates.

set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

running="$(compose ps --status running --services 2>/dev/null || true)"
if [[ $'\n'$running$'\n' != *$'\n'ollama$'\n'* ]]; then
  echo "Ollama is not running. Start the stack first: docker compose up -d" >&2
  exit 1
fi

read -r -a models <<<"$(env_get CHAT_MODELS gemma3:12b) $(env_get EMBEDDING_MODEL bge-m3)"

for model in "${models[@]}"; do
  printf '%sPulling %s%s\n' "$C_BOLD" "$model" "$C_RESET"
  compose exec -T ollama ollama pull "$model"
done

printf '\n%sInstalled models:%s\n' "$C_BOLD" "$C_RESET"
compose exec -T ollama ollama list
