#!/usr/bin/env python3
"""Judge for H-47: broken or huge Markdown never freezes the screen.

Input (JSON, from a file path argument or stdin):
{
  "cases": [
    {"label": "stars-5000",               # the H-47 case labels (see judge_text_preserved.py)
     "sent_at_ms": 1000,                  # when the send was made (any common clock, in ms)
     "bubble_shown_at_ms": 1350,          # when the AI bubble with the whole reply was visible;
                                          # null if it never appeared
     "responsive": true}                  # after the bubble appeared, scrolling the conversation and
                                          # typing into the input both reacted within a second
  ]
}

FAIL if any case's AI bubble took longer than LIMIT_MS from the send, never appeared, or the screen
did not react afterwards. INCONCLUSIVE if a required case is missing or a field is malformed.
"""
import json
import sys

LIMIT_MS = 5000
REQUIRED = {
    "unclosed-fence", "unmatched", "crossing", "markers-only", "stars-5000", "underscores-5000",
    "brackets", "backticks-5000", "nested-list-200", "list-2000", "fence-first-line",
}


def judge(data):
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        return "INCONCLUSIVE", ["no cases"]
    by_label = {c.get("label"): c for c in cases if isinstance(c, dict)}
    missing = REQUIRED - set(by_label)
    if missing:
        return "INCONCLUSIVE", [f"missing cases: {sorted(missing)}"]
    verdict, reasons = "PASS", []
    for label in sorted(REQUIRED):
        c = by_label[label]
        sent, shown, responsive = c.get("sent_at_ms"), c.get("bubble_shown_at_ms"), c.get("responsive")
        if not isinstance(sent, (int, float)) or not isinstance(responsive, bool):
            return "INCONCLUSIVE", [f"{label}: sent_at_ms and responsive are required"]
        if shown is None:
            verdict = "FAIL"
            reasons.append(f"{label}: the AI bubble never appeared")
            continue
        if not isinstance(shown, (int, float)) or shown < sent:
            return "INCONCLUSIVE", [f"{label}: bubble_shown_at_ms missing or before the send"]
        took = shown - sent
        if took > LIMIT_MS or not responsive:
            verdict = "FAIL"
            reasons.append(f"{label}: shown after {took:.0f} ms, responsive={responsive}")
        else:
            reasons.append(f"{label}: shown after {took:.0f} ms, responsive")
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
