# Changelog

## v0.2.0 (2026-10-30)

- **Open WebUI v0.12.5** instead of v0.12.4: security fixes. Existing
  installs update with `docker compose pull open-webui` and
  `docker compose up -d`. It has no database changes, so going back only
  needs `OPEN_WEBUI_VERSION=v0.12.4` in `.env`.
- **New default model:** `brainharbor-hu` is now built on qwen3.5:35b-a3b
  instead of gemma4:26b. In a deeper Hungarian test (13 tasks, 208 answers)
  it made fewer mistakes and answered faster. It is a 22 GB download, tested
  with a 12 GB GPU and 32 GB of RAM.
  - Existing installs switch on the next `scripts/pull-models.sh`, which
    downloads it.
  - To keep gemma4:26b, or on a machine with less memory, set
    `HU_BASE_MODEL=gemma4:26b` and `CHAT_MODELS=gemma4:26b` in `.env`, then
    run `scripts/pull-models.sh`.
  - `scripts/preflight.sh` now recommends 32 GB of RAM for the default model.
- **Document search (RAG):** the model now sees which file each search
  result comes from, and Open WebUI no longer rewrites the question into
  search queries first. In `tests/hu/rag` (17 Hungarian company documents,
  3 runs per question) the held-out questions got 52 correct answers of 54
  instead of 50, and the median answer time went from 4.2 s to 2.5 s.
  Without the file names, the model mixed up similar rules from different
  documents, such as the domestic and the foreign travel allowance.
  - Existing installs get the file names after `docker compose up -d`.
  - Query rewriting is turned off on new installs only. On an existing
    one, turn off Retrieval Query Generation under Admin Panel > Settings >
    Interface.
- **`tests/hu`:** seven new cases (proofreading, data extraction, questions
  about a policy document, a discount reply), a check for numbers that aren't
  in the prompt, and long documents in prompts (`document_file`).
- **`tests/hu/rag`:** a test of Open WebUI's document search (RAG) with
  Hungarian documents: 17 made-up company documents, 70 questions with an
  answer key, and a runner that compares embedding models, chunk sizes,
  hybrid search, a reranker and prompt templates on a separate, throwaway
  Open WebUI.

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
