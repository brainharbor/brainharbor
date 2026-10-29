# brAInharbor

**English** · [Magyar](README.hu.md)

A private AI assistant for small teams, running on your own hardware.
Chat with local language models and search your own documents, without
sending anything to a cloud service.

> Status: **v0.1.0**, the first release. Things may still change in
> incompatible ways before 1.0; read the [changelog](CHANGELOG.md) before
> upgrading.

## What's inside

| Component | Role |
| --- | --- |
| [Ollama](https://github.com/ollama/ollama) | Runs the language and embedding models, on the GPU when available |
| [Open WebUI](https://github.com/open-webui/open-webui) | Chat interface, user accounts, document upload and search (RAG) |
| [Caddy](https://caddyserver.com) | HTTPS in front of everything; the only service exposed on the host |
| `ollama/` | `brainharbor-hu`: qwen3.5:35b-a3b with the Hungarian system prompt and settings that tested best |
| `open-webui/` | Open WebUI defaults: default model, thinking off, Hungarian system prompt |
| `scripts/` | Pre-flight checks, model downloads, backup and restore |
| `tests/hu/` | Hungarian language tests: same questions to every model, mistakes counted |

Versions are pinned in `.env.example` and only changed together with a test run.

## Requirements

- Linux with Docker Engine and the Docker Compose plugin
- 32 GB RAM recommended: the default model is a 22 GB download, tested with
  a 12 GB GPU and 32 GB of RAM. With less memory, build it on gemma4:26b
  (18 GB) instead; see `.env.example`
- Optional but strongly recommended: an NVIDIA GPU with 8 GB+ VRAM,
  a recent driver (570+ for RTX 50-series) and the NVIDIA Container Toolkit
- Free disk: about 20 GB for the container images (in Docker's folder,
  usually on `/`) and about 50 GB for models

## Quick start

```sh
git clone https://github.com/brainharbor/brainharbor.git
cd brainharbor
cp .env.example .env
# edit .env: set DOMAIN and WEBUI_SECRET_KEY (openssl rand -hex 32)

scripts/preflight.sh            # checks the host; add --gpu-test for a GPU smoke test
docker compose up -d
scripts/pull-models.sh          # downloads the models, builds brainharbor-hu
```

Open `https://<DOMAIN>` and create the first account; it becomes the administrator.
Later sign-ups wait for the administrator's approval.

With `CADDY_TLS=internal`, browsers will warn until they trust Caddy's local
root certificate. Export it with:

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./brainharbor-root.crt
```

and import it on client machines as a trusted root (certificate authority).
Firefox keeps its own list: Settings > Privacy & Security > Certificates >
View Certificates > Authorities > Import, then tick "Trust this CA to
identify websites". Don't just accept the browser's warning instead: Caddy
renews its local certificates every 12 hours, and the warning comes back.

## Hungarian defaults

Out of the box, new chats use `brainharbor-hu`: qwen3.5:35b-a3b with a
Hungarian system prompt, temperature 0.3 and thinking turned off. In our
tests (13 tasks, 208 answers) it made fewer mistakes than gemma4:26b and
answered faster, and thinking only made answers slower. It still makes
some, and they can sound plausible: an invented date, or a bigger promise
than asked. Have a person read what goes out, especially numbers, dates
and offers.

- `ollama/brainharbor-hu.Modelfile` holds the model, the system prompt and
  the temperature. `scripts/pull-models.sh` builds it.
- `open-webui/defaults.env` makes it the default model, turns thinking off
  and sets the same system prompt in Open WebUI. It also switches Open
  WebUI to legacy function calling: in its default (native) mode, the
  descriptions of Open WebUI's built-in tools add about 6000 tokens to
  every message, and the answer starts seconds later.
- Document search (RAG): `compose.yaml` shows the model which file each
  search result comes from, and `open-webui/defaults.env` turns off the
  rewriting of questions into search queries. In our test with Hungarian
  company documents ([tests/hu/rag](tests/hu/rag/README.md)) this gave 52
  correct answers of 54 instead of 50, and answers came 40% faster. The
  model sees the file names, so name documents after what is in them:
  `belfoldi-kikuldetes.pdf`, not `scan_0012.pdf`.

## Storage

All data lives in `data/` next to `compose.yaml`: models, Open WebUI's
database and uploads, and Caddy's certificates. On ZFS, see
[docs/zfs.md](docs/zfs.md) for a dataset layout with suitable settings,
and how to move an existing install onto it.

## Backups

```sh
sudo scripts/backup.sh                 # one archive in backups/: data, certificates, .env
sudo scripts/restore.sh backups/brainharbor-YYYYMMDD-HHMMSS.tar.gz
```

The archive holds everything except the models, which are downloaded
again on restore. Nightly runs, offsite copies, ZFS snapshots and moving to
a new machine: [docs/backup.md](docs/backup.md).

## Good to know

- Open WebUI stores most settings in its own database after the first start.
  Changing the related `.env` values or `open-webui/defaults.env` later has
  no effect; change them in the admin panel instead.
- Open WebUI is used unmodified and keeps its own branding, as its
  [license](https://docs.openwebui.com/license/) requires.

## Testing Hungarian output

Small local models make mistakes in Hungarian, and they differ a lot.
`tests/hu/run.py` asks each model the same questions several times, measures
speed, and flags spelling errors, foreign characters and known mistakes.
See [tests/hu/README.md](tests/hu/README.md).

## Done for v0.1

- [x] Compose stack with GPU support and HTTPS
- [x] Pre-flight check script
- [x] Hungarian language test suite
- [x] ZFS-friendly data layout and documentation
- [x] Backup and restore scripts, tested on a clean machine
- [x] README in Hungarian and English
- [x] CI: shellcheck, compose validation, system prompt kept in sync

## License

MIT, see [LICENSE](LICENSE). Ollama, Open WebUI, Caddy and the models
keep their own licenses.
