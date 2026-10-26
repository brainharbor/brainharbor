# shellcheck shell=bash
# Shared helpers for brAInharbor scripts. Source it, don't run it.

# Repository root, independent of where the script was started from.
BH_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BH_ENV_FILE="${BH_ENV_FILE:-$BH_ROOT/.env}"

# Colors are used by the scripts that source this file.
# shellcheck disable=SC2034
if [[ -t 1 ]]; then
  C_RED=$'\e[31m' C_GREEN=$'\e[32m' C_YELLOW=$'\e[33m' C_BOLD=$'\e[1m' C_RESET=$'\e[0m'
else
  C_RED='' C_GREEN='' C_YELLOW='' C_BOLD='' C_RESET=''
fi

# env_get KEY [DEFAULT]
# Reads KEY from .env without sourcing it, so the file is never executed.
# The last assignment wins; surrounding single or double quotes are removed.
env_get() {
  local key="$1" default="${2-}" value=""
  if [[ -f "$BH_ENV_FILE" ]]; then
    value="$(grep -E "^[[:space:]]*${key}=" "$BH_ENV_FILE" | tail -n 1 | cut -d= -f2- || true)"
    value="${value%$'\r'}"
    if [[ $value =~ ^\"(.*)\"$ || $value =~ ^\'(.*)\'$ ]]; then
      value="${BASH_REMATCH[1]}"
    fi
  fi
  printf '%s' "${value:-$default}"
}

# Resolves a data directory from .env relative to the repository root.
data_dir() {
  local dir
  dir="$(env_get "$1" "$2")"
  [[ $dir == /* ]] || dir="$BH_ROOT/${dir#./}"
  printf '%s' "$dir"
}

compose() {
  (cd "$BH_ROOT" && docker compose "$@")
}

# service_running NAME: true if the compose service NAME is running.
service_running() {
  local running
  running="$(compose ps --status running --services 2>/dev/null || true)"
  [[ $'\n'$running$'\n' == *$'\n'"$1"$'\n'* ]]
}

have() { command -v "$1" >/dev/null 2>&1; }

step() { printf '%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"; }
warn() { printf '%sWarning:%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%sError:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

# zfs_dataset DIR
# Prints the ZFS dataset that holds DIR, and the dataset's mount point on a
# second line. Fails when DIR is not on ZFS.
zfs_dataset() {
  if ! have zfs || [[ "$(findmnt -n -o FSTYPE --target "$1" 2>/dev/null)" != zfs ]]; then
    return 1
  fi
  findmnt -n -o SOURCE --target "$1"
  findmnt -n -o TARGET --target "$1"
}
