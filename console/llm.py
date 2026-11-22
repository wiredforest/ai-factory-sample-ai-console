"""会話のメッセージ全体を AI に渡し、応答の全文を受け取る。CONSOLE_LLM で偽の応答役に切り替える。"""

import http.client
import json
import os
import urllib.request
from collections.abc import Sequence
from typing import Literal, Protocol, TypedDict

API_URL = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"
MODEL = "claude-haiku-5-5"
MAX_TOKENS = 1024
REPLY_TIMEOUT_S = 30
CHECK_TIMEOUT_S = 5


class Message(TypedDict):
    role: Literal["user", "assistant"]
    content: str


class Unreachable(Exception):
    """AI に繋がらない。"""


class Responder(Protocol):
    def reply(self, messages: Sequence[Message]) -> str:
        """古い順のメッセージ全体を受け取り、応答の全文を返す。繋がらなければ Unreachable を送出する。"""

    def reachable(self) -> bool: ...


class Fake:
    def reply(self, messages: Sequence[Message]) -> str:
        said = [m["content"] for m in messages if m["role"] == "user"]
        return f"fake reply #{len(said)}: {said[-1]}"

    def reachable(self) -> bool:
        return True


class FakeDown:
    def reply(self, messages: Sequence[Message]) -> str:
        raise Unreachable("AI に繋がりません (CONSOLE_LLM=fake-down)")

    def reachable(self) -> bool:
        return False


class Anthropic:
    def reply(self, messages: Sequence[Message]) -> str:
        body = {"model": MODEL, "max_tokens": MAX_TOKENS, "messages": list(messages)}
        result = call("POST", "/messages", REPLY_TIMEOUT_S, body)
        return "".join(block["text"] for block in result["content"] if block["type"] == "text")

    def reachable(self) -> bool:
        try:
            call("GET", "/models", CHECK_TIMEOUT_S)
        except Unreachable:
            return False
        return True


def call(method: str, path: str, timeout: float, body: object = None) -> dict:
    # 鍵は呼ぶたびに環境変数から読む。例外の文言には鍵を入れない。
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise Unreachable("AI に繋がりません (ANTHROPIC_API_KEY が設定されていません)")
    request = urllib.request.Request(
        API_URL + path,
        method=method,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (OSError, http.client.HTTPException) as e:
        # urlopen は 2xx 以外を HTTPError (OSError の仲間) として送出する。
        raise Unreachable(f"AI に繋がりません ({type(e).__name__}: {e})") from e


RESPONDERS: dict[str, Responder] = {"fake": Fake(), "fake-down": FakeDown(), "anthropic": Anthropic()}


def responder(llm: str) -> Responder:
    """CONSOLE_LLM の値 (config.Config.llm) に対応する応答役を返す。"""
    return RESPONDERS[llm]
