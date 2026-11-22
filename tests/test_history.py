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
    assert re.search(r'document\.addEventListener\("conversation-updated", loadHistory\)', page)
    assert re.search(r'fetch\("/api/conversations"\)', page)
    assert "dataset.conversationId = conversation.id" in page
