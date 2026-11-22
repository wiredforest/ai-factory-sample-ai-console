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

### 2026-11-23 — 2 回目 (issue #7 の送信の後)

- **検証したソース**: `8d8e9f304724590f2656f04aa02ce0a54f1312f4` (アプリのコードは `main` の `a90f9df` と同じ。差分は `harness/mutations/defects.json` だけ)。リソースは `runtime_resource.py prepare` でこのリビジョンに結び付けた (`resource_digest` `38a0700b…`)
- **アサーション**: 3 つ。前回の 2 つ (契約、最初の画面) に、**状態の変化を経るもの**を足した。ブラウザで実際に入力して 2 回送り、吹き出しが 4 つ順に並ぶこと、2 回目の応答が `fake reply #2: …` であること、履歴に会話がちょうど 1 件あることを確かめる。API やデータベースで状態を作ることは禁じた
- **結果** (Archon の共有ワークフローのネイティブの戻り値):

| 対照 | 候補 | 期待 | 結果 | 落ちたアサーション |
|---|---|---|---|---|
| 基準状態 | `factory-v1:d7eaaf8c…` | verified | verified (matched) | — |
| `conversation-not-continued` (画面の JS が送るたびに新しい会話を作る) | `factory-v1:7e112e8a…` | failed | failed (matched)、`score-runtime` で CAUGHT | `two-turns-in-one-conversation` |
| `empty-history-message-hidden` (会話が無いのに空の表示を出さない) | `factory-v1:897336be…` | failed | failed (matched)、`score-runtime` で CAUGHT | `first-screen-empty-history-visible` |
| 別のビルドを指す識別値 (すべて 0) | 基準状態のスロット | inconclusive | inconclusive (`target identity does not match the requested candidate`) | — |

  スイート `archon-verify-runtime-suite` `ab4893ea…` は `expectations_passed: true`、`baseline_verified: true`。識別値の対照は `archon-verify-runtime` `cd54310e…`。検証担当は `agent-browser` で画面を操作し、スクリーンショットを 6 枚残した。基準状態では 2 回目の応答が `#2`、欠陥入りでは `#1` で履歴に会話が 2 件並ぶことを、スクリーンショットで確かめた

- **通常のチェックでの欠陥の検出** (11 件。2 件のアンカーを今のコードに合わせ、3 件を足した):
  - CAUGHT (unit): `health-says-not-ok`、`build-id-ignores-candidate`、`listens-on-all-interfaces`、`config-error-exits-zero`、`fake-down-rejected`、`default-llm-flipped`、`port-in-use-exits-zero`、`history-not-sent-to-ai`
  - ESCAPED: `empty-history-message-hidden`、`conversation-not-continued` (どちらも上のランタイムの校正で捕まった)、`empty-history-message-invisible` (見た目だけ。まだどの段でも試していない)
- **残っている制約**:
  - この WSL では `XDG_RUNTIME_DIR` (`/run/user/1000`) が存在しないため、`agent-browser` はこの変数が設定されていると起動しない。手元のシェルから校正や検証を起動するときは外すこと (スケジュール実行のサービスでは外してある)
  - 欠陥のアンカーはコードの変更で外れる (今回 2 件が外れていた)。外れた欠陥は `NOT_INJECTED` になり、何も確かめない。機能が入るたびに `run.py apply` で全件の注入を確かめる
