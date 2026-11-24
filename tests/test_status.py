import http.client
import json
import os
import re
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from test_app import TextById
from test_llm import FakeUrlopen, http_error

from console import llm, status
from console.db import Database
from console.server import ConsoleServer

LABELS = [("app", "アプリ自身"), ("llm", "AI への接続"), ("db", "会話の記録")]


@pytest.fixture
def db():
    with Database(os.environ["CONSOLE_DB_PATH"]) as db:
        yield db


def items(app) -> list[dict]:
    r = app.get("/api/status")
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/json; charset=utf-8"
    return json.loads(r.body)["items"]


def oks(items: list[dict]) -> list[bool]:
    assert [(item["key"], item["label"]) for item in items] == LABELS
    return [item["ok"] for item in items]


def test_all_three_are_ok_with_fake_and_a_writable_record(start_app):
    assert items(start_app()) == [
        {"key": "app", "label": "アプリ自身", "ok": True},
        {"key": "llm", "label": "AI への接続", "ok": True},
        {"key": "db", "label": "会話の記録", "ok": True},
    ]


def test_fake_down_makes_only_llm_false(start_app):
    assert oks(items(start_app(CONSOLE_LLM="fake-down"))) == [True, False, True]


def test_anthropic_without_key_makes_only_llm_false(start_app):
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert oks(items(start_app(CONSOLE_LLM="anthropic"))) == [True, False, True]


def test_unopenable_record_keeps_the_app_up_and_makes_only_db_false(tmp_path, start_app):
    app = start_app(CONSOLE_DB_PATH=str(tmp_path / "missing" / "console.sqlite3"))
    assert oks(items(app)) == [True, True, False]
    health = app.get("/health")
    assert (health.status, health.body) == (200, b"ok")
    for r in (app.get("/api/conversations"), app.post_json("/api/messages", {"conversation_id": None, "text": "りんごの話"})):
        assert r.status == 503
        assert json.loads(r.body)["error"] == "db_unavailable"
    assert not (tmp_path / "missing").exists()


def test_a_record_that_becomes_openable_later_stays_false_until_restart(tmp_path, start_app):
    record = tmp_path / "missing" / "console.sqlite3"
    app = start_app(CONSOLE_DB_PATH=str(record))
    record.parent.mkdir()
    Database(str(record)).close()
    assert oks(items(app)) == [True, True, False]
    assert app.get("/api/conversations").status == 503


def test_checking_three_times_changes_no_conversation_or_message(start_app, db):
    conversation = db.add_exchange(None, "りんごの話", "fake reply #1: りんごの話")
    before = db.list_conversations()
    app = start_app()
    for _ in range(3):
        assert oks(items(app)) == [True, True, True]
    assert db.list_conversations() == before
    assert len(before) == 1
    assert len(db.conversation(conversation).messages) == 2


def test_page_has_the_recheck_button_and_reads_the_status(start_app):
    page = start_app().get("/").body.decode("utf-8")
    parser = TextById()
    parser.feed(page)
    assert "再確認" in parser.text["status"]
    assert re.search(r'<button[^>]*id="recheck"[^>]*>再確認</button>', page)
    assert 'fetch("/api/status")' in page
    assert 'recheckButton.addEventListener("click", loadStatus)' in page


class Exploding:
    def reply(self, messages):
        raise RuntimeError("boom")

    def reachable(self) -> bool:
        raise RuntimeError("boom")


def test_a_probe_that_raises_leaves_the_other_two_and_the_200(capsys):
    with ConsoleServer(0, os.environ["CONSOLE_DB_PATH"], Exploding()) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            try:
                conn.request("GET", "/api/status")
                r = conn.getresponse()
                assert r.status == 200
                assert oks(json.loads(r.read())["items"]) == [True, False, True]
            finally:
                conn.close()
        finally:
            server.shutdown()
    err = capsys.readouterr().err
    assert "llm の確認が RuntimeError を出しました" in err
    assert "boom" not in err


def serve_health(code: int, body: bytes) -> HTTPServer:
    class Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(code)
            if code == 302:
                self.send_header("Location", "/health-elsewhere")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args) -> None:
            pass

    stub = HTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    return stub


@pytest.mark.parametrize(
    ("code", "body", "expected"),
    [(200, b"ok", True), (200, b"nope", False), (302, b"ok", False), (500, b"ok", False)],
)
def test_app_is_ok_only_for_200_and_body_ok(code, body, expected):
    stub = serve_health(code, body)
    try:
        assert status.app_ok(stub.server_address[1]) is expected
    finally:
        stub.shutdown()
        stub.server_close()


def test_app_is_false_when_nothing_listens():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert status.app_ok(port) is False


@pytest.mark.parametrize(("stub", "llm_ok"), [
    ({"answer": {"type": "model", "id": "claude-sonnet-5-5"}}, True),
    ({"error": http_error(404)}, False),
], ids=["200", "HTTPError-404"])
def test_llm_is_ok_only_when_the_chosen_model_is_found(monkeypatch, db, stub, llm_ok):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake = FakeUrlopen(**stub)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    stub_app = serve_health(200, b"ok")
    try:
        port = stub_app.server_address[1]
        db_path = os.environ["CONSOLE_DB_PATH"]
        checked = status.check(port, db_path, llm.Anthropic("claude-sonnet-5-5"))["items"]
        with_fake = status.check(port, db_path, llm.Fake())["items"]
    finally:
        stub_app.shutdown()
        stub_app.server_close()
    assert oks(checked) == [True, llm_ok, True]
    assert [checked[0], checked[2]] == [with_fake[0], with_fake[2]]
    # 状態の確認はモデルの情報を 1 回取るだけで、Messages API は呼ばない。
    assert [(request.get_method(), request.full_url) for request, _ in fake.calls] == [
        ("GET", "https://api.anthropic.com/v1/models/claude-sonnet-5-5"),
    ]
