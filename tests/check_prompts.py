#!/usr/bin/env python3
"""Checks that the Hungarian system prompt is the same everywhere.

The prompt that won the Hungarian tests (tests/hu/prompts/hu-v3.txt) is
copied into two files that ship it:

- ollama/brainharbor-hu.Modelfile, the SYSTEM of the brainharbor-hu model;
- open-webui/defaults.env, the "system" of DEFAULT_MODEL_PARAMS.

Exit code 0 if all three match, 1 otherwise. Run from anywhere:
python3 tests/check_prompts.py
"""

import difflib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = ROOT / "tests/hu/prompts/hu-v3.txt"
MODELFILE = ROOT / "ollama/brainharbor-hu.Modelfile"
DEFAULTS_ENV = ROOT / "open-webui/defaults.env"


def from_modelfile(path: Path) -> str:
    match = re.search(
        r'^SYSTEM """(.*?)"""',
        path.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise ValueError('no SYSTEM """...""" block')
    return match.group(1)


def from_defaults_env(path: Path) -> str:
    match = re.search(
        r"^DEFAULT_MODEL_PARAMS='(.*)'$", path.read_text(encoding="utf-8"), re.MULTILINE
    )
    if not match:
        raise ValueError("no DEFAULT_MODEL_PARAMS='...' line")
    return json.loads(match.group(1))["system"]


def main() -> int:
    # The reference file ends with a newline; the copies don't.
    reference = REFERENCE.read_text(encoding="utf-8").removesuffix("\n")
    failed = False
    for path, extract in (
        (MODELFILE, from_modelfile),
        (DEFAULTS_ENV, from_defaults_env),
    ):
        name = path.relative_to(ROOT)
        try:
            prompt = extract(path)
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            print(f"FAIL  {name}: {error}")
            failed = True
            continue
        if prompt == reference:
            print(f"ok    {name}")
            continue
        failed = True
        print(f"FAIL  {name} differs from {REFERENCE.relative_to(ROOT)}:")
        diff = difflib.unified_diff(
            reference.splitlines(),
            prompt.splitlines(),
            str(REFERENCE.relative_to(ROOT)),
            str(name),
            lineterm="",
        )
        print("\n".join(diff))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
