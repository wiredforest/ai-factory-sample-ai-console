#!/usr/bin/env python3
"""決定論的なミューテーションの一覧表示・1 件の適用、または指定した通常チェックのログの採点を行う。

候補の準備、検証、再試行は共有ワークフローが担当する。
このツールはサブプロセスを起動せず、エージェントや他のワークフローを実行しない。
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Literal, NamedTuple
HERE = Path(__file__).resolve().parent
DEFECTS = HERE / "defects.json"

PRODUCT_RUNGS = {"static", "unit", "e2e", "holdout"}

# アサーション失敗ごとに 1 行出力する検査段階と、その段階で証拠として必要な行。
# どちらもプロダクトに欠陥がなくても `GATE_FAILED` に至ることがある
# （ウォッチドッグは強制終了時に 'e2e' と出力する）。段階名だけでは欠陥検出の証拠にならない。
ASSERTION_EVIDENCE = {"e2e": "E2E_FAIL", "holdout": "HOLDOUT_FAIL"}

# `ci.py` が「この段階ではプロダクトを判定できなかった」ことを示す文言。
# GATE_FAILED の直前に失敗の詳細として表示される。テストスイート自身の出力に偶然この文言があっても
# 判定不能とする。実際には検出していない欠陥を検出済みとして数えず、ゲートを明示的に失敗させる。
NOT_A_VERDICT = ("TIMEOUT after ", "could not run ", "UNIT_ERROR:")
IMPORT_FAILURES = ("ModuleNotFoundError:", "ImportError:", "Error: Cannot find module")
INFRASTRUCTURE_RUNGS = {"app-start", "e2e-harness", "holdout-harness", "mutations"}

Outcome = Literal["CAUGHT", "ESCAPED", "INCONCLUSIVE"]


class Verdict(NamedTuple):
    outcome: Outcome
    label: str  # 判定を下した検査段階。どの段階でも判定できなかった場合は失敗の種類。
    detail: str  # ログの読み手向けに、表示不適な文字を除き長さを制限した詳細。


_UNPRINTABLE = re.compile(r"[^\x20-\x7e]+")


def sanitize(text: str, limit: int = 200) -> str:
    """ビルドが出力した内容を、表示可能な文字からなる長さ制限付きの 1 行に整える。

    使い捨てのビルドはこのマシンの環境を引き継ぐ。接続文字列を引用するトレースバックや
    自身の設定を表示するランナーなど、ツールがその環境の情報を出力することがある。
    キャプチャした内容の引用は短い抜粋にとどめる。
    """
    flat = " ".join(_UNPRINTABLE.sub(" ", text).split())
    return flat[: limit - 3] + "..." if len(flat) > limit else flat


def named_rung(out: str) -> str | None:
    """`ci.py` が停止理由として出力した検査段階を返す。出力がなければ None。

    最後のマーカーを採用する。`fail()` は失敗したコマンドの出力を先に表示し、最後にマーカーを出す。
    その後には何も出力しないため、スイート自身がログに `GATE_FAILED: unit` を出していても、
    本来のマーカーには影響しない。
    """
    step = None
    for line in out.splitlines():
        if line.strip().startswith("GATE_FAILED:"):
            step = line.split(":", 1)[1].strip()
    return step


def has_line(out: str, prefix: str) -> bool:
    return any(line.strip().startswith(prefix) for line in out.splitlines())


def classify(rc: int, out: str) -> Verdict:
    """このビルドの終了コードとログから、注入した欠陥について実際に確認できることを返す。"""
    if rc == 0:
        if not has_line(out, "CHECKS_OK"):
            return Verdict(
                "INCONCLUSIVE", "no-verdict",
                "ゲートは CHECKS_OK を出力せず終了コード 0 を返したため、検査が完了していません",
            )
        return Verdict("ESCAPED", "gate-ok", "")

    # このハーネスでは全体を通して 124 がタイムアウトを表す。`ci.py` が 124 を返すのは
    # e2e のウォッチドッグによる終了時だけ。終了時に段階名 'e2e' が出力されても、
    # アサーションが一つでも評価されたことを意味しない。
    if rc == 124:
        return Verdict(
            "INCONCLUSIVE", "timeout",
            "ゲートが判定を下す前に制限時間に達し、強制終了しました",
        )

    rung = named_rung(out)
    if rung is None:
        return Verdict(
            "INCONCLUSIVE", "unknown-exit",
            f"ゲートは検査段階を示さず終了コード {rc} を返しました。プロダクトに対する判定は記録されていません",
        )
    if not rung:
        return Verdict("INCONCLUSIVE", "malformed-marker", "GATE_FAILED に検査段階の指定がありません")
    if rung not in PRODUCT_RUNGS:
        return Verdict(
            "INCONCLUSIVE", rung if rung in INFRASTRUCTURE_RUNGS else "unknown-rung",
            "ハーネスまたは実行環境で失敗しました。プロダクトの失敗を示すものではありません",
        )
    if any(signature in out for signature in IMPORT_FAILURES):
        return Verdict(
            "INCONCLUSIVE", rung,
            "モジュールのインポートに失敗し、プロダクトの検証結果を確定できません",
        )
    for signature in NOT_A_VERDICT:
        if has_line(out, signature):
            return Verdict(
                "INCONCLUSIVE", rung,
                f"'{rung}' は '{signature.strip()}' により失敗しました。この検査段階を実行できておらず、"
                f"欠陥の検出には至っていません",
            )
    evidence = ASSERTION_EVIDENCE.get(rung)
    if evidence and not has_line(out, evidence):
        return Verdict(
            "INCONCLUSIVE", rung,
            f"'{rung}' は失敗しましたが、{evidence} の行が 1 件もなく、"
            f"アサーションが欠陥を判定した記録がありません",
        )
    return Verdict("CAUGHT", rung, "")


def classify_runtime(result: dict, candidate: str) -> Verdict:
    """ホストでの試行を識別する値に対応付けて、共有ランタイムの型付きの戻り値を処理する。

    呼び出し元は信頼できるネイティブの戻り値を渡す。モデルのプロセス終了コードは使わない。
    結果の帰属情報とアサーションの成果物は、引き続き共有スイートが管理する。
    """
    unknown = Verdict("INCONCLUSIVE", "runtime", "ランタイムの型付き結果が欠落しているか、不整合または不一致があります")
    if (not isinstance(result, dict) or not isinstance(candidate, str) or not candidate.strip()
            or result.get("candidate") != candidate.strip()
            or not isinstance(result.get("checkout"), str)
            or type(result.get("verified")) is not bool
            or not isinstance(result.get("summary"), str) or not result["summary"].strip()):
        return unknown
    verdict = result.get("verdict")
    if verdict == "verified" and result["verified"] is True:
        return Verdict("ESCAPED", "runtime", "共有ランタイムがこの候補を検証し、合格と判定しました")
    if verdict == "failed" and result["verified"] is False:
        return Verdict("CAUGHT", "runtime", "共有ランタイムがプロダクトの失敗を記録しました")
    return unknown


def apply(dest: Path, d: dict) -> tuple[bool, str]:
    """テキスト置換によるミューテーションを行い、(注入できたか, できなかった理由) を返す。

    置換対象を識別するアンカーは一意でなければならない。これは過度な用心ではなく、
    このランナーで実際に起き、ファクトリーが発見した不具合に基づく制約である。

    `replace(find, replace, 1)` が置き換えるのは最初に一致した箇所である。ファイル内の
    より前の位置にバイト単位で同一の文字列が追加されると、意図した行の代わりに
    新しい行を書き換え始めるが、それを知らせるものはない。本来の箇所は正常なままなので、
    そこに対する検査は失敗せず、欠陥は ESCAPED と報告される。注入箇所の誤りが、
    実際には存在しないハーネスの検出漏れに見えてしまう。

    実際、新ルートに追加された `return self._error(409, str(e))` が以前からあるハンドラーの
    記述と一致し、古い方を対象とする e2e アサーションでは欠陥を試せなくなった。
    結果としてレポート全体が誤解を招くものになった。

    そのため、アンカーが一意でなければ注入せず、ファイル名と一致件数を報告する。
    正確に配置できない欠陥では検出能力を確かめられない。
    """
    target = (dest / d["file"]).resolve()
    if not target.is_relative_to(dest.resolve()):
        return False, "ミューテーションのパスが候補のディレクトリ外を指しています"
    if not d.get("find") or d.get("find") == d.get("replace"):
        return False, "ミューテーションには空でない一意のアンカーと、その内容を変える置換が必要です"
    if not target.exists():
        return False, f"{d['file']} がビルド用コピーに存在しません"
    body = target.read_text(encoding="utf-8")
    count = body.count(d["find"])
    if count == 0:
        return False, f"アンカーが {d['file']} に見つかりません"
    if count > 1:
        return False, (
            f"アンカーは {count} 件、{d['file']} にあり、置換箇所を特定できません。"
            f"最初の一致箇所を置換すると、この欠陥の対象とは別の行に当たる可能性があります。"
            f"アンカーを一意になるまで長くするか、重複箇所の記述を変更してください。"
        )
    target.write_text(body.replace(d["find"], d["replace"], 1), encoding="utf-8")
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--defects", type=Path, default=DEFECTS)
    subs = parser.add_subparsers(dest="action", required=True)
    subs.add_parser("list")
    mutation = subs.add_parser("apply")
    mutation.add_argument("id")
    mutation.add_argument("--candidate", type=Path, required=True)
    score = subs.add_parser("score")
    score.add_argument("--log", type=Path, required=True)
    score.add_argument("--exit-code", type=int, required=True)
    runtime = subs.add_parser("score-runtime", help="共有ランタイムが実際に返した型付きの結果を採点する")
    runtime.add_argument("--result", type=Path, required=True)
    runtime.add_argument("--candidate", required=True, help="照合に使うホスト上の試行の識別子")
    args = parser.parse_args()
    if args.action == "score-runtime":
        try:
            result = json.loads(args.result.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            result = None
        verdict = classify_runtime(result, args.candidate)
        print(json.dumps(verdict._asdict()))
        return 0 if verdict.outcome == "CAUGHT" else 1
    if args.action == "score":
        verdict = classify(args.exit_code, args.log.read_text(encoding="utf-8"))
        print(json.dumps(verdict._asdict()))
        return 0 if verdict.outcome == "CAUGHT" else 1
    defects = json.loads(args.defects.read_text(encoding="utf-8"))["defects"]
    if args.action == "list":
        print(json.dumps(defects, indent=2))
        return 0
    matches = [d for d in defects if d.get("id") == args.id]
    if len(matches) != 1:
        parser.error("ミューテーション ID はちょうど 1 件の欠陥を特定する必要があります")
    injected, why = apply(args.candidate, matches[0])
    print(json.dumps({"id": args.id, "injected": injected, "reason": why}))
    return 0 if injected else 1

if __name__ == "__main__":
    raise SystemExit(main())
