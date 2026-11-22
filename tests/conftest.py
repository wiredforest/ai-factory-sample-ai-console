import http.client
import os
import queue
import socket
import subprocess
import sys
import threading
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
    monkeypatch.setenv("CONSOLE_DB_PATH", str(tmp_path / "console.sqlite3"))


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

    def get(self, path: str) -> Response:
        """リダイレクトを辿らずに GET する。"""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path)
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
        return RunningApp(port=port, startup_line=line.rstrip("\n"))

    yield start
    for proc in procs:
        proc.terminate()
        proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()
