#!/usr/bin/env bash
# pull-models.sh: downloads the models listed in .env (CHAT_MODELS and
# EMBEDDING_MODEL) into the running Ollama container, then creates the
# brAInharbor models described in ollama/*.Modelfile. Safe to re-run;
# models already present are only checked for updates, and the brAInharbor
# models are rebuilt from their current Modelfiles. HU_BASE_MODEL in .env, if
# set, replaces the FROM line of brainharbor-hu.

set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

service_running ollama || die "Ollama is not running. Start the stack first: docker compose up -d"

read -r -a models <<<"$(env_get CHAT_MODELS qwen3.5:35b-a3b) $(env_get EMBEDDING_MODEL bge-m3)"

for model in "${models[@]}"; do
  printf '%sPulling %s%s\n' "$C_BOLD" "$model" "$C_RESET"
  compose exec -T ollama ollama pull "$model"
done

# The Modelfile goes in on stdin, so the container needs no extra mount.
# Ollama pulls the FROM model itself if it isn't there yet.
for modelfile in "$BH_ROOT"/ollama/*.Modelfile; do
  [[ -e $modelfile ]] || continue
  name="$(basename "$modelfile" .Modelfile)"
  base=""
  [[ $name == brainharbor-hu ]] && base="$(env_get HU_BASE_MODEL)"
  printf '%sCreating %s%s%s\n' "$C_BOLD" "$name" "${base:+ on $base}" "$C_RESET"
  # shellcheck disable=SC2016 # $1 is expanded by the shell in the container
  if [[ -n $base ]]; then
    sed "s|^FROM .*|FROM ${base}|" "$modelfile"
  else
    cat "$modelfile"
  fi |
    compose exec -T ollama sh -c 'cat > /tmp/Modelfile && ollama create "$1" -f /tmp/Modelfile' \
      sh "$name"
done

printf '\n%sInstalled models:%s\n' "$C_BOLD" "$C_RESET"
compose exec -T ollama ollama list
