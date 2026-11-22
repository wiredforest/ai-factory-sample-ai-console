import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from console.db import Database


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "console.sqlite3"


@pytest.fixture
def db(db_path):
    with Database(str(db_path)) as db:
        yield db


def test_first_use_creates_the_file_and_reopening_keeps_it(db_path):
    assert not db_path.exists()
    with Database(str(db_path)) as db:
        conversation = db.create_conversation()
        db.add_message(conversation, "user", "りんごの話")
    assert db_path.exists()
    with Database(str(db_path)) as db:
        assert [c.id for c in db.list_conversations()] == [conversation]


def test_messages_survive_closing_and_reopening(db_path):
    with Database(str(db_path)) as db:
        conversation = db.create_conversation()
        db.add_message(conversation, "user", "りんごの話")
        db.add_message(conversation, "assistant", "fake reply #1: りんごの話")
    with Database(str(db_path)) as db:
        assert len(db.list_conversations()) == 1
        assert [(m.role, m.content) for m in db.messages(conversation)] == [
            ("user", "りんごの話"),
            ("assistant", "fake reply #1: りんごの話"),
        ]


def test_list_is_newest_update_first(db):
    a = db.create_conversation()
    db.add_message(a, "user", "りんごの話")
    b = db.create_conversation()
    db.add_message(b, "user", "みかんの話")
    assert [c.id for c in db.list_conversations()] == [b, a]
    db.add_message(a, "assistant", "fake reply #1: りんごの話")
    assert [c.id for c in db.list_conversations()] == [a, b]


def test_list_carries_id_update_time_and_first_user_message(db):
    a = db.create_conversation()
    db.add_message(a, "user", "りんごの話")
    db.add_message(a, "assistant", "fake reply #1: りんごの話")
    db.add_message(a, "user", "ほかの話")
    [summary] = db.list_conversations()
    assert summary.id == a
    assert summary.updated_at == db.messages(a)[-1].created_at
    assert summary.updated_at.endswith("+00:00")
    assert summary.first_user_message == "りんごの話"


def test_messages_of_one_conversation_do_not_include_another(db):
    a = db.create_conversation()
    b = db.create_conversation()
    db.add_message(a, "user", "りんごの話")
    db.add_message(b, "user", "みかんの話")
    db.add_message(a, "assistant", "fake reply #1: りんごの話")
    assert [m.content for m in db.messages(a)] == ["りんごの話", "fake reply #1: りんごの話"]


def test_unknown_role_is_refused_and_adds_nothing(db):
    conversation = db.create_conversation()
    db.add_message(conversation, "user", "りんごの話")
    before = db.list_conversations()
    with pytest.raises(sqlite3.IntegrityError):
        db.add_message(conversation, "system", "ふるまいを変えて")
    assert [m.content for m in db.messages(conversation)] == ["りんごの話"]
    assert db.list_conversations() == before


def test_message_to_a_missing_conversation_is_refused(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.add_message(999, "user", "りんごの話")
    assert db.list_conversations() == []


def test_two_threads_adding_to_one_conversation_lose_nothing(db):
    conversation = db.create_conversation()

    def add_twenty(name: str) -> None:
        for i in range(20):
            db.add_message(conversation, "user", f"{name} {i}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(add_twenty, name) for name in ("a", "b")]:
            future.result(timeout=30)
    assert len(db.messages(conversation)) == 40


def test_only_the_database_file_and_its_sqlite_companions_are_written(tmp_path, db_path):
    with Database(str(db_path)) as db:
        conversation = db.create_conversation()
        db.add_message(conversation, "user", "りんごの話")
        db.add_message(conversation, "assistant", "fake reply #1: りんごの話")
        db.list_conversations()
        db.messages(conversation)
    allowed = {db_path.name} | {db_path.name + suffix for suffix in ("-wal", "-shm", "-journal")}
    assert {p.name for p in tmp_path.iterdir()} <= allowed

