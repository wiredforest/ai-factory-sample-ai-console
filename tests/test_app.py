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


def highlight_rules_of(page: str, constant: str) -> str:
    rules = re.search(rf"const {constant} = \{{\n(.*?)\n  \}};", page, re.DOTALL)
    assert rules, constant
    return rules[1]


def keywords_of(rules: str) -> set[str]:
    listed = re.search(r"keywords: \[(.*?)\]", rules, re.DOTALL)
    assert listed
    return set(re.findall(r'"(\w+)"', listed[1]))


def test_page_colours_only_ts_js_python_and_bash_code_blocks(start_app):
    page = start_app().get("/").body.decode("utf-8")
    # 色分けするのはこの 10 個の言語の指定 (大文字小文字は問わない) だけ。ほかの言語や指定なしの code は文字だけのまま。
    aliases = dict(re.findall(r'\["(\w+)", (\w+_HIGHLIGHT)\]', page))
    assert aliases == {
        "typescript": "SCRIPT_HIGHLIGHT", "ts": "SCRIPT_HIGHLIGHT", "javascript": "SCRIPT_HIGHLIGHT", "js": "SCRIPT_HIGHLIGHT",
        "python": "PYTHON_HIGHLIGHT", "py": "PYTHON_HIGHLIGHT",
        "bash": "BASH_HIGHLIGHT", "sh": "BASH_HIGHLIGHT", "shell": "BASH_HIGHLIGHT",
    }
    # 中身はまず文字のまま入れ、色分けする言語のときだけ、同じ文字を span で包んだものに置き換える。
    plain = 'code.textContent = fence.lines.map((line) => line.replace(indent, "")).join("\\n");'
    coloured = "const rules = HIGHLIGHT_RULES.get(fence.lang.toLowerCase());\n"
    coloured += "    if (rules) code.replaceChildren(highlighted(code.textContent, rules));"
    assert plain + "\n    " + coloured in page
    assert page.count("highlighted(") == 2
    # 色分けは 1 つの関数で、span は createElement で作り、文字は textContent で入れる。
    function = re.search(r"function highlighted\(source, rules\) \{\n(.*?)\n  \}\n", page, re.DOTALL)
    assert function
    assert 'document.createElement("span")' in function[1]
    assert "span.className = `hl-${kind}`;" in function[1]
    assert "span.textContent = text;" in function[1]
    # 包まれない文字も、間と最後の分を落とさずに入れる。
    assert "fragment.append(source.slice(last, match.index));" in function[1]
    assert "fragment.append(source.slice(last));" in function[1]
    # コメントと文字列を語より先に取るので、その中の語はキーワードにならない。
    assert "(${rules.comments.join(\"|\")})|(${rules.strings.join(\"|\")})|(${number})|(${rules.word})" in function[1]


def test_page_lists_the_keywords_strings_and_comments_of_each_coloured_language(start_app):
    page = start_app().get("/").body.decode("utf-8")
    script = highlight_rules_of(page, "SCRIPT_HIGHLIGHT")
    assert keywords_of(script) >= {
        "const", "let", "var", "function", "return", "if", "else", "for", "while", "class", "new", "import",
        "export", "from", "async", "await", "typeof", "interface", "type",
    }
    assert "String.raw`\\/\\/.*`" in script
    assert "String.raw`\\/\\*[\\s\\S]*?\\*\\/`" in script
    python = highlight_rules_of(page, "PYTHON_HIGHLIGHT")
    assert keywords_of(python) >= {
        "def", "return", "if", "elif", "else", "for", "while", "in", "class", "import", "from", "as", "lambda",
        "None", "True", "False", "and", "or", "not", "with", "try", "except",
    }
    assert "String.raw`#.*`" in python
    assert 'String.raw`"""[\\s\\S]*?"""`' in python
    bash = highlight_rules_of(page, "BASH_HIGHLIGHT")
    assert keywords_of(bash) >= {"if", "then", "else", "elif", "fi", "for", "in", "do", "done", "while", "case", "esac", "function"}
    # シェルの # は行頭か空白の後だけがコメント。
    assert "String.raw`(?<!\\S)#.*`" in bash
    # 数値は語の一部でない十進の整数と小数。
    assert "String.raw`(?<![\\w$.])\\d+(?:\\.\\d+)?(?![\\w$])`" in page


def test_page_gives_each_highlight_class_its_own_colour(start_app):
    page = start_app().get("/").body.decode("utf-8")
    colours = {}
    for kind in ("keyword", "string", "comment", "number"):
        rule = re.search(rf"\.bubble \.text \.hl-{kind} \{{([^}}]*)\}}", page)
        assert rule, kind
        colours[kind] = re.search(r"(?<!-)color: (#\w+);", rule[1])[1].lower()
    pre = re.search(r"\.bubble \.text pre \{([^}]*)\}", page)
    base = re.search(r"(?<!-)color: (#\w+);", pre[1])[1].lower()
    # 4 つとも互いに違い、どれもコードの地の文字の色と違う。
    assert len(set(colours.values())) == 4
    assert base not in colours.values()


def test_page_shows_list_items_only_where_a_marker_is_followed_by_a_space(start_app):
    page = start_app().get("/").body.decode("utf-8")
    # 項目の行の判定はページの JS の正規表現そのもの。Python の re でも同じ意味になる書き方なので、取り出して行に当てる。
    found = re.search(r"const item = /(.+)/\.exec\(line\);", page)
    assert found
    item = re.compile(found.group(1))
    assert item.match("- 卵").groups() == ("", "-", "卵")
    assert item.match("* 牛乳").group(3) == "牛乳"
    assert item.match("+ 砂糖").group(3) == "砂糖"
    assert item.match("2. 焼く").groups() == ("", "2.", "焼く")
    assert item.match("   - 弱火").groups() == ("   ", "-", "弱火")
    assert item.match("\t- タブ").group(1) == "\t"
    # 印の後に空白が無い行と、印の後に文字が無い行は箇条書きにしない。
    for line in ("-ハイフンだけ", "1.5倍にする", "**太字**", "---", "- ", "1)まだ"):
        assert not item.match(line), line
    # 番号の印は ol、ほかの印は ul。項目は li。どれも createElement で組み立てる。
    assert 'const tag = item[2].endsWith(".") ? "ol" : "ul";' in page
    assert "document.createElement(tag)" in page
    assert 'document.createElement("li")' in page
    # 入れ子は親の項目の印より 2 文字以上深い字下げ。タブは 4 文字ぶん進める。
    assert "indent >= parent.indent + 2" in page
    assert 'char === "\\t" ? width + 4 - (width % 4)' in page


def test_page_interprets_emphasis_and_inline_code_only_inside_assistant_headings_paragraphs_and_items(start_app):
    page = start_app().get("/").body.decode("utf-8")
    # 見出し・段落 (続きの行も)・項目・項目の中の段落の文字は、行ごとに inlineNodes で解釈してから入れる。
    assert "element.append(...inlineNodes(heading[2].trimEnd()));" in page
    assert "level.item.append(...inlineNodes(item[3]));" in page
    assert 'paragraph.append(document.createElement("br"), ...inlineNodes(lists.length ? line.trimStart() : line));' in page
    assert "paragraph.append(...inlineNodes(line.trimStart()));" in page
    assert "paragraph.append(...inlineNodes(line));" in page
    # 強調は strong、斜体は em、打ち消し線は del。インラインのコードの中身は解釈せず、文字のまま code に入れる。
    assert 'document.createElement(closer.char === "~" ? "del" : used === 2 ? "strong" : "em")' in page
    assert "code.textContent = line.slice(end, close);" in page
    # 対にならなかった記号は、書かれたままの文字に戻す。語の中の _ は開きも閉じもしない。
    assert "return items.map(literal);" in page
    assert "return item.char ? item.char.repeat(item.count) : item;" in page
    assert 'if (char === "_") {' in page
    # 自分の発言は解釈しない。
    assert "body.textContent = text;" in page
    # インラインのコードは等幅で出す (コードブロックの code は pre の書式のまま)。
    rule = re.search(r"\.bubble \.text :not\(pre\) > code \{([^}]*)\}", page)
    assert rule
    assert "monospace" in rule[1]


def test_page_shows_links_as_text_and_leaves_images_bare_urls_tags_and_broken_links_as_written(start_app):
    page = start_app().get("/").body.decode("utf-8")
    # リンク・画像・本文の URL の書式はページの JS の正規表現そのもの。Python の re でも同じ意味になる書き方なので、取り出して試す。
    patterns = {}
    for name in ("LINK", "IMAGE", "BARE_URL"):
        found = re.search(rf"const {name} = /(.+)/y;", page)
        assert found, name
        patterns[name] = re.compile(found.group(1))
    link, image, bare_url = patterns["LINK"], patterns["IMAGE"], patterns["BARE_URL"]

    # リンクは文字と URL に分ける。URL がどんな形でも同じで、対になった ( ) は 1 段まで URL に含める。
    assert link.match("[公式](https://example.com/a) を見て").groups() == ("公式", "https://example.com/a")
    assert link.match("[危ない](javascript:window.mdXss=3)").groups() == ("危ない", "javascript:window.mdXss=3")
    assert link.match("[Foo](https://en.wikipedia.org/wiki/Foo_(bar)) と").groups() == ("Foo", "https://en.wikipedia.org/wiki/Foo_(bar)")
    # 崩れたリンクはリンクとみなさない。対になっていない [ の後ろの崩れていないリンクだけを書き換える。
    for broken in ("[閉じない](https://example.com/d", "[a [b] c](u)", "[空白](https://example.com/e f)", "[](https://example.com/f)"):
        assert not link.match(broken), broken
    assert not link.match("[a [b](u)") and link.match("[a [b](u)", 3).groups() == ("b", "u")
    # 画像は ! から ) までを書かれたまま残す。
    assert image.match("![図](https://example.net/should-not-load.png)")[0] == "![図](https://example.net/should-not-load.png)"
    # 本文の URL は、最後の句読点と記号を含めずに書かれたまま残す。URL の中の _ と * は記号にしない。
    assert bare_url.match("https://example.org/b も")[0] == "https://example.org/b"
    assert bare_url.match("https://ex.com/path/_private_/ と")[0] == "https://ex.com/path/_private_/"
    assert bare_url.match("https://example.com/*a*/_b_ と")[0] == "https://example.com/*a*/_b"
    assert bare_url.match("https://a.com/x**")[0] == "https://a.com/x"
    assert bare_url.match("https://example.com/q?a=1&b=2#top.")[0] == "https://example.com/q?a=1&b=2#top"
    assert bare_url.match('https://example.com/c">タグのリンク</a>')[0] == "https://example.com/c"
    # 本文の URL は URL に使える ASCII の文字のところまで。日本語の文で空白なしに続く全角の文字の手前で止まり、
    # その後ろの強調・斜体・インラインのコードは解釈される (行の残りを書かれたままの文字にしない)。
    for line, url in (
        ("公式サイト（https://example.com/a）で**重要**と書かれています", "https://example.com/a"),
        ("詳しくは https://example.com/a、`npm install` を実行します", "https://example.com/a"),
        ("**https://example.com/a**。", "https://example.com/a"),
        ("参照: https://example.com/a。*注意*してください", "https://example.com/a"),
        ("詳しくは https://example.com/a を見て、**重要**です", "https://example.com/a"),
    ):
        assert bare_url.search(line)[0] == url, line

    # inlineNodes の中で、画像と本文の URL は記号を解釈せずに文字のまま、リンクは文字の側だけを解釈して「文字 (URL)」にする。
    assert 'const verbatim = (char === "!" && matchAt(IMAGE, line, i)) || (char === "h" && matchAt(BARE_URL, line, i));' in page
    assert "text += verbatim[0];" in page
    assert 'const link = char === "[" && matchAt(LINK, line, i);' in page
    assert "items.push(...inlineNodes(link[1]), ` (${link[2]})`);" in page
    # 自分の吹き出しは打ったとおり。
    assert "body.textContent = text;" in page
