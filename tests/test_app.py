import os
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
