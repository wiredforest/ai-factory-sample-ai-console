"""サーバの状態のスライス: アプリ自身・AI への接続・会話の記録が動いているかを、その場で実際に確かめる。"""

import http.client
from collections.abc import Callable

from console import db, llm

HEALTH_TIMEOUT_S = 2


def app_ok(port: int) -> bool:
    # 待ち受けは 127.0.0.1 だけ。http.client はリダイレクトを辿らないので、/health の契約から外れたら偽になる。
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=HEALTH_TIMEOUT_S)
    try:
        conn.request("GET", "/health")
        response = conn.getresponse()
        return response.status == 200 and response.read() == b"ok"
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def ok(probe: Callable[[], bool]) -> bool:
    # 1 つの確認が例外を出しても、他の項目と応答は保つ。
    try:
        return probe()
    except Exception:  # noqa: BLE001
        return False


def check(port: int, db_path: str, responder: llm.Responder) -> dict:
    """3 項目をこの順に確かめる。"""
    probes = [
        ("app", "アプリ自身", lambda: app_ok(port)),
        ("llm", "AI への接続", responder.reachable),
        ("db", "会話の記録", lambda: db.check(db_path)),
    ]
    return {"items": [{"key": key, "label": label, "ok": ok(probe)} for key, label, probe in probes]}
