#!/usr/bin/env bash
# backup.sh: saves what it takes to rebuild this install into one archive:
# Open WebUI's data (accounts, chats, uploaded documents, search index),
# Caddy's certificates and local CA, .env, and the list of models. Models
# are left out; restore.sh downloads them again.
#
# Usage: sudo scripts/backup.sh [--keep N]
#   --keep N  keep the N newest archives (default: BACKUP_KEEP in .env, or 7)
#
# On ZFS the data is read from a snapshot and nothing stops. Elsewhere Open
# WebUI is stopped while its data is copied, usually for a few seconds.
# Schedules, offsite copies and restoring: docs/backup.md.

set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

KEEP=""
while (($#)); do
  case "$1" in
    --keep) KEEP="${2-}"; shift ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

((EUID == 0)) || die "Run it as root (sudo $0): the containers write their data as root."
have docker || die "docker not found"
have rsync || die "rsync not found; install it first (e.g. apt install rsync)"
[[ -f $BH_ENV_FILE ]] || die "$BH_ENV_FILE not found"

KEEP="${KEEP:-$(env_get BACKUP_KEEP 7)}"
[[ $KEEP =~ ^[0-9]+$ ]] || die "The number of archives to keep must be a number (found '$KEEP')"

backup_dir="$(data_dir BACKUP_DIR ./backups)"
declare -A dirs=(
  [webui]="$(data_dir WEBUI_DATA_DIR ./data/webui)"
  [caddy]="$(data_dir CADDY_DATA_DIR ./data/caddy)"
)
[[ -f ${dirs[webui]}/webui.db ]] || die "No Open WebUI database in ${dirs[webui]}; nothing to back up yet"
[[ -d ${dirs[caddy]} ]] || die "${dirs[caddy]} not found; start the stack once first"

# Archives hold secrets (.env, Caddy's CA key) and every chat.
umask 077
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
exec 9>"$backup_dir/.lock"
flock -n 9 || die "Another backup is running"

stamp="$(date +%Y%m%d-%H%M%S)"
# The newest copy, kept between runs so rsync only copies what changed.
# BACKUP-INFO.txt is written last: without it the copy is incomplete.
stage="$backup_dir/latest"
snapshots=()  # ZFS snapshots taken by this run
stopped=0     # 1 while this script keeps Open WebUI stopped
archive=""

cleanup() {
  if ((stopped)); then
    quiet compose start open-webui || warn "Could not start Open WebUI again: docker compose start open-webui"
  fi
  local snapshot
  for snapshot in "${snapshots[@]}"; do
    zfs destroy "$snapshot" || warn "Could not remove the snapshot $snapshot"
  done
  [[ -z $archive ]] || rm -f -- "$archive.part"
}
trap cleanup EXIT

# quiet CMD...: runs CMD and shows its output only if it fails.
quiet() {
  local out
  out="$("$@" 2>&1)" || { printf '%s\n' "$out" >&2; return 1; }
}

now_ms() { echo $(($(date +%s%N) / 1000000)); }

step "brAInharbor backup $stamp"
mkdir -p "$stage/config" "$stage/webui" "$stage/caddy"
rm -f "$stage/BACKUP-INFO.txt" "$stage/config/local-changes.patch"

# ---------------------------------------------------------------------------
if service_running ollama && models="$(compose exec -T ollama ollama list </dev/null)"; then
  awk 'NR > 1 {print $1}' <<<"$models" >"$stage/models.txt"
  echo "Models: $(grep -c . "$stage/models.txt") listed"
elif [[ -f $stage/models.txt ]]; then
  warn "Could not list the models; keeping the list from the previous backup"
else
  warn "Could not list the models; the backup has none"
fi

cp -p "$BH_ENV_FILE" "$stage/config/.env"
commit="unknown"
git_() { git -c safe.directory="$BH_ROOT" -C "$BH_ROOT" "$@"; }
if have git && git_ rev-parse --git-dir >/dev/null 2>&1; then
  commit="$(git_ rev-parse --short HEAD)"
  # Edits to tracked files (Caddyfile, compose files) are saved for reference.
  # git diff may rewrite the index, and as root that would leave
  # .git/index owned by root, so it gets a copy of the index.
  index="$(mktemp)"
  cp "$(git_ rev-parse --absolute-git-dir)/index" "$index"
  GIT_INDEX_FILE="$index" git_ diff HEAD >"$stage/config/local-changes.patch"
  rm -f "$index" "$index.lock"
  if [[ -s $stage/config/local-changes.patch ]]; then
    commit="$commit, with local changes"
  else
    rm -f "$stage/config/local-changes.patch"
  fi
fi
echo "Settings: .env (commit $commit)"

# ---------------------------------------------------------------------------
# On ZFS, copy from a snapshot: all datasets are snapshotted at the same
# moment, and SQLite recovers from it like from a power cut.
declare -A from=() wanted=()
for name in webui caddy; do
  dir="$(realpath "${dirs[$name]}")"
  if info="$(zfs_dataset "$dir")"; then
    dataset="${info%%$'\n'*}" mountpoint="${info#*$'\n'}"
    mountpoint="${mountpoint%/}"
    wanted["$dataset@brainharbor-backup-$stamp"]=1
    from[$name]="$mountpoint/.zfs/snapshot/brainharbor-backup-$stamp${dir#"$mountpoint"}"
  else
    from[$name]="$dir"
  fi
done
if ((${#wanted[@]})); then
  zfs snapshot "${!wanted[@]}"
  snapshots=("${!wanted[@]}")
fi

# copy SRC DEST [RSYNC_OPTION...]
# Exit code 24 (files vanished during the copy) is only a warning.
copy() {
  local src="$1" dest="$2" code=0
  shift 2
  rsync -a --delete --numeric-ids "$@" "$src/" "$dest/" || code=$?
  ((code == 0 || code == 24)) || die "Copying $src failed (rsync exit code $code)"
  ((code == 0)) || warn "Some files in $src changed during the copy"
}

consistency="ZFS snapshot"
if [[ ${from[webui]} == "$(realpath "${dirs[webui]}")" ]]; then
  # Not on ZFS: a database copied while Open WebUI writes to it can be
  # broken, so Open WebUI stops for the copy.
  consistency="copied while Open WebUI was not running"
  if service_running open-webui; then
    quiet compose stop open-webui
    stopped=1
    stopped_at="$(now_ms)"
  fi
fi
copy "${from[webui]}" "$stage/webui" \
  --exclude=/cache/tiktoken/ --exclude=/cache/whisper/ --exclude=/cache/embedding/
if ((stopped)); then
  quiet compose start open-webui
  stopped=0
  ms=$(($(now_ms) - stopped_at))
  consistency="copied while Open WebUI was stopped for $((ms / 1000)).$((ms % 1000 / 100)) s"
fi
echo "Open WebUI data: $consistency"

# Caddy writes rarely (certificate renewals); it keeps running.
copy "${from[caddy]}" "$stage/caddy"
echo "Caddy data: copied"

for snapshot in "${snapshots[@]}"; do
  zfs destroy "$snapshot"
done
snapshots=()

# ---------------------------------------------------------------------------
# Check the copied databases with the Python in the Open WebUI image. The
# check also folds each write-ahead log into its database file.
echo "Checking the databases:"
read -r -d '' check_databases <<'PY' || true
import sqlite3
import sys
from pathlib import Path

failed = False
for path in [Path('/data/webui.db'), *sorted(Path('/data/vector_db').glob('*.sqlite3'))]:
    con = sqlite3.connect(path)
    con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    result = con.execute('PRAGMA integrity_check').fetchone()[0]
    con.close()
    print(f'  {path.relative_to("/data")}: {result}')
    failed = failed or result != 'ok'
sys.exit(1 if failed else 0)
PY
if ! { errors="$(compose run --rm --no-deps -T -v "$stage/webui:/data" --entrypoint python3 \
  open-webui -c "$check_databases" </dev/null 2>&1 >&3)"; } 3>&1; then
  printf '%s\n' "$errors" >&2
  die "A database in the copy is damaged or could not be checked; this backup is not usable"
fi

{
  echo "format: 1"
  echo "created: $(date -Iseconds)"
  echo "host: $(uname -n)"
  echo "folder: $BH_ROOT"
  echo "commit: $commit"
  echo "open-webui: $consistency"
} >"$stage/BACKUP-INFO.txt"

# ---------------------------------------------------------------------------
if ((KEEP == 0)); then
  step "Done in $((SECONDS)) s: $stage"
  exit 0
fi

archive="$backup_dir/brainharbor-$stamp.tar.gz"
members=(BACKUP-INFO.txt)
[[ -f $stage/models.txt ]] && members+=(models.txt)
members+=(config caddy webui)
tar -C "$stage" --numeric-owner -czf "$archive.part" "${members[@]}"
mv "$archive.part" "$archive"
# Apparent size: on ZFS, du counts blocks, and a file just written may not have any yet.
echo "Archive: $archive ($(du -h --apparent-size "$archive" | cut -f1))"

# Names sort by date, oldest first.
shopt -s nullglob
archives=("$backup_dir"/brainharbor-*.tar.gz)
shopt -u nullglob
excess=$((${#archives[@]} - KEEP))
for ((i = 0; i < excess; i++)); do
  rm -f -- "${archives[i]}"
  echo "Removed: ${archives[i]}"
done

step "Done in $((SECONDS)) s"
