import http.client
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOOT_TIMEOUT_S = 15


@pytest.fixture(autouse=True)
def console_env(monkeypatch, tmp_path):
    """本物の AI を呼ばない環境。テストもアプリのサブプロセスもこの環境で動く。"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FACTORY_RUNTIME_CANDIDATE", raising=False)
    monkeypatch.setenv("CONSOLE_LLM", "fake")
    monkeypatch.delenv("CONSOLE_MODEL", raising=False)
    monkeypatch.setenv("CONSOLE_DB_PATH", str(tmp_path / "console.sqlite3"))
    monkeypatch.setattr(urllib.request, "urlopen", refuse_network)


def refuse_network(*args, **kwargs):
    # pytest.fail は BaseException なので、呼ぶ側の except OSError や except Exception に握りつぶされない。
    pytest.fail("テストはネットワークに出ない。urllib.request.urlopen は自分で差し替えた偽物を使うこと")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def app_argv(port: int) -> list[str]:
    return [sys.executable, "app.py", "--port", str(port)]


@dataclass
class Response:
    status: int
    headers: http.client.HTTPMessage
    body: bytes


@dataclass
class RunningApp:
    port: int
    startup_line: str
    proc: subprocess.Popen

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)

    def get(self, path: str) -> Response:
        """リダイレクトを辿らずに GET する。"""
        return self.request("GET", path)

    def delete(self, path: str) -> Response:
        return self.request("DELETE", path)

    def post_json(self, path: str, value: object) -> Response:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        return self.request("POST", path, body, {"Content-Type": "application/json"})

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> Response:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body, headers or {})
            r = conn.getresponse()
            return Response(status=r.status, headers=r.headers, body=r.read())
        finally:
            conn.close()


@pytest.fixture
def start_app():
    """app.py をサブプロセスで起動し、起動ログが出るまで待つ。終わったら止める。"""
    procs: list[subprocess.Popen] = []

    def start(**env: str) -> RunningApp:
        port = free_port()
        proc = subprocess.Popen(
            app_argv(port), cwd=ROOT, env={**os.environ, **env},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        procs.append(proc)
        lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: lines.put(proc.stdout.readline()), daemon=True).start()
        try:
            line = lines.get(timeout=BOOT_TIMEOUT_S)
        except queue.Empty:
            pytest.fail(f"{BOOT_TIMEOUT_S} 秒たっても起動ログが出ない")
        if not line:
            pytest.fail(f"起動に失敗した (終了コード {proc.wait()}): {proc.stderr.read()}")
        return RunningApp(port=port, startup_line=line.rstrip("\n"), proc=proc)

    yield start
    for proc in procs:
        proc.terminate()
        proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()
