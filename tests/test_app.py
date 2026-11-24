import os
import re
import subprocess
from html.parser import HTMLParser

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
    assert "if (event.detail.conversationId === currentConversationId) newConversationButton.click();" in page


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
