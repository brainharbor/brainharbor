#!/usr/bin/env python3
"""Hungarian document search (RAG) test for brAInharbor.

Measures how well Open WebUI's built-in document search finds and answers
from the Hungarian documents in documents/, and which settings help. It
changes global RAG settings and re-indexes, so it runs against a separate,
throwaway Open WebUI (compose.rag-test.yaml), never your own.

  docker compose -f tests/hu/rag/compose.rag-test.yaml up -d
  python3 tests/hu/rag/rag.py setup      # admin account, documents, knowledge base
  python3 tests/hu/rag/rag.py search     # pass 1: search only, no chat model
  python3 tests/hu/rag/rag.py answers    # pass 2: full answers
  python3 tests/hu/rag/rag.py report tests/hu/results/rag/search-20261029-101500

Settings come from configs.toml, questions and the answer key from
questions.toml. Results go to tests/hu/results/rag/. Reports leave out the
holdout questions unless --holdout is given. Python 3.11+, standard library
only.
"""

import argparse
import collections
import difflib
import json
import os
import re
import secrets
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("Python 3.11 or newer is needed (for tomllib).")
import tomllib  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import run as hu  # noqa: E402  (tests/hu/run.py: spelling and number checks)

RESULTS = HERE.parent / "results" / "rag"
STATE_FILE = RESULTS / "state.json"
KB_NAME = "Példa Kft. – RAG-teszt"
SEARCH_K = 10            # chunks asked for in pass 1; hit@k is counted up to this
HIT_AT = (3, 5, 8, 10)
MIN_FREE_GB = 2.0        # stop before a step if less memory than this is left
# An answer that says the documents do not cover the question.
REFUSAL_RE = re.compile(
    r"nem tartalmaz|nem szerepel|nincs (benne|információ|adat|szó|említés)|nem található|nem derül ki"
    r"|nem említ|nem találo?k|nem találtam|nem áll rendelkezés|nem ad (választ|tájékoztatást)"
    r"|nem tér ki|nem részletez|nem rendelkezik (róla|erről)|nem határoz(za)? meg", re.I)
CITATION_RE = re.compile(r"\[\d+\]")
SETTING_KEYS = {
    "embedding_engine", "embedding_model", "chunk_size", "chunk_overlap", "chunk_min_size_target",
    "markdown_header_splitter", "top_k", "hybrid", "bm25_weight", "reranker_engine", "reranker_model",
    "top_k_reranker", "relevance_threshold", "template", "query_generation", "model", "context",
}
INDEX_KEYS = ("embedding_engine", "embedding_model", "chunk_size", "chunk_overlap",
              "chunk_min_size_target", "markdown_header_splitter")


# ---------------------------------------------------------------------------
# Open WebUI API


class WebUI:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.token = None
        # The test instance is local; never send these requests through an HTTP proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, payload=None, timeout=60, body=None, content_type="application/json"):
        if payload is not None:
            body = json.dumps(payload).encode()
        headers = {"Content-Type": content_type} if body is not None else {}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.url + path, data=body, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                data = response.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except (ValueError, AttributeError):
                pass
            raise RuntimeError(f"{method} {path}: HTTP {e.code}: {detail}") from None
        return json.loads(data) if data else None

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, payload=None, **kw):
        return self.call("POST", path, payload if payload is not None else {}, **kw)

    def upload(self, path):
        """Uploads a file and waits until Open WebUI has extracted its text."""
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + \
            f"\r\n--{boundary}--\r\n".encode()
        return self.call("POST", "/api/v1/files/?process=true&process_in_background=false", body=body,
                         content_type=f"multipart/form-data; boundary={boundary}", timeout=300)

    def wait_until_up(self, seconds=180):
        deadline = time.time() + seconds
        while True:
            try:
                if self.get("/health", timeout=5):
                    return
            except (OSError, RuntimeError):
                pass
            if time.time() > deadline:
                sys.exit(f"Open WebUI does not answer at {self.url}. Is the test instance running?\n"
                         f"  docker compose -f tests/hu/rag/compose.rag-test.yaml up -d")
            time.sleep(3)


def load_state():
    return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}


def save_state(state):
    RESULTS.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def connect(args, state):
    ui = WebUI(args.url)
    ui.wait_until_up()
    if not state.get("password"):
        sys.exit("No test account yet: run `rag.py setup` first.")
    ui.token = ui.post("/api/v1/auths/signin", {"email": state["email"], "password": state["password"]})["token"]
    return ui


# ---------------------------------------------------------------------------
# Settings


def load_configs():
    data = tomllib.loads((HERE / "configs.toml").read_text(encoding="utf-8"))
    base = data["base"]
    for section in ("search", "answers"):
        for entry in data.get(section, []):
            unknown = set(entry) - SETTING_KEYS - {"name"}
            if unknown:
                sys.exit(f"configs.toml, {entry.get('name')}: unknown setting(s) {sorted(unknown)}")
    merge = lambda entries: {e["name"]: {**base, **{k: v for k, v in e.items() if k != "name"}} for e in entries}
    return base, merge(data.get("search", [])), merge(data.get("answers", []))


def pick_configs(configs, only):
    if not only:
        return configs
    names = hu.csv_list(only)
    missing = [n for n in names if n not in configs]
    if missing:
        sys.exit(f"Not in configs.toml: {', '.join(missing)}. Known: {', '.join(configs)}")
    return {n: configs[n] for n in names}


def template_text(cfg, state):
    if cfg["template"] == "default":
        return state["default_template"]
    return (HERE / "templates" / cfg["template"]).read_text(encoding="utf-8").strip()


def apply(ui, state, cfg, ollama_url):
    """Puts cfg into effect in Open WebUI; re-indexes when the index would differ. Returns seconds spent indexing."""
    current = ui.get("/api/v1/retrieval/embedding")
    if (current["RAG_EMBEDDING_ENGINE"], current["RAG_EMBEDDING_MODEL"]) != (cfg["embedding_engine"], cfg["embedding_model"]):
        ui.post("/api/v1/retrieval/embedding/update", {
            "RAG_EMBEDDING_ENGINE": cfg["embedding_engine"],
            "RAG_EMBEDDING_MODEL": cfg["embedding_model"],
            "ollama_config": {"url": ollama_url, "key": ""},
        }, timeout=900)  # a sentence-transformers model may be downloaded here
    ui.post("/api/v1/retrieval/config/update", {
        "RAG_TEMPLATE": template_text(cfg, state),
        "TOP_K": cfg["top_k"],
        "RAG_FULL_CONTEXT": False,
        "BYPASS_EMBEDDING_AND_RETRIEVAL": False,
        "ENABLE_RAG_HYBRID_SEARCH": cfg["hybrid"],
        "HYBRID_BM25_WEIGHT": cfg["bm25_weight"],
        "TOP_K_RERANKER": cfg["top_k_reranker"],
        "RELEVANCE_THRESHOLD": cfg["relevance_threshold"],
        "RAG_RERANKING_ENGINE": cfg["reranker_engine"],
        "RAG_RERANKING_MODEL": cfg["reranker_model"],
        "TEXT_SPLITTER": "",
        "ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER": cfg["markdown_header_splitter"],
        "CHUNK_SIZE": cfg["chunk_size"],
        "CHUNK_OVERLAP": cfg["chunk_overlap"],
        "CHUNK_MIN_SIZE_TARGET": cfg["chunk_min_size_target"],
    }, timeout=900)  # a reranker model may be downloaded here
    tasks = ui.get("/api/v1/tasks/config")
    if tasks.get("ENABLE_RETRIEVAL_QUERY_GENERATION") != cfg["query_generation"]:
        ui.post("/api/v1/tasks/config/update", {**tasks, "ENABLE_RETRIEVAL_QUERY_GENERATION": cfg["query_generation"]})
    # Open WebUI does not always refuse what it cannot do: a reranker that fails to
    # load switches hybrid search off without an error. Read the settings back.
    got, emb = ui.get("/api/v1/retrieval/config"), ui.get("/api/v1/retrieval/embedding")
    expected = {"ENABLE_RAG_HYBRID_SEARCH": cfg["hybrid"], "RAG_RERANKING_MODEL": cfg["reranker_model"],
                "CHUNK_SIZE": cfg["chunk_size"], "CHUNK_OVERLAP": cfg["chunk_overlap"], "TOP_K": cfg["top_k"]}
    wrong = {k: got.get(k) for k, v in expected.items() if got.get(k) != v}
    if emb["RAG_EMBEDDING_MODEL"] != cfg["embedding_model"]:
        wrong["RAG_EMBEDDING_MODEL"] = emb["RAG_EMBEDDING_MODEL"]
    if wrong:
        raise RuntimeError(f"Open WebUI kept other values: {wrong}. If a model is named there, it could not be "
                           f"loaded or downloaded: see `docker compose -f tests/hu/rag/compose.rag-test.yaml logs`.")
    index = {k: cfg[k] for k in INDEX_KEYS}
    if state.get("indexed_with") == index:
        return 0.0
    state.pop("indexed_with", None)
    save_state(state)
    started = time.time()
    ui.post("/api/v1/knowledge/reindex", timeout=3600)
    took = time.time() - started
    chunks = count_chunks(ui, state)
    if chunks["missing"]:
        raise RuntimeError(f"Re-indexing left out {', '.join(chunks['missing'])}: the embedding model probably "
                           f"failed. See `docker compose -f tests/hu/rag/compose.rag-test.yaml logs`.")
    state["indexed_with"] = index
    state["chunks"] = chunks["count"]
    save_state(state)
    return took


def count_chunks(ui, state):
    """How many chunks the knowledge base has, and which documents have none (asks for every chunk once)."""
    # Not /query/collection: with hybrid search on, Open WebUI v0.12.4 runs it
    # there even when the request says hybrid: false, and returns top_k_reranker chunks.
    res = ui.post("/api/v1/retrieval/query/doc",
                  {"collection_name": state["kb_id"], "query": "szabályzat", "k": 100000, "hybrid": False},
                  timeout=600)
    names = {(meta or {}).get("name") or (meta or {}).get("source") for meta in res["metadatas"][0]}
    return {"count": len(res["documents"][0]), "missing": sorted(set(state["files"]) - names)}


def ollama_ps(ui):
    """Loaded models and the share of each in VRAM, through Open WebUI's Ollama proxy."""
    try:
        models = ui.get("/ollama/api/ps", timeout=10).get("models") or []
    except (RuntimeError, OSError, ValueError):
        return None
    return [{"name": m.get("name"), "size_gb": round(m.get("size", 0) / 1e9, 2),
             "vram_share": round(m.get("size_vram", 0) / m["size"], 3) if m.get("size") else None} for m in models]


def free_memory_gb():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return None


def check_memory(where):
    free = free_memory_gb()
    if free is not None and free < MIN_FREE_GB:
        sys.exit(f"Only {free:.1f} GB of memory is free before {where}; stopping so the machine does not swap. "
                 f"Close other programs and run the same command again: finished configs are kept.")
    return free


# ---------------------------------------------------------------------------
# Questions and scoring


def load_questions():
    questions = tomllib.loads((HERE / "questions.toml").read_text(encoding="utf-8"))["questions"]
    for q in questions:
        for source in q.get("sources", []):
            source["docs"] = source["doc"] if isinstance(source["doc"], list) else [source["doc"]]
    return {q["id"]: q for q in questions}


def norm(text):
    return re.sub(r"\s+", " ", text).strip().casefold()


def contains_quote(chunk, quote):
    """True if the chunk holds the quote, or most of it when a chunk boundary cut it in two."""
    chunk, quote = norm(chunk), norm(quote)
    if quote in chunk:
        return True
    if len(quote) < 40:
        return False
    match = difflib.SequenceMatcher(None, chunk, quote, autojunk=False).find_longest_match(
        0, len(chunk), 0, len(quote))
    return match.size >= 0.7 * len(quote)


def source_ranks(q, chunks):
    """For each source of the question, the 1-based rank of the first chunk that holds it (None: not found)."""
    return [next((i for i, c in enumerate(chunks, 1) if contains_quote(c["text"], s["quote"])), None)
            for s in q.get("sources", [])]


def hit(q, ranks, k):
    found = [r is not None and r <= k for r in ranks]
    return any(found) if q.get("any_source") else all(found)


def matches(text, entry):
    lowered = text.casefold()
    return any(alt.casefold() in lowered for alt in entry.split("|"))


def grade(q, content):
    """helyes / részben / hibás for answerable questions; for "nincs" questions: kimondta / válaszolt."""
    refused = bool(REFUSAL_RE.search(content))
    forbidden = [f for f in q.get("forbid", []) if matches(content, f)]
    if q["type"] == "nincs":
        return ("kimondta" if refused else "válaszolt"), refused, forbidden, []
    missing = [r for r in q.get("require", []) if not matches(content, r)]
    if forbidden or len(missing) == len(q["require"]):
        verdict = "hibás"
    elif missing:
        verdict = "részben"
    else:
        verdict = "helyes"
    return verdict, refused, forbidden, missing


# ---------------------------------------------------------------------------
# setup


def cmd_setup(args):
    state = load_state()
    ui = WebUI(args.url)
    ui.wait_until_up()
    if state.get("password"):
        ui.token = ui.post("/api/v1/auths/signin", {"email": state["email"], "password": state["password"]})["token"]
    else:
        state = {"email": "ragteszt@example.com", "password": secrets.token_urlsafe(18)}
        try:
            res = ui.post("/api/v1/auths/signup", {"name": "RAG-teszt", "email": state["email"],
                                                    "password": state["password"]})
        except RuntimeError as e:
            sys.exit(f"Could not create the test account ({e}).\nThis instance already has users: start a fresh "
                     f"one with `docker compose -f tests/hu/rag/compose.rag-test.yaml down -v`, then `up -d`.")
        if res.get("role") != "admin":
            sys.exit("The test account did not become admin: this is not a fresh Open WebUI instance.")
        ui.token = res["token"]
        save_state(state)
    print(f"Signed in to {args.url} as {state['email']}")

    if "default_template" not in state:
        state["default_template"] = ui.get("/api/v1/retrieval/config")["RAG_TEMPLATE"]
        save_state(state)

    base, search, answers = load_configs()
    needed = sorted({c["embedding_model"] for c in [*search.values(), *answers.values()]
                     if c["embedding_engine"] == "ollama"} | {c["model"] for c in answers.values()})
    available = {m["name"] for m in ui.get("/ollama/api/tags").get("models", [])}
    missing = [m for m in needed if hu.full_model_name(m) not in available]
    if missing:
        print("Missing in Ollama (add them before `search` / `answers`; see tests/hu/rag/README.md):")
        for m in missing:
            print(f"  {m}")

    # The documents are indexed with the shipped settings as they are added.
    if not state.get("kb_id"):
        state["indexed_with"] = {k: base[k] for k in INDEX_KEYS}
    apply(ui, state, base, args.ollama_url)

    files = state.setdefault("files", {})
    for path in sorted((HERE / "documents").glob("*.md")):
        if path.name not in files:
            files[path.name] = ui.upload(path)["id"]
            save_state(state)
            print(f"  uploaded {path.name}")
    if not state.get("kb_id"):
        state["kb_id"] = ui.post("/api/v1/knowledge/create", {"name": KB_NAME, "description": "tests/hu/rag"})["id"]
        save_state(state)
    in_kb = {f["id"] for f in ui.get(f"/api/v1/knowledge/{state['kb_id']}/files").get("items", [])}
    for name, file_id in files.items():
        if file_id not in in_kb:
            ui.post(f"/api/v1/knowledge/{state['kb_id']}/file/add", {"file_id": file_id}, timeout=300)
            print(f"  added {name} to the knowledge base")
    chunks = count_chunks(ui, state)
    if chunks["missing"]:
        sys.exit(f"Not indexed: {', '.join(chunks['missing'])}. See `docker compose -f "
                 f"tests/hu/rag/compose.rag-test.yaml logs`; is bge-m3 pulled in Ollama?")
    state["chunks"] = chunks["count"]
    save_state(state)

    # Side check: does text come out of PDF and DOCX whole, accents included?
    for path in sorted((HERE / "formats").glob("*")):
        if path.suffix not in (".pdf", ".docx"):
            continue
        file_id = state.setdefault("format_files", {}).get(path.name) or ui.upload(path)["id"]
        state["format_files"][path.name] = file_id
        save_state(state)
        text = ui.get(f"/api/v1/files/{file_id}/data/content").get("content", "")
        source = (HERE / "documents" / (path.stem + ".md")).read_text(encoding="utf-8")
        plain = lambda t: re.sub(r"[#|*\-\s]+", " ", t).strip()
        similarity = difflib.SequenceMatcher(None, plain(source), plain(text), autojunk=False).ratio()
        accents = lambda t: sum(t.count(c) for c in "őűŐŰ")
        print(f"  {path.name}: {len(text)} characters extracted, {similarity:.0%} like the source, "
              f"ő/ű {accents(text)} of {accents(source)}")
    print(f"Ready: {len(files)} documents, {state['chunks']} chunks in the knowledge base. Next: rag.py search")


# ---------------------------------------------------------------------------
# search (pass 1)


def search_one(ui, state, cfg, q):
    payload = {"collection_names": [state["kb_id"]], "query": q["question"], "k": max(SEARCH_K, cfg["top_k"])}
    if cfg["hybrid"]:
        payload.update(hybrid=True, k_reranker=SEARCH_K, hybrid_bm25_weight=cfg["bm25_weight"])
    started = time.time()
    res = ui.post("/api/v1/retrieval/query/collection", payload, timeout=600)
    took = time.time() - started
    chunks = [{"text": text, "doc": (meta or {}).get("name") or (meta or {}).get("source"), "score": score}
              for text, meta, score in zip(res["documents"][0], res["metadatas"][0], res["distances"][0])]
    return chunks[:SEARCH_K], took


def cmd_search(args):
    state = load_state()
    ui = connect(args, state)
    questions = load_questions()
    configs = pick_configs(load_configs()[1], args.only)
    out_dir = args.out or RESULTS / f"search-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    done = {(r["config"], r["id"]) for r in hu.load_results(out_dir)}
    print(f"Pass 1: {len(configs)} configs × {len(questions)} questions -> {out_dir}\n"
          f"(If it stops, the same command with --out {out_dir} continues where it left off.)")
    try:
        run_search(ui, state, configs, questions, out_dir, done, args)
    except KeyboardInterrupt:
        print("\nStopped; the report covers the finished part.")
    write_search_report(out_dir, questions, args.holdout)


def run_search(ui, state, configs, questions, out_dir, done, args):
    with open(out_dir / "results.jsonl", "a", encoding="utf-8") as out:
        for name, cfg in configs.items():
            if all((name, qid) in done for qid in questions):
                continue
            free = check_memory(name)
            try:
                index_s = apply(ui, state, cfg, args.ollama_url)
            except RuntimeError as e:
                print(f"{name}: skipped, the settings could not be applied: {e}")
                continue
            print(f"{name}: indexed in {index_s:.0f} s" if index_s else f"{name}: index unchanged", flush=True)
            for qid, q in questions.items():
                if (name, qid) in done:
                    continue
                try:
                    chunks, took = search_one(ui, state, cfg, q)
                    error = None
                except RuntimeError as e:
                    chunks, took, error = [], None, str(e)
                out.write(json.dumps({"config": name, "id": qid, "settings": cfg, "chunks": chunks, "seconds": took,
                                      "index_seconds": index_s, "index_chunks": state.get("chunks"),
                                      "free_gb": free, "error": error},
                                     ensure_ascii=False) + "\n")
                out.flush()
                index_s = None


def write_search_report(out_dir, questions, holdout):
    records = [r for r in hu.load_results(out_dir) if r["id"] in questions]
    splits = {"tune", "holdout"} if holdout else {"tune"}
    by_config = collections.defaultdict(list)
    for r in records:
        if questions[r["id"]]["split"] in splits:
            by_config[r["config"]].append(r)
    types = ["egyszeru", "tobbreszes", "ekezet", "folytatas", "tablazat"]
    lines = [f"# Pass 1: search\n\nQuestions: {' + '.join(sorted(splits))}. A hit: every source quote of the "
             f"question is in the first k chunks (any one, for questions with any_source).\n"]
    rows, type_rows, misses = [], [], []
    for name, recs in by_config.items():
        answerable = [r for r in recs if questions[r["id"]]["type"] != "nincs"]
        ranks = {r["id"]: source_ranks(questions[r["id"]], r["chunks"]) for r in answerable}
        pct = lambda subset, k: (f"{sum(hit(questions[r['id']], ranks[r['id']], k) for r in subset) / len(subset):.0%}"
                                 if subset else "–")
        index_s = next((r["index_seconds"] for r in recs if r.get("index_seconds")), None)
        rows.append([name, len(answerable), *[pct(answerable, k) for k in HIT_AT],
                     hu.fmt(hu.median([r["seconds"] for r in recs]), 2), hu.fmt(index_s),
                     recs[0].get("index_chunks") or "–", sum(bool(r.get("error")) for r in recs)])
        type_rows.append([name, *[pct([r for r in answerable if questions[r["id"]]["type"] == t], 5) for t in types]])
        none_scores = [r["chunks"][0]["score"] for r in recs if questions[r["id"]]["type"] == "nincs" and r["chunks"]]
        answ_scores = [r["chunks"][0]["score"] for r in answerable if r["chunks"]]
        type_rows[-1].append(f"{hu.fmt(hu.median(answ_scores), 3)} / {hu.fmt(hu.median(none_scores), 3)}")
        for r in answerable:
            if not hit(questions[r["id"]], ranks[r["id"]], 5):
                misses.append(f"- {name} {r['id']}: ranks {ranks[r['id']]} – {questions[r['id']]['question']}")
    lines.append(hu.table(["Config", "Questions", *[f"hit@{k}" for k in HIT_AT], "Search s", "Index s", "Chunks", "Errors"], rows))
    lines.append("\n## hit@5 by question type\n\nLast column: median score of the best chunk, answerable / "
                 "no-answer questions (if these differ a lot, a relevance threshold could work).\n")
    lines.append(hu.table(["Config", *types, "Best score"], type_rows))
    lines.append("\n## Not in the first 5\n")
    lines.extend(misses or ["(none)"])
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:3]))
    print(f"Report: {out_dir / 'summary.md'}")


# ---------------------------------------------------------------------------
# answers (pass 2)


def answer_one(ui, state, cfg, q):
    messages = []
    history = q.get("history", [])
    for i, text in enumerate(history):
        messages.append({"role": "user" if i % 2 == 0 else "assistant", "content": text})
    messages.append({"role": "user", "content": q["question"]})
    if cfg["context"] == "full":
        docs = sorted({d for s in q.get("sources", []) for d in s["docs"]}) or []
        files = [{"type": "file", "id": state["files"][d], "context": "full"} for d in docs]
    else:
        files = [{"type": "collection", "id": state["kb_id"]}]
    payload = {"model": cfg["model"], "messages": messages, "stream": False}
    if files:
        payload["files"] = files
    started = time.time()
    res = ui.post("/api/chat/completions", payload, timeout=900)
    took = time.time() - started
    content = res["choices"][0]["message"]["content"] or ""
    sources = []
    for s in res.get("sources") or []:
        for text, meta in zip(s.get("document", []), s.get("metadata", [])):
            sources.append({"doc": (meta or {}).get("name") or (meta or {}).get("source"), "text": text})
    return {"content": content, "sources": sources, "usage": res.get("usage"), "seconds": took}


def cmd_answers(args):
    state = load_state()
    ui = connect(args, state)
    questions = load_questions()
    configs = pick_configs(load_configs()[2], args.only)
    out_dir = args.out or RESULTS / f"answers-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    done = {(r["config"], r["id"], r["run"]) for r in hu.load_results(out_dir)}
    total = len(configs) * len(questions) * args.runs
    print(f"Pass 2: {len(configs)} configs × {len(questions)} questions × {args.runs} run(s) = {total} answers "
          f"-> {out_dir}\n(If it stops, the same command with --out {out_dir} continues where it left off.)")
    try:
        run_answers(ui, state, configs, questions, out_dir, done, args, total)
    except KeyboardInterrupt:
        print("\nStopped; the report covers the finished part.")
    write_answers_report(out_dir, questions, args.holdout)


def run_answers(ui, state, configs, questions, out_dir, done, args, total):
    count = 0
    with open(out_dir / "results.jsonl", "a", encoding="utf-8") as out:
        for name, cfg in configs.items():
            todo = [(qid, run) for run in range(1, args.runs + 1) for qid in questions if (name, qid, run) not in done]
            count += len(questions) * args.runs - len(todo)
            if not todo:
                continue
            if hu.full_model_name(cfg["model"]) not in {m["name"] for m in ui.get("/ollama/api/tags").get("models", [])}:
                print(f"{name}: skipped, Ollama has no model {cfg['model']} (see README.md)")
                continue
            free = check_memory(name)
            try:
                index_s = apply(ui, state, cfg, args.ollama_url)
            except RuntimeError as e:
                print(f"{name}: skipped, the settings could not be applied: {e}")
                continue
            print(f"{name}: {len(todo)} answers" + (f", indexed in {index_s:.0f} s" if index_s else ""), flush=True)
            for qid, run in todo:
                count += 1
                q = questions[qid]
                record = {"config": name, "id": qid, "run": run, "settings": cfg, "index_seconds": index_s,
                          "free_gb": free}
                try:
                    record.update(answer_one(ui, state, cfg, q))
                except (RuntimeError, OSError, KeyError, IndexError) as e:
                    record.update(content="", sources=[], usage=None, seconds=None, error=str(e))
                record["ollama_ps"] = ollama_ps(ui)
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
                index_s = None
                if count % 10 == 0 or count == total:
                    print(f"  {count}/{total}", flush=True)


def answer_checks(q, record, checker):
    content = hu.THINK_RE.sub("", record.get("content") or "")
    verdict, refused, forbidden, missing = grade(q, content)
    given = "\n".join([q["question"], *q.get("history", []), *(s["text"] for s in record.get("sources") or [])])
    if record["settings"]["context"] == "full":
        given += "\n".join((HERE / "documents" / d).read_text(encoding="utf-8")
                           for s in q.get("sources", []) for d in s["docs"])
    return {
        "verdict": verdict,
        "refused": refused,
        "forbidden": forbidden,
        "missing": missing,
        "new_numbers": sorted(hu.numbers(content) - hu.numbers(given)),
        "cited": bool(CITATION_RE.search(content)),
        "spelling": checker.spelling(content) if checker else None,
        "foreign": [ch for ch in content if hu.is_foreign(ch)],
        "source_found": hit(q, source_ranks(q, [{"text": s["text"]} for s in record.get("sources") or []]), 10 ** 6)
        if q.get("sources") else None,
    }


def write_answers_report(out_dir, questions, holdout):
    records = [r for r in hu.load_results(out_dir) if r["id"] in questions]
    splits = {"tune", "holdout"} if holdout else {"tune"}
    records = [r for r in records if questions[r["id"]]["split"] in splits]
    checker = hu.Checker({})
    checker = checker if checker.hunspell else None
    by_config = collections.defaultdict(list)
    for r in records:
        r["checks"] = answer_checks(questions[r["id"]], r, checker)
        by_config[r["config"]].append(r)
    pct = lambda n, d: f"{n / d:.0%}" if d else "–"
    rows, type_rows = [], []
    types = ["egyszeru", "tobbreszes", "ekezet", "folytatas", "tablazat"]
    for name, recs in by_config.items():
        ans = [r for r in recs if questions[r["id"]]["type"] != "nincs"]
        none = [r for r in recs if questions[r["id"]]["type"] == "nincs"]
        verdicts = collections.Counter(r["checks"]["verdict"] for r in ans)
        usage = [r.get("usage") or {} for r in recs]
        # With full documents, a question without sources gets no document at all.
        full = all((r.get("settings") or {}).get("context") == "full" for r in recs)
        rows.append([
            name, f"{len(ans)} + {len(none)}",
            pct(verdicts["helyes"], len(ans)), pct(verdicts["részben"], len(ans)), pct(verdicts["hibás"], len(ans)),
            pct(sum(r["checks"]["source_found"] for r in ans), len(ans)),
            "–" if full else pct(sum(r["checks"]["verdict"] == "kimondta" for r in none), len(none)),
            sum(bool(r["checks"]["new_numbers"]) for r in recs),
            pct(sum(r["checks"]["cited"] for r in ans), len(ans)),
            "–" if not checker else sum(len(r["checks"]["spelling"] or []) for r in recs),
            hu.fmt(hu.median([r["seconds"] for r in recs]), 1),
            hu.fmt(hu.median([u.get("prompt_eval_count") or u.get("prompt_tokens") for u in usage])),
            sum(bool(r.get("error")) for r in recs),
        ])
        type_rows.append([name, *[pct(sum(r["checks"]["verdict"] == "helyes" for r in ans
                                          if questions[r["id"]]["type"] == t),
                                      sum(questions[r["id"]]["type"] == t for r in ans)) for t in types]])
    lines = [f"# Pass 2: answers\n\nQuestions: {' + '.join(sorted(splits))}. Automatic grading: helyes = every "
             "required fact is in the answer; hibás = none of them, or a forbidden value. Borderline answers "
             "still need reading. Answers: questions with an answer + without one.\n",
             hu.table(["Config", "Answers", "Helyes", "Részben", "Hibás", "Source reached the model",
                       "No-answer questions declined", "Answers with new numbers", "Cited", "Spelling flags",
                       "Median s", "Median prompt tokens", "Errors"], rows),
             "\n## Correct answers by question type\n", hu.table(["Config", *types], type_rows)]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    parts = ["# Answers\n"]
    for qid, q in questions.items():
        recs = [r for r in records if r["id"] == qid]
        if not recs:
            continue
        parts.append(f"## {qid} ({q['type']}): {q['question']}\n")
        if q.get("history"):
            parts.append("Earlier: " + " / ".join(q["history"]) + "\n")
        parts.append("Key: " + ("no answer in the documents" if q["type"] == "nincs" else ", ".join(q["require"]))
                     + "\n")
        for r in recs:
            c = r["checks"]
            notes = [c["verdict"]] + ([f"forbidden: {', '.join(c['forbidden'])}"] if c["forbidden"] else []) + \
                ([f"new numbers: {c['new_numbers']}"] if c["new_numbers"] else []) + \
                ([f"error: {r['error']}"] if r.get("error") else [])
            parts.append(f"**{r['config']}** (run {r['run']}, {hu.fmt(r.get('seconds'), 1)} s): {'; '.join(notes)}\n\n"
                         f"{hu.quote(r.get('content') or '')}\n")
    (out_dir / "answers.md").write_text("\n".join(parts), encoding="utf-8")
    print("\n".join(lines[:2]))
    print(f"Report: {out_dir / 'summary.md'}, every answer: {out_dir / 'answers.md'}")


# ---------------------------------------------------------------------------


def cmd_report(args):
    questions = load_questions()
    out_dir = Path(args.dir)
    if out_dir.name.startswith("search"):
        write_search_report(out_dir, questions, args.holdout)
    else:
        write_answers_report(out_dir, questions, args.holdout)


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=os.environ.get("RAG_TEST_URL", "http://127.0.0.1:3001"),
                    help="the throwaway Open WebUI (default: %(default)s)")
    ap.add_argument("--ollama-url", default="http://ollama:11434",
                    help="Ollama as Open WebUI sees it (default: %(default)s)")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="create the test account, upload the documents, build the knowledge base")
    for name, text in (("search", "pass 1: search only"), ("answers", "pass 2: full answers")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--only", help="comma-separated config names from configs.toml")
        p.add_argument("--out", type=Path, help="continue an interrupted run in this folder")
        p.add_argument("--holdout", action="store_true", help="include the holdout questions in the report")
        if name == "answers":
            p.add_argument("--runs", type=int, default=1, help="answers per question and config (default: 1)")
    p = sub.add_parser("report", help="rewrite the report of a results folder")
    p.add_argument("dir")
    p.add_argument("--holdout", action="store_true", help="include the holdout questions")
    return ap.parse_args()


def main():
    args = parse_args()
    {"setup": cmd_setup, "search": cmd_search, "answers": cmd_answers, "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    main()
