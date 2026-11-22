#!/usr/bin/env python3
"""Judge for H-09: history list is newest-first and entries are distinguishable.

Input (JSON, from a file path argument or stdin):
{
  "created": ["A", "B", "C", "D"],            # conversation ids in the order they were created
  "observations": [                             # one per look at the history list (before/after restart)
    {"label": "before-restart",
     "displayed": ["D", "C", "B", "A"],         # ids top-to-bottom as identified by the runner
     "cues": ["...", "...", "...", "..."]},     # the visible text of each entry, same order
    {"label": "after-restart", "displayed": [...], "cues": [...]}
  ]
}

Exit code / output: prints PASS, FAIL or INCONCLUSIVE with reasons.
"""
import json
import sys


def judge(data):
    reasons = []
    created = data.get("created")
    obs = data.get("observations")
    if not isinstance(created, list) or len(created) < 2:
        return "INCONCLUSIVE", ["need at least 2 created conversations"]
    if not isinstance(obs, list) or len(obs) < 2:
        return "INCONCLUSIVE", ["need observations before and after restart"]
    expected = list(reversed(created))
    verdict = "PASS"
    for o in obs:
        label = o.get("label", "?")
        shown = o.get("displayed")
        cues = o.get("cues")
        if not isinstance(shown, list) or not isinstance(cues, list):
            return "INCONCLUSIVE", [f"{label}: displayed/cues missing"]
        if len(cues) != len(shown):
            return "INCONCLUSIVE", [f"{label}: cues and displayed differ in length"]
        if any(x is None or x == "" for x in shown):
            return "INCONCLUSIVE", [f"{label}: some entries could not be identified"]
        if shown != expected:
            verdict = "FAIL"
            reasons.append(f"{label}: order {shown} != newest-first {expected}")
        norm = [" ".join(str(c).split()) for c in cues]
        if any(c == "" for c in norm):
            verdict = "FAIL"
            reasons.append(f"{label}: an entry shows no cue")
        if len(set(norm)) != len(norm):
            verdict = "FAIL"
            reasons.append(f"{label}: two or more entries look identical")
    if verdict == "PASS":
        reasons.append("newest-first and distinguishable in every observation")
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
