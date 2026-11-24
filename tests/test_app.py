import json
import os
import re
import subprocess
from html.parser import HTMLParser

import pytest
from conftest import ROOT, app_argv, free_port


def test_health_is_ok_without_redirect(start_app):
    r = start_app().get("/health")
    assert r.status == 200
    assert r.body == b"ok"
    assert r.headers["Location"] is None


def test_build_id_returns_the_candidate_verbatim(start_app):
    r = start_app(FACTORY_RUNTIME_CANDIDATE="cand-123").get("/build-id")
    assert r.status == 200
    assert r.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert r.body == b"cand-123"


def test_build_id_without_candidate_is_not_empty(start_app):
    r = start_app().get("/build-id")
    assert r.status == 200
    assert r.body.strip()


VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class TextById(HTMLParser):
    """id を持つ要素ごとに、その中の文字列を集める。"""

    def __init__(self) -> None:
        super().__init__()
        self.open: list[str | None] = []
        self.text: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag in VOID_ELEMENTS:
            return
        element_id = dict(attrs).get("id")
        if element_id is not None:
            self.text[element_id] = ""
        self.open.append(element_id)

    def handle_endtag(self, tag):
        self.open.pop()

    def handle_data(self, data):
        for element_id in self.open:
            if element_id is not None:
                self.text[element_id] += data


def test_page_shows_the_three_areas(start_app):
    r = start_app().get("/")
    assert r.status == 200
    assert r.headers["Content-Type"].startswith("text/html")
    page = r.body.decode("utf-8")
    for heading in ("会話", "履歴", "サーバの状態"):
        assert heading in page
    parser = TextById()
    parser.feed(page)
    assert {"chat", "history", "status"} <= parser.text.keys()
    assert "会話がまだありません" in parser.text["history"]


def test_page_offers_deleting_a_conversation_after_confirming(start_app):
    page = start_app().get("/").body.decode("utf-8")
    assert "削除" in page
    assert "取り消せません" in page
    parser = TextById()
    parser.feed(page)
    assert "会話を削除できませんでした" in parser.text["history"]
    # 確認で取りやめたら送らず、200 を受け取ってから一覧から外す。
    assert re.search(r"if \(!window\.confirm\(.*?\)\) return;\n.*?method: \"DELETE\".*?if \(!response\.ok\) \{.*?\}\n.*?item\.remove\(\);", page, re.DOTALL)
    # 開いている会話が消えたら、会話の送受信が新しい会話を押したときと同じにする。
    assert 'new CustomEvent("conversation-deleted"' in page
    assert re.search(r"if \(event\.detail\.conversationId === currentConversationId\) \{\n.*?newConversationButton\.click\(\);", page, re.DOTALL)
    # 開いている途中の会話を消したら開かず、会話の送受信の待ちを開けなかったときと同じに解く (送信の結果が出ないままにしない)。
    assert re.search(r'if \(openingId === conversation\.id\) \{\n\s*openRequest\+\+;\n\s*document\.dispatchEvent\(new CustomEvent\("conversation-open-failed"\)\);', page)


def test_listens_on_loopback_only(start_app):
    app = start_app()
    assert app.startup_line == f"listening on http://127.0.0.1:{app.port}"


def run_until_exit(port: int | None = None, **env: str | None) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, **env}
    environ = {k: v for k, v in merged.items() if v is not None}
    return subprocess.run(
        app_argv(free_port() if port is None else port), check=False, cwd=ROOT, env=environ,
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )


def test_missing_db_path_exits_2():
    result = run_until_exit(CONSOLE_DB_PATH=None)
    assert result.returncode == 2
    assert "CONSOLE_DB_PATH" in result.stderr


def test_unknown_llm_exits_2():
    result = run_until_exit(CONSOLE_LLM="bogus")
    assert result.returncode == 2
    assert "CONSOLE_LLM" in result.stderr


@pytest.mark.parametrize("model", ["no-such-model", ""], ids=["unknown-name", "empty"])
@pytest.mark.parametrize(("llm", "status_oks"), [
    ("fake", [True, True, True]),
    ("fake-down", [True, False, True]),
])
def test_fake_responders_ignore_console_model(start_app, model, llm, status_oks):
    app = start_app(CONSOLE_LLM=llm, CONSOLE_MODEL=model)
    status = json.loads(app.get("/api/status").body)["items"]
    assert [(item["label"], item["ok"]) for item in status] == list(zip(["アプリ自身", "AI への接続", "会話の記録"], status_oks))
    if llm == "fake":
        r = app.post_json("/api/messages", {"conversation_id": None, "text": "りんごの話"})
        assert r.status == 200
        assert json.loads(r.body)["messages"][-1] == {"role": "assistant", "text": "fake reply #1: りんごの話"}
    assert app.proc.poll() is None


def test_port_in_use_exits_2_with_one_line_and_leaves_the_first_running(start_app):
    first = start_app(FACTORY_RUNTIME_CANDIDATE="first")
    result = run_until_exit(port=first.port, FACTORY_RUNTIME_CANDIDATE="second")
    assert result.returncode == 2
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert lines == [f"error: ポート {first.port} はすでに使われています"]
    assert "Traceback" not in result.stderr
    assert "OSError" not in result.stderr
    assert "listening on" not in result.stdout
    health = first.get("/health")
    assert health.status == 200
    assert health.body == b"ok"
    assert first.get("/build-id").body == b"first"


def test_port_in_use_with_an_unopenable_record_still_exits_2_with_one_line(start_app, tmp_path):
    first = start_app()
    result = run_until_exit(port=first.port, CONSOLE_DB_PATH=str(tmp_path / "missing" / "console.sqlite3"))
    assert result.returncode == 2
    assert result.stderr.splitlines() == [f"error: ポート {first.port} はすでに使われています"]


def test_page_lets_the_send_result_update_the_llm_status(start_app):
    page = start_app().get("/").body.decode("utf-8")
    # 会話の送受信は status を直接呼ばず、document の CustomEvent で送信の結果を知らせる。
    assert 'new CustomEvent("llm-reached", { detail: { ok: false } })' in page
    assert 'new CustomEvent("llm-reached", { detail: { ok: true } })' in page
    assert 'document.addEventListener("llm-reached"' in page
    # 変えるのは「AI への接続」だけ。送信のたびに確かめ直さず、時間でも繰り返さない。
    assert """statusList.querySelector('[data-key="llm"] .state')""" in page
    assert page.count('fetch("/api/status")') == 1
    assert "setInterval" not in page


def failure_reasons(page):
    """ページの failureReasons の対応表を、理由ごとの文言として取り出す。"""
    table = re.search(r"const failureReasons = \{(.*?)\n  \};", page, re.DOTALL)
    assert table is not None
    return dict(re.findall(r'^\s*(\w+): "([^"]*)",$', table.group(1), re.MULTILINE))


def test_page_tells_the_send_failure_kinds_apart(start_app):
    page = start_app().get("/").body.decode("utf-8")
    reasons = failure_reasons(page)
    assert set(reasons) == {"llm_key_rejected", "llm_busy", "llm_unavailable"}
    assert reasons["llm_unavailable"] == "AI に繋がりません"
    # 鍵を直すべきか、少し待って送り直すべきかが分かる。
    assert "鍵" in reasons["llm_key_rejected"] and "正しくありません" in reasons["llm_key_rejected"]
    assert "混んで" in reasons["llm_busy"] and "少し待って" in reasons["llm_busy"] and "送り直" in reasons["llm_busy"]
    assert len(set(reasons.values())) == 3
    # 鍵の文字列そのものは出さない。環境変数の名前だけを示す。
    assert "sk-ant" not in page
    assert "return failureReasons[error] ?? " in page


def test_page_words_every_send_failure_through_the_same_table(start_app):
    page = start_app().get("/").body.decode("utf-8")
    # 会話の領域の表示・別の会話への知らせ・知らせからの送り直しの失敗は、どれも failureReason で文言を決める。
    assert "const message = failureReason(error);" in page
    assert "if (await stillShown(view)) showUnsent(bubble, text, message, origin);" in page
    assert "else showUnsentElsewhere(origin, text, message);" in page
    assert "describe(failureReason(error));" in page
    # 理由ごとの文言を、表の外で API の理由から直に決めない。
    assert page.count("failureReasons[") == 1
    assert 'error === "llm_key_rejected"' not in page


def test_page_marks_the_llm_down_only_when_the_ai_was_not_reached(start_app):
    page = start_app().get("/").body.decode("utf-8")
    # 混んでいるときは AI に届いているので、「AI への接続」を異常にしない。
    assert 'const llmUnreachedErrors = ["llm_unavailable", "llm_key_rejected"];' in page
    assert (
        'if (llmUnreachedErrors.includes(error)) document.dispatchEvent(new CustomEvent("llm-reached", { detail: { ok: false } }));'
        in page
    )
    assert page.count('new CustomEvent("llm-reached", { detail: { ok: false } })') == 1
    assert 'error === "llm_busy"' not in page


def test_page_rechecks_the_status_on_return_and_tells_an_unreachable_server_apart(start_app):
    page = start_app().get("/").body.decode("utf-8")
    parser = TextById()
    parser.feed(page)
    assert "サーバに届きません" in parser.text["status"]
    assert "状態を読めませんでした" in parser.text["status"]
    assert 'document.addEventListener("visibilitychange"' in page
    # 確かめはページを開いたとき・タブに戻ったときだけ。時間で繰り返さない。
    assert "setInterval" not in page


class AncestorsById(HTMLParser):
    """id を持つ要素ごとに、それを囲む要素の id を集める。"""

    def __init__(self) -> None:
        super().__init__()
        self.open: list[str | None] = []
        self.ancestors: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        if tag in VOID_ELEMENTS:
            return
        element_id = dict(attrs).get("id")
        if element_id is not None:
            self.ancestors[element_id] = [i for i in self.open if i is not None]
        self.open.append(element_id)

    def handle_endtag(self, tag):
        self.open.pop()


def test_failures_sent_from_another_conversation_are_shown_outside_the_chat_area(start_app):
    # 会話を切り替えると会話の領域は置き換わる。別の会話で送れなかった知らせは、その外に置かないと一緒に消える。
    parser = AncestorsById()
    parser.feed(start_app().get("/").body.decode("utf-8"))
    assert "send-failures" in parser.ancestors
    assert "chat" not in parser.ancestors["send-failures"]
    assert "send-failures" not in parser.ancestors["messages"]


def test_page_never_injects_text_as_html_nor_loads_anything_from_outside(start_app):
    page = start_app().get("/").body.decode("utf-8")
    # AI の応答は記録の原文のまま届く。HTML として差し込むと、応答に書かれたタグやスクリプトが動く。
    for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser", "createContextualFragment"):
        assert api not in page
    assert not {"a", "img", "script", "iframe"} & set(re.findall(r"createElement\([\"'`](\w+)", page))
    # 外部のライブラリ・スクリプト・スタイル・フォントを読み込まない。
    for loader in ("<script src", "<link", "@import"):
        assert loader not in page
    assert not re.search(r"\b(?:src|href)\s*=\s*[\"'`]?https?://", page, re.IGNORECASE)


def test_page_shows_code_blocks_as_uninterpreted_monospace_text_inside_the_bubble(start_app):
    page = start_app().get("/").body.decode("utf-8")
    # 閉じたフェンスは pre の中の code になり、中身は解釈せずに文字のまま入る。言語は data-lang に持たせる。
    assert 'document.createElement("pre")' in page
    assert 'document.createElement("code")' in page
    assert "code.dataset.lang = fence.lang" in page
    assert 'code.textContent = fence.lines.map((line) => line.replace(indent, "")).join("\\n");' in page
    # 閉じないまま終わったフェンスは、開きの行から終わりまでを書かれたままの段落にして、文字を失わない。
    assert "paragraph.append(fence.line);" in page
    # 等幅で吹き出しと違う背景に出し、長い行は吹き出しからはみ出させずに横に送る。Markdown の原文は色を変える。
    rule = re.search(r"\.bubble \.text pre \{([^}]*)\}", page)
    assert rule
    assert "monospace" in rule[1]
    assert "white-space: pre;" in rule[1]
    assert "overflow-x: auto;" in rule[1]
    assert "background:" in rule[1]
    assert re.search(r"\.bubble \.text pre\.markdown \{[^}]*background:", page)


def test_page_keeps_the_text_after_the_fence_language_visible_above_the_code_block(start_app):
    page = start_app().get("/").body.decode("utf-8")
    # 開きのフェンスの行 (```js title="a.js" 注記) の、言語の語の後ろの文字も吹き出しから消さない。
    # 言語は情報の最初の語で、それより後ろがあれば、情報を書かれたまま文字としてブロックの上に出す。
    assert "const info = opening[3].trim();" in page
    assert "info, lang: info.split(/\\s+/)[0]," in page
    assert "if (fence.info !== fence.lang) {" in page
    assert 'info.className = "code-info";' in page
    assert "info.textContent = fence.info;" in page
    assert page.index("blocks.push(info);") < page.index("blocks.push(codeBlock(fence));")
    # 画面に見えるように出す (隠さない)。
    rule = re.search(r"\.bubble \.text \.code-info \{([^}]*)\}", page)
    assert rule
    assert "display: none" not in rule[1]
    assert "visibility" not in rule[1]
