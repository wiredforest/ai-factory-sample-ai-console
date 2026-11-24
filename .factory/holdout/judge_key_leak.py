#!/usr/bin/env python3
"""Judge for H-35: the key given to the app never shows up anywhere the user or a log can see.

Input (JSON, from a file path argument or stdin):
{
  "canary": "sk-ant-api03-Q8f...",          # the value put in ANTHROPIC_API_KEY for the run(s);
                                            # PREFIX followed by a freshly generated random part
                                            # of at least MIN_RANDOM_CHARS letters/digits
  "runs": ["fake", "fake-down"],            # which CONSOLE_LLM settings the canary was given with
  "captures": [                             # every file the runner saved, one entry per file or folder
    {"surface": "screen_text", "path": "/tmp/h35/screen-step01.txt"},
    {"surface": "page_traffic", "path": "/tmp/h35/browser.har"},
    {"surface": "server_log", "path": "/tmp/h35/app-stdout-stderr.log"},
    {"surface": "state_dir", "path": "/tmp/h35/state"},      # a folder: every file under it is read
    {"surface": "contract", "path": "/tmp/h35/health-and-build-id.txt"}
  ]
}

Surfaces:
  screen_text   visible text of the screen at each step (status menu, failure notices, bubbles, history)
  page_traffic  the page itself and every response body the browser received from the app (e.g. a HAR)
  server_log    everything the app process wrote to stdout and stderr, for every run
  state_dir     the state folder (the conversation record and anything else the app wrote there)
  contract      bodies of /health and /build-id

FAIL if the whole canary, or any run of FRAGMENT_CHARS consecutive characters of its random part,
appears in any captured file (searched as raw bytes, case-sensitive, and also URL-encoded).
INCONCLUSIVE if the canary is malformed or too short, both runs are not listed, a required surface
is missing, or a captured path cannot be read or is empty.

Exit code / output: prints PASS, FAIL or INCONCLUSIVE with reasons.
"""
import json
import os
import re
import sys
from urllib.parse import quote

PREFIX = "sk-ant-api03-"
MIN_RANDOM_CHARS = 24
FRAGMENT_CHARS = 8
REQUIRED_SURFACES = {"screen_text", "page_traffic", "server_log", "state_dir", "contract"}
REQUIRED_RUNS = {"fake", "fake-down"}


def files_under(path):
    if os.path.isdir(path):
        for root, _dirs, names in os.walk(path):
            for name in names:
                yield os.path.join(root, name)
    else:
        yield path


def needles(canary):
    random_part = canary[len(PREFIX):]
    found = {canary.encode()}
    for i in range(len(random_part) - FRAGMENT_CHARS + 1):
        found.add(random_part[i:i + FRAGMENT_CHARS].encode())
    found.add(quote(canary, safe="").encode())
    return found


def judge(data):
    canary = data.get("canary")
    if not isinstance(canary, str) or not canary.startswith(PREFIX):
        return "INCONCLUSIVE", [f"canary missing or not starting with {PREFIX}"]
    random_part = canary[len(PREFIX):]
    if len(random_part) < MIN_RANDOM_CHARS or not re.fullmatch(r"[A-Za-z0-9]+", random_part):
        return "INCONCLUSIVE", [f"canary random part must be >= {MIN_RANDOM_CHARS} letters/digits"]
    runs = set(data.get("runs") or [])
    if not REQUIRED_RUNS <= runs:
        return "INCONCLUSIVE", [f"runs missing: {sorted(REQUIRED_RUNS - runs)}"]
    captures = data.get("captures")
    if not isinstance(captures, list) or not captures:
        return "INCONCLUSIVE", ["no captures"]
    surfaces = {c.get("surface") for c in captures if isinstance(c, dict)}
    missing = REQUIRED_SURFACES - surfaces
    if missing:
        return "INCONCLUSIVE", [f"surfaces missing: {sorted(missing)}"]

    targets = needles(canary)
    hits, checked = [], 0
    for c in captures:
        surface, path = c.get("surface"), c.get("path")
        if not isinstance(path, str) or not os.path.exists(path):
            return "INCONCLUSIVE", [f"{surface}: path not found: {path}"]
        files = list(files_under(path))
        if not files:
            return "INCONCLUSIVE", [f"{surface}: no files under {path}"]
        surface_bytes = 0
        for f in files:
            try:
                with open(f, "rb") as fh:
                    blob = fh.read()
            except OSError as e:
                return "INCONCLUSIVE", [f"{surface}: cannot read {f}: {e}"]
            surface_bytes += len(blob)
            checked += 1
            for n in targets:
                if n in blob:
                    hits.append(f"{surface}: {f} contains a fragment of the key")
                    break
        if surface_bytes == 0 and surface != "state_dir":
            return "INCONCLUSIVE", [f"{surface}: captured files are all empty"]
    if hits:
        return "FAIL", hits
    return "PASS", [f"{checked} files across {len(surfaces)} surfaces, no fragment of the key"]


def main():
    raw = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"INCONCLUSIVE\n- invalid JSON: {e}")
        return 2
    verdict, reasons = judge(data)
    print(verdict)
    for r in reasons:
        print(f"- {r}")
    return {"PASS": 0, "FAIL": 1}.get(verdict, 2)


if __name__ == "__main__":
    sys.exit(main())
