#!/usr/bin/env python3
"""Hungarian language tests for brAInharbor.

Sends every test case in suite.toml to every chosen model and prompt variant,
several times, through the Ollama API. Answers, speed metrics and automatic
checks go to tests/hu/results/<timestamp>/: results.jsonl (raw data),
summary.md (tables) and answers.md (every answer, for reading).

  python3 tests/hu/run.py --dry-run
  python3 tests/hu/run.py --models gemma4:12b,gemma4:26b --variants hu-short,hu-example --runs 3
  python3 tests/hu/run.py --report tests/hu/results/20261026-201500

Needs Python 3.11+ (standard library only) and the Ollama API reachable from
this host; see compose.ollama-api.yaml. With Hunspell and its Hungarian
dictionary installed, answers are spell-checked too.
"""

import argparse
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("Python 3.11 or newer is needed (for tomllib).")
import tomllib  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ORDERS = ("grouped", "interleaved", "shuffle")
METRICS = ("total_duration", "load_duration", "prompt_eval_count", "prompt_eval_duration",
           "eval_count", "eval_duration", "done_reason")

THINK_RE = re.compile(r"<think>.*?</think>", re.S)
PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{1,80}\]")
URL_RE = re.compile(r"https?://\S+|[\w.+-]+@[\w-]+\.[\w.-]+")
HU_LETTERS = set("áéíóöőúüűÁÉÍÓÖŐÚÜŰ")
LIST_NUMBER_RE = re.compile(r"^\s*\d{1,2}[.)]\s", re.M)  # not "2026. november" at a line start
THOUSANDS_RE = re.compile(r"(?<=\d)[   .](?=\d{3}\b)")  # 40 000 or 40.000 -> 40000
MINUTES_RE = re.compile(r"(?<![\d.])(\d{1,2})[:.]\d{2}\b(?!\.\d)")  # 9:00 or 9.00 -> 9, not 2026.11.21


def env_get(key, default=""):
    """Reads KEY from the repository's .env without executing it; the last one wins."""
    value = ""
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            name, sep, rest = line.strip().partition("=")
            if sep and name == key:
                value = rest.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return value or default


def csv_list(text):
    return [item for item in re.split(r"[,\s]+", text) if item]


def full_model_name(name):
    """Ollama lists "gemma4" as "gemma4:latest"."""
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


# ---------------------------------------------------------------------------
# Suite


def load_suite(path):
    with open(path, "rb") as f:
        suite = tomllib.load(f)
    for variant in suite.get("variants", {}).values():
        if "system_file" in variant:
            variant["system"] = (path.parent / variant["system_file"]).read_text(encoding="utf-8").strip()
        variant.setdefault("system", "")
        variant.setdefault("options", {})
    cases = {}
    for case in suite.get("cases", []):
        if case["id"] in cases:
            sys.exit(f"Duplicate case id in {path}: {case['id']}")
        prompt = case["prompt"].strip()
        if "document_file" in case:
            if "{document}" not in prompt:
                sys.exit(f"Case {case['id']} in {path}: document_file needs {{document}} in the prompt")
            document = (path.parent / case["document_file"]).read_text(encoding="utf-8").strip()
            # answers.md shows the file name instead of the whole document.
            case["prompt_shown"] = prompt.replace("{document}", f"[{case['document_file']}]")
            prompt = prompt.replace("{document}", document)
        case["prompt"] = prompt
        cases[case["id"]] = case
    suite["cases"] = cases
    suite.setdefault("variants", {})
    suite.setdefault("run", {})
    suite.setdefault("checks", {})
    return suite


def pick(requested, available, kind):
    unknown = [name for name in requested if name not in available]
    if unknown:
        sys.exit(f"Unknown {kind}: {', '.join(unknown)} (defined: {', '.join(available)})")
    return requested


def make_plan(models, variants, cases, runs, order, seed):
    if order == "interleaved":
        jobs = [(m, v, c, r) for r in runs for c in cases for v in variants for m in models]
    else:
        jobs = [(m, v, c, r) for m in models for v in variants for r in runs for c in cases]
        if order == "shuffle":
            random.Random(seed).shuffle(jobs)
    return jobs


# ---------------------------------------------------------------------------
# Ollama


class Ollama:
    def __init__(self, url, timeout):
        self.url = url.rstrip("/")
        self.timeout = timeout
        # Ollama is local; never send these requests through an HTTP proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path, payload=None, timeout=15):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.url + path, data=data,
                                         headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                body = json.loads(body).get("error", body)
            except (ValueError, AttributeError):
                pass
            raise RuntimeError(f"HTTP {e.code}: {body}") from None

    def chat(self, payload):
        return self.call("/api/chat", payload, timeout=self.timeout)

    def gpu_share(self, model):
        """Fraction of the loaded model that sits in VRAM, from /api/ps."""
        try:
            loaded = self.call("/api/ps").get("models") or []
        except (RuntimeError, OSError, ValueError):
            return None
        for entry in loaded:
            if model in (entry.get("name"), entry.get("model")) and entry.get("size"):
                return round(entry.get("size_vram", 0) / entry["size"], 3)
        return None


def run_job(ollama, suite, model, variant_name, case_id, run, seed):
    variant = suite["variants"][variant_name]
    options = {"seed": seed + run - 1, **variant["options"]}
    messages = []
    if variant["system"]:
        messages.append({"role": "system", "content": variant["system"]})
    messages.append({"role": "user", "content": suite["cases"][case_id]["prompt"]})
    payload = {"model": model, "messages": messages, "stream": False, "options": options}
    if "think" in variant:
        payload["think"] = variant["think"]

    record = {"time": datetime.now().isoformat(timespec="seconds"), "model": model,
              "variant": variant_name, "case": case_id, "run": run, "options": options,
              "think": variant.get("think")}
    start = time.monotonic()
    try:
        response = ollama.chat(payload)
    except RuntimeError as e:
        record["error"] = str(e)
    except (OSError, ValueError) as e:
        record["error"] = str(e)
        record["connection_error"] = True
    else:
        message = response.get("message") or {}
        record["content"] = message.get("content", "")
        record["thinking"] = message.get("thinking", "")
        record["metrics"] = {key: response.get(key) for key in METRICS}
    record["wall_s"] = round(time.monotonic() - start, 2)
    record["gpu_share"] = ollama.gpu_share(model)
    return record


# ---------------------------------------------------------------------------
# Checks


def is_foreign(ch):
    """A letter that has no place in Hungarian text: another script, or õ/û instead of ő/ű."""
    return unicodedata.category(ch).startswith("L") and not ch.isascii() and ch not in HU_LETTERS


def shingles(text, n):
    words = re.findall(r"\w+", text.casefold())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def numbers(text):
    """Whole numbers in the text, apart from list numbering, minutes, placeholders, URLs and e-mail addresses."""
    text = LIST_NUMBER_RE.sub(" ", URL_RE.sub(" ", PLACEHOLDER_RE.sub(" ", text)))
    text = MINUTES_RE.sub(r"\1", THOUSANDS_RE.sub("", text))
    return {int(n) for n in re.findall(r"\d+", text)}


class Checker:
    def __init__(self, checks):
        self.dictionary = checks.get("dictionary", "hu_HU")
        # An entry also covers the word with suffixes: "Zöldkert" covers "Zöldkerttől".
        self.ignore = tuple(word.casefold() for word in checks.get("ignore_words", []))
        self.avoid = checks.get("avoid", [])
        self.ngram = checks.get("copy_ngram", 6)
        self.hunspell = shutil.which("hunspell")
        if self.hunspell and self._hunspell("alma") is None:
            self.hunspell = None

    def _hunspell(self, text):
        result = subprocess.run([self.hunspell, "-d", self.dictionary, "-i", "utf-8", "-l"],
                                input=text, capture_output=True, encoding="utf-8")
        return result.stdout.split() if result.returncode == 0 else None

    def spelling(self, text):
        if not self.hunspell:
            return None
        text = URL_RE.sub(" ", PLACEHOLDER_RE.sub(" ", text))
        words = (word.strip(".,;:!?()\"'„”»«…-") for word in self._hunspell(text) or [])
        return [word for word in words
                if word and not word.isupper() and not any(c.isdigit() for c in word)
                and not word.casefold().startswith(self.ignore)]

    def check(self, content, case, system):
        text = THINK_RE.sub("", content)
        lowered = text.casefold()
        answer = shingles(text, self.ngram)
        new_numbers = None
        if case.get("check_numbers"):
            given = numbers(case["prompt"] + "\n" + system) | set(case.get("allow_numbers", []))
            new_numbers = [str(n) for n in sorted(numbers(text) - given)]
        return {
            "spelling": self.spelling(text),
            "foreign": [ch for ch in text if is_foreign(ch)],
            "known": [p for p in self.avoid + case.get("avoid", []) if p.casefold() in lowered],
            "missing": [r for r in case.get("require", [])
                        if not any(alt.casefold() in lowered for alt in r.split("|"))],
            "numbers": new_numbers,
            "copied": len(answer & shingles(system, self.ngram)) / len(answer)
            if answer and system else None,
        }


def cut_off(record):
    """No answer at all, or generation stopped by the length limit (e.g. thinking that never ended)."""
    return not THINK_RE.sub("", record["content"]).strip() or record["metrics"].get("done_reason") == "length"


def problem_count(record):
    found = record["found"]
    return cut_off(record) + sum(len(found[key] or [])
                                 for key in ("spelling", "foreign", "known", "missing", "numbers"))


# ---------------------------------------------------------------------------
# Report


def rate(count, nanoseconds):
    return count / (nanoseconds / 1e9) if count and nanoseconds else None


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def fmt(value, digits=0, suffix=""):
    return "–" if value is None else f"{value:.{digits}f}{suffix}"


def table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def quote(text):
    return "\n".join("> " + line if line else ">" for line in text.strip().splitlines()) or "> (empty)"


def load_results(out_dir):
    records = []
    path = out_dir / "results.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                pass  # a line cut short by an interrupted run
    return records


def write_report(out_dir, checker, current_cases):
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    cases, variants = meta["suite"]["cases"], meta["suite"]["variants"]
    # Prompts are what was sent; the checks come from the current suite file.
    for case_id, case in cases.items():
        for key, default in (("require", []), ("avoid", []), ("check_numbers", False), ("allow_numbers", [])):
            if case_id in current_cases:
                case[key] = current_cases[case_id].get(key, default)
    records = load_results(out_dir)
    for r in records:
        if "error" not in r:
            r["found"] = checker.check(r["content"], cases[r["case"]], variants[r["variant"]]["system"])

    groups = defaultdict(list)
    for r in records:
        groups[(r["model"], r["variant"])].append(r)
    keys = [(m, v) for m in meta["models"] for v in meta["variants"] if (m, v) in groups]

    speed_rows, quality_rows = [], []
    for model, variant in keys:
        rs = groups[(model, variant)]
        ok = [r for r in rs if "error" not in r]
        ms = [r["metrics"] for r in ok]
        gpu = [r["gpu_share"] for r in ok if r.get("gpu_share") is not None]
        speed_rows.append([
            model, variant, len(ok),
            fmt(median(rate(m["eval_count"], m["eval_duration"]) for m in ms)),
            fmt(median(rate(m["prompt_eval_count"], m["prompt_eval_duration"]) for m in ms)),
            fmt(median(m["prompt_eval_count"] for m in ms)),
            fmt(median(m["eval_count"] for m in ms)),
            fmt(median((m["total_duration"] - (m["load_duration"] or 0)) / 1e9
                       for m in ms if m["total_duration"]), 1),
            fmt(max((m["load_duration"] or 0) / 1e9 for m in ms) if ms else None, 1),
            fmt(min(gpu) * 100 if gpu else None, 0, "%"),
        ])
        found = [r["found"] for r in ok]
        spelling = [len(f["spelling"]) for f in found if f["spelling"] is not None]
        copied = [f["copied"] for f in found if f["copied"] is not None]
        quality_rows.append([
            model, variant, len(ok),
            fmt(sum(spelling) / len(spelling) if spelling else None, 1),
            sum(len(f["foreign"]) for f in found),
            sum(len(f["known"]) for f in found),
            sum(len(f["missing"]) for f in found),
            sum(len(f["numbers"] or []) for f in found),
            sum(cut_off(r) for r in ok),
            fmt(max(copied) * 100 if copied else None, 0, "%"),
            len(rs) - len(ok),
        ])

    case_ids = [c for c in meta["cases"] if any(r["case"] == c for r in records)]
    case_rows = []
    for model, variant in keys:
        row = [f"{model} · {variant}"]
        for case_id in case_ids:
            ok = [r for r in groups[(model, variant)] if r["case"] == case_id and "error" not in r]
            row.append(fmt(sum(problem_count(r) for r in ok) / len(ok), 1) if ok else "–")
        case_rows.append(row)

    words, word_models = Counter(), defaultdict(set)
    for r in records:
        for word in (r.get("found") or {}).get("spelling") or []:
            words[word] += 1
            word_models[word].add(r["model"])
    word_rows = [[w, n, ", ".join(sorted(word_models[w]))] for w, n in words.most_common(25)]

    variant_lines = [f"- **{v}**: {variants[v].get('description', '')}" for v in meta["variants"]]
    spelling_note = (f"Hunspell ({checker.dictionary})" if checker.hunspell else
                     "not available (install hunspell and the Hungarian dictionary, then use --report)")
    summary = "\n\n".join(filter(None, [
        f"# Hungarian test results, {meta['started'].replace('T', ' ')}",
        "\n".join([
            f"- Ollama {meta['ollama_version']} at {meta['url']}",
            f"- GPU: {meta.get('gpu') or 'unknown'}",
            f"- {meta['runs']} run(s) per case, order: {meta['order']}, seed: {meta['seed']}",
            f"- Requests: {len(records)} done of {meta['planned']} planned",
            f"- Spelling check: {spelling_note}",
        ]),
        "Variants:\n" + "\n".join(variant_lines),
        "## Speed\n\nMedians over all answers. Response time excludes model loading. "
        "GPU is the share of the model in VRAM as Ollama reports it; it looks too high "
        "when part of a model runs on the CPU.\n\n" + table(
            ["Model", "Variant", "Answers", "Gen tok/s", "Prompt tok/s", "Prompt tokens",
             "Output tokens", "Response s", "Load s", "GPU"], speed_rows),
        "## Automatic checks\n\nSpelling: words Hunspell doesn't know. Foreign: letters "
        "from other scripts, or õ/û. Known: mistakes listed under [checks] avoid. Missing: "
        "required facts not in the answer. New numbers: numbers that aren't in the prompt, "
        "in cases with check_numbers; usually invented dates, amounts or order numbers. Cut "
        "off: empty answers, or answers stopped by the length limit (usually endless "
        "thinking). Copied: largest share of an answer taken verbatim from the system "
        "prompt.\n\n" + table(
            ["Model", "Variant", "Answers", "Spelling / answer", "Foreign", "Known", "Missing",
             "New numbers", "Cut off", "Copied", "Failed"], quality_rows),
        "## Problems per answer, by case\n\nSum of the checks above, averaged over runs.\n\n"
        + table(["Model · variant"] + case_ids, case_rows) if case_ids else "",
        "## Words flagged by the spelling check\n\nReal mistakes, names, or words to add to "
        "ignore_words.\n\n" + table(["Word", "Count", "Models"], word_rows) if word_rows else "",
        "Read every answer in answers.md: the checks can't judge style or meaning.",
    ])) + "\n"
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")

    parts = [f"# Answers, {meta['started'].replace('T', ' ')}"]
    for case_id in case_ids:
        parts.append(f"## {case_id}\n\n" + quote(cases[case_id].get("prompt_shown", cases[case_id]["prompt"])))
        for model, variant in keys:
            for r in sorted((r for r in groups[(model, variant)] if r["case"] == case_id),
                            key=lambda r: r["run"]):
                head = f"### {model} · {variant} · run {r['run']}"
                if "error" in r:
                    parts.append(f"{head}\n\nERROR: {r['error']}")
                    continue
                f, m = r["found"], r["metrics"]
                notes = [f"{fmt(rate(m['eval_count'], m['eval_duration']))} tok/s",
                         f"{r['wall_s']:.1f} s"]
                for label, key in (("spelling", "spelling"), ("foreign", "foreign"),
                                   ("known", "known"), ("missing", "missing"), ("new numbers", "numbers")):
                    if f[key]:
                        notes.append(f"{label}: " + ", ".join(dict.fromkeys(f[key])))
                if cut_off(r):
                    notes.append(f"CUT OFF ({m.get('done_reason') or 'empty'})")
                if f["copied"]:
                    notes.append(f"copied: {f['copied'] * 100:.0f}%")
                parts.append(f"{head}\n\n{' · '.join(notes)}\n\n{quote(THINK_RE.sub('', r['content']))}")
    (out_dir / "answers.md").write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return summary


# ---------------------------------------------------------------------------
# Main


def gpu_name():
    if not shutil.which("nvidia-smi"):
        return None
    result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                            capture_output=True, text=True)
    return "; ".join(result.stdout.strip().splitlines()) if result.returncode == 0 else None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Runs the Hungarian test suite against Ollama.",
        epilog="Defaults come from the [run] table of the suite file.")
    parser.add_argument("--models", type=csv_list,
                        help="comma-separated; default: [run] models, else CHAT_MODELS in .env")
    parser.add_argument("--variants", type=csv_list, help="comma-separated variant names")
    parser.add_argument("--cases", type=csv_list, help="comma-separated case ids (default: all)")
    parser.add_argument("--runs", type=int, help="answers per model, variant and case")
    parser.add_argument("--order", choices=ORDERS, help="request order (see suite.toml)")
    parser.add_argument("--seed", type=int, default=1, help="seed of run 1; run N uses seed+N-1 (default: 1)")
    parser.add_argument("--url", help="Ollama URL (default: $OLLAMA_URL, else http://127.0.0.1:OLLAMA_API_PORT)")
    parser.add_argument("--timeout", type=int, default=900, help="seconds per request (default: 900)")
    parser.add_argument("--suite", type=Path, default=HERE / "suite.toml")
    parser.add_argument("--out", type=Path, help="results directory (default: tests/hu/results/<timestamp>)")
    parser.add_argument("--dry-run", action="store_true", help="check the setup and print the plan only")
    parser.add_argument("--report", type=Path, metavar="DIR",
                        help="only rebuild summary.md and answers.md of an earlier run, "
                             "with the checks of the current suite file")
    return parser.parse_args()


def main():
    args = parse_args()
    suite = load_suite(args.suite)
    checker = Checker(suite["checks"])
    if args.report:
        print(write_report(args.report, checker, suite["cases"]))
        return

    defaults = suite["run"]
    models = args.models or defaults.get("models") or csv_list(env_get("CHAT_MODELS"))
    models = [full_model_name(m) for m in models]
    variants = pick(args.variants or defaults.get("variants") or list(suite["variants"]),
                    suite["variants"], "variant")
    cases = pick(args.cases or list(suite["cases"]), suite["cases"], "case")
    runs = args.runs or defaults.get("runs", 3)
    order = args.order or defaults.get("order", "grouped")
    if not models:
        sys.exit("No models given: use --models or set CHAT_MODELS in .env.")
    if order not in ORDERS:
        sys.exit(f"Unknown order in {args.suite}: {order}")

    url = args.url or os.environ.get("OLLAMA_URL") or f"http://127.0.0.1:{env_get('OLLAMA_API_PORT', '11434')}"
    ollama = Ollama(url, args.timeout)
    try:
        version = ollama.call("/api/version").get("version", "?")
        installed = {m["name"] for m in ollama.call("/api/tags").get("models") or []}
    except (RuntimeError, OSError, ValueError) as e:
        sys.exit(f"Cannot reach Ollama at {url}: {e}\n"
                 "Publish its API on this host: add compose.ollama-api.yaml to COMPOSE_FILE in .env,\n"
                 "then run: docker compose up -d")
    print(f"Ollama {version} at {url}")
    pinned = env_get("OLLAMA_VERSION")
    if pinned and pinned.lstrip("v") != version.lstrip("v"):
        print(f"Note: .env pins Ollama {pinned}. Is this brAInharbor's Ollama, or another one on this host?")
    missing = [m for m in models if m not in installed]
    if missing:
        sys.exit("Not installed: " + ", ".join(missing) + "\nPull with: docker compose exec ollama ollama pull NAME")

    jobs = make_plan(models, variants, cases, range(1, runs + 1), order, args.seed)
    print(f"{len(jobs)} requests: {len(models)} model(s) × {len(variants)} variant(s) × "
          f"{len(cases)} case(s) × {runs} run(s), order: {order}")
    if checker.hunspell:
        print(f"Spelling check: Hunspell {checker.dictionary}")
    else:
        print("Spelling check: off (hunspell or its Hungarian dictionary is not installed)")
    if args.dry_run:
        for i, job in enumerate(jobs[:12], 1):
            print(f"  {i:>3}. {' · '.join(map(str, job[:3]))} · run {job[3]}")
        if len(jobs) > 12:
            print(f"  ... and {len(jobs) - 12} more")
        return

    started = datetime.now()
    out_dir = args.out or HERE / "results" / started.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"started": started.isoformat(timespec="seconds"), "url": url, "ollama_version": version,
            "gpu": gpu_name(), "models": models, "variants": variants, "cases": cases, "runs": runs,
            "order": order, "seed": args.seed, "planned": len(jobs),
            "suite": {"variants": suite["variants"], "cases": suite["cases"]}}
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    width = len(str(len(jobs)))
    t0 = time.monotonic()
    connection_errors = 0
    try:
        with open(out_dir / "results.jsonl", "a", encoding="utf-8") as results:
            for i, (model, variant, case_id, run) in enumerate(jobs, 1):
                print(f"[{i:>{width}}/{len(jobs)}] {model} · {variant} · {case_id} · run {run}: ",
                      end="", flush=True)
                record = run_job(ollama, suite, model, variant, case_id, run, args.seed)
                results.write(json.dumps(record, ensure_ascii=False) + "\n")
                results.flush()
                connection_errors = connection_errors + 1 if record.get("connection_error") else 0
                if connection_errors == 3:
                    print(f"ERROR {record['error']}\nOllama stopped answering; giving up.")
                    break
                if "error" in record:
                    print(f"ERROR {record['error']}")
                    continue
                f = checker.check(record["content"], suite["cases"][case_id], suite["variants"][variant]["system"])
                m = record["metrics"]
                left = round((time.monotonic() - t0) / i * (len(jobs) - i) / 60)
                print(f"{fmt(rate(m['eval_count'], m['eval_duration']))} tok/s, {record['wall_s']:.1f} s, "
                      f"spelling {fmt(len(f['spelling']) if f['spelling'] is not None else None)}, "
                      f"foreign {len(f['foreign'])}, known {len(f['known'])}, missing {len(f['missing'])}"
                      + (f", new numbers {len(f['numbers'])}" if f["numbers"] is not None else "")
                      + ("  CUT OFF" if cut_off(record) else "")
                      + (f"  (about {left} min left)" if left else ""))
    except KeyboardInterrupt:
        print("\nInterrupted; writing the report for the finished requests.")

    print("\n" + write_report(out_dir, checker, suite["cases"]))
    print(f"Results: {out_dir}")


if __name__ == "__main__":
    main()
