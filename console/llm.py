"""会話のメッセージ全体を AI に渡し、応答の全文を受け取る。CONSOLE_LLM で偽の応答役に切り替える。"""

import http.client
import json
import os
import urllib.request
from collections.abc import Callable, Sequence
from typing import Literal, Protocol, TypedDict

API_URL = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"
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
    def __init__(self, model: str) -> None:
        self.model = model

    def reply(self, messages: Sequence[Message]) -> str:
        body = {"model": self.model, "max_tokens": MAX_TOKENS, "messages": list(messages)}
        answer = call("POST", "/messages", REPLY_TIMEOUT_S, body)
        try:
            content = json.loads(answer)["content"]
            return "".join(block["text"] for block in content if block["type"] == "text")
        except (ValueError, KeyError, TypeError) as e:
            raise Unreachable(f"AI に繋がりません (応答の形が想定外です: {type(e).__name__})") from e

    def reachable(self) -> bool:
        try:
            call("GET", "/models", CHECK_TIMEOUT_S)
        except Unreachable:
            return False
        return True


def call(method: str, path: str, timeout: float, body: object = None) -> bytes:
    """2xx の応答の本文を返す。繋がらなければ Unreachable を送出する。"""
    # 鍵は呼ぶたびに環境変数から読む。例外の文言には鍵を入れない。
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
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
            return response.read()
    except ValueError:
        # http.client はヘッダに使えない文字 (改行、latin-1 の外の文字) を、鍵の全文入りの ValueError で拒む。
        # 鍵が traceback に出ないよう、元の例外はつながない。
        raise Unreachable("AI に繋がりません (ANTHROPIC_API_KEY にヘッダに使えない文字があります)") from None
    except (OSError, http.client.HTTPException) as e:
        # urlopen は 2xx 以外を HTTPError (OSError の仲間) として送出する。
        raise Unreachable(f"AI に繋がりません ({type(e).__name__}: {e})") from e


# 応答役をモデルの名前から作る。偽の応答役はモデルを使わない。
RESPONDERS: dict[str, Callable[[str], Responder]] = {
    "fake": lambda model: Fake(),
    "fake-down": lambda model: FakeDown(),
    "anthropic": Anthropic,
}


def responder(llm: str, model: str) -> Responder:
    """CONSOLE_LLM と CONSOLE_MODEL の値 (config.Config.llm・model) に対応する応答役を返す。"""
    return RESPONDERS[llm](model)
