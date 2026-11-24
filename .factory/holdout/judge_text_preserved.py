#!/usr/bin/env python3
"""Judge for H-37, H-42 and H-47: the AI bubble keeps every character of the reply.

The fake responder replies `fake reply #<n>: <sent>`. This judge compares what the AI bubble
shows (its visible text, e.g. innerText) with that reply text.

Input (JSON, from a file path argument or stdin):
{
  "scenario": "H-42",                      # or "H-37", "H-47"; decides which case labels are required
  "cases": [
    {"label": "U",                         # case label named in the scenario
     "mode": "verbatim",                   # "verbatim" or "markers" (the scenario says which)
     "n": 1,                               # the <n> the reply should carry
     "sent": "前置き\n\n| 列A | 列B |...",   # exactly what was sent (real newlines, tabs, backticks)
     "bubble_text": "fake reply #1: ..."}  # the visible text of the whole AI bubble
  ]
}

Both modes ignore whitespace (spaces, tabs, line breaks), so layout is judged by eye, not here.
"verbatim": nothing else is ignored; every other character of the reply must appear, in order,
            with nothing added, removed or replaced (entities such as `&lt;` must stay `&lt;`).
"markers":  additionally ignores the Markdown marker characters # * _ ~ ` - + and bullet glyphs
            the screen may draw as text; every other character must still appear, in order.

PASS if every required case matches. FAIL if any case differs (the first difference is shown).
INCONCLUSIVE if a required case is missing or a case lacks a field.

H-37 also checks the <n> sequence: cases 2..5 carry <n> of case 1 plus 1, 2, 3, 4; each
"<k>-reopened" case carries the same <n> as case <k>; case 6 carries <n> of case 5 plus 1.
"""
import json
import sys

REQUIRED = {
    "H-37": {
        **{str(k): "markers" for k in range(1, 6)},
        **{f"{k}-reopened": "markers" for k in range(1, 6)},
        "6": "markers",
    },
    "H-42": {"U": "verbatim", "image-only": "verbatim"},
    "H-47": {
        "unclosed-fence": "markers",
        "unmatched": "verbatim",
        "crossing": "markers",
        "markers-only": "verbatim",
        "stars-5000": "verbatim",
        "underscores-5000": "verbatim",
        "brackets": "verbatim",
        "backticks-5000": "verbatim",
        "nested-list-200": "markers",
        "list-2000": "markers",
        "fence-first-line": "markers",
    },
}
MARKERS = set("#*_~`-+") | set("•◦▪▫‣⁃●○■□")


def normalize(text, mode):
    out = []
    for ch in text:
        if ch.isspace() or ch in "​‌‍﻿":
            continue
        if mode == "markers" and ch in MARKERS:
            continue
        out.append(ch)
    return "".join(out)


def first_diff(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return min(len(a), len(b))


def judge(data):
    scenario = data.get("scenario")
    if scenario not in REQUIRED:
        return "INCONCLUSIVE", [f"unknown scenario {scenario!r}; expected one of {sorted(REQUIRED)}"]
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        return "INCONCLUSIVE", ["no cases"]
    by_label = {}
    for c in cases:
        if not isinstance(c, dict) or not isinstance(c.get("label"), str):
            return "INCONCLUSIVE", ["a case has no label"]
        by_label[c["label"]] = c
    missing = set(REQUIRED[scenario]) - set(by_label)
    if missing:
        return "INCONCLUSIVE", [f"missing cases: {sorted(missing)}"]
    verdict, reasons = "PASS", []
    for label, mode in REQUIRED[scenario].items():
        c = by_label[label]
        if c.get("mode", mode) != mode:
            return "INCONCLUSIVE", [f"{label}: mode must be {mode!r}"]
        n, sent, shown = c.get("n"), c.get("sent"), c.get("bubble_text")
        if not isinstance(n, int) or not isinstance(sent, str) or not isinstance(shown, str):
            return "INCONCLUSIVE", [f"{label}: n, sent and bubble_text are required"]
        expected = normalize(f"fake reply #{n}: {sent}", mode)
        actual = normalize(shown, mode)
        if expected == actual:
            reasons.append(f"{label}: all {len(expected)} characters kept ({mode})")
            continue
        verdict = "FAIL"
        i = first_diff(expected, actual)
        reasons.append(
            f"{label}: differs at character {i} ({mode}); expected ...{expected[max(0, i - 15):i + 25]!r}"
            f" but shown ...{actual[max(0, i - 15):i + 25]!r} (lengths {len(expected)} vs {len(actual)})"
        )
    if scenario == "H-37":
        n = {label: by_label[label]["n"] for label in REQUIRED[scenario]}
        want = {str(k): n["1"] + k - 1 for k in range(1, 6)}
        want.update({f"{k}-reopened": n[str(k)] for k in range(1, 6)})
        want["6"] = n["5"] + 1
        for label, w in want.items():
            if n[label] != w:
                verdict = "FAIL"
                reasons.append(f"{label}: <n> is {n[label]} but should be {w}")
    return verdict, reasons


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
