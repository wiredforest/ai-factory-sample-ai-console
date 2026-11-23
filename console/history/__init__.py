"""履歴のスライス: 記録された会話を、見分ける手掛かりと更新時刻つきで新しい順に並べ、選んだ会話のやり取りを返す。"""

import re

from console.db import ConversationSummary, Database

TITLE_LENGTH = 30
# SQLite の INTEGER に収まらない id は、問い合わせると OverflowError になる。そのような会話は無い。
MAX_ID = 2**63 - 1


def title(summary: ConversationSummary) -> str:
    # 会話を見分ける手掛かりは最初の user の発言 (MISSION Q1 への提案)。記録の形は変えず、表示のときに作る。
    text = summary.first_user_message or ""
    return text if len(text) <= TITLE_LENGTH else text[:TITLE_LENGTH] + "…"


def list_conversations(db: Database) -> dict:
    return {
        "conversations": [
            {"id": summary.id, "title": title(summary), "updated_at": summary.updated_at}
            for summary in db.list_conversations()
        ]
    }


def conversation(db: Database, raw_id: str) -> dict | None:
    """URL の id の部分を受け取り、その会話のメッセージを記録した順に返す。そのような会話が無ければ None。"""
    conversation_id = parse_id(raw_id)
    if conversation_id is None or not db.has_conversation(conversation_id):
        return None
    return {
        "id": conversation_id,
        "messages": [
            {"role": m.role, "text": m.content, "created_at": m.created_at}
            for m in db.messages(conversation_id)
        ],
    }


def delete(db: Database, raw_id: str) -> dict | None:
    """URL の id の部分を受け取り、その会話とメッセージを消してコミットしてから id を返す。そのような会話が無ければ None。

    記録に書けなければ sqlite3.Error をそのまま送出する。そのときは何も消えていない。
    """
    conversation_id = parse_id(raw_id)
    if conversation_id is None or not db.delete_conversation(conversation_id):
        return None
    return {"id": conversation_id}


def parse_id(raw_id: str) -> int | None:
    # 0 始まりや符号、全角の数字は会話の id として書かれない。int() に任せると受け付けてしまう。
    if not re.fullmatch(r"[1-9][0-9]*", raw_id):
        return None
    conversation_id = int(raw_id)
    return conversation_id if conversation_id <= MAX_ID else None
