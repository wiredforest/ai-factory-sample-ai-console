#!/usr/bin/env python3
"""Judge for H-44 and H-53: showing the screen never talks to anything but the app itself.

Input (JSON, from a file path argument or stdin):
{
  "scenario": "H-44",                      # or "H-53"
  "app_origin": "http://127.0.0.1:8000",   # the origin the browser opened the app at
  "capture_ok": true,                      # the runner confirms every request the page made, from the
                                           # first page load to the end of the steps, was captured
                                           # (e.g. HAR with cache disabled, across reloads and tabs)
  "requests": ["http://127.0.0.1:8000/", "http://127.0.0.1:8000/app.js", ...],
  "canary_requests": []                    # H-44 only: every request the canary receiver logged
}

A request counts as external unless its scheme, host and port equal app_origin (ws/wss count as
http/https of the same host and port). data:, blob: and about: URLs are not network requests and
are ignored here; the scenario checks by eye that no image is shown.
FAIL if any external request was made, or (H-44) the canary receiver logged any request.
INCONCLUSIVE if capture is not confirmed complete, no request was captured, or a field is missing.
"""
import json
import sys
from urllib.parse import urlsplit

IGNORED_SCHEMES = {"data", "blob", "about"}
DEFAULT_PORTS = {"http": 80, "https": 443}
WS = {"ws": "http", "wss": "https"}


def origin(url):
    parts = urlsplit(url)
    scheme = WS.get(parts.scheme.lower(), parts.scheme.lower())
    host = (parts.hostname or "").lower()
    port = parts.port or DEFAULT_PORTS.get(scheme)
    return scheme, host, port


def judge(data):
    scenario = data.get("scenario")
    if scenario not in {"H-44", "H-53"}:
        return "INCONCLUSIVE", [f"unknown scenario {scenario!r}"]
    app = data.get("app_origin")
    if not isinstance(app, str) or not app:
        return "INCONCLUSIVE", ["app_origin missing"]
    if data.get("capture_ok") is not True:
        return "INCONCLUSIVE", ["capture not confirmed complete"]
    reqs = data.get("requests")
    if not isinstance(reqs, list) or not reqs or not all(isinstance(r, str) for r in reqs):
        return "INCONCLUSIVE", ["no captured requests"]
    canary = data.get("canary_requests")
    if scenario == "H-44" and not isinstance(canary, list):
        return "INCONCLUSIVE", ["canary_requests missing"]
    app_origin = origin(app)
    external = []
    for r in reqs:
        scheme = urlsplit(r).scheme.lower()
        if scheme in IGNORED_SCHEMES:
            continue
        if origin(r) != app_origin:
            external.append(r)
    verdict, reasons = "PASS", []
    if external:
        verdict = "FAIL"
        reasons.append(f"{len(external)} request(s) outside {app}: {external[:10]}")
    else:
        reasons.append(f"all {len(reqs)} captured requests stayed on {app}")
    if isinstance(canary, list):
        if canary:
            verdict = "FAIL"
            reasons.append(f"canary receiver got {len(canary)} request(s): {canary[:10]}")
        else:
            reasons.append("canary receiver got no request")
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
