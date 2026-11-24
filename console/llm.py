"""会話のメッセージ全体を AI に渡し、応答の全文を受け取る。CONSOLE_LLM で偽の応答役に切り替える。"""

import http.client
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from datetime import date
from typing import Literal, Protocol, TypedDict

API_URL = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"
# Haiku 5.5 は考えてから答え、その分も max_tokens を使う。検索に時間がかかり、ストリーミングはしないので長く待つ。
MAX_TOKENS = 4096
REPLY_TIMEOUT_S = 90
CHECK_TIMEOUT_S = 5

# stop_reason ごとに応答の本文へ添える一文。end_turn・stop_sequence の応答には何も添えない。
TRUNCATED_NOTE = "[この応答は長さの上限に達したため、ここで途中で切れています]"
REFUSED_NOTE = "[AI はこの依頼に答えるのを断りました]"

# web search ツール。検索するかどうかは AI に任せ、1 回の応答で検索するのは最大 SEARCH_MAX_USES 回まで。
# 版はモデルから決める。検索結果を絞り込む版に対応するモデルはここに並べ、ほかのモデル (Haiku 5.5 や一覧に無い名前) は基本の版を使う。
SEARCH_MAX_USES = 3
SEARCH_TOOL_BASIC = "web_search_20250305"
SEARCH_TOOL_FILTERING = "web_search_20260209"
FILTERING_SEARCH_MODELS = frozenset({"claude-sonnet-5-5", "claude-opus-5-5"})

# 検索を使った応答の本文の後に置く出典の欄の見出し。欄の形は cited が決める。
SOURCES_HEADING = "出典:"


class Message(TypedDict):
    role: Literal["user", "assistant"]
    content: str


class Unreachable(Exception):
    """AI に繋がらない。"""


class KeyRejected(Unreachable):
    """AI が鍵を受け付けない (401・403)。待っても直らない。"""


class Busy(Unreachable):
    """AI が混んでいる (429・529)。少し待って送り直せば送れる。"""


class Responder(Protocol):
    def reply(self, messages: Sequence[Message]) -> str:
        """古い順のメッセージ全体を受け取り、応答の全文を返す。繋がらなければ Unreachable (またはその下位の型) を送出する。"""

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


def today() -> date:
    """アプリを動かしている手元の今日の日付。"""
    return date.today()


def system_prompt() -> str:
    """送るたびに今日の日付を求めて、要求の最上位の system に入れる短い指示を作る。記録には残さない。"""
    return f"今日の日付は {today().isoformat()} です。日本語で簡潔に答えてください。"


class Anthropic:
    def __init__(self, model: str) -> None:
        self.model = model

    def reply(self, messages: Sequence[Message]) -> str:
        body = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system_prompt(),
            "messages": list(messages),
            "tools": search_tools(self.model),
        }
        answer = call("POST", "/messages", REPLY_TIMEOUT_S, body)
        try:
            data = json.loads(answer)
            text = "".join(block["text"] for block in data["content"] if block["type"] == "text")
            found = sources(data["content"])
            stop_reason = data.get("stop_reason")
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise Unreachable(f"AI に繋がりません (応答の形が想定外です: {type(e).__name__})") from e
        return noted(cited(text, found), stop_reason)

    def reachable(self) -> bool:
        try:
            call("GET", "/models", CHECK_TIMEOUT_S)
        except Unreachable:
            return False
        return True


def search_tools(model: str) -> list[dict[str, object]]:
    """要求の tools に入れる、モデルに合った版の web search ツール 1 つだけの一覧。"""
    version = SEARCH_TOOL_FILTERING if model in FILTERING_SEARCH_MODELS else SEARCH_TOOL_BASIC
    return [{"type": version, "name": "web_search", "max_uses": SEARCH_MAX_USES}]


def sources(content: list[dict[str, object]]) -> list[tuple[str, str]]:
    """検索を使った応答の出典 (題名, URL) を、最初に現れた順に URL の重複なく返す。検索を使わなかった応答では空。

    text のブロックの citations (web_search_result_location) を使い、1 つも無ければ web_search_tool_result の結果を使う。
    """
    results = [block for block in content if block["type"] == "web_search_tool_result"]
    if not results:
        return []
    citations = [
        citation
        for block in content if block["type"] == "text"
        for citation in block.get("citations") or []
        if citation.get("type") == "web_search_result_location"
    ]
    # 検索が失敗した web_search_tool_result の content は一覧ではなく、エラーの 1 つのオブジェクトになる。
    found = citations or [
        result
        for block in results if isinstance(block.get("content"), list)
        for result in block["content"]
        if result.get("type") == "web_search_result"
    ]
    seen: dict[str, str] = {}
    for source in found:
        url, title = source.get("url"), source.get("title")
        if isinstance(url, str) and url.strip() and url.strip() not in seen:
            # 題名の改行で 1 行 1 つの形が崩れないよう、空白をつめる。
            seen[url.strip()] = " ".join(title.split()) if isinstance(title, str) else ""
    return [(title, url) for url, title in seen.items()]


def cited(text: str, found: Sequence[tuple[str, str]]) -> str:
    """本文の後に空行を置き、出典の欄 (見出しと、1 行に 1 つの「- 題名 (URL)」か題名の無い「- URL」) を添える。出典が無ければ本文のまま。"""
    if not found:
        return text
    lines = [f"- {title} ({url})" if title else f"- {url}" for title, url in found]
    return "\n\n".join(part for part in (text, "\n".join([SOURCES_HEADING, *lines])) if part)


def noted(text: str, stop_reason: object) -> str:
    """途中で切れた応答には後に、断られた応答には先に、そのことを告げる一文を空行で分けて添える。"""
    if stop_reason == "max_tokens":
        return "\n\n".join(part for part in (text, TRUNCATED_NOTE) if part)
    if stop_reason == "refusal":
        return "\n\n".join(part for part in (REFUSED_NOTE, text) if part)
    return text


def call(method: str, path: str, timeout: float, body: object = None) -> bytes:
    """2xx の応答の本文を返す。繋がらなければ Unreachable (またはその下位の型) を送出する。"""
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
    except urllib.error.HTTPError as e:
        # urlopen は 2xx 以外を HTTPError (OSError の仲間) として送出する。
        raise refusal(e.code) from e
    except (OSError, http.client.HTTPException) as e:
        raise Unreachable(f"AI に繋がりません ({type(e).__name__}: {e})") from e


def refusal(code: int) -> Unreachable:
    if code in (401, 403):
        return KeyRejected(f"AI が鍵を受け付けません (HTTP {code})")
    if code in (429, 529):
        return Busy(f"AI が混んでいます (HTTP {code})")
    return Unreachable(f"AI に繋がりません (HTTP {code})")


# 応答役をモデルの名前から作る。偽の応答役はモデルを使わない。
RESPONDERS: dict[str, Callable[[str], Responder]] = {
    "fake": lambda model: Fake(),
    "fake-down": lambda model: FakeDown(),
    "anthropic": Anthropic,
}


def responder(llm: str, model: str) -> Responder:
    """CONSOLE_LLM と CONSOLE_MODEL の値 (config.Config.llm・model) に対応する応答役を返す。"""
    return RESPONDERS[llm](model)
