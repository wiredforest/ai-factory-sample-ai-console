"""テスト対象のソフトウェアにアクセスする。ほとんどの対象は次の 3 形式で扱える。

    http     サーバー。動的ポートで起動し、応答するまでポーリングする。
    cli      コマンド。引数を渡して起動し、標準出力・標準エラー・終了コードを検証する。
    library  プロセスを起動しない。E2E テストで直接インポートして呼び出す。

形式は `harness.config.json` の `driver` で選ぶ。どの形式も APP_STARTED を出力する。
このマーカーは「テスト対象にアクセスできる」ことを表す。サーバーとライブラリでは
その意味合いが異なるが、いずれにとっても以降の検証に欠かせない前提である。

分離した理由。最初に思いつく HTTP 専用の実装は urllib、ヘルスチェック用のパス、
GET、POST だけで済む。しかし、それでは説明上は汎用の基盤でも、CLI、ライブラリ、
バッチジョブ、デスクトップアプリなど、ソフトウェアの大半にひな形を使えず、
そのことにすら気付かない。プロセス管理の仕組みは共通化できるが、アクセス方法は対象によって異なる。

テストからアクセスできることは、アーキテクチャ上の制約でもある。描画済みのウィンドウ、
ゲームループ、キャンバス、ネイティブ UI はこの 3 形式のどれにも該当しない。
そこで、シミュレーションと描画、ドメインとビューを切り離し、E2E からスクリプトで
操作できるヘッドレスなインターフェースの背後にルールを置く。新規開発ではコードを
書く前にこの方針を明示する。その段階ならほとんど費用はかからず、後からでは書き直しになる。

間違えても気付きにくい共通処理は 1 か所にまとめる。

  * 動的ポートを使い、二つのサイクルが衝突しないようにする。
  * 一定時間スリープせず、応答が来るまで待つ。固定時間のスリープはタイミング競合を招く。
  * 起動しなければ明確に失敗させる。「テスト不能」扱いにすると、クラッシュしたアプリが合格してしまう。
  * 失敗時を含むすべての経路で後始末する。残ったプロセスはポートを占有し、次のサイクルに影響する。
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class AppDidNotStart(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def unquote(token: str) -> str:
    """トークンの両端が同じ引用符なら、外側の 1 組を取り除く。

    コマンドの分割には `posix=False` を使用する。Windows のパス内のバックスラッシュを
    温存できる一方、分割したすべてのトークンに引用符が付いたままになる。

    以前は argv[0] からしか引用符を外さなかったため、残りの引数で重大な問題が起きた。
    `python -c "import app"` は `python`、`-c`、`"import app"` の 3 トークンに分かれ、
    Python はインポート文を実行せず、文字列リテラル `"import app"` を評価して終了コード 0 を返した。
    実測では `python -c "import definitely_not_a_module"` でさえ 0 で終了する。
    ライブラリドライバーのインポート検査は `APP_STARTED driver=library` の根拠となる唯一の検査なのに、
    失敗することがなかった。後に提供された Node 版にもまったく同じ不備があった。

    すべてのトークンから引用符を外すことで、バックスラッシュを保ちつつ POSIX モードと同じ引用符処理ができる。
    """
    if len(token) > 1 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _argv(cmd: str) -> list[str]:
    """コマンドを分割し、PATHEXT を使って argv[0] の実体を特定する。

    この解決処理がないと、Windows の `.cmd` シム（npm、npx、yarn、pnpm）が
    「the system cannot find the file specified」というエラーになる。
    PATH にあり、どの端末でも動くツールが、未インストールであるかのように見えてしまう。
    """
    parts = [unquote(t) for t in shlex.split(cmd, posix=False)]
    if parts:
        parts[0] = shutil.which(parts[0]) or parts[0]
    return parts


# --------------------------------------------------------------------------- http
class HttpApp:
    """動的ポートでサーバーを起動し、ヘルスチェックに通るまでポーリングする。"""

    def __init__(self, cfg: dict, env: dict | None = None) -> None:
        self.cfg = cfg.get("http", {})
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None
        self.env_overrides = env or {}

    def __enter__(self) -> "HttpApp":
        cmd = (self.cfg.get("start") or "").replace("{port}", str(self.port))
        if not cmd:
            raise AppDidNotStart("driver=http ですが、harness.config.json の http.start が空です")

        merged = {**(self.cfg.get("env") or {}), **self.env_overrides}
        env = None
        if merged:
            env = {**os.environ, **{k: str(v).replace("{port}", str(self.port))
                                    for k, v in merged.items()}}
        self.proc = subprocess.Popen(
            _argv(cmd), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env,
        )
        self._await_health()
        print(f"APP_STARTED port={self.port}", flush=True)
        return self

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._free_the_port()

    def _free_the_port(self) -> None:
        """このオブジェクトが起動したものかどうかにかかわらず、まだ待ち受け中のプロセスを終了する。

        `self.proc` を終了してポートが空くのは、ポートを使うプロセスがこのオブジェクトの
        起動したものと同一の場合だけである。ジャーニーの途中でアプリを再起動すると、
        代わりに起動したプロセスは追跡されない。元のプロセスは終了してもエージェントが
        起動したコピーはポートを占有し続ける。`__exit__` はすでに終了したプロセスを
        終了しようとするだけで、ファクトリーは後始末を終えたつもりでも、
        次のサイクルで `[Errno 10048] address already in use` が発生する。

        午前中にゲートを繰り返し実行した後で実測したところ、孤立したインタープリターが
        4 つ残り、4 ポートを占有していた。後始末のコメントで警告していたとおりのリークだった。

        後始末は意図的にベストエフォートとする。失敗時を含むすべての終了経路で実行されるため、
        ポートを解放できなくても、すでに報告された失敗を後始末のトレースバックで覆い隠してはいけない。
        """
        if not self.port:
            return
        try:
            if os.name == "nt":
                out = subprocess.run(
                    ["netstat", "-ano"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=30,
                ).stdout
                pids = {
                    line.split()[-1]
                    for line in out.splitlines()
                    if f":{self.port} " in line and "LISTENING" in line
                }
                for pid in pids:
                    if pid.isdigit() and pid != "0" and (
                        not self.proc or str(self.proc.pid) != pid
                    ):
                        subprocess.run(["taskkill", "/PID", pid, "/F"],
                                       capture_output=True, timeout=30)
            else:
                out = subprocess.run(
                    ["lsof", "-ti", f"tcp:{self.port}"], capture_output=True,
                    text=True, timeout=30,
                ).stdout
                for pid in out.split():
                    if pid.isdigit() and (not self.proc or str(self.proc.pid) != pid):
                        subprocess.run(["kill", "-9", pid], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            # 例外を握りつぶさずログに残す。ポートの占有が続くと次のサイクルに実害が出る。
            # ここで黙っていると、ログを一行読めば済む問題が原因不明になってしまう。
            print(f"PORT_NOT_FREED {self.port} - まだ待ち受け中のプロセスがある可能性があります",
                  flush=True)

    def _await_health(self) -> None:
        path = self.cfg.get("health_path", "/health")
        want = self.cfg.get("health_contains", "")
        deadline = time.time() + int(self.cfg.get("boot_timeout_s", 45))
        last = "応答は一度もありませんでした"
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                out = (self.proc.stdout.read() if self.proc.stdout else "") or ""
                raise AppDidNotStart(
                    f"アプリは応答前に終了コード {self.proc.returncode} で終了しました:\n{out[-2500:]}"
                )
            try:
                status, body, _ = self.get(path)
                if status == 200 and (not want or want in body):
                    return
                # どの条件で失敗したかを明示する。最初の版は「never became healthy」と報告した後で、
                # ステータス 200 と正常に見える本文を表示していた。失敗していたのは文字列の包含チェックで、
                # 設定した文字列ではコロンの後に空白がなく、アプリの JSON には空白があった。
                # これではアプリの停止に見え、設定ファイルを 1 文字直せば済む問題に対して
                # 1 時間もサーバーを調べることになる。
                if status != 200:
                    last = f"応答は {status}、期待値は 200 です。body={body[:160]!r}"
                else:
                    last = (
                        f"応答は 200 ですが、本文に health_contains={want!r} が含まれていません。"
                        f"受信した本文: {body[:160]!r}。アプリは稼働しています。"
                        f"harness.config.json に設定した文字列が一致していません"
                    )
            except (urllib.error.URLError, OSError) as e:
                last = f"まだ接続を受け付けていません ({e})"
            time.sleep(0.2)
        raise AppDidNotStart(
            f"{path} が制限時間内にヘルスチェックを通過しませんでした。{last}。"
            f"「テスト不能」ではなく、検証の失敗です。"
        )

    def get(self, path: str, follow: bool = False, headers: dict | None = None):
        """(status, body, headers) を返す。

        リダイレクトを観測できるように `follow=False` とする。
        自動で追従すると、リダイレクトを確かめるはずのテストでその応答を検証できなくなる。
        """
        req = urllib.request.Request(self.base + path, headers=headers or {})

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None

        opener = (
            urllib.request.build_opener()
            if follow
            else urllib.request.build_opener(_NoRedirect)
        )
        try:
            with opener.open(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)

    def request(self, method: str, path: str, body: str = "", headers: dict | None = None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(
            self.base + path,
            data=body.encode("utf-8") if body else None,
            headers=h,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)

    def post(self, path: str, body: str, headers: dict | None = None):
        return self.request("POST", path, body, headers)

    def post_json(self, path: str, payload: dict):
        status, body, _ = self.post(path, json.dumps(payload))
        try:
            return status, json.loads(body)
        except ValueError:
            return status, {"_unparseable": body[:400]}

    def get_json(self, path: str):
        status, body, _ = self.get(path)
        try:
            return status, json.loads(body)
        except ValueError:
            return status, {"_unparseable": body[:400]}

    def delete(self, path: str):
        return self.request("DELETE", path)


# --------------------------------------------------------------------------- cli
class CliApp:
    """コマンドラインプログラム。`app.run("--flag value")` は (rc, stdout, stderr) を返す。

    ヘルスチェックに相当するのは軽い動作確認のための起動である。
    バイナリが自分のバージョンすら表示できなければ、それ以降のテストを実行しても意味がない。
    """

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg.get("cli", {})

    def __enter__(self) -> "CliApp":
        if not self.cfg.get("invoke"):
            raise AppDidNotStart("driver=cli ですが、harness.config.json の cli.invoke が空です")
        rc, out, err = self.run(self.cfg.get("smoke_args", "--help"))
        want = self.cfg.get("smoke_contains", "")
        if rc not in (0, 1) or (want and want not in (out + err)):
            raise AppDidNotStart(f"動作確認用の起動に失敗しました: rc={rc}\n{(out + err)[-1500:]}")
        print("APP_STARTED driver=cli", flush=True)
        return self

    def __exit__(self, *exc) -> None:
        return None

    def run(self, args: str = "", stdin: str = "", timeout: int = 120):
        cmd = self.cfg.get("invoke", "").replace("{args}", args)
        p = subprocess.run(
            _argv(cmd), cwd=ROOT, input=stdin or None, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return p.returncode, p.stdout or "", p.stderr or ""


# --------------------------------------------------------------------------- library
class LibraryApp:
    """プロセスは起動せず、E2E テストが対象をインポートして呼び出す。

    ここでの APP_STARTED は、少なくともインポートできたことを表す。サーバーから応答が
    あった場合と同じく、以降のアサーションが、読み込み済みの対象に対して行われることを裏付ける。
    """

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg.get("library", {})

    def __enter__(self) -> "LibraryApp":
        check = self.cfg.get("import_check", "")
        if check:
            p = subprocess.run(
                _argv(check), cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
            if p.returncode != 0:
                raise AppDidNotStart(
                    f"ライブラリのインポートに失敗しました:\n{(p.stdout + p.stderr)[-1500:]}"
                )
        sys.path.insert(0, str(ROOT))
        print("APP_STARTED driver=library", flush=True)
        return self

    def __exit__(self, *exc) -> None:
        return None


DRIVERS = {"http": HttpApp, "cli": CliApp, "library": LibraryApp}


def make_driver(cfg: dict, env: dict | None = None):
    """テスト対象を起動する。

    `env` は、このドライバーに限って設定の env ブロックを上書きする。
    用途は一つで、どのホールドアウトでもいずれ必要になる再起動後の検証である。

    「プロセスをまたいでもデータが残るか」は単一のプロセス内では確かめられない。
    永続ストレージへの書き込みが取りこぼされても、再起動までは何もかも正しく動く。
    メモリ内には同じ正しい結果があるので、単一プロセスのテストでは違いが分からない。
    検証するには、同じ状態に対してもう一つのプロセスを起動する必要がある。
    その状態を指定する値を、次のように固定する。

        DB = ".factory/runs/holdout.db"
        with make_driver(CONFIG, env={"APP_DB": DB}) as app:
            ...                                    # 何かを記録する
            with make_driver(CONFIG, env={"APP_DB": DB}) as restarted:
                ...                                # 記録はまだ残っているか？

    固定しないと、2 個目のドライバーは新しいポートを確保し、設定の `{port}` 置換によって
    別の状態ファイルを受け取る。空のアプリを起動して空であることを確認するだけで、
    シナリオは何も検証できなかったことにさえ気付けない。
    """
    name = (cfg.get("driver") or "http").strip().lower()
    if name not in DRIVERS:
        raise AppDidNotStart(
            f"harness.config.json のドライバー {name!r} は不明です。指定可能な値: "
            f"{', '.join(sorted(DRIVERS))}"
        )
    if name == "http":
        return HttpApp(cfg, env)
    return DRIVERS[name](cfg)
