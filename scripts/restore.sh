#!/usr/bin/env bash
# restore.sh: brings an install back from a backup made by backup.sh, on
# this machine or on a new one. On a new machine, clone the repository,
# copy the backup over and run this; it brings its own .env.
#
# Usage: sudo scripts/restore.sh [options] BACKUP
#   BACKUP       an archive made by backup.sh, or its BACKUP_DIR/latest folder
#   --keep-env   keep this machine's .env (data folders, domain, GPU, versions)
#                instead of installing the one from the backup
#   --no-models  don't download the models now; run scripts/pull-models.sh later
#   --yes        don't ask before replacing data
#
# Nothing is deleted: data found in the way is renamed or, on ZFS, kept in
# a snapshot. More in docs/backup.md.

set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

KEEP_ENV=0
PULL_MODELS=1
ASSUME_YES=0
backup=""
while (($#)); do
  case "$1" in
    --keep-env) KEEP_ENV=1 ;;
    --no-models) PULL_MODELS=0 ;;
    --yes) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; exit 2 ;;
    *) [[ -z $backup ]] || { echo "Give only one backup" >&2; exit 2; }; backup="$1" ;;
  esac
  shift
done
[[ -n $backup ]] || { usage >&2; exit 2; }

((EUID == 0)) || die "Run it as root (sudo $0 ...): the restored files must keep their owners."
have docker || die "docker not found"
((!KEEP_ENV)) || [[ -f $BH_ENV_FILE ]] || die "--keep-env: there is no $BH_ENV_FILE"

stamp="$(date +%Y%m%d-%H%M%S)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

# get PATH: copies PATH from the backup to $work/PATH; fails if it's missing.
# unpack NAME DIR: copies the folder NAME from the backup into DIR.
if [[ -d $backup ]]; then
  get() { [[ -e $backup/$1 ]] && mkdir -p "$work/$(dirname "$1")" && cp -a "$backup/$1" "$work/$1"; }
  unpack() { cp -a "$backup/$1/." "$2/"; }
  has_db() { [[ -f $backup/webui/webui.db ]]; }
elif [[ -f $backup ]]; then
  step "Checking $backup"
  tar -tzf "$backup" >"$work/list" || die "$backup is damaged or not a .tar.gz archive"
  get() { grep -qxF "$1" "$work/list" && tar -xzf "$backup" -C "$work" "$1"; }
  unpack() { tar -xzf "$backup" -C "$2" --numeric-owner --strip-components=1 "$1"; }
  has_db() { grep -qxF webui/webui.db "$work/list"; }
else
  die "$backup not found"
fi

get BACKUP-INFO.txt || die "No BACKUP-INFO.txt: not a backup.sh backup, or one that didn't finish"
grep -qx 'format: 1' "$work/BACKUP-INFO.txt" || die "Unknown backup format; use the restore.sh that came with it"
get config/.env || die "The backup has no .env"
has_db || die "The backup has no Open WebUI database"
get models.txt || true
get config/local-changes.patch || true

echo "Backup:"
sed 's/^/  /' "$work/BACKUP-INFO.txt"

# ---------------------------------------------------------------------------
# Data folders come from the .env that will be in use after the restore.
env_file="$work/config/.env"
((!KEEP_ENV)) || env_file="$BH_ENV_FILE"
declare -A dirs=(
  [webui]="$(BH_ENV_FILE="$env_file" data_dir WEBUI_DATA_DIR ./data/webui)"
  [caddy]="$(BH_ENV_FILE="$env_file" data_dir CADDY_DATA_DIR ./data/caddy)"
)
declare -A labels=([webui]="Open WebUI data" [caddy]="Caddy data")

# keep_mode DIR: how the data already in DIR is kept.
keep_mode() {
  if [[ ! -d $1 || -z "$(ls -A "$1")" ]]; then
    echo empty
  elif zfs_dataset "$1" >/dev/null; then
    echo zfs
  elif mountpoint -q "$1"; then
    echo mount
  else
    echo rename
  fi
}

echo
echo "This will:"
echo "  - stop brAInharbor if it is running"
if ((KEEP_ENV)); then
  echo "  - keep this machine's .env; the backup's copy goes to .env.from-backup-$stamp"
elif [[ -f $BH_ENV_FILE ]] && ! cmp -s "$BH_ENV_FILE" "$env_file"; then
  echo "  - install the backup's .env; the current one is kept as .env.before-restore-$stamp"
else
  echo "  - install the backup's .env"
fi
for name in webui caddy; do
  dir="${dirs[$name]}"
  case "$(keep_mode "$dir")" in
    empty) echo "  - restore ${labels[$name]} into $dir" ;;
    zfs) echo "  - replace ${labels[$name]} in $dir (current data kept in a @before-restore-$stamp snapshot)" ;;
    rename) echo "  - replace ${labels[$name]} in $dir (current data moved to $dir.before-restore-$stamp)" ;;
    mount) die "$dir is a mount point with data in it. Move that data elsewhere and run this again." ;;
  esac
done
if ((PULL_MODELS)); then
  echo "  - start brAInharbor and download the models (can take a long time)"
else
  echo "  - start brAInharbor"
fi
echo

if ((!ASSUME_YES)); then
  [[ -t 0 ]] || die "Not asking without a terminal; add --yes to go ahead"
  answer=""
  read -r -p "Type yes to continue: " answer || true
  [[ $answer == yes ]] || die "Cancelled; nothing has changed"
fi

# ---------------------------------------------------------------------------
# The running stack is stopped with the .env it was started with.
if [[ -f $BH_ENV_FILE ]] && [[ -n "$(compose ps -q 2>/dev/null)" ]]; then
  step "Stopping brAInharbor"
  compose down
fi

# .env stays editable by whoever owns the repository, without sudo.
owner="$(stat -c %u "$BH_ROOT/compose.yaml")"
group="$(stat -c %g "$BH_ROOT/compose.yaml")"
if ((KEEP_ENV)); then
  install -m 600 -o "$owner" -g "$group" "$work/config/.env" "$BH_ENV_FILE.from-backup-$stamp"
else
  if [[ -f $BH_ENV_FILE ]] && ! cmp -s "$BH_ENV_FILE" "$env_file"; then
    mv "$BH_ENV_FILE" "$BH_ENV_FILE.before-restore-$stamp"
  fi
  install -m 600 -o "$owner" -g "$group" "$work/config/.env" "$BH_ENV_FILE"
fi

kept=()
for name in webui caddy; do
  dir="${dirs[$name]}"
  step "Restoring ${labels[$name]} into $dir"
  case "$(keep_mode "$dir")" in
    zfs)
      info="$(zfs_dataset "$dir")"
      zfs snapshot "${info%%$'\n'*}@before-restore-$stamp"
      kept+=("${info%%$'\n'*}@before-restore-$stamp")
      # Leave .zfs (visible with snapdir=visible) and anything mounted below alone.
      find "$dir" -mindepth 1 -maxdepth 1 ! -name .zfs -exec rm -rf --one-file-system -- {} +
      ;;
    rename)
      mv "$dir" "$dir.before-restore-$stamp"
      kept+=("$dir.before-restore-$stamp")
      ;;
  esac
  mkdir -p "$dir"
  unpack "$name" "$dir"
done

# ---------------------------------------------------------------------------
step "Starting brAInharbor"
if ! compose up -d --wait --wait-timeout 600; then
  die "The data is restored, but brAInharbor did not start; see the error above. Common causes: a full disk, a GPU in COMPOSE_FILE on a machine without one, ports in use. Fix it, then: docker compose up -d && scripts/pull-models.sh"
fi

if ((PULL_MODELS)); then
  "$BH_ROOT/scripts/pull-models.sh"
  # Models that were installed by hand, outside CHAT_MODELS.
  if [[ -s $work/models.txt ]]; then
    installed="$(compose exec -T ollama ollama list </dev/null | awk 'NR > 1 {print $1}')"
    while read -r model <&3; do
      if [[ -z $model ]] || grep -qxF "$model" <<<"$installed"; then
        continue
      fi
      step "Pulling $model"
      compose exec -T ollama ollama pull "$model" </dev/null ||
        warn "Could not download $model; if it was built from a Modelfile, build it again"
    done 3<"$work/models.txt"
  fi
fi

# ---------------------------------------------------------------------------
echo
step "Restored"
echo "Open https://$(env_get DOMAIN) and log in with an account from the backup."
echo "If this is a new machine, point $(env_get DOMAIN) at it in DNS or /etc/hosts."
echo "Clients that trusted the old Caddy certificate trust this one too."
((PULL_MODELS)) || echo "Download the models when you're ready: scripts/pull-models.sh"
if [[ -f $work/config/local-changes.patch ]]; then
  echo "The backed-up install had local changes to tracked files; they are in"
  echo "config/local-changes.patch in the backup and were not applied."
fi
if ((${#kept[@]})); then
  echo "The data that was replaced is still here, until you remove it:"
  printf '  %s\n' "${kept[@]}"
fi
