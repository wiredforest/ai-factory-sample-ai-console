"""サーバの状態のスライス: アプリ自身・AI への接続・会話の記録が動いているかを、その場で実際に確かめる。"""

import http.client
import sys
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


def ok(key: str, probe: Callable[[], bool]) -> bool:
    # 1 つの確認が例外を出しても、他の項目と応答は保つ。型名だけを出す: 例外の文言に鍵が入らないとは確かめていない。
    try:
        return probe()
    except Exception as e:  # noqa: BLE001
        print(f"warning: サーバの状態 {key} の確認が {type(e).__name__} を出しました", file=sys.stderr, flush=True)
        return False


def check(port: int, db_path: str | None, responder: llm.Responder) -> dict:
    """3 項目をこの順に確かめる。

    db_path は、サーバが起動時に記録を開けなかったとき None。会話は再起動まで記録されないので、
    後からパスが直っても会話の記録は偽のまま。
    """
    probes = [
        ("app", "アプリ自身", lambda: app_ok(port)),
        ("llm", "AI への接続", responder.reachable),
        ("db", "会話の記録", lambda: db_path is not None and db.check(db_path)),
    ]
    return {"items": [{"key": key, "label": label, "ok": ok(key, probe)} for key, label, probe in probes]}
