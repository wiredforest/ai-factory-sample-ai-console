"""127.0.0.1 で待ち受ける HTTP サーバと、工場との契約エンドポイント。"""

import argparse
import errno
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from console import config

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
        else:
            self.send_error(404)

    def reply(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int) -> None:
        super().__init__((HOST, port), Handler)
        self.build_id = build_id()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app.py", description="自分専用の AI 会話コンソール")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        config.load()
    except config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        server = ConsoleServer(args.port)
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
