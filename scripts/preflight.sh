#!/usr/bin/env bash
# preflight.sh: checks whether this host is ready to run brAInharbor.
# It only reads; it never changes the system.
#
# Usage: scripts/preflight.sh [--gpu-test]
#   --gpu-test  also starts a throwaway container to prove Docker can use the GPU
#
# Exit code: 0 if no check failed (warnings allowed), 1 otherwise.

set -uo pipefail

# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

GPU_TEST=0
for arg in "$@"; do
  case "$arg" in
    --gpu-test) GPU_TEST=1 ;;
    -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

FAILS=0
WARNS=0
pass()    { printf '  %sPASS%s  %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn()    { printf '  %sWARN%s  %s\n' "$C_YELLOW" "$C_RESET" "$*"; WARNS=$((WARNS + 1)); }
fail()    { printf '  %sFAIL%s  %s\n' "$C_RED" "$C_RESET" "$*"; FAILS=$((FAILS + 1)); }
info()    { printf '        %s\n' "$*"; }
section() { printf '\n%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"; }

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
section "System"

if [[ "$(uname -s)" == Linux ]]; then
  pass "Linux $(uname -r) ($(uname -m))"
else
  fail "Only Linux hosts are supported (found $(uname -s))"
fi

mem_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
mem_gib=$((mem_kib / 1024 / 1024))
# Thresholds are slightly below 16/8 GiB because MemTotal excludes kernel-reserved memory.
if ((mem_gib >= 15)); then
  pass "RAM: ${mem_gib} GiB"
elif ((mem_gib >= 7)); then
  warn "RAM: ${mem_gib} GiB (16 GiB or more recommended)"
else
  fail "RAM: ${mem_gib} GiB (at least 8 GiB needed)"
fi

pass "CPU: $(nproc) threads"

# ---------------------------------------------------------------------------
section "Docker"

docker_ok=0
if ! have docker; then
  fail "docker is not installed (https://docs.docker.com/engine/install/)"
elif ! docker info >/dev/null 2>&1; then
  fail "Cannot talk to the Docker daemon (is it running? is this user in the 'docker' group?)"
else
  docker_ok=1
  pass "Docker Engine $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
  if compose_version="$(docker compose version --short 2>/dev/null)"; then
    pass "Docker Compose ${compose_version}"
  else
    fail "Docker Compose v2 plugin missing (the 'docker compose' command)"
  fi
fi

# ---------------------------------------------------------------------------
section "Configuration (.env)"

if [[ ! -f "$BH_ENV_FILE" ]]; then
  fail ".env not found: cp .env.example .env, then edit it"
else
  pass ".env found"

  domain="$(env_get DOMAIN)"
  if [[ -z $domain ]]; then
    fail "DOMAIN is empty"
  else
    pass "DOMAIN=${domain}"
  fi

  secret="$(env_get WEBUI_SECRET_KEY)"
  if [[ -z $secret || $secret == change-me ]]; then
    fail "WEBUI_SECRET_KEY is not set; generate one with: openssl rand -hex 32"
  elif ((${#secret} < 32)); then
    warn "WEBUI_SECRET_KEY is short (${#secret} chars); 32+ recommended"
  else
    pass "WEBUI_SECRET_KEY is set"
  fi

  tls="$(env_get CADDY_TLS internal)"
  if [[ $tls == internal ]]; then
    pass "CADDY_TLS=internal (Caddy's local CA; clients must trust its root certificate)"
  elif [[ $tls == *@*.* ]]; then
    pass "CADDY_TLS=${tls} (public Let's Encrypt certificate)"
  else
    fail "CADDY_TLS must be 'internal' or an email address (found '${tls}')"
  fi

  if ((docker_ok)); then
    if compose_err="$(compose config --quiet 2>&1)"; then
      pass "docker compose config is valid"
    else
      fail "docker compose config failed:"
      info "$compose_err"
    fi
  fi
fi

# ---------------------------------------------------------------------------
section "GPU"

compose_files="$(env_get COMPOSE_FILE compose.yaml)"
gpu_wanted=0
[[ $compose_files == *compose.gpu.yaml* ]] && gpu_wanted=1

if ! have nvidia-smi; then
  if ((gpu_wanted)); then
    fail "compose.gpu.yaml is enabled but nvidia-smi was not found (NVIDIA driver missing?)"
  else
    warn "No NVIDIA GPU tools found; running on CPU only (slow for anything above ~4B models)"
  fi
else
  while IFS=',' read -r name driver vram_mib cap; do
    name="${name# }" driver="${driver# }" vram_mib="${vram_mib# }" cap="${cap# }"
    driver_major="${driver%%.*}"
    vram_gib=$((vram_mib / 1024))
    pass "GPU: ${name}, ${vram_gib} GiB VRAM, driver ${driver}, compute ${cap}"

    # Blackwell (RTX 50xx, compute capability 12.x) needs driver 570 or newer.
    cap_major="${cap%%.*}"
    if [[ $cap_major =~ ^[0-9]+$ ]] && ((cap_major >= 12)) && ((driver_major < 570)); then
      fail "Driver ${driver} is too old for this GPU; Blackwell cards need 570+ (open kernel modules)"
    elif ((driver_major < 535)); then
      warn "Driver ${driver} is old; 535 or newer recommended"
    fi

    if ((vram_gib >= 20)); then
      info "Fits ~27–32B models fully on the GPU at 4-bit."
    elif ((vram_gib >= 11)); then
      info "Fits ~12–14B models fully on the GPU at 4-bit (e.g. gemma3:12b)."
    elif ((vram_gib >= 7)); then
      info "Fits ~7–9B models fully on the GPU at 4-bit."
    else
      warn "Under 8 GiB VRAM: stick to ~4B models or expect CPU offload"
    fi
  done < <(nvidia-smi --query-gpu=name,driver_version,memory.total,compute_cap --format=csv,noheader,nounits 2>/dev/null)

  if ((docker_ok)); then
    runtimes="$(docker info --format '{{json .Runtimes}}' 2>/dev/null)"
    if [[ $runtimes == *nvidia* ]] || have nvidia-ctk; then
      pass "NVIDIA Container Toolkit found"
    else
      fail "NVIDIA Container Toolkit not found (https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)"
    fi

    if ((GPU_TEST)); then
      if out="$(docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L 2>&1)"; then
        pass "Container GPU test: ${out%%$'\n'*}"
      else
        fail "Container GPU test failed:"
        info "$out"
      fi
    else
      info "Run with --gpu-test to start a test container on the GPU."
    fi
  fi
fi

# ---------------------------------------------------------------------------
section "Storage"

# check_dir LABEL ENV_KEY DEFAULT MIN_GIB
check_dir() {
  local label="$1" dir probe free_gib fstype source
  dir="$(data_dir "$2" "$3")"
  probe="$dir"
  # The directory may not exist yet; measure the closest existing parent.
  while [[ ! -d $probe ]]; do probe="$(dirname "$probe")"; done

  free_gib=$(($(df -Pk "$probe" | awk 'NR==2 {print $4}') / 1024 / 1024))
  fstype="$(df -PT "$probe" | awk 'NR==2 {print $2}')"
  source="$(df -P "$probe" | awk 'NR==2 {print $1}')"

  if ((free_gib >= $4)); then
    pass "${label}: ${dir} (${fstype}, ${free_gib} GiB free)"
  elif ((free_gib >= $4 / 2)); then
    warn "${label}: ${dir} has only ${free_gib} GiB free (${4}+ recommended)"
  else
    fail "${label}: ${dir} has only ${free_gib} GiB free (${4}+ needed)"
  fi
  [[ -d $dir ]] || info "Does not exist yet; Docker will create it on first start."

  if [[ $fstype == zfs ]] && have zfs; then
    info "ZFS dataset ${source}: recordsize=$(zfs get -H -o value recordsize "$source" 2>/dev/null)," \
      "compression=$(zfs get -H -o value compression "$source" 2>/dev/null)"
  fi
}

check_dir "Models" MODELS_DIR ./data/models 50
check_dir "Web UI data" WEBUI_DATA_DIR ./data/webui 10
check_dir "Caddy data" CADDY_DATA_DIR ./data/caddy 1

# ---------------------------------------------------------------------------
section "Network"

for key in HTTP_PORT HTTPS_PORT; do
  port="$(env_get "$key" "$([[ $key == HTTP_PORT ]] && echo 80 || echo 443)")"
  listeners=""
  have ss && listeners="$(ss -ltnH "sport = :${port}" 2>/dev/null)"
  if [[ -n $listeners ]]; then
    if ((docker_ok)) && [[ "$(compose ps --status running --services 2>/dev/null)" == *caddy* ]]; then
      pass "Port ${port} is used by brAInharbor's own Caddy"
    else
      fail "Port ${port} is already in use; free it or change ${key} in .env"
    fi
  else
    pass "Port ${port} is free"
  fi
done

domain="$(env_get DOMAIN)"
if [[ -n $domain ]]; then
  resolved="$(getent hosts "$domain" 2>/dev/null)"
  resolved="${resolved%%[[:space:]]*}"
  if [[ -n $resolved ]]; then
    pass "${domain} resolves to ${resolved}"
  elif [[ "$(env_get CADDY_TLS internal)" == internal ]]; then
    warn "${domain} does not resolve here; add it to local DNS or /etc/hosts on clients"
  else
    fail "${domain} does not resolve; a public certificate needs a public DNS record"
  fi
fi

if have curl; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 https://registry.ollama.ai/v2/ || true)"
  if [[ $code != 000 ]]; then
    pass "Ollama model registry reachable (needed for scripts/pull-models.sh)"
  else
    warn "Cannot reach registry.ollama.ai; model downloads will fail"
  fi
else
  warn "curl not installed; skipped the internet check"
fi

# ---------------------------------------------------------------------------
section "Summary"

if ((FAILS > 0)); then
  printf '  %s%d failed%s, %d warning(s). Fix the failures, then run this again.\n' \
    "$C_RED" "$FAILS" "$C_RESET" "$WARNS"
  exit 1
fi
printf '  %sReady%s, %d warning(s). Next: docker compose up -d && scripts/pull-models.sh\n' \
  "$C_GREEN" "$C_RESET" "$WARNS"
