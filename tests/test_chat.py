import json
import os
import re
import socket
import sqlite3
import threading

import pytest

from console import chat, llm
from console.db import Database

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
    return len(conversations), sum(len(db.messages(c.id)) for c in conversations)


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
    assert [m.role for m in db.messages(conversation.id)] == ["user", "assistant", "user", "assistant"]


def test_the_record_survives_a_restart(start_app, db):
    app = start_app()
    _, first = send(app, None, FIRST)
    send(app, first["conversation_id"], SECOND)
    app.stop()
    start_app()
    assert [(m.role, m.content) for m in db.messages(first["conversation_id"])] == [
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
    assert len(db.messages(conversation)) == 2


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
    assert db.messages(reply["conversation_id"])[0].content == "  りんご\n"


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
    assert [m.content for m in db.messages(apple["conversation_id"])] == ["りんごの話", "fake reply #1: りんごの話"]


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
    assert re.search(r"const shown = view === chatView;\n      if \(shown\) \{\n.*?currentConversationId = conversation_id;\n      \}", page, re.DOTALL)
    # 送れなかった表示も消え、送信中に新しい会話にしたら、前の会話の送れなかった表示と本文を持ち込まない。
    assert "unsent = [];" in body
    assert page.count("if (view === chatView) showUnsent(") == 2


def test_page_says_when_the_ai_is_unreachable_and_puts_the_text_back(start_app):
    page = start_app().get("/").body.decode("utf-8")
    assert 'error === "llm_unavailable" ? "AI に繋がりません"' in page
    assert "。もう一度送信できます。" in page
    assert 'notice.className = "error"' in page
    assert "composerText.value = text" in page
