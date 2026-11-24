import http.client
import json
import os
import re
import socket
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest
from test_llm import LEAKY_KEY, FakeUrlopen, http_error, responder

from console import chat, llm
from console.db import Database
from console.server import ConsoleServer

FIRST = "私の好きな果物はびわです"
SECOND = "私の好きな果物は何ですか"


@pytest.fixture
def db():
    with Database(os.environ["CONSOLE_DB_PATH"]) as db:
        yield db


def send(app, conversation_id, text):
    r = app.post_json("/api/messages", {"conversation_id": conversation_id, "text": text})
    return r, json.loads(r.body)


def counts(db: Database) -> tuple[int, int]:
    conversations = db.list_conversations()
    return len(conversations), sum(len(db.conversation(c.id).messages) for c in conversations)


def test_first_message_starts_a_conversation_and_returns_both_bubbles(start_app):
    r, body = send(start_app(), None, FIRST)
    assert r.status == 200
    assert type(body["conversation_id"]) is int
    assert body["messages"] == [
        {"role": "user", "text": FIRST},
        {"role": "assistant", "text": f"fake reply #1: {FIRST}"},
    ]


def test_second_message_in_the_same_conversation_sees_the_first(start_app, db):
    app = start_app()
    _, first = send(app, None, FIRST)
    r, second = send(app, first["conversation_id"], SECOND)
    assert r.status == 200
    assert second["conversation_id"] == first["conversation_id"]
    assert second["messages"] == [
        {"role": "user", "text": SECOND},
        {"role": "assistant", "text": f"fake reply #2: {SECOND}"},
    ]
    [conversation] = db.list_conversations()
    assert conversation.id == first["conversation_id"]
    assert [m.role for m in db.conversation(conversation.id).messages] == ["user", "assistant", "user", "assistant"]


def test_the_record_survives_a_restart(start_app, db):
    app = start_app()
    _, first = send(app, None, FIRST)
    send(app, first["conversation_id"], SECOND)
    app.stop()
    start_app()
    assert [(m.role, m.content) for m in db.conversation(first["conversation_id"]).messages] == [
        ("user", FIRST),
        ("assistant", f"fake reply #1: {FIRST}"),
        ("user", SECOND),
        ("assistant", f"fake reply #2: {SECOND}"),
    ]


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_blank_text_is_refused_and_records_nothing(start_app, db, text):
    app = start_app()
    _, first = send(app, None, FIRST)
    before = counts(db)
    for conversation_id in (None, first["conversation_id"]):
        r, body = send(app, conversation_id, text)
        assert (r.status, body) == (400, {"error": "empty_text"})
    assert counts(db) == before


@pytest.mark.parametrize("conversation_id", [99999, 0, -1, 2**63, 2**80])
def test_unknown_conversation_is_refused_and_records_nothing(start_app, db, conversation_id):
    app = start_app()
    send(app, None, FIRST)
    before = counts(db)
    r, body = send(app, conversation_id, SECOND)
    assert (r.status, body) == (404, {"error": "conversation_not_found"})
    assert counts(db) == before


@pytest.mark.parametrize(
    "request_body",
    [b"", b"not json", b"[]", b'{"conversation_id": null}', b'{"conversation_id": "1", "text": "a"}',
     b'{"conversation_id": true, "text": "a"}', b'{"conversation_id": null, "text": 1}', b"\xff"],
)
def test_malformed_request_is_refused_and_records_nothing(start_app, db, request_body):
    r = start_app().request("POST", "/api/messages", request_body, {"Content-Type": "application/json"})
    assert (r.status, json.loads(r.body)) == (400, {"error": "invalid_request"})
    assert counts(db) == (0, 0)


def test_reply_is_one_json_response_with_a_length(start_app):
    r, _ = send(start_app(), None, FIRST)
    assert r.headers["Content-Type"].startswith("application/json")
    assert int(r.headers["Content-Length"]) == len(r.body)
    assert r.headers["Transfer-Encoding"] is None


def test_unreachable_ai_records_nothing_not_even_a_conversation(start_app, db):
    r, body = send(start_app(CONSOLE_LLM="fake-down"), None, FIRST)
    assert (r.status, body["error"]) == (503, "llm_unavailable")
    assert counts(db) == (0, 0)


def test_unreachable_ai_adds_nothing_to_an_existing_conversation(start_app, db):
    conversation = db.create_conversation()
    db.add_message(conversation, "user", FIRST)
    db.add_message(conversation, "assistant", f"fake reply #1: {FIRST}")
    before = db.list_conversations()
    r, body = send(start_app(CONSOLE_LLM="fake-down"), conversation, SECOND)
    assert (r.status, body["error"]) == (503, "llm_unavailable")
    assert db.list_conversations() == before
    assert len(db.conversation(conversation).messages) == 2


def test_anthropic_without_key_is_unavailable_without_going_out(start_app, db, monkeypatch):
    # HTTPS を受ける代理の罠。urllib は https_proxy に従うので、外へ出ようとすればここに繋がる。
    with socket.create_server(("127.0.0.1", 0)) as trap:
        for name in ("no_proxy", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
        proxy = f"http://127.0.0.1:{trap.getsockname()[1]}"
        monkeypatch.setenv("https_proxy", proxy)
        monkeypatch.setenv("HTTPS_PROXY", proxy)
        trap.setblocking(False)

        assert "ANTHROPIC_API_KEY" not in os.environ
        r, body = send(start_app(CONSOLE_LLM="anthropic"), None, FIRST)
        assert (r.status, body["error"]) == (503, "llm_unavailable")
        with pytest.raises(BlockingIOError):
            trap.accept()
        assert counts(db) == (0, 0)

        # 罠が効いていることの確かめ: 鍵があれば外へ出ようとして罠に繋がる。すぐ切るので繋がらない扱いになる。
        trap.setblocking(True)
        trap.settimeout(10)
        caught = []
        threading.Thread(target=lambda: caught.append(trap.accept()[0].close()), daemon=True).start()
        r, body = send(start_app(CONSOLE_LLM="anthropic", ANTHROPIC_API_KEY="not-a-real-key"), None, FIRST)
        assert (r.status, body["error"]) == (503, "llm_unavailable")
        assert caught == [None]
        assert counts(db) == (0, 0)


FAILURE_MESSAGES = {
    "llm_key_rejected": "AI の鍵 (ANTHROPIC_API_KEY) が正しくありません",
    "llm_busy": "AI が混んでいます。少し待ってから送り直してください",
    "llm_unavailable": "AI に繋がりません",
}


def post_in_process(responder: llm.Responder, conversation_id: int | None) -> tuple[int, bytes]:
    """同じプロセスでサーバを動かして送る。urlopen の差し替えがサーバにも効く。"""
    with ConsoleServer(0, os.environ["CONSOLE_DB_PATH"], responder) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            try:
                body = json.dumps({"conversation_id": conversation_id, "text": SECOND}, ensure_ascii=False)
                conn.request("POST", "/api/messages", body.encode("utf-8"), {"Content-Type": "application/json"})
                r = conn.getresponse()
                return r.status, r.read()
            finally:
                conn.close()
        finally:
            server.shutdown()


# stub が None のときは urlopen を差し替えない (conftest の refuse_network のまま)。外へ出ようとすればテストが失敗する。
@pytest.mark.parametrize(("llm_name", "key", "stub", "error"), [
    ("anthropic", LEAKY_KEY, {"error": http_error(401)}, "llm_key_rejected"),
    ("anthropic", LEAKY_KEY, {"error": http_error(403)}, "llm_key_rejected"),
    ("anthropic", LEAKY_KEY, {"error": http_error(429)}, "llm_busy"),
    ("anthropic", LEAKY_KEY, {"error": http_error(529)}, "llm_busy"),
    ("anthropic", LEAKY_KEY, {"error": http_error(500)}, "llm_unavailable"),
    ("anthropic", LEAKY_KEY, {"error": http_error(404)}, "llm_unavailable"),
    ("anthropic", LEAKY_KEY, {"error": urllib.error.URLError("connection refused")}, "llm_unavailable"),
    ("anthropic", LEAKY_KEY, {"error": TimeoutError("timed out")}, "llm_unavailable"),
    ("anthropic", LEAKY_KEY, {"answer": {"content": None}}, "llm_unavailable"),
    ("anthropic", None, None, "llm_unavailable"),
    ("fake-down", LEAKY_KEY, None, "llm_unavailable"),
], ids=["HTTPError-401", "HTTPError-403", "HTTPError-429", "HTTPError-529", "HTTPError-500", "HTTPError-404",
        "URLError", "TimeoutError", "unexpected-shape", "anthropic-without-key", "fake-down"])
def test_a_failed_send_says_why_records_nothing_and_never_shows_the_key(db, monkeypatch, capfd, llm_name, key, stub, error):
    if key is not None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    if stub is not None:
        monkeypatch.setattr(urllib.request, "urlopen", FakeUrlopen(**stub))
    conversation = db.add_exchange(None, FIRST, f"fake reply #1: {FIRST}")
    before = counts(db)

    for conversation_id in (None, conversation):
        status, body = post_in_process(responder(llm_name), conversation_id)
        assert (status, json.loads(body)) == (503, {"error": error, "message": FAILURE_MESSAGES[error]})
        assert "LEAKCHECK" not in body.decode("utf-8")
    assert counts(db) == before
    assert "LEAKCHECK" not in capfd.readouterr().err


class Recording:
    def __init__(self) -> None:
        self.seen: list[list[llm.Message]] = []

    def reply(self, messages):
        self.seen.append(list(messages))
        return "了解"

    def reachable(self) -> bool:
        return True


def test_the_whole_conversation_goes_to_the_ai_oldest_first(db):
    responder = Recording()
    first = chat.send(db, responder, {"conversation_id": None, "text": "りんごの話"})
    chat.send(db, responder, {"conversation_id": first["conversation_id"], "text": "続きです"})
    assert responder.seen[-1] == [
        {"role": "user", "content": "りんごの話"},
        {"role": "assistant", "content": "了解"},
        {"role": "user", "content": "続きです"},
    ]


def test_text_is_recorded_as_sent_without_trimming(db):
    reply = chat.send(db, llm.Fake(), {"conversation_id": None, "text": "  りんご\n"})
    assert reply["messages"][0] == {"role": "user", "text": "  りんご\n"}
    assert db.conversation(reply["conversation_id"]).messages[0].content == "  りんご\n"


def test_a_failed_write_leaves_neither_the_conversation_nor_the_user_message(db):
    class Broken(llm.Fake):
        def reply(self, messages):
            return None  # assistant の本文は NOT NULL なので、記録の 2 行目で失敗する

    with pytest.raises(sqlite3.IntegrityError):
        chat.send(db, Broken(), {"conversation_id": None, "text": "りんごの話"})
    assert counts(db) == (0, 0)


def test_a_conversation_deleted_while_the_ai_replies_is_not_found_and_records_nothing(db):
    conversation = chat.send(db, llm.Fake(), {"conversation_id": None, "text": "りんごの話"})["conversation_id"]

    class DeletingWhileReplying(llm.Fake):
        def reply(self, messages):
            db.delete_conversation(conversation)
            return super().reply(messages)

    with pytest.raises(chat.Rejected) as rejected:
        chat.send(db, DeletingWhileReplying(), {"conversation_id": conversation, "text": "続きです"})
    assert (rejected.value.status, rejected.value.error) == (404, "conversation_not_found")
    assert counts(db) == (0, 0)


def test_a_conversation_deleted_while_the_ai_replies_is_not_found_even_when_another_reuses_its_id(db):
    apple = chat.send(db, llm.Fake(), {"conversation_id": None, "text": "りんごの話"})["conversation_id"]
    orange = []

    class DeletingAndStartingAnotherWhileReplying(llm.Fake):
        def reply(self, messages):
            db.delete_conversation(apple)
            orange.append(chat.send(db, llm.Fake(), {"conversation_id": None, "text": "みかんの話"})["conversation_id"])
            return super().reply(messages)

    with pytest.raises(chat.Rejected) as rejected:
        chat.send(db, DeletingAndStartingAnotherWhileReplying(), {"conversation_id": apple, "text": "りんごの続き"})
    assert (rejected.value.status, rejected.value.error) == (404, "conversation_not_found")
    assert orange == [apple]
    assert [m.content for m in db.conversation(apple).messages] == ["みかんの話", "fake reply #1: みかんの話"]


def test_page_has_a_textarea_and_a_send_button(start_app):
    page = start_app().get("/").body.decode("utf-8")
    assert "<textarea" in page
    assert '<button type="submit">送信</button>' in page


def test_null_after_an_existing_conversation_starts_another_one(start_app, db):
    app = start_app()
    _, apple = send(app, None, "りんごの話")
    r, orange = send(app, None, "みかんの話")
    assert r.status == 200
    assert orange["conversation_id"] != apple["conversation_id"]
    assert orange["messages"][-1] == {"role": "assistant", "text": "fake reply #1: みかんの話"}
    assert sorted(c.id for c in db.list_conversations()) == sorted([apple["conversation_id"], orange["conversation_id"]])
    assert [m.content for m in db.conversation(apple["conversation_id"]).messages] == ["りんごの話", "fake reply #1: りんごの話"]


def test_page_has_a_new_conversation_button_that_only_clears_the_view(start_app):
    page = start_app().get("/").body.decode("utf-8")
    assert '<button type="button" id="new-conversation">新しい会話</button>' in page
    clicked = re.search(r'newConversationButton\.addEventListener\("click", \(\) => \{(.*?)\n  \}\);', page, re.DOTALL)
    assert clicked
    body = clicked.group(1)
    assert "chatMessages.replaceChildren();" in body
    assert 'composerText.value = "";' in body
    assert "currentConversationId = null;" in body
    assert "chatView++;" in body
    # 押しただけでは会話を作らない。
    assert "fetch(" not in body
    # 履歴のサイドメニューは conversation-new を受けて、どの会話にも aria-current を付けない。
    listened = re.search(r'addEventListener\("conversation-new", \(\) => \{(.*?)\n  \}\);', page, re.DOTALL)
    assert listened
    assert "currentHistoryId = null;" in listened.group(1)
    assert "markCurrent();" in listened.group(1)
    # 送信中に新しい会話にしたら、前の会話の応答で今の会話を上書きしない。
    assert re.search(r"const shown = await stillShown\(view\);\n      if \(shown\) \{\n.*?currentConversationId = conversation_id;\n      \}", page, re.DOTALL)
    assert "return view === chatView;" in page
    # 送れなかった表示も消え、送信中に新しい会話にしたら、前の会話の送れなかった表示と本文を持ち込まない。
    assert "unsent = [];" in body
    assert page.count("if (await stillShown(view)) showUnsent(") == 2
    # 会話を開いている途中に新しい会話にしたら、開くのを待っている送信の結果も、新しい会話には出さない。
    assert body.index("chatView++;") < body.index("settleOpening();")


def test_page_says_when_the_ai_is_unreachable_and_puts_the_text_back(start_app):
    page = start_app().get("/").body.decode("utf-8")
    assert 'error === "llm_unavailable" ? "AI に繋がりません"' in page
    assert "。もう一度送信できます。" in page
    assert 'notice.className = "error"' in page
    assert "composerText.value = text" in page


def test_a_resend_from_the_unsent_notice_updates_the_llm_status_by_the_same_rule_as_a_send(start_app):
    # 送れなかった知らせからの送り直しも送信の 1 つ。通常の送信と同じ規則で「AI への接続」に知らせる (#61)。
    page = start_app().get("/").body.decode("utf-8")
    rule = re.search(r"function reportSent\(ok, error\) \{\n(.*?)\n  \}\n", page, re.DOTALL).group(1)
    # 応答が返ったら正常、AI に繋がらずに断られたら異常。それ以外の断りでは知らせない (= 変えない)。
    assert rule.splitlines() == [
        '    if (ok) document.dispatchEvent(new CustomEvent("llm-reached", { detail: { ok: true } }));',
        '    else if (error === "llm_unavailable") document.dispatchEvent(new CustomEvent("llm-reached", { detail: { ok: false } }));',
    ]
    # 規則はここ 1 箇所だけ。送信も送り直しも、ほかで llm-reached を送らない。
    assert page.count('new CustomEvent("llm-reached"') == 2
    send = re.search(r"async function sendMessage\(\) \{\n(.*?)\n  \}\n", page, re.DOTALL).group(1)
    resend = re.search(r'resend\.addEventListener\("click", async \(\) => \{\n(.*?)\n    \}\);\n', page, re.DOTALL).group(1)
    for body in (send, resend):
        # サーバに届かず fetch が投げたときは知らせない。
        unreachable = re.search(r"\} catch \(error\) \{\n(.*?)\n +\}\n", body, re.DOTALL).group(1)
        assert "reportSent(" not in unreachable
        # 断られたら、本文の error を渡して規則に決めさせる。
        refused = re.search(r"if \(!response\.ok\) \{\n(.*?)\n +\}\n", body, re.DOTALL).group(1)
        assert re.findall(r"reportSent\(.*?\);", refused) == ["reportSent(false, error);"]
        assert refused.index("const error = await errorOf(response);") < refused.index("reportSent(false, error);")
        assert refused.index("reportSent(false, error);") < refused.index("throw ")
        # 応答が返ったら、会話の一覧に知らせたあとで正常にする。
        replied = body.split("const { conversation_id, messages } = await response.json();", 1)[1]
        assert re.findall(r"reportSent\(.*?\);", replied) == ["reportSent(true);"]
        assert replied.index('new CustomEvent("conversation-updated"') < replied.index("reportSent(true);")
        assert replied.index("reportSent(true);") < replied.index("} finally {")
