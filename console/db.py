"""会話とメッセージを SQLite の 1 ファイルに記録し、読み出す共有部品。"""

import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self, get_args

Role = Literal["user", "assistant"]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations (id),
    role TEXT NOT NULL CHECK (role IN ({", ".join(f"'{role}'" for role in get_args(Role))})),
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


def check(path: str) -> bool:
    """path の記録を開き、会話の表を読み、書き込みを 1 回してロールバックする。すべてできたら True。

    記録の中身は変えない。ファイルが無ければ作らない (mode=rw)。
    """
    try:
        uri = Path(path).absolute().as_uri() + "?mode=rw"
        with closing(sqlite3.connect(uri, uri=True, autocommit=True)) as conn:
            # 先に書き込みのロックを取る。読んでから書き込みに上がると、他の書き込みと重なったときに待たずに失敗する。
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("SELECT 1 FROM conversations LIMIT 1").fetchall()
                at = now()
                conn.execute("INSERT INTO conversations (created_at, updated_at) VALUES (?, ?)", (at, at))
            finally:
                conn.execute("ROLLBACK")
    except sqlite3.Error:
        return False
    return True


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
        with self._lock, self._conn:
            self._insert_message(conversation_id, role, content)

    def add_exchange(self, conversation_id: int | None, user: str, assistant: str) -> int:
        """user の発言と assistant の応答をこの順に 1 つのトランザクションで記録し、会話の id を返す。

        conversation_id が None なら、同じトランザクションで新しい会話を作る。途中で失敗したら何も残らない。
        """
        with self._lock, self._conn:
            if conversation_id is None:
                at = now()
                conversation_id = self._conn.execute(
                    "INSERT INTO conversations (created_at, updated_at) VALUES (?, ?)", (at, at)
                ).lastrowid
            self._insert_message(conversation_id, "user", user)
            self._insert_message(conversation_id, "assistant", assistant)
        return conversation_id

    def _insert_message(self, conversation_id: int, role: Role, content: str) -> None:
        # ロックを持ち、トランザクションの中で呼ぶ。
        at = now()
        self._conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, at),
        )
        self._conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (at, conversation_id))

    def has_conversation(self, conversation_id: int) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return row is not None

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
