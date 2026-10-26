# brAInharbor

A private AI assistant for small teams, running on your own hardware.
Chat with local language models and search your own documents, without
sending anything to a cloud service.

> Status: **v0.1 in progress.** Expect breaking changes until the first tagged release.

## What's inside

| Component | Role |
| --- | --- |
| [Ollama](https://github.com/ollama/ollama) | Runs the language and embedding models, on the GPU when available |
| [Open WebUI](https://github.com/open-webui/open-webui) | Chat interface, user accounts, document upload and search (RAG) |
| [Caddy](https://caddyserver.com) | HTTPS in front of everything; the only service exposed on the host |
| `scripts/` | Pre-flight checks, model downloads, and (soon) backup and restore |
| `tests/hu/` | Hungarian language tests: same questions to every model, mistakes counted |

Versions are pinned in `.env.example` and only changed together with a test run.

## Requirements

- Linux with Docker Engine and the Docker Compose plugin
- 16 GB RAM or more
- Optional but strongly recommended: an NVIDIA GPU with 8 GB+ VRAM,
  a recent driver (570+ for RTX 50-series) and the NVIDIA Container Toolkit
- ~50 GB free disk for models

## Quick start

```sh
git clone https://github.com/brainharbor/brainharbor.git
cd brainharbor
cp .env.example .env
# edit .env: set DOMAIN and WEBUI_SECRET_KEY (openssl rand -hex 32)

scripts/preflight.sh            # checks the host; add --gpu-test for a GPU smoke test
docker compose up -d
scripts/pull-models.sh          # downloads the models listed in .env
```

Open `https://<DOMAIN>` and create the first account; it becomes the administrator.
Later sign-ups wait for the administrator's approval.

With `CADDY_TLS=internal`, browsers will warn until they trust Caddy's local
root certificate. Export it with:

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./brainharbor-root.crt
```

and import it on client machines.

## Good to know

- Open WebUI stores most settings in its own database after the first start.
  Changing the related `.env` values later has no effect; change them in
  the admin panel instead.
- Open WebUI is used unmodified and keeps its own branding, as its
  [license](https://docs.openwebui.com/license/) requires.

## Testing Hungarian output

Small local models make mistakes in Hungarian, and they differ a lot.
`tests/hu/run.py` asks each model the same questions several times, measures
speed, and flags spelling errors, foreign characters and known mistakes.
See [tests/hu/README.md](tests/hu/README.md).

## Roadmap to v0.1

- [x] Compose stack with GPU support and HTTPS
- [x] Pre-flight check script
- [x] Hungarian language test suite
- [ ] ZFS-friendly data layout and documentation
- [ ] Backup and restore scripts, tested on a clean machine
- [ ] Hungarian and English documentation
- [ ] CI: shellcheck, compose validation

## License

MIT, see [LICENSE](LICENSE). Bundled components keep their own licenses.
