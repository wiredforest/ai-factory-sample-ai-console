import http.client
import io
import json
import traceback
import urllib.error
import urllib.request
from datetime import date

import pytest

from console import chat, config, llm
from console.db import Database

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


LEAKY_KEY = "sk-ant-test-LEAKCHECK-0123456789"


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", LEAKY_KEY)


def responder(name: str) -> llm.Responder:
    """アプリと同じく、環境変数から読んだ設定で応答役を作る。"""
    return llm.responder(name, config.load().model)


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(llm.API_URL, code, "error", hdrs=None, fp=None)


def test_every_llm_choice_has_a_responder():
    assert set(llm.RESPONDERS) == set(config.LLM_CHOICES)


def test_fake_replies_with_the_count_and_the_last_utterance():
    assert responder("fake").reply(FRUIT) == "fake reply #1: 私の好きな果物はびわです"


def test_fake_counts_every_user_message_in_the_history():
    assert responder("fake").reply(FRUIT_FOLLOW_UP) == "fake reply #2: 私の好きな果物は何ですか"


def test_fake_is_reachable_and_fake_down_is_not():
    assert responder("fake").reachable() is True
    assert responder("fake-down").reachable() is False
    with pytest.raises(llm.Unreachable):
        responder("fake-down").reply(FRUIT)


def test_anthropic_without_a_key_stays_off_the_network(stub_urlopen):
    fake = stub_urlopen(answer={})
    anthropic = responder("anthropic")
    with pytest.raises(llm.Unreachable):
        anthropic.reply(FRUIT)
    assert anthropic.reachable() is False
    assert fake.calls == []


def test_anthropic_posts_the_whole_history_and_joins_the_text(stub_urlopen, api_key):
    fake = stub_urlopen(answer={"content": [{"type": "text", "text": "こんにちは"}]})
    assert responder("anthropic").reply(FRUIT_FOLLOW_UP) == "こんにちは"
    [(request, timeout)] = fake.calls
    assert request.get_method() == "POST"
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_header("X-api-key") == LEAKY_KEY
    assert request.get_header("Anthropic-version") == "2023-06-01"
    assert request.get_header("Content-type") == "application/json"
    body = json.loads(request.data)
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 4096
    assert body["messages"] == FRUIT_FOLLOW_UP
    assert timeout == 90


def test_anthropic_tells_the_date_of_each_send_in_the_system_prompt(stub_urlopen, api_key, monkeypatch):
    fake = stub_urlopen(answer={"content": [{"type": "text", "text": "はい"}]})
    anthropic = responder("anthropic")
    path = config.load().db_path
    with Database(path) as db:
        monkeypatch.setattr(llm, "today", lambda: date(2026, 11, 24))
        first = chat.send(db, anthropic, {"conversation_id": None, "text": FRUIT[0]["content"]})
        monkeypatch.setattr(llm, "today", lambda: date(2027, 1, 2))
        chat.send(db, anthropic, {"conversation_id": first["conversation_id"], "text": "今日は何日ですか"})
    [before, after] = [json.loads(request.data)["system"] for request, _ in fake.calls]
    assert isinstance(before, str)
    assert "2026-11-24" in before
    assert "日本語" in before
    assert "簡潔" in before
    assert isinstance(after, str)
    assert "2027-01-02" in after
    assert "2026-11-24" not in after


def test_the_system_prompt_stays_out_of_the_messages_and_the_record(stub_urlopen, api_key):
    fake = stub_urlopen(answer={"content": [{"type": "text", "text": "はい"}]})
    anthropic = responder("anthropic")
    with Database(config.load().db_path) as db:
        conversation_id = None
        for sends, text in enumerate(["一つ目", "二つ目", "三つ目"], start=1):
            sent = chat.send(db, anthropic, {"conversation_id": conversation_id, "text": text})
            conversation_id = sent["conversation_id"]
            recorded = db.conversation(conversation_id).messages
            assert len(recorded) == 2 * sends
            assert {m.role for m in recorded} == {"user", "assistant"}
            assert not any(m.content == json.loads(fake.calls[-1][0].data)["system"] for m in recorded)
    for request, timeout in fake.calls:
        body = json.loads(request.data)
        assert {m["role"] for m in body["messages"]} <= {"user", "assistant"}
        assert timeout == 90


@pytest.mark.parametrize(("value", "model"), [
    ("claude-sonnet-5-5", "claude-sonnet-5-5"),
    (None, "claude-haiku-5-5"),
], ids=["sonnet", "unset"])
def test_anthropic_sends_the_model_from_console_model(stub_urlopen, api_key, monkeypatch, value, model):
    if value is not None:
        monkeypatch.setenv("CONSOLE_MODEL", value)
    fake = stub_urlopen(answer={"content": [{"type": "text", "text": "こんにちは"}]})
    responder("anthropic").reply(FRUIT)
    [(request, _)] = fake.calls
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert json.loads(request.data)["model"] == model


def test_a_past_conversation_continues_with_the_model_chosen_after_a_restart(stub_urlopen, api_key, monkeypatch):
    fake = stub_urlopen(answer={"content": [{"type": "text", "text": "びわですね"}]})
    path = config.load().db_path
    monkeypatch.setenv("CONSOLE_MODEL", "claude-haiku-5-5")
    with Database(path) as db:
        first = chat.send(db, responder("anthropic"), {"conversation_id": None, "text": FRUIT[0]["content"]})
    monkeypatch.setenv("CONSOLE_MODEL", "claude-opus-5-5")
    with Database(path) as db:
        chat.send(db, responder("anthropic"), {"conversation_id": first["conversation_id"], "text": "私の好きな果物は何ですか"})
        assert len(db.conversation(first["conversation_id"]).messages) == 4
    [before, after] = [json.loads(request.data) for request, _ in fake.calls]
    assert before["model"] == "claude-haiku-5-5"
    assert after["model"] == "claude-opus-5-5"
    assert after["messages"] == [
        {"role": "user", "content": "私の好きな果物はびわです"},
        {"role": "assistant", "content": "びわですね"},
        {"role": "user", "content": "私の好きな果物は何ですか"},
    ]


@pytest.mark.parametrize(("model", "version"), [
    ("claude-haiku-5-5", "web_search_20250305"),
    ("claude-sonnet-5-5", "web_search_20260209"),
    ("claude-opus-5-5", "web_search_20260209"),
    ("claude-unknown-9", "web_search_20250305"),
], ids=["haiku", "sonnet", "opus", "unlisted"])
def test_anthropic_offers_the_web_search_tool_of_the_version_the_model_supports(
        stub_urlopen, api_key, monkeypatch, model, version):
    monkeypatch.setenv("CONSOLE_MODEL", model)
    fake = stub_urlopen(answer={"content": [{"type": "text", "text": "こんにちは"}]})
    responder("anthropic").reply(FRUIT)
    [(request, _)] = fake.calls
    body = json.loads(request.data)
    assert body["model"] == model
    assert body["tools"] == [{"type": version, "name": "web_search", "max_uses": 3}]


def test_anthropic_offers_only_the_web_search_tool_and_leaves_the_search_to_the_ai(stub_urlopen, api_key):
    fake = stub_urlopen(answer={"content": [{"type": "text", "text": "こんにちは"}]})
    responder("anthropic").reply(FRUIT)
    [(request, _)] = fake.calls
    body = json.loads(request.data)
    [tool] = body["tools"]
    assert set(tool) == {"type", "name", "max_uses"}
    assert "tool_choice" not in body
    for key in ("allowed_domains", "blocked_domains", "tool_choice"):
        assert key not in request.data.decode("utf-8")


def test_anthropic_reply_after_a_search_joins_only_the_text_blocks(stub_urlopen, api_key):
    stub_urlopen(answer={"stop_reason": "end_turn", "content": [
        {"type": "text", "text": "検索します"},
        {"type": "server_tool_use", "id": "srvtoolu_01", "name": "web_search", "input": {"query": "QUERYMARK びわ 旬"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_01", "content": [
            {"type": "web_search_result", "url": "https://example.com/biwa", "title": "びわの旬",
             "encrypted_content": "RESULTMARK", "page_age": None},
        ]},
        {"type": "text", "text": "答えです"},
    ]})
    reply = responder("anthropic").reply(FRUIT)
    assert reply.index("検索します") < reply.index("答えです")
    assert "QUERYMARK" not in reply
    assert "RESULTMARK" not in reply


@pytest.mark.parametrize("model", [None, "", "claude-sonnet-5-5", "claude-opus-5-5", "claude-unknown-9"],
                         ids=["unset", "empty", "sonnet", "opus", "unlisted"])
@pytest.mark.parametrize("name", ["fake", "fake-down"])
def test_fake_responders_stay_off_the_network_whatever_the_model(stub_urlopen, api_key, monkeypatch, name, model):
    if model is not None:
        monkeypatch.setenv("CONSOLE_MODEL", model)
    fake = stub_urlopen(answer={"content": [{"type": "text", "text": "こんにちは"}]})
    chosen = responder(name)
    if name == "fake":
        assert chosen.reply(FRUIT_FOLLOW_UP) == "fake reply #2: 私の好きな果物は何ですか"
        assert chosen.reachable() is True
    else:
        with pytest.raises(llm.Unreachable):
            chosen.reply(FRUIT_FOLLOW_UP)
        assert chosen.reachable() is False
    assert fake.calls == []


def test_anthropic_skips_blocks_that_are_not_text(stub_urlopen, api_key):
    stub_urlopen(answer={"content": [
        {"type": "text", "text": "前半"},
        {"type": "thinking", "thinking": "..."},
        {"type": "text", "text": "後半"},
    ]})
    assert responder("anthropic").reply(FRUIT) == "前半後半"


@pytest.mark.parametrize(("answer", "reply"), [
    ({"stop_reason": "max_tokens", "content": [{"type": "text", "text": "前半です"}]}, "前半です\n\n" + llm.TRUNCATED_NOTE),
    ({"stop_reason": "max_tokens", "content": [{"type": "thinking", "thinking": "..."}]}, llm.TRUNCATED_NOTE),
    ({"stop_reason": "max_tokens", "content": []}, llm.TRUNCATED_NOTE),
    ({"stop_reason": "refusal", "content": []}, llm.REFUSED_NOTE),
    ({"stop_reason": "refusal", "content": [{"type": "text", "text": "一部"}]}, llm.REFUSED_NOTE + "\n\n一部"),
    ({"stop_reason": "end_turn", "content": [{"type": "text", "text": "完成"}]}, "完成"),
    ({"stop_reason": "stop_sequence", "content": [{"type": "text", "text": "完成"}]}, "完成"),
], ids=["max_tokens", "max_tokens-thinking-only", "max_tokens-empty", "refusal-empty", "refusal-partial",
        "end_turn", "stop_sequence"])
def test_anthropic_says_when_a_reply_was_cut_off_or_refused(stub_urlopen, api_key, answer, reply):
    stub_urlopen(answer=answer)
    assert responder("anthropic").reply(FRUIT) == reply


def test_the_notes_are_distinct_sentences():
    assert llm.TRUNCATED_NOTE.strip() and llm.REFUSED_NOTE.strip()
    assert llm.TRUNCATED_NOTE != llm.REFUSED_NOTE
    assert "\n" not in llm.TRUNCATED_NOTE + llm.REFUSED_NOTE


@pytest.mark.parametrize(("answer", "reply"), [
    ({"stop_reason": "max_tokens", "content": [{"type": "text", "text": "前半です"}]}, "前半です\n\n" + llm.TRUNCATED_NOTE),
    ({"stop_reason": "refusal", "content": [{"type": "text", "text": "一部"}]}, llm.REFUSED_NOTE + "\n\n一部"),
], ids=["max_tokens", "refusal"])
def test_a_cut_off_or_refused_reply_is_recorded_with_its_note_and_sent_back_as_is(stub_urlopen, api_key, answer, reply):
    fake = stub_urlopen(answer=answer)
    path = config.load().db_path
    with Database(path) as db:
        first = chat.send(db, responder("anthropic"), {"conversation_id": None, "text": FRUIT[0]["content"]})
        assert first["messages"][-1] == {"role": "assistant", "text": reply}
        [*_, recorded] = db.conversation(first["conversation_id"]).messages
        assert (recorded.role, recorded.content) == ("assistant", reply)
    with Database(path) as db:
        assert [(m.role, m.content) for m in db.conversation(first["conversation_id"]).messages] == [
            ("user", FRUIT[0]["content"]),
            ("assistant", reply),
        ]
        chat.send(db, responder("anthropic"), {"conversation_id": first["conversation_id"], "text": "続けてください"})
    after = json.loads(fake.calls[-1][0].data)
    assert after["messages"] == [
        {"role": "user", "content": FRUIT[0]["content"]},
        {"role": "assistant", "content": reply},
        {"role": "user", "content": "続けてください"},
    ]


@pytest.mark.parametrize(("error", "kind"), [
    (http_error(401), llm.KeyRejected),
    (http_error(403), llm.KeyRejected),
    (http_error(429), llm.Busy),
    (http_error(529), llm.Busy),
    (http_error(500), llm.Unreachable),
    (http_error(404), llm.Unreachable),
    (urllib.error.URLError("connection refused"), llm.Unreachable),
    (TimeoutError("timed out"), llm.Unreachable),
], ids=["HTTPError-401", "HTTPError-403", "HTTPError-429", "HTTPError-529", "HTTPError-500", "HTTPError-404",
        "URLError", "TimeoutError"])
def test_anthropic_failures_are_told_apart_without_the_key(stub_urlopen, api_key, error, kind):
    stub_urlopen(error=error)
    with pytest.raises(llm.Unreachable) as raised:
        responder("anthropic").reply(FRUIT)
    assert type(raised.value) is kind
    assert "LEAKCHECK" not in "".join(traceback.format_exception(raised.value))


def test_anthropic_strips_the_line_break_around_the_key(stub_urlopen, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key\r\n")
    fake = stub_urlopen(answer={"content": [{"type": "text", "text": "こんにちは"}]})
    assert responder("anthropic").reply(FRUIT) == "こんにちは"
    [(request, _)] = fake.calls
    assert request.get_header("X-api-key") == "test-key"


@pytest.mark.parametrize("key", ["sk-SECRET\nX-Injected: 1", "sk-SECRET-鍵"], ids=["line-break", "non-latin-1"])
def test_anthropic_key_unfit_for_a_header_becomes_unreachable_without_the_key(stub_urlopen, monkeypatch, key):
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    stub_urlopen(answer={})
    anthropic = responder("anthropic")
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
    with pytest.raises(llm.Unreachable) as raised:
        responder("anthropic").reply(FRUIT)
    assert "LEAKCHECK" not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize(("stub", "reachable"), [
    ({"answer": {"data": []}}, True),
    ({"answer": b"not json"}, True),
    ({"error": http_error(401)}, False),
], ids=["200", "200-not-json", "401"])
def test_anthropic_check_lists_the_models(stub_urlopen, api_key, stub, reachable):
    fake = stub_urlopen(**stub)
    assert responder("anthropic").reachable() is reachable
    [(request, timeout)] = fake.calls
    assert request.get_method() == "GET"
    assert request.full_url == "https://api.anthropic.com/v1/models"
    assert request.get_header("X-api-key") == LEAKY_KEY
    assert timeout == 5


def test_calling_urlopen_without_a_stub_fails_the_test(api_key):
    with pytest.raises(pytest.fail.Exception):
        urllib.request.urlopen("https://api.anthropic.com/v1/models")
    with pytest.raises(pytest.fail.Exception):
        responder("anthropic").reply(FRUIT)
