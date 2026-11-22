"""127.0.0.1 で待ち受ける HTTP サーバと、工場との契約エンドポイント。"""

import argparse
import errno
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from console import chat, config, history, llm
from console.db import Database

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
HERE = Path(__file__).resolve().parent
INDEX_HTML = (HERE / "static" / "index.html").read_bytes()


def build_id() -> str:
    candidate = os.environ.get("FACTORY_RUNTIME_CANDIDATE")
    if candidate is not None:
        return candidate
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False, cwd=HERE, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else "unknown"


class Handler(BaseHTTPRequestHandler):
    server: ConsoleServer

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self.reply(INDEX_HTML, "text/html; charset=utf-8")
        elif path == "/health":
            self.reply(b"ok", "text/plain; charset=utf-8")
        elif path == "/build-id":
            self.reply(self.server.build_id.encode("utf-8"), "text/plain; charset=utf-8")
        elif path == "/api/conversations":
            self.reply_json(history.list_conversations(self.server.db))
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/messages":
            self.post_message()
        else:
            self.send_error(404)

    def post_message(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length)) if length > 0 else None
        except ValueError:
            self.reply_json({"error": "invalid_request"}, 400)
            return
        try:
            # 記録してコミットし終えてから応答を返す。応答は全文を 1 回で返す (ストリーミングしない)。
            self.reply_json(chat.send(self.server.db, self.server.responder, request))
        except chat.Rejected as e:
            self.reply_json({"error": e.error}, e.status)
        except llm.Unreachable:
            # 何も記録していない。応答の形は S4 (#12) の ticket が決めたもの。画面の表示は #12 で足す。
            self.reply_json({"error": "llm_unavailable", "message": "AI に繋がりません"}, 503)

    def reply_json(self, value: object, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.reply(body, "application/json; charset=utf-8", status)

    def reply(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, db: Database, responder: llm.Responder) -> None:
        super().__init__((HOST, port), Handler)
        self.build_id = build_id()
        self.db = db
        self.responder = responder


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app.py", description="自分専用の AI 会話コンソール")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        settings = config.load()
    except config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    with Database(settings.db_path) as db:
        try:
            server = ConsoleServer(args.port, db, llm.responder(settings.llm))
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
            print(f"error: ポート {args.port} はすでに使われています", file=sys.stderr)
            return 2
        with server:
            print(f"listening on http://{HOST}:{server.server_address[1]}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    return 0
