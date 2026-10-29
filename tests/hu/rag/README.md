# Hungarian document search (RAG) test

Open WebUI can answer from uploaded documents: it cuts them into chunks,
finds the chunks closest to the question and pastes them into the prompt.
Its defaults were made for English. This test measures how well that works
with Hungarian documents, and which settings help. It only changes Open
WebUI's own settings and prompt template; there is no separate search
engine.

- `documents/`: 17 made-up documents of a made-up company (Példa Kft., a
  kitchen appliance webshop), about 67,000 characters: policies, a price
  list, warranty and returns, user manuals, an FAQ. Some rules look alike
  on purpose (domestic and foreign daily allowance, the E1 and E2 error
  codes of two appliances), so a search that finds the wrong one shows up.
- `formats/`: one of the documents as .docx and .pdf, to check that text
  extraction keeps ő and ű
- `questions.toml`: 70 questions with the answer key. Each says where the
  answer is (a verbatim quote) and what the answer must and must not
  contain. 12 have no answer in the documents. 22 are holdout: reports
  leave them out until the settings are chosen.
- `configs.toml`: the settings tried; `[base]` is what brAInharbor shipped
  before these tests, `P8-filename-no-query-gen` is what it ships now
- `templates/`: Hungarian RAG prompt templates
- `compose.rag-test.yaml`: a separate Open WebUI for the test
- `rag.py`: the runner (Python 3.11+, standard library only)

`questions.toml` was written before the first run, with MD5
`9cea3a651b16042746bb73494adfac95`. Changes made after results were seen
are listed with their reason at the top of the file; the current MD5 is
`d6e57c1aa3f9da28e86b5e676418c005`.

## A separate Open WebUI

The RAG settings are global, and the test changes them and re-indexes the
documents many times. So it runs against its own Open WebUI on
127.0.0.1:3001 with its own data volume, never the one your team uses. It
borrows the stack's Ollama over the stack's Docker network, so the stack
has to be running.

```sh
docker compose up -d
docker compose -f tests/hu/rag/compose.rag-test.yaml up -d
```

`rag.py setup` creates an admin account in it (`ragteszt@example.com`, the
password is in `tests/hu/results/rag/state.json`), uploads the documents and
builds the knowledge base. To look around, open http://127.0.0.1:3001.

When you're done, remove it with its data:

```sh
docker compose -f tests/hu/rag/compose.rag-test.yaml down -v
rm tests/hu/results/rag/state.json
```

## Models

bge-m3 comes with the stack. The other two embedding models to compare:

```sh
docker compose exec ollama ollama pull qwen3-embedding:0.6b
docker compose exec ollama ollama pull embeddinggemma
```

The reranker (`BAAI/bge-reranker-v2-m3`, 2.3 GB) is downloaded by the test
Open WebUI the first time a config needs it, and runs on the CPU.

To compare chat models fairly, each needs the Hungarian system prompt in
its Modelfile. Open WebUI v0.12.4 doesn't send the `system` value of
`DEFAULT_MODEL_PARAMS`, so a plain `gemma4:26b` would answer without it:

```sh
sed 's|^FROM .*|FROM gemma4:26b|' ollama/brainharbor-hu.Modelfile |
    docker compose exec -T ollama sh -c 'cat > /tmp/Modelfile && ollama create brainharbor-hu-gemma -f /tmp/Modelfile'
```

## Running

```sh
python3 tests/hu/rag/rag.py setup      # account, documents, knowledge base
python3 tests/hu/rag/rag.py search     # pass 1: search only
python3 tests/hu/rag/rag.py answers    # pass 2: full answers
python3 tests/hu/rag/rag.py search --only A1-bge-m3,D1-hybrid
python3 tests/hu/rag/rag.py report tests/hu/results/rag/search-20261029-101500
```

Pass 1 asks every search config in `[[search]]` for the 10 best chunks of
each question and checks whether the source quotes are among them. It needs
no chat model, so it's fast, apart from re-indexing and the reranker.

Pass 2 asks the chat model through Open WebUI, as a user would, with the
configs in `[[answers]]`. A config can set the template, the model, retrieval
query generation, or `context = "full"`: the question's source documents
whole, as an upper bound for what better search could give.

Settings that Open WebUI reads only from the environment can't be changed
through the API. `RAG_SOURCE_METADATA_KEYS` is one: it decides whether the
model sees which file a chunk is from. The configs that need it say so in
`configs.toml`; start the test Open WebUI with it before running them:

```sh
RAG_SOURCE_METADATA_KEYS=source docker compose -f tests/hu/rag/compose.rag-test.yaml up -d
python3 tests/hu/rag/rag.py answers --only P7-filename,P8-filename-no-query-gen --runs 3
```

Ctrl-C stops a pass and still writes the report. The run prints its results
folder; the same command with `--out <folder>` continues where it stopped.
Every config is applied, then read back from Open WebUI: if Open WebUI kept
other values, or a model can't be loaded, the config is skipped with a
message instead of measuring the wrong thing.

The runner stops when the machine has less than 2 GB of free memory left.

## Results

Each pass writes `tests/hu/results/rag/<pass>-<timestamp>/`:

- `summary.md`: pass 1: hit@3/5/8/10 per config, by question type, and the
  questions whose source was missed; pass 2: correct, partly correct and
  wrong answers, declined no-answer questions, invented numbers, citations,
  spelling, speed and prompt size
- `answers.md` (pass 2): every answer under its question, with its checks
- `results.jsonl`: raw data, one line per question and config

Reports cover the tuning questions only. Add `--holdout` once, at the end,
to check the chosen settings on the questions nothing was tuned on.

The automatic grading only looks for the required facts and the forbidden
values. It doesn't judge whether the answer reads well or is right in every
detail: read `answers.md`.

## Adding questions

A question needs an `id`, `type`, `split`, `question`, and `sources` unless
its type is `nincs`; `require`, `forbid`, `history` and `note` are optional.
The header of `questions.toml` explains the fields. In `require`, use word
stems where a suffix changes the word: `24 ór` matches "24 órán", `24 óra`
doesn't. Keep ids stable, and add new questions instead of changing old
ones, so results from different days can be compared.
