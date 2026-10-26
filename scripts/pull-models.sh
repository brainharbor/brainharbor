#!/usr/bin/env bash
# pull-models.sh: downloads the models listed in .env (CHAT_MODELS and
# EMBEDDING_MODEL) into the running Ollama container, then creates the
# brAInharbor models described in ollama/*.Modelfile. Safe to re-run;
# models already present are only checked for updates, and the brAInharbor
# models are rebuilt from their current Modelfiles.

set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

service_running ollama || die "Ollama is not running. Start the stack first: docker compose up -d"

read -r -a models <<<"$(env_get CHAT_MODELS gemma4:26b) $(env_get EMBEDDING_MODEL bge-m3)"

for model in "${models[@]}"; do
  printf '%sPulling %s%s\n' "$C_BOLD" "$model" "$C_RESET"
  compose exec -T ollama ollama pull "$model"
done

# The Modelfile goes in on stdin, so the container needs no extra mount.
# Ollama pulls the FROM model itself if it isn't there yet.
for modelfile in "$BH_ROOT"/ollama/*.Modelfile; do
  [[ -e $modelfile ]] || continue
  name="$(basename "$modelfile" .Modelfile)"
  printf '%sCreating %s%s\n' "$C_BOLD" "$name" "$C_RESET"
  # shellcheck disable=SC2016 # $1 is expanded by the shell in the container
  compose exec -T ollama sh -c 'cat > /tmp/Modelfile && ollama create "$1" -f /tmp/Modelfile' \
    sh "$name" <"$modelfile"
done

printf '\n%sInstalled models:%s\n' "$C_BOLD" "$C_RESET"
compose exec -T ollama ollama list
