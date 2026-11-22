# ファクトリーの運用

このアプリケーションは、リビジョンを固定した Archon SDLC のソースを使って動作します。
インストールとアップグレードの手順は [factory/MIGRATION.md](factory/MIGRATION.md) を参照してください。

すべての判定は共有ワークフローのゲートが担います。
ワークフローで定義されたゲートに応答する前に、対象の実行を識別する情報と、その実行の状態を確認してください。

プロジェクトの実行時シナリオは `harness/END-TO-END.md` にあります。
ホールドアウトのシナリオは `.factory/holdout/HOLDOUT.md` にあり、`.factory/hooks/holdout_fence.py` がアクセスを制限します。
このファイルを読めるのは holdout-writer と holdout-verifier のサブエージェントだけです。
通常のチェックは `python harness/ci.py` で実行します。

テスト後は、このアプリケーションで検証したソースの SHA、候補、実行 ID、カバレッジ、
新規環境で検証した証拠、連携に関して残っている制約をここに記録してください。

## 検証系の校正の記録

### 2026-11-22 — 初回 (issue #4 の骨組みの後)

- **検証したソース**: `9c587cfed2f6385994260effe4f3441c7b47a666` (アプリのコードは `main` の `633245f` と同じ。差分は `harness/mutations/defects.json` だけ)。ランタイムのリソースは `runtime_resource.py prepare` でこのリビジョンに結び付けた (`source_tree` `c72e4a1`、`resource_digest` `7805f76a…`)
- **ランタイムホストの設定**: リポジトリの外の非公開の場所 (`~/.factory-runtime/ai-factory-sample-ai-console/`) に置く。`include` は `app.py` と `console`、起動は issue #4 の契約表どおり (`["{python}", "app.py", "--port", "{port}"]`、`CONSOLE_DB_PATH={state}/console.sqlite3`、`CONSOLE_LLM=fake`)
- **アサーション**: 2 つ。`/health` と `/build-id` の契約、ジャーニー 1 の最初の画面 (3 つの領域があり、履歴に「会話がまだありません」が実際に表示される。`hidden` 属性や CSS で隠れたものは数えない)
- **結果** (いずれも Archon の共有ワークフローのネイティブの戻り値):

| 対照 | 候補 | 期待 | 結果 | run |
|---|---|---|---|---|
| 基準状態 | `factory-v1:cce78464…` | verified | verified (matched) | `archon-verify-runtime-suite` `cf6f7f1c…` |
| 関連する欠陥 `empty-history-message-hidden` | `factory-v1:2552bfc7…` | failed | failed (matched)。`score-runtime` で CAUGHT (runtime) | 同上 |
| 別のビルドを指す識別値 (すべて 0) | 基準状態のスロット | inconclusive | inconclusive (`target identity does not match the requested candidate`) | `archon-verify-runtime` `e4669779…` |

  スイートは `expectations_passed: true`、`baseline_verified: true`。

- **通常のチェックでの欠陥の検出** (`harness/mutations/defects.json` の 8 件を、使い捨てのコピーに 1 件ずつ入れて `python harness/ci.py`):
  - CAUGHT (unit): `health-says-not-ok`、`build-id-ignores-candidate`、`listens-on-all-interfaces`、`config-error-exits-zero`、`fake-down-rejected`、`default-llm-flipped`
  - ESCAPED: `empty-history-message-hidden`、`empty-history-message-invisible`。前者は上のランタイムの校正で捕まった。後者 (白い背景に白い文字) は、まだどの段でも試していない
- **残っている制約**:
  - アプリにはまだ状態の変化 (メッセージの送信) が無いため、アサーションは最初の画面に留まる。issue #7 (送信) が入ったら、実際の状態遷移を経るアサーションを足して、校正をやり直す
  - 検証担当はブラウザを持たず、配信された HTML を読んで判定している。`empty-history-message-invisible` のような見た目だけの欠陥は、MISSION の「ファクトリーの責任範囲外」として人のレビューが担う
  - `archon-loop` のアプリ検査は、ランタイムホストではなく `harness/RUNTIME.md` の手順でアプリを起動している。この校正が示すのは、ランタイム検証の判定が健全なことまでで、ラップのアプリ検査の判定がこれと同じ厳しさであることは示していない
