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
    """urllib.request.urlopen の差し替え。受け取った要求を記録し、決めた応答 (bytes ならそのまま、ほかは JSON) を返すか例外を送出する。

    answers を渡すと、呼ばれるたびにその順で応答を返す (例外なら送出する)。尽きたら最初からくり返す。
    """

    def __init__(self, answer: object = None, error: Exception | None = None, answers: list[object] | None = None) -> None:
        self.answers = answers if answers is not None else [error if error is not None else answer]
        self.calls: list[tuple[urllib.request.Request, float]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
        put_headers(request)
        self.calls.append((request, timeout))
        answer = self.answers[(len(self.calls) - 1) % len(self.answers)]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, bytes):
            return io.BytesIO(answer)
        return io.BytesIO(json.dumps(answer).encode("utf-8"))


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
    notes = [llm.TRUNCATED_NOTE, llm.REFUSED_NOTE, llm.SEARCH_FAILED_NOTE]
    assert all(note.strip() for note in notes)
    assert len(set(notes)) == len(notes)
    assert not any("\n" in note for note in notes)


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


MODEL_INFO = {"type": "model", "id": "claude-sonnet-5-5", "display_name": "Claude Sonnet 5.5"}


@pytest.mark.parametrize(("stub", "reachable"), [
    ({"answer": MODEL_INFO}, True),
    ({"answer": b"not json"}, True),
    ({"error": http_error(404)}, False),
    ({"error": http_error(401)}, False),
    ({"error": http_error(500)}, False),
    ({"error": urllib.error.URLError("connection refused")}, False),
], ids=["200", "200-not-json", "HTTPError-404", "HTTPError-401", "HTTPError-500", "URLError"])
def test_anthropic_check_gets_the_chosen_model(stub_urlopen, api_key, monkeypatch, stub, reachable):
    monkeypatch.setenv("CONSOLE_MODEL", "claude-sonnet-5-5")
    fake = stub_urlopen(**stub)
    assert responder("anthropic").reachable() is reachable
    [(request, timeout)] = fake.calls
    assert request.get_method() == "GET"
    assert request.full_url == "https://api.anthropic.com/v1/models/claude-sonnet-5-5"
    assert request.data is None
    assert request.get_header("X-api-key") == LEAKY_KEY
    assert timeout == 5


def test_anthropic_check_puts_the_model_name_in_one_segment_of_the_path(stub_urlopen, api_key, monkeypatch):
    monkeypatch.setenv("CONSOLE_MODEL", "a/b c")
    fake = stub_urlopen(error=http_error(404))
    assert responder("anthropic").reachable() is False
    [(request, _)] = fake.calls
    assert request.full_url == "https://api.anthropic.com/v1/models/a%2Fb%20c"


def test_calling_urlopen_without_a_stub_fails_the_test(api_key):
    with pytest.raises(pytest.fail.Exception):
        urllib.request.urlopen("https://api.anthropic.com/v1/models")
    with pytest.raises(pytest.fail.Exception):
        responder("anthropic").reply(FRUIT)


def searched(*content: dict, stop_reason: str = "end_turn") -> dict:
    """検索を 1 回使った応答。server_tool_use と web_search_tool_result (結果は results) の後に content を続ける。"""
    return {"stop_reason": stop_reason, "content": [
        {"type": "server_tool_use", "id": "srvtoolu_01", "name": "web_search", "input": {"query": "びわ"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_01", "content": [
            {"type": "web_search_result", "url": "https://r.example/1", "title": "R1", "encrypted_content": "x"},
        ]},
        *content,
    ]}


def location(url: str, title: str | None) -> dict:
    return {"type": "web_search_result_location", "url": url, "title": title, "encrypted_index": "x", "cited_text": "..."}


CITED = searched(
    {"type": "text", "text": "びわの旬は"},
    {"type": "text", "text": "初夏です", "citations": [
        location("https://a.example/1", "A"),
        location("https://b.example/2", ""),
    ]},
    {"type": "text", "text": "。", "citations": [location("https://a.example/1", "A")]},
)
CITED_REPLY = "びわの旬は初夏です。\n\n出典:\n- A (https://a.example/1)\n- https://b.example/2"


def test_a_searched_reply_lists_its_cited_sources_once_each_after_the_text(stub_urlopen, api_key):
    stub_urlopen(answer=CITED)
    reply = responder("anthropic").reply(FRUIT)
    assert reply == CITED_REPLY
    assert reply.count("https://a.example/1") == 1


def test_a_searched_reply_without_citations_lists_the_search_results_in_order(stub_urlopen, api_key):
    stub_urlopen(answer={"stop_reason": "end_turn", "content": [
        {"type": "server_tool_use", "id": "srvtoolu_01", "name": "web_search", "input": {"query": "びわ"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_01", "content": [
            {"type": "web_search_result", "url": "https://c.example/3", "title": "C", "encrypted_content": "x"},
            {"type": "web_search_result", "url": "https://d.example/4", "title": "", "encrypted_content": "x"},
        ]},
        {"type": "server_tool_use", "id": "srvtoolu_02", "name": "web_search", "input": {"query": "びわ 旬"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_02", "content": [
            {"type": "web_search_result", "url": "https://c.example/3", "title": "C", "encrypted_content": "x"},
            {"type": "web_search_result", "url": "https://e.example/5", "title": "E", "encrypted_content": "x"},
        ]},
        {"type": "text", "text": "初夏です"},
    ]})
    assert responder("anthropic").reply(FRUIT) == (
        "初夏です\n\n出典:\n- C (https://c.example/3)\n- https://d.example/4\n- E (https://e.example/5)"
    )


@pytest.mark.parametrize("content", [
    [{"type": "text", "text": "前半"}, {"type": "text", "text": "後半"}],
    [{"type": "text", "text": "前半", "citations": [location("https://a.example/1", "A")]},
     {"type": "text", "text": "後半", "citations": None}],
    [],
], ids=["plain", "citations-without-a-search", "empty"])
def test_a_reply_without_a_search_is_only_its_text(stub_urlopen, api_key, content):
    stub_urlopen(answer={"stop_reason": "end_turn", "content": content})
    reply = responder("anthropic").reply(FRUIT)
    assert "出典:" not in reply
    assert reply == "".join(block["text"] for block in content)


SEARCH = {"type": "server_tool_use", "id": "srvtoolu_01", "name": "web_search", "input": {"query": "びわ"}}
SEARCH_ERROR = {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_01",
                "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"}}
FAILED = {"stop_reason": "end_turn", "content": [SEARCH, SEARCH_ERROR, {"type": "text", "text": "知っている範囲では…"}]}
FAILED_REPLY = "知っている範囲では…\n\n" + llm.SEARCH_FAILED_NOTE


def test_a_reply_after_a_failed_search_is_its_text_and_then_the_note(stub_urlopen, api_key):
    stub_urlopen(answer=FAILED)
    reply = responder("anthropic").reply(FRUIT)
    assert reply == FAILED_REPLY
    assert "出典:" not in reply


def test_the_note_is_added_once_however_many_searches_failed(stub_urlopen, api_key):
    stub_urlopen(answer={"stop_reason": "end_turn", "content": [
        SEARCH, SEARCH_ERROR,
        {**SEARCH, "id": "srvtoolu_02"},
        {**SEARCH_ERROR, "tool_use_id": "srvtoolu_02",
         "content": {"type": "web_search_tool_result_error", "error_code": "too_many_requests"}},
        {"type": "text", "text": "検索できませんでした"},
    ]})
    reply = responder("anthropic").reply(FRUIT)
    assert reply == "検索できませんでした\n\n" + llm.SEARCH_FAILED_NOTE
    assert reply.count(llm.SEARCH_FAILED_NOTE) == 1


def test_a_failed_search_with_no_text_is_only_the_note(stub_urlopen, api_key):
    stub_urlopen(answer={"stop_reason": "end_turn", "content": [SEARCH, SEARCH_ERROR]})
    assert responder("anthropic").reply(FRUIT) == llm.SEARCH_FAILED_NOTE


@pytest.mark.parametrize("answer", [CITED, searched(), searched({"type": "text", "text": "答え"})],
                         ids=["cited", "no-text", "uncited"])
def test_a_successful_search_adds_no_note(stub_urlopen, api_key, answer):
    stub_urlopen(answer=answer)
    assert llm.SEARCH_FAILED_NOTE not in responder("anthropic").reply(FRUIT)


def test_the_note_comes_before_the_sources_of_the_searches_that_worked(stub_urlopen, api_key):
    stub_urlopen(answer=searched(
        {**SEARCH, "id": "srvtoolu_02"}, {**SEARCH_ERROR, "tool_use_id": "srvtoolu_02"},
        {"type": "text", "text": "答え"}, stop_reason="max_tokens",
    ))
    assert responder("anthropic").reply(FRUIT) == (
        "答え\n\n" + llm.SEARCH_FAILED_NOTE + "\n\n出典:\n- R1 (https://r.example/1)\n\n" + llm.TRUNCATED_NOTE
    )


def test_a_reply_after_a_failed_search_is_recorded_with_its_note_and_sent_back_as_is(stub_urlopen, api_key):
    fake = stub_urlopen(answer=FAILED)
    path = config.load().db_path
    with Database(path) as db:
        first = chat.send(db, responder("anthropic"), {"conversation_id": None, "text": FRUIT[0]["content"]})
        assert first["messages"][-1] == {"role": "assistant", "text": FAILED_REPLY}
    with Database(path) as db:
        assert [(m.role, m.content) for m in db.conversation(first["conversation_id"]).messages] == [
            ("user", FRUIT[0]["content"]),
            ("assistant", FAILED_REPLY),
        ]
        chat.send(db, responder("anthropic"), {"conversation_id": first["conversation_id"], "text": "ほかには"})
    after = json.loads(fake.calls[-1][0].data)
    assert after["messages"][1] == {"role": "assistant", "content": FAILED_REPLY}


def test_the_sources_come_before_the_note_of_a_cut_off_reply(stub_urlopen, api_key):
    stub_urlopen(answer=searched({"type": "text", "text": "途中", "citations": [location("https://a.example/1", "A")]},
                                 stop_reason="max_tokens"))
    assert responder("anthropic").reply(FRUIT) == "途中\n\n出典:\n- A (https://a.example/1)\n\n" + llm.TRUNCATED_NOTE


def test_a_searched_reply_with_no_text_is_only_its_sources(stub_urlopen, api_key):
    stub_urlopen(answer=searched())
    assert responder("anthropic").reply(FRUIT) == "出典:\n- R1 (https://r.example/1)"


def test_a_title_keeps_its_source_on_one_line(stub_urlopen, api_key):
    stub_urlopen(answer=searched({"type": "text", "text": "答え", "citations": [
        location("https://a.example/1", "びわ\nの旬"),
        location("https://b.example/2", None),
    ]}))
    assert responder("anthropic").reply(FRUIT) == "答え\n\n出典:\n- びわ の旬 (https://a.example/1)\n- https://b.example/2"


def test_a_searched_reply_is_recorded_with_its_sources_and_sent_back_as_is(stub_urlopen, api_key):
    fake = stub_urlopen(answer=CITED)
    path = config.load().db_path
    with Database(path) as db:
        first = chat.send(db, responder("anthropic"), {"conversation_id": None, "text": FRUIT[0]["content"]})
        assert first["messages"][-1] == {"role": "assistant", "text": CITED_REPLY}
    with Database(path) as db:
        assert [(m.role, m.content) for m in db.conversation(first["conversation_id"]).messages] == [
            ("user", FRUIT[0]["content"]),
            ("assistant", CITED_REPLY),
        ]
        chat.send(db, responder("anthropic"), {"conversation_id": first["conversation_id"], "text": "ほかには"})
    after = json.loads(fake.calls[-1][0].data)
    assert after["messages"][1] == {"role": "assistant", "content": CITED_REPLY}


PAUSED = {"stop_reason": "pause_turn", "content": [
    {"type": "text", "text": "前半"},
    {"type": "server_tool_use", "id": "srvtoolu_01", "name": "web_search", "input": {"query": "びわ"}},
]}
FINISHED = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "後半"}]}


def posted(fake: FakeUrlopen) -> list[dict]:
    assert all(request.get_method() == "POST" and request.full_url.endswith("/messages") for request, _ in fake.calls)
    return [json.loads(request.data) for request, _ in fake.calls]


def test_a_paused_reply_is_continued_with_its_content_and_the_same_request(stub_urlopen, api_key):
    fake = stub_urlopen(answers=[PAUSED, FINISHED])
    reply = responder("anthropic").reply(FRUIT_FOLLOW_UP)
    first, second = posted(fake)
    assert first["messages"] == FRUIT_FOLLOW_UP
    assert second["messages"] == [*FRUIT_FOLLOW_UP, {"role": "assistant", "content": PAUSED["content"]}]
    for key in ("model", "system", "tools", "max_tokens"):
        assert second[key] == first[key]
    assert reply == "前半後半"
    assert llm.UNFINISHED_NOTE not in reply


def test_a_reply_still_paused_after_two_continues_says_it_is_unfinished(stub_urlopen, api_key):
    fake = stub_urlopen(answers=[PAUSED])
    reply = responder("anthropic").reply(FRUIT)
    first, second, third = posted(fake)
    assert first["messages"] == FRUIT
    assert second["messages"] == [*FRUIT, {"role": "assistant", "content": PAUSED["content"]}]
    assert third["messages"] == [*second["messages"], {"role": "assistant", "content": PAUSED["content"]}]
    assert reply == "前半前半前半\n\n" + llm.UNFINISHED_NOTE


def test_the_unfinished_note_is_its_own_sentence():
    assert llm.UNFINISHED_NOTE.strip() and "\n" not in llm.UNFINISHED_NOTE
    assert llm.UNFINISHED_NOTE not in (llm.TRUNCATED_NOTE, llm.REFUSED_NOTE)


@pytest.mark.parametrize("answer", [FINISHED, {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "途中"}]}],
                         ids=["end_turn", "max_tokens"])
def test_a_reply_that_did_not_pause_is_posted_once(stub_urlopen, api_key, answer):
    fake = stub_urlopen(answer=answer)
    responder("anthropic").reply(FRUIT)
    assert len(posted(fake)) == 1


@pytest.mark.parametrize(("error", "kind"), [
    (http_error(429), llm.Busy),
    (http_error(401), llm.KeyRejected),
    (TimeoutError("timed out"), llm.Unreachable),
], ids=["HTTPError-429", "HTTPError-401", "TimeoutError"])
def test_a_failed_continue_fails_the_send_like_the_first_post(stub_urlopen, api_key, error, kind):
    fake = stub_urlopen(answers=[PAUSED, error])
    with pytest.raises(llm.Unreachable) as raised:
        responder("anthropic").reply(FRUIT)
    assert type(raised.value) is kind
    assert len(fake.calls) == 2
    assert "LEAKCHECK" not in "".join(traceback.format_exception(raised.value))


def test_a_continue_of_an_unexpected_shape_becomes_unreachable(stub_urlopen, api_key):
    stub_urlopen(answers=[PAUSED, {"content": None}])
    with pytest.raises(llm.Unreachable):
        responder("anthropic").reply(FRUIT)


def test_the_sources_and_the_note_follow_the_text_of_every_part(stub_urlopen, api_key):
    stub_urlopen(answers=[
        {"stop_reason": "pause_turn", "content": searched({"type": "text", "text": "びわの旬は"})["content"]},
        {"stop_reason": "max_tokens", "content": [
            {"type": "text", "text": "初夏です", "citations": [location("https://a.example/1", "A")]},
        ]},
    ])
    assert responder("anthropic").reply(FRUIT) == (
        "びわの旬は初夏です\n\n出典:\n- A (https://a.example/1)\n\n" + llm.TRUNCATED_NOTE
    )


def test_a_continued_reply_is_recorded_as_one_exchange_and_sent_back_as_is(stub_urlopen, api_key):
    fake = stub_urlopen(answers=[PAUSED, FINISHED])
    path = config.load().db_path
    with Database(path) as db:
        first = chat.send(db, responder("anthropic"), {"conversation_id": None, "text": FRUIT[0]["content"]})
        assert first["messages"][-1] == {"role": "assistant", "text": "前半後半"}
    with Database(path) as db:
        assert len(db.list_conversations()) == 1
        assert [(m.role, m.content) for m in db.conversation(first["conversation_id"]).messages] == [
            ("user", FRUIT[0]["content"]),
            ("assistant", "前半後半"),
        ]
        fake.answers = [FINISHED]
        chat.send(db, responder("anthropic"), {"conversation_id": first["conversation_id"], "text": "ほかには"})
    assert posted(fake)[-1]["messages"] == [
        {"role": "user", "content": FRUIT[0]["content"]},
        {"role": "assistant", "content": "前半後半"},
        {"role": "user", "content": "ほかには"},
    ]
