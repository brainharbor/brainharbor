# Hungarian language tests

Small local models write uneven Hungarian. One answer in the chat window
proves little, so this suite asks every model the same questions several
times and counts what went wrong.

- `suite.toml`: test cases, prompt variants, known mistakes and run defaults
- `prompts/`: system prompts used by the variants
- `run.py`: the runner (Python 3.11+, standard library only)
- `results/`: one folder per run, not committed

## Setup

The runner talks to Ollama directly, so its API has to be reachable from
the host. In `.env`:

```sh
COMPOSE_FILE=compose.yaml:compose.gpu.yaml:compose.ollama-api.yaml
```

then `docker compose up -d`. The API listens on 127.0.0.1 only. If another
Ollama already uses port 11434 on this host, set `OLLAMA_API_PORT` to
something else; `scripts/preflight.sh` reports the clash.

The runner needs Python 3.11 or newer. On older systems (Ubuntu 22.04 has
3.10), run it in a container; the spelling check is off there:

```sh
docker run --rm -it --network host -u "$(id -u):$(id -g)" -v "$PWD:/work" -w /work \
    python:3.13-slim python tests/hu/run.py --dry-run
```

Optional but recommended: Hunspell with the Hungarian dictionary enables
the spelling check.

```sh
sudo pacman -S hunspell hunspell-hu      # Arch, EndeavourOS
sudo apt install hunspell hunspell-hu    # Debian, Ubuntu
```

## Running

```sh
python3 tests/hu/run.py --dry-run        # checks the setup, prints the plan
python3 tests/hu/run.py                  # defaults from [run] in suite.toml
python3 tests/hu/run.py --models gemma4:12b,gemma4:26b \
    --variants hu-short,hu-example,hu-example-nothink --runs 3
python3 tests/hu/run.py --cases delay-letter,translation --runs 5 --order shuffle
```

Ctrl-C stops the run and still writes the report for the finished answers.

Order (`--order`):

- `grouped` (default): one model at a time. On a single GPU this is the
  fastest, because each model is loaded once.
- `interleaved`: the models take turns on every request. Fair against slow
  drift (heat, other load on the machine), but models that don't fit in
  memory together are reloaded every time.
- `shuffle`: random order, repeatable with the same `--seed`.

Order doesn't change the answers: every request stands alone, without chat
history. Run N always uses seed `--seed + N - 1`, so the same run of two
models gets the same seed.

## Results

Each run writes `tests/hu/results/<timestamp>/`:

- `summary.md`: speed (tokens/s, response time, share of the model in VRAM)
  and automatic checks per model and variant, problems by case, and the
  words the spelling check flagged
- `answers.md`: every answer under its question, with its flags, for reading
- `results.jsonl`: raw data, one request per line
- `meta.json`: settings, Ollama version, GPU, and the exact prompts used

The automatic checks:

| Check | Catches | Misses |
| --- | --- | --- |
| Spelling | made-up words ("termégről"), English words | real words in the wrong place ("késedelmééről" is a valid form) |
| Foreign | Chinese and other scripts, õ/û instead of ő/ű | |
| Known | mistakes found earlier, listed under `[checks] avoid` | new ones |
| Missing | facts from the prompt left out (date, place, offer) | facts that are wrong |
| New numbers | invented dates, amounts, order numbers (cases with `check_numbers`) | numbers written as words; correct calculations show up too unless listed in `allow_numbers` |
| Copied | answers lifted from the style example | |

They don't judge style or meaning: read `answers.md`. When you find a new
recurring mistake, add it to `avoid`, then rebuild an earlier report. The
known mistakes and required facts come from the current suite file:

```sh
python3 tests/hu/run.py --report tests/hu/results/20261026-201500
```

Prompt tokens here are much fewer than in Open WebUI, which also sends the
chat history and its tool descriptions. That makes the numbers comparable
between models, not equal to what users see in the chat window.

## Checking brainharbor-hu

The `modelfile` variant sends no system prompt or temperature, so the model
uses its own from the Modelfile. With the same seed, `brainharbor-hu` should
answer like `gemma4:26b` with `hu-v3-nothink`, with the same prompt token
counts. In the first check (2026-10-26) 14 of 18 answers were identical word
for word; the other 4 were all in run 1, so most likely Ollama's prompt
cache, still holding earlier requests, nudged the numbers. Different prompt
token counts, or differences in every run, mean the Modelfile and
`prompts/hu-v3.txt` have drifted apart. `modelfile-16k` does the same with a
16k context, to measure what that costs in speed.

```sh
python3 tests/hu/run.py --models brainharbor-hu --variants modelfile,modelfile-16k
```

## Adding cases and variants

A case needs an `id` and a `prompt`; the rest is optional:

- `require`: facts the answer must contain; `"a|b"` accepts either form
- `avoid`: known mistakes for this case only, on top of `[checks] avoid`
- `check_numbers = true`: flag every number in the answer that isn't in the
  prompt or the system prompt. List numbers, minutes ("9:00") and thousands
  separators ("40 000") are handled; correct derived numbers, such as a
  computed discount, go in `allow_numbers`.
- `document_file`: a text file, relative to `suite.toml`, pasted into the
  prompt where it says `{document}`. `answers.md` shows the file name
  instead of the whole text.

A variant can set `system_file`, Ollama `options` (temperature, num_ctx,
top_p, ...) and `think`. Keep case ids stable, so results from different
days can be compared.
