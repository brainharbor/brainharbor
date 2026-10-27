# Changelog

## v0.1.0 (2026-10-27)

First release: a private AI assistant for a small team on one Linux
machine, with Hungarian defaults.

- **Stack:** Docker Compose with Ollama 0.40.9, Open WebUI v0.12.4 and
  Caddy 2.11.7. HTTPS comes from Caddy's local CA, or from Let's Encrypt.
  NVIDIA GPU support is optional (`compose.gpu.yaml`).
- **`scripts/preflight.sh`** checks the host before the first start:
  - Docker, GPU driver and container toolkit
  - free disk for images, models and data
  - ports, DNS and `.env`
- **`scripts/pull-models.sh`** downloads the models and builds
  `brainharbor-hu`.
- **Hungarian defaults:**
  - `brainharbor-hu` is gemma4:26b with a Hungarian system prompt and
    temperature 0.3.
  - Open WebUI starts with it as the default model, thinking off, legacy
    function calling and a Hungarian interface.
- **`tests/hu`:** a scripted, reproducible Hungarian test suite. It
  compares models and prompts and counts spelling mistakes, foreign
  characters and known errors.
- **ZFS:** a dataset layout with suitable settings, and how to move an
  existing install onto it (`docs/zfs.md`).
- **Backup and restore:**
  - `scripts/backup.sh` makes one archive. On ZFS it works from a snapshot,
    with no downtime.
  - `scripts/restore.sh` restores it on the same machine or a new one.
  - Tested with ZFS, and on an empty Debian 13 VM (`docs/backup.md`).
- **CI:**
  - shellcheck
  - compose file validation
  - a check that the system prompt is the same in every file

Known limitations:

- The detailed guides (`docs/`, `tests/hu/README.md`) are in English only.
- `restore.sh` downloading the models after a restore has not been tested
  end to end yet.
- The default model still makes mistakes in Hungarian. Have a person read
  what goes out.
