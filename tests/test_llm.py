import http.client
import io
import json
import traceback
import urllib.error
import urllib.request

import pytest

from console import config, llm

FRUIT = [{"role": "user", "content": "私の好きな果物はびわです"}]
FRUIT_FOLLOW_UP = [
    *FRUIT,
    {"role": "assistant", "content": "fake reply #1: 私の好きな果物はびわです"},
    {"role": "user", "content": "私の好きな果物は何ですか"},
]


class FakeUrlopen:
    """urllib.request.urlopen の差し替え。受け取った要求を記録し、決めた応答 (bytes ならそのまま、ほかは JSON) を返すか例外を送出する。"""

    def __init__(self, answer: object = None, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[tuple[urllib.request.Request, float]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
        put_headers(request)
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        if isinstance(self.answer, bytes):
            return io.BytesIO(self.answer)
        return io.BytesIO(json.dumps(self.answer).encode("utf-8"))


def put_headers(request: urllib.request.Request) -> None:
    """本物の urlopen と同じく http.client にヘッダを渡し、送れないヘッダを同じ例外で拒ませる。接続はしない。"""
    conn = http.client.HTTPSConnection(request.host)
    try:
        conn.putrequest(request.get_method(), request.selector)
        for name, value in request.header_items():
            conn.putheader(name, value)
    finally:
        conn.close()


@pytest.fixture
def stub_urlopen(monkeypatch):
    def install(**kwargs) -> FakeUrlopen:
        fake = FakeUrlopen(**kwargs)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        return fake

    return install


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(llm.API_URL, code, "error", hdrs=None, fp=None)


def test_every_llm_choice_has_a_responder():
    assert set(llm.RESPONDERS) == set(config.LLM_CHOICES)


def test_fake_replies_with_the_count_and_the_last_utterance():
    assert llm.responder("fake").reply(FRUIT) == "fake reply #1: 私の好きな果物はびわです"


def test_fake_counts_every_user_message_in_the_history():
    assert llm.responder("fake").reply(FRUIT_FOLLOW_UP) == "fake reply #2: 私の好きな果物は何ですか"


def test_fake_is_reachable_and_fake_down_is_not():
    assert llm.responder("fake").reachable() is True
    assert llm.responder("fake-down").reachable() is False
    with pytest.raises(llm.Unreachable):
        llm.responder("fake-down").reply(FRUIT)


def test_anthropic_without_a_key_stays_off_the_network(stub_urlopen):
    fake = stub_urlopen(answer={})
    anthropic = llm.responder("anthropic")
    with pytest.raises(llm.Unreachable):
        anthropic.reply(FRUIT)
    assert anthropic.reachable() is False
    assert fake.calls == []


def test_anthropic_posts_the_whole_history_and_joins_the_text(stub_urlopen, api_key):
    fake = stub_urlopen(answer={"content": [{"type": "text", "text": "こんにちは"}]})
    assert llm.responder("anthropic").reply(FRUIT_FOLLOW_UP) == "こんにちは"
    [(request, timeout)] = fake.calls
    assert request.get_method() == "POST"
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_header("X-api-key") == "test-key"
    assert request.get_header("Anthropic-version") == "2023-06-01"
    assert request.get_header("Content-type") == "application/json"
    body = json.loads(request.data)
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 1024
    assert body["messages"] == FRUIT_FOLLOW_UP
    assert timeout == 30


def test_anthropic_skips_blocks_that_are_not_text(stub_urlopen, api_key):
    stub_urlopen(answer={"content": [
        {"type": "text", "text": "前半"},
        {"type": "thinking", "thinking": "..."},
        {"type": "text", "text": "後半"},
    ]})
    assert llm.responder("anthropic").reply(FRUIT) == "前半後半"


@pytest.mark.parametrize("error", [
    urllib.error.URLError("connection refused"),
    http_error(500),
    TimeoutError("timed out"),
], ids=["URLError", "HTTPError-500", "TimeoutError"])
def test_anthropic_failures_become_unreachable_without_the_key(stub_urlopen, api_key, error):
    stub_urlopen(error=error)
    with pytest.raises(llm.Unreachable) as raised:
        llm.responder("anthropic").reply(FRUIT)
    assert "test-key" not in str(raised.value)


def test_anthropic_strips_the_line_break_around_the_key(stub_urlopen, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key\r\n")
    fake = stub_urlopen(answer={"content": [{"type": "text", "text": "こんにちは"}]})
    assert llm.responder("anthropic").reply(FRUIT) == "こんにちは"
    [(request, _)] = fake.calls
    assert request.get_header("X-api-key") == "test-key"


@pytest.mark.parametrize("key", ["sk-SECRET\nX-Injected: 1", "sk-SECRET-鍵"], ids=["line-break", "non-latin-1"])
def test_anthropic_key_unfit_for_a_header_becomes_unreachable_without_the_key(stub_urlopen, monkeypatch, key):
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    stub_urlopen(answer={})
    anthropic = llm.responder("anthropic")
    with pytest.raises(llm.Unreachable) as raised:
        anthropic.reply(FRUIT)
    assert "SECRET" not in "".join(traceback.format_exception(raised.value))
    assert anthropic.reachable() is False


@pytest.mark.parametrize("answer", [
    b"<html>not json</html>",
    {},
    {"content": None},
    {"content": [{"type": "text"}]},
], ids=["not-json", "no-content", "content-null", "text-block-without-text"])
def test_anthropic_reply_of_an_unexpected_shape_becomes_unreachable(stub_urlopen, api_key, answer):
    stub_urlopen(answer=answer)
    with pytest.raises(llm.Unreachable):
        llm.responder("anthropic").reply(FRUIT)


@pytest.mark.parametrize(("stub", "reachable"), [
    ({"answer": {"data": []}}, True),
    ({"answer": b"not json"}, True),
    ({"error": http_error(401)}, False),
], ids=["200", "200-not-json", "401"])
def test_anthropic_check_lists_the_models(stub_urlopen, api_key, stub, reachable):
    fake = stub_urlopen(**stub)
    assert llm.responder("anthropic").reachable() is reachable
    [(request, timeout)] = fake.calls
    assert request.get_method() == "GET"
    assert request.full_url == "https://api.anthropic.com/v1/models"
    assert request.get_header("X-api-key") == "test-key"
    assert timeout == 5


def test_calling_urlopen_without_a_stub_fails_the_test(api_key):
    with pytest.raises(pytest.fail.Exception):
        urllib.request.urlopen("https://api.anthropic.com/v1/models")
    with pytest.raises(pytest.fail.Exception):
        llm.responder("anthropic").reply(FRUIT)
