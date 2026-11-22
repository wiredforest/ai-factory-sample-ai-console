"""会話とメッセージを SQLite の 1 ファイルに記録し、読み出す共有部品。"""

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Self

Role = Literal["user", "assistant"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations (id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages (conversation_id, id);
"""


@dataclass(frozen=True)
class ConversationSummary:
    id: int
    updated_at: str
    first_user_message: str | None


@dataclass(frozen=True)
class Message:
    role: Role
    content: str
    created_at: str


def now() -> str:
    # 桁を固定する。isoformat() はマイクロ秒が 0 だと小数部を省き、文字列の順が時刻の順でなくなる。
    return datetime.now(UTC).isoformat(timespec="microseconds")


class Database:
    """1 つの接続をロックで守り、複数のスレッドから使えるようにする。書き込みは戻る前にコミットする。"""

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def create_conversation(self) -> int:
        at = now()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO conversations (created_at, updated_at) VALUES (?, ?)", (at, at)
            )
        return cur.lastrowid

    def add_message(self, conversation_id: int, role: Role, content: str) -> None:
        at = now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (conversation_id, role, content, at),
            )
            self._conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (at, conversation_id))

    def list_conversations(self) -> list[ConversationSummary]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT c.id, c.updated_at, (
                    SELECT m.content FROM messages m
                    WHERE m.conversation_id = c.id AND m.role = 'user'
                    ORDER BY m.id LIMIT 1
                )
                FROM conversations c
                ORDER BY c.updated_at DESC, c.id DESC
                """
            ).fetchall()
        return [ConversationSummary(*row) for row in rows]

    def messages(self, conversation_id: int) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        return [Message(*row) for row in rows]
