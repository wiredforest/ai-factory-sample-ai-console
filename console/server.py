"""127.0.0.1 で待ち受ける HTTP サーバと、工場との契約エンドポイント。"""

import argparse
import errno
import json
import os
import sqlite3
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from console import chat, config, history, llm, status
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


def open_record(path: str) -> Database | None:
    try:
        return Database(path)
    except sqlite3.Error as e:
        # 記録を開けなくても待ち受ける。サーバの状態で「会話の記録」が異常と分かる (#8)。
        print(f"warning: 会話の記録 {path} を開けません ({e})", file=sys.stderr, flush=True)
        return None


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
            if self.server.db is None:
                self.reply_db_unavailable()
            else:
                self.reply_json(history.list_conversations(self.server.db))
        elif path == "/api/status":
            self.reply_json(status.check(self.server.server_address[1], self.server.db_path, self.server.responder))
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
        if self.server.db is None:
            self.reply_db_unavailable()
            return
        try:
            # 記録してコミットし終えてから応答を返す。応答は全文を 1 回で返す (ストリーミングしない)。
            self.reply_json(chat.send(self.server.db, self.server.responder, request))
        except chat.Rejected as e:
            self.reply_json({"error": e.error}, e.status)
        except llm.Unreachable:
            # 何も記録していない。応答の形は S4 (#12) の ticket が決めたもの。画面の表示は #12 で足す。
            self.reply_json({"error": "llm_unavailable", "message": "AI に繋がりません"}, 503)

    def reply_db_unavailable(self) -> None:
        self.reply_json({"error": "db_unavailable", "message": "会話の記録を開けません"}, 503)

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

    # bind に失敗すると、__init__ が db を設定する前に server_close が呼ばれる。
    db: Database | None = None

    def __init__(self, port: int, db_path: str, responder: llm.Responder) -> None:
        # 記録を開くのはポートを取れてから。ポート使用中なら記録に触れずに 1 行のエラーで終わる (#16)。
        super().__init__((HOST, port), Handler)
        self.build_id = build_id()
        self.db_path = db_path
        self.db = open_record(db_path)
        self.responder = responder

    def server_close(self) -> None:
        super().server_close()
        if self.db is not None:
            self.db.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app.py", description="自分専用の AI 会話コンソール")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        settings = config.load()
    except config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        server = ConsoleServer(args.port, settings.db_path, llm.responder(settings.llm))
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
