import json
import os
import re
import sqlite3
from contextlib import closing

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


def row_counts() -> tuple[int, int]:
    with closing(sqlite3.connect(os.environ["CONSOLE_DB_PATH"])) as conn:
        return conn.execute("SELECT (SELECT count(*) FROM conversations), (SELECT count(*) FROM messages)").fetchone()


def test_deleting_a_conversation_removes_it_and_its_messages_and_leaves_the_others(start_app):
    app = start_app()
    a = send(app, None, "りんごの話")["conversation_id"]
    b = send(app, None, "みかんの話")["conversation_id"]
    kept = opened(app, b)
    r = app.delete(f"/api/conversations/{a}")
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(r.body) == {"id": a}
    assert [(c["id"], c["title"]) for c in conversations(app)] == [(b, "みかんの話")]
    assert app.get(f"/api/conversations/{a}").status == 404
    assert opened(app, b) == kept
    with Database(os.environ["CONSOLE_DB_PATH"]) as reopened:
        assert reopened.messages(a) is None
        assert [c.id for c in reopened.list_conversations()] == [b]


@pytest.mark.parametrize("raw_id", ["999", "0", "01", "abc"])
def test_deleting_an_unknown_or_non_integer_id_is_not_found_and_deletes_nothing(start_app, raw_id):
    app = start_app()
    send(app, None, "りんごの話")
    before = row_counts()
    r = app.delete(f"/api/conversations/{raw_id}")
    assert r.status == 404
    assert json.loads(r.body) == {"error": "conversation_not_found"}
    assert row_counts() == before


def test_deleting_from_a_read_only_record_fails_and_keeps_the_conversation(start_app, db):
    conversation = start_conversation(db, "りんごの話")
    db.add_message(conversation, "assistant", "fake reply #1: りんごの話")
    os.chmod(os.environ["CONSOLE_DB_PATH"], 0o444)
    app = start_app()
    r = app.delete(f"/api/conversations/{conversation}")
    assert r.status == 503
    assert json.loads(r.body) == {"error": "db_write_failed", "message": "会話を削除できませんでした"}
    assert [c["id"] for c in conversations(app)] == [conversation]
    assert len(opened(app, conversation)) == 2
    # 無い会話は、書けない記録でも「無い」と答える。
    assert app.delete("/api/conversations/999").status == 404


def test_deleting_without_the_record_is_unavailable(start_app, tmp_path):
    app = start_app(CONSOLE_DB_PATH=str(tmp_path / "missing" / "console.sqlite3"))
    r = app.delete("/api/conversations/1")
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


def history_section(page: str) -> str:
    return re.search(r'<section id="history">.*?</section>', page, re.DOTALL).group(0)


def load_history_source(page: str) -> str:
    return re.search(r"async function loadHistory\(\) \{\n(.*?)\n  \}\n", page, re.DOTALL).group(1)


def test_page_has_an_unreadable_notice_apart_from_the_empty_one_and_both_start_hidden(start_app):
    history = history_section(start_app().get("/").body.decode("utf-8"))
    assert '<p class="empty" hidden>会話がまだありません</p>' in history
    notice = re.search(r'<p class="history-error" hidden>([^<]+)</p>', history)
    assert notice
    assert "読めませんでした" in notice.group(1)
    assert "会話がまだありません" not in notice.group(1)


def test_a_503_and_a_failed_fetch_both_go_to_the_unreadable_notice(start_app):
    source = load_history_source(start_app().get("/").body.decode("utf-8"))
    attempt, failure = re.fullmatch(r"(?s).*?try \{\n(.*?)\n    \} catch \(error\) \{\n(.*?)\n    \}\n.*", source).groups()
    # fetch が投げても、503 などで !response.ok になっても、同じ catch に入る。
    assert 'await fetch("/api/conversations")' in attempt
    assert re.search(r"if \(!response\.ok\) throw ", attempt)
    assert "response.json()" in attempt
    assert "historyError.hidden = false;" in failure
    assert "historyEmpty.hidden = true;" in failure
    assert "historyList.replaceChildren();" in failure
    # 一覧を読めたら、前に出した「読めなかった」表示は消す。
    succeeded = source.split(failure, 1)[1]
    assert "historyError.hidden = true;" in succeeded
    assert "historyEmpty.hidden = conversations.length > 0;" in succeeded
