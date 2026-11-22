"""履歴のスライス: 記録された会話を、見分ける手掛かりと更新時刻つきで新しい順に並べる。"""

from console.db import ConversationSummary, Database

TITLE_LENGTH = 30


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
