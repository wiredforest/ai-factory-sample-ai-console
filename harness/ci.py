#!/usr/bin/env python3
"""通常の静的検査とユニットテストを実行する。ランタイム検証は別の共有ワークフローが担当する。"""
from __future__ import annotations
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG = json.loads((HERE / "harness.config.json").read_text(encoding="utf-8"))

def unquote(token: str) -> str:
    """トークンの両端にある、対応する引用符を一組だけ取り除く。

    コマンドは `posix=False` で分割する。Windows のパス中のバックスラッシュを保持できるが、
    分割後の各トークンには引用符まで残る。

    以前はこの処理を argv[0] にしか適用せず、残りの引数で重大な問題が起きた。
    `python -c "import app"` は `python`、`-c`、`"import app"` の 3 トークンで届き、
    Python は `"import app"` を文字列リテラルとして評価して終了コード 0 を返した。
    実測では `python -c "import definitely_not_a_module"` も 0 で終了する。
    つまり `APP_STARTED driver=library` の唯一の根拠であるライブラリドライバーの
    インポート検査は決して失敗しなかった。後から同梱した Node 版にも同じ欠陥があった。

    すべてのトークンから引用符を取り除けば、パスのバックスラッシュを保ちつつ、
    posix モードと同じ引用符処理ができる。
    """
    if len(token) > 1 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def resolve(argv: list[str]) -> list[str]:
    """argv[0] を OS が実際に実行できるパスに解決する。

    Windows では、ここで指定する npm、npx、yarn、pnpm など、JS ツールの多くは `.cmd` の
    シムを経由する。シェルを介さない subprocess は PATHEXT を参照しない。このため
    `"unit": "npm test"` が正しくても "the system cannot find the file specified" とエラーになる。
    PATH が通っており、どの端末でも使えるのに、ツールが未インストールのように見える。

    まず引用符を取り除く。この順序には意味がある。posix=False による分割は Windows のパス中の
    バックスラッシュを保持する一方、トークンに引用符を残す。空白を含むインタープリターのパスが
    引用符付きで届けば `shutil.which` に一致せず、subprocess は、まさにこの関数が防ごうとする
    紛らわしいエラーで失敗する。Windows の一般的なインストール先が `C:\\Program Files` である。
    """
    if not argv:
        return argv
    # argv[0] を含む全トークンを処理する。先頭だけではインポート検査が失敗を検出できない。
    # 経緯は unquote() を参照。
    argv = [unquote(t) for t in argv]
    return [shutil.which(argv[0]) or argv[0], *argv[1:]]


def run(step: str, cmd: str | list[str], timeout: int = 600) -> tuple[int, str]:
    """検査を 1 段実行する。ハングした検査から結果は得られないため、タイムアウトは失敗とし、スキップにはしない。"""
    argv = resolve(shlex.split(cmd, posix=False) if isinstance(cmd, str) else list(cmd))
    try:
        p = subprocess.run(
            argv, cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s"
    except OSError as e:
        return 127, f"could not run {argv[0] if argv else cmd!r}: {e}"


def fail(step: str, detail: str = "") -> int:
    """実行を実際に停止させた検査段階の名前を報告する。

    ゲートは実行順とは別の固定順でマーカーを検査する。この処理がないとスイートが早い段階で
    停止したとき、最初に照合したマーカーが原因として報告される。そのマーカーが欠けているのは
    事実でも、実際の原因は何段階も手前にある。誰も見ていなかった失敗の調査に手間がかかるのは、
    多くの場合、自分が返した原因名が間違っているためだ。
    """
    if detail:
        print(detail.strip()[-4000:], flush=True)
    print(f"GATE_FAILED: {step}", flush=True)
    return 1


def skipped(name: str, marker: str) -> None:
    """コマンドが未設定の検査段階をログに明示する。

    未設定の段階で何も出力しないと、通過した段階と区別できない。それはこのファイルが防ぐべき
    誤判定そのものである。未設定である事実をログに残すことで、必要なら該当マーカーを必須にするよう
    ゲートを設定できる。
    """
    print(f"{marker}_SKIPPED harness.config.json に '{name}' コマンドが設定されていません", flush=True)


def main() -> int:
    if any(arg not in {"--quick"} for arg in sys.argv[1:]):
        print("使い方: python harness/ci.py [--quick]（静的検査とユニットテストのみ）")
        return 2
    print("HARNESS_START mode=ordinary", flush=True)
    if not CONFIG.get("static") and not CONFIG.get("unit"):
        return fail("configuration", "NO_CHECKS: static または unit のコマンドを設定してください")
    # --- 1. 静的検査 -----------------------------------------------------------
    static_cmd = CONFIG.get("static") or ""
    if not static_cmd:
        skipped("static", "STATIC")
    else:
        rc, out = run("static", static_cmd)
        if rc != 0:
            return fail("static", out)
        print("STATIC_OK", flush=True)

    # --- 2. ユニットテスト -------------------------------------------------------------
    unit_cmd = CONFIG.get("unit") or ""
    if not unit_cmd:
        skipped("unit", "UNIT")
    else:
        rc, out = run("unit", unit_cmd)
        if rc != 0:
            return fail("unit", out)
        pattern = (CONFIG.get("unit_count_pattern") or "").strip()
        if pattern:
            m = re.search(pattern, out)
            ran = int(m.group(1)) if m else 0
            # テスト 0 件は合格ではない。何も検出しなかったスイートも 0 で終了し、問題なく見える。
            # このファイルを独立に実装した 2 者とも、指示がなくてもこの防止策を加えた。
            # 実際に必要なチェックだと分かる。
            if ran == 0:
                return fail(
                    "unit",
                    "UNIT_ERROR: ランナーが報告したテスト件数は 0 件です。テストを実行していないスイートは合格にできません。"
                    "実行件数を取得できる場合は unit_count_pattern を修正してください。\n"
                    + out[-2000:],
                )
            print(f"UNIT_PASSED tests={ran}", flush=True)
        else:
            print(
                "UNIT_PASSED tests=unknown (unit_count_pattern が未設定のため、"
                "ここでは合格したスイートとテストがないスイートを区別できません)",
                flush=True,
            )

    print("RUNTIME_NOT_RUN: ランタイム検証は別の共有ワークフローで実行します", flush=True)
    print("CHECKS_OK mode=ordinary", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
