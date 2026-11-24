#!/usr/bin/env python3
"""Judge for H-26: with no user action, the screen does not re-check status at short intervals.

Input (JSON, from a file path argument or stdin):
{
  "observations": [                        # one per idle window (tab visible, tab hidden)
    {"label": "visible-idle",
     "capture_ok": true,                   # the runner confirms every status-check request was captured
     "window_seconds": 180,                # length of the idle window actually observed
     "check_times": [0.4, 0.5, 61.2]},     # seconds from the window start, one per request the
                                           # screen sent to the app to check status
    {"label": "hidden-idle", "capture_ok": true, "window_seconds": 180, "check_times": []}
  ]
}

Requests within GROUP_SECONDS of the first request of a check are counted as part of that check.
FAIL if two consecutive checks in any window are less than MIN_GAP_SECONDS apart.
INCONCLUSIVE if a window is missing, shorter than MIN_WINDOW_SECONDS, or not fully captured.

Exit code / output: prints PASS, FAIL or INCONCLUSIVE with reasons.
"""
import json
import sys

GROUP_SECONDS = 2.0
MIN_GAP_SECONDS = 10.0
MIN_WINDOW_SECONDS = 120.0
REQUIRED_LABELS = {"visible-idle", "hidden-idle"}


def group(times):
    checks = []
    for t in sorted(times):
        if not checks or t - checks[-1] > GROUP_SECONDS:
            checks.append(t)
    return checks


def judge(data):
    obs = data.get("observations")
    if not isinstance(obs, list) or not obs:
        return "INCONCLUSIVE", ["no observations"]
    labels = {o.get("label") for o in obs if isinstance(o, dict)}
    missing = REQUIRED_LABELS - labels
    if missing:
        return "INCONCLUSIVE", [f"missing windows: {sorted(missing)}"]
    verdict, reasons = "PASS", []
    for o in obs:
        label = o.get("label", "?")
        if o.get("capture_ok") is not True:
            return "INCONCLUSIVE", [f"{label}: capture not confirmed complete"]
        window = o.get("window_seconds")
        times = o.get("check_times")
        if not isinstance(window, (int, float)) or window < MIN_WINDOW_SECONDS:
            return "INCONCLUSIVE", [f"{label}: window shorter than {MIN_WINDOW_SECONDS:.0f}s"]
        if not isinstance(times, list) or not all(isinstance(t, (int, float)) for t in times):
            return "INCONCLUSIVE", [f"{label}: check_times missing or not numbers"]
        if any(t < 0 or t > window for t in times):
            return "INCONCLUSIVE", [f"{label}: a check time lies outside the window"]
        checks = group(times)
        gaps = [b - a for a, b in zip(checks, checks[1:])]
        short = [round(g, 1) for g in gaps if g < MIN_GAP_SECONDS]
        if short:
            verdict = "FAIL"
            reasons.append(
                f"{label}: {len(checks)} checks in {window}s, gaps under {MIN_GAP_SECONDS:.0f}s: {short}"
            )
        else:
            reasons.append(f"{label}: {len(checks)} checks in {window}s, no gap under {MIN_GAP_SECONDS:.0f}s")
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
