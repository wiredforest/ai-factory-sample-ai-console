"""会話のスライス: 送られた発言を、それまでのやり取りと合わせて AI に渡し、発言と応答の全文を記録して返す。"""

from console import llm
from console.db import Database

# SQLite の INTEGER に収まらない id は、問い合わせると OverflowError になる。そのような会話は無い。
MAX_ID = 2**63 - 1


class Rejected(Exception):
    """送信を受け付けない。何も記録していない。"""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


def send(db: Database, responder: llm.Responder, request: object) -> dict:
    """`{"conversation_id": <整数 または null>, "text": <文字列>}` を受け取り、記録してコミットしてから応答の本文を返す。

    応答役が llm.Unreachable を送出したときは、何も記録せずにそのまま送出する。
    """
    if not isinstance(request, dict):
        raise Rejected(400, "invalid_request")
    conversation_id = request.get("conversation_id")
    text = request.get("text")
    # bool は int の仲間なので、別に除く。
    if not (conversation_id is None or (type(conversation_id) is int)) or not isinstance(text, str):
        raise Rejected(400, "invalid_request")
    if not text.strip():
        raise Rejected(400, "empty_text")
    if conversation_id is None:
        history: list[llm.Message] = []
    else:
        if not 0 < conversation_id <= MAX_ID or not db.has_conversation(conversation_id):
            raise Rejected(404, "conversation_not_found")
        history = [{"role": m.role, "content": m.content} for m in db.messages(conversation_id)]
    answer = responder.reply([*history, {"role": "user", "content": text}])
    conversation_id = db.add_exchange(conversation_id, text, answer)
    return {
        "conversation_id": conversation_id,
        "messages": [{"role": "user", "text": text}, {"role": "assistant", "text": answer}],
    }
