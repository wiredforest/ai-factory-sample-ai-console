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
