# アプリの動かし方 (ランタイムの手順)

`archon-loop` などの共有ワークフローに `runtime` 入力として渡す文書です。アプリを利用者と同じ経路で起動し、外から確かめるための手順を書きます。
値は issue #4 (`walking-skeleton`) の「工場との契約」の表に従います。契約が変わったら、所有者がこの文書を直します (保護対象)。

## 前提

- Python 3.14 以降 (`python --version`)。実行時の依存パッケージは無いので、アプリを動かすのに `uv sync` などは要りません。
- 静的検査とユニットテストは `python harness/ci.py` でまとめて動かします (中で `uv run ruff check .` と `uv run pytest -q` を実行します)。
- **本物の AI を呼ばないこと。** 起動するときは必ず `ANTHROPIC_API_KEY` を環境から外し、`CONSOLE_LLM` を `fake` か `fake-down` にします。`anthropic` や未設定では起動しません。

## 起動する

試行ごとに新しい状態ディレクトリと空いているポートを使います。固定のポート (`8765` など) は使いません。並行するラップと衝突するためです。状態とログは、リポジトリの中ではなく、その実行の `$ARTIFACTS_DIR` の下に置きます。

```bash
STATE=$(mktemp -d "$ARTIFACTS_DIR/state.XXXXXX")
PORT=$(python -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1]); s.close()")
env -u ANTHROPIC_API_KEY \
  CONSOLE_DB_PATH="$STATE/console.sqlite3" \
  CONSOLE_LLM=fake \
  FACTORY_RUNTIME_CANDIDATE="$(git rev-parse HEAD)" \
  python app.py --port "$PORT" > "$ARTIFACTS_DIR/app-$PORT.log" 2>&1 &
APP_PID=$!
```

起動を待つ: `http://127.0.0.1:$PORT/health` が 200 で本文 `ok` を返すまで、最大 45 秒、1 秒おきに確かめます。45 秒たっても返らなければ、ログ (`$ARTIFACTS_DIR/app-$PORT.log`) を証拠にして起動の失敗として報告します。

自分が起動したアプリを確かめていることを示す: `curl -s http://127.0.0.1:$PORT/build-id` の本文が、`FACTORY_RUNTIME_CANDIDATE` に渡した値 (確かめるチェックアウトのコミット) とちょうど一致することを確かめ、その出力を証拠として保存します。一致しなければ、別のプロセスを見ているので、その先の結果は使えません。

## 利用者と同じ経路で操作する

- 画面: ブラウザで `http://127.0.0.1:$PORT/` を開きます。会話の領域 (`id="chat"`)、履歴のサイドメニュー (`id="history"`)、サーバの状態のメニュー (`id="status"`) があります。ブラウザを操作できる道具があれば使い、スクリーンショットを保存して、開いて中身を確かめます。
- API: 画面が呼ぶ JSON の API を `curl` で直接呼んでもかまいません。画面の HTML と JS が実際に呼ぶ経路を使い、テスト用の近道は使いません。リクエストとレスポンスを、そのまま `$ARTIFACTS_DIR` にファイルで保存します。
- 偽の応答役 (`CONSOLE_LLM=fake`) は `fake reply #<n>: <直前の発言>` を返します。`<n>` はその会話でのそれまでの往復の数 (1 往復目は 1) です。
- データベースに直接書き込んだり、結果の状態を前もって作ったりしてはいけません。状態は、利用者の操作で作ります。

## 再起動する

再起動しても記録が残るか (ジャーニー 3) を確かめるときは、同じ `STATE` のまま起動し直します。

```bash
kill "$APP_PID"; wait "$APP_PID" 2>/dev/null
```

ポートが閉じたことを確かめてから、上の「起動する」と同じコマンドを、`STATE` を作り直さずに実行します (ポートは新しく選んでかまいません)。

## AI に繋がらない状態

ジャーニー 4 は `CONSOLE_LLM=fake-down` で起動します (ほかは同じ)。「AI への接続」だけが異常になり、送ったメッセージが残ることを確かめます。

## どのジャーニーを確かめるか

シナリオは `harness/END-TO-END.md` の 4 本です。PRD から作り始めたため、ジャーニーの手順の中には、まだ作られていない機能に依存するものがあります (issue #4〜#12 の順に揃います)。

- 確かめる変更と、それをマージした結果のデフォルトブランチにある機能を使う手順は、すべて実行します。
- 依存する機能がまだどの issue でも作られていない手順は、その issue 番号を挙げて「未実装のため対象外」と報告します。PASS とは書きません。
- 以前のラップで通っていた手順が通らなくなったら、それはリグレッションです。対象外にしてはいけません。

## 後片付け

確かめ終えたら、起動したプロセスを止め (`kill "$APP_PID"; wait "$APP_PID"`)、ポートが閉じたことを確かめます。`STATE` は証拠として `$ARTIFACTS_DIR` に残してかまいません。リポジトリの中にはファイルを作りません。
