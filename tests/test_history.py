import json
import os
import re

import pytest

from console.db import Database


@pytest.fixture
def db():
    with Database(os.environ["CONSOLE_DB_PATH"]) as db:
        yield db


def start_conversation(db: Database, first_message: str) -> int:
    conversation = db.create_conversation()
    db.add_message(conversation, "user", first_message)
    return conversation


def conversations(app) -> list[dict]:
    r = app.get("/api/conversations")
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/json; charset=utf-8"
    return json.loads(r.body)["conversations"]


def test_empty_record_lists_no_conversations(start_app):
    r = start_app().get("/api/conversations")
    assert r.status == 200
    assert json.loads(r.body) == {"conversations": []}


def test_newest_update_comes_first_and_a_new_message_moves_it_up(start_app, db):
    a = start_conversation(db, "りんごの話")
    b = start_conversation(db, "みかんの話")
    app = start_app()
    assert [(c["id"], c["title"]) for c in conversations(app)] == [(b, "みかんの話"), (a, "りんごの話")]
    db.add_message(a, "assistant", "fake reply #1: りんごの話")
    assert [(c["id"], c["title"]) for c in conversations(app)] == [(a, "りんごの話"), (b, "みかんの話")]


def test_each_conversation_carries_id_title_and_iso_update_time(start_app, db):
    a = start_conversation(db, "りんごの話")
    db.add_message(a, "assistant", "fake reply #1: りんごの話")
    db.add_message(a, "user", "ほかの話")
    [conversation] = conversations(start_app())
    assert conversation == {"id": a, "title": "りんごの話", "updated_at": db.messages(a)[-1].created_at}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00", conversation["updated_at"])


def test_title_is_cut_to_thirty_characters_only_past_thirty(start_app, db):
    thirty = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほ"
    assert len(thirty) == 30
    start_conversation(db, thirty + "ま")
    start_conversation(db, thirty)
    assert [c["title"] for c in conversations(start_app())] == [thirty, thirty + "…"]


def test_restarting_on_the_same_record_lists_the_same_conversations(start_app, db):
    start_conversation(db, "りんごの話")
    start_conversation(db, "みかんの話")
    first = start_app()
    before = conversations(first)
    first.stop()
    after = conversations(start_app())
    assert [c["title"] for c in after] == ["みかんの話", "りんごの話"]
    assert after == before


def test_page_reloads_the_list_on_conversation_updated(start_app):
    page = start_app().get("/").body.decode("utf-8")
    listener = re.search(r'document\.addEventListener\("conversation-updated", \(event\) => \{(.*?)\n  \}\);', page, re.DOTALL)
    assert listener and "loadHistory();" in listener.group(1)
    assert re.search(r'fetch\("/api/conversations"\)', page)
    assert "dataset.conversationId = conversation.id" in page


def send(app, conversation_id, text) -> dict:
    r = app.post_json("/api/messages", {"conversation_id": conversation_id, "text": text})
    assert r.status == 200
    return json.loads(r.body)


def opened(app, conversation_id) -> list[dict]:
    r = app.get(f"/api/conversations/{conversation_id}")
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/json; charset=utf-8"
    body = json.loads(r.body)
    assert body["id"] == conversation_id
    return body["messages"]


def roles_and_texts(messages: list[dict]) -> list[tuple[str, str]]:
    return [(m["role"], m["text"]) for m in messages]


def test_opening_a_conversation_returns_only_its_messages_in_order(start_app, db):
    app = start_app()
    a = send(app, None, "りんごの話")["conversation_id"]
    b = send(app, None, "みかんの話")["conversation_id"]
    messages = opened(app, a)
    assert roles_and_texts(messages) == [("user", "りんごの話"), ("assistant", "fake reply #1: りんごの話")]
    assert "みかん" not in json.dumps(messages, ensure_ascii=False)
    assert [m["created_at"] for m in messages] == [m.created_at for m in db.messages(a)]
    assert roles_and_texts(opened(app, b)) == [("user", "みかんの話"), ("assistant", "fake reply #1: みかんの話")]


def test_continuing_an_opened_conversation_keeps_one_conversation_and_survives_restarts(start_app):
    app = start_app()
    a = send(app, None, "りんごの話")["conversation_id"]
    b = send(app, None, "みかんの話")["conversation_id"]
    app.stop()
    app = start_app()
    assert len(opened(app, a)) == 2
    reply = send(app, a, "続きです")
    assert reply["conversation_id"] == a
    assert reply["messages"][-1] == {"role": "assistant", "text": "fake reply #2: 続きです"}
    assert [(c["id"], c["title"]) for c in conversations(app)] == [(a, "りんごの話"), (b, "みかんの話")]
    app.stop()
    assert roles_and_texts(opened(start_app(), a)) == [
        ("user", "りんごの話"),
        ("assistant", "fake reply #1: りんごの話"),
        ("user", "続きです"),
        ("assistant", "fake reply #2: 続きです"),
    ]


def test_a_conversation_with_no_messages_opens_empty(start_app, db):
    conversation = db.create_conversation()
    assert opened(start_app(), conversation) == []


@pytest.mark.parametrize(
    "raw_id", ["99999", "abc", "0", "-1", "01", "1.0", "%EF%BC%91", "%31", "", "1/", str(2**63), "9" * 40]
)
def test_unknown_or_non_integer_ids_are_not_found(start_app, raw_id):
    app = start_app()
    send(app, None, "りんごの話")
    r = app.get(f"/api/conversations/{raw_id}")
    assert r.status == 404
    assert r.headers["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(r.body) == {"error": "conversation_not_found"}


def test_opening_without_the_record_is_unavailable(start_app, tmp_path):
    app = start_app(CONSOLE_DB_PATH=str(tmp_path / "missing" / "console.sqlite3"))
    r = app.get("/api/conversations/1")
    assert r.status == 503
    assert json.loads(r.body)["error"] == "db_unavailable"


def test_page_opens_the_selected_conversation_in_the_chat_area(start_app):
    page = start_app().get("/").body.decode("utf-8")
    assert re.search(r'addEventListener\("click", \(\) => openConversation\(conversation\.id\)\)', page)
    assert "fetch(`/api/conversations/${conversationId}`)" in page
    assert re.search(r'item\.setAttribute\("aria-current", "true"\)', page)
    assert re.search(r'item\.removeAttribute\("aria-current"\)', page)
    opened_listener = re.search(r'document\.addEventListener\("conversation-opened", \(event\) => \{(.*?)\n  \}\);', page, re.DOTALL)
    assert opened_listener
    body = opened_listener.group(1)
    assert "chatMessages.replaceChildren();" in body
    assert "addBubble(message.role, message.text)" in body
    assert "currentConversationId = conversationId;" in body
