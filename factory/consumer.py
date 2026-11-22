"""固定リビジョンにある完全な Archon ソース環境を呼び出す。各段階のポリシーはここでは定義しない。"""
from __future__ import annotations

import calendar
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / "pack.json").read_text(encoding="utf-8"))
SETTINGS = ".factory/consumer.json"
ENTRY = "packages/cli/src/cli.ts"
SCHEDULE = ".factory/schedule.json"
TRIGGER = ".factory/trigger.json"
PAUSE = ".factory/PAUSE"
# マージゲートを持つ共有ワークフローと、それぞれが受け付けるマージポリシーのキー。
MERGING_WORKFLOWS = {"archon-merge-queue": ("required_checks", "protected_paths"),
                     "archon-loop": ("protected_paths",)}
# ホールドアウトを保護する Claude Code フックをプロジェクト自身の設定に登録し、
# どのラップのチェックアウトにも持たせる。.factory/holdout/ を読めるのは専用サブエージェントだけ。
FENCE_SCRIPT = ".factory/hooks/holdout_fence.py"
FENCE_HOOKS = {"PreToolUse": "pre", "PostToolUse": "post"}
FENCE_MATCHERS = {
    "PreToolUse": "Bash|PowerShell|Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit|NotebookRead|Agent|Task",
    "PostToolUse": "Bash|PowerShell|Read|Grep|Glob|Agent|Task"}
FENCE_IGNORE = ".factory/holdout/"


def fence_command(mode: str) -> str:
    return f'python "$CLAUDE_PROJECT_DIR/{FENCE_SCRIPT}" {mode}'


def windowless() -> bool:
    """Windows のタイマーが使う pythonw では、子の出力を表示するコンソールがないため True を返す。"""
    return sys.platform == "win32" and sys.stdout is None


def execute(argv: list[str], cwd: Path, *, capture: bool = True,
            timeout: int | None = 180, env: dict | None = None,
            output=None) -> subprocess.CompletedProcess:
    # Windows の .cmd ランチャーも含め、PATH と PATHEXT を一度だけ解決する。
    argv = [shutil.which(argv[0]) or argv[0], *argv[1:]]
    flags = {}
    # ウィンドウのない親からコンソールプログラムを起動すると、既定では新しいコンソールが開く。
    # 出力をキャプチャーする場合やログに残す場合は開く必要がない。
    if sys.platform == "win32" and (capture or output is not None or windowless()):
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW
    streams = ({"stdout": output, "stderr": subprocess.STDOUT} if output is not None
               else {"capture_output": capture})
    return subprocess.run(argv, cwd=cwd, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=env, **streams, **flags)


def checked(argv: list[str], cwd: Path, timeout: int = 180) -> str:
    result = execute(argv, cwd, timeout=timeout)
    if result.returncode:
        raise ValueError(f"コマンドが終了コード {result.returncode} で終了しました: {argv[:3]}\n"
                         + (result.stderr or result.stdout)[-3000:])
    return result.stdout


def project_root() -> Path:
    return Path(checked(["git", "rev-parse", "--show-toplevel"], Path.cwd()).strip()).resolve()


def shared_root(root: Path) -> Path:
    common = checked(["git", "rev-parse", "--git-common-dir"], root).strip()
    return (root / common).resolve().parent


def read_settings(root: Path) -> dict:
    path = shared_root(root) / SETTINGS
    if not path.is_file():
        raise ValueError("Integration pin required. 次のコマンドを実行してください: factory init --source <complete Archon "
                         "checkout or URL> --revision <40-character SHA> --cache <directory>.")
    settings = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError(f"コンシューマー設定が無効です: {path}")
    return settings


def verify_source(settings: dict) -> Path:
    revision = settings.get("revision", "")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("連携リビジョンには小文字の完全なコミット SHA を指定してください")
    source = Path(settings["source"]).resolve()
    if source.name != revision:
        raise ValueError("ソースには、完全な SHA を名前にした固定リビジョンごとの不変ディレクトリを使ってください")
    actual = checked(["git", "rev-parse", "HEAD"], source).strip()
    if actual != revision:
        raise ValueError(f"Source SHA mismatch: 期待値は {revision}、実際は {actual} です")
    dirty = checked(["git", "status", "--porcelain", "--untracked-files=all"], source)
    if dirty.strip():
        # 変更されたパスと復旧方法を示す。よくある原因は固定したツリー内への依存パッケージの導入で、
        # 無害に見えても、その後のすべての実行を止める。
        changed = [line[3:].strip() for line in dirty.strip().splitlines() if line[3:].strip()]
        shown = ", ".join(changed[:5])
        if len(changed) > 5:
            shown += f", and {len(changed) - 5} more"
        raise ValueError(
            f"Pinned source has changes ({shown}). 固定したツリーはバイト単位で検証されます。 "
            f"復元するには、お使いのリポジトリで rm -rf {source} && python3 bin/factory.py init を実行してください。 "
            f"エンジンの挙動を変更するには、ツリーの編集ではなく factory/pack.json の固定先を更新してください"
        )
    # 定義の配置先では無視対象のファイルも調べる。未追跡ファイルと同様、無視対象のワークフローも
    # コミット済みの同名定義を隠し得る。実行時の依存パッケージは引き続き無視する。
    tracked = set(checked(["git", "ls-files"], source).splitlines())
    for directory in (source / ".archon", source / "packages/cli/src"):
        if directory.is_symlink() or not directory.resolve().is_relative_to(source):
            raise ValueError(f"定義ディレクトリが固定したソースの外を指しています: {directory}")
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"定義の配置先ではシンボリックリンクを利用できません: {path}")
            if path.is_file() and path.relative_to(source).as_posix() not in tracked:
                raise ValueError(f"Untracked authoring file in pinned source: {path}")
    for rel in (ENTRY, "package.json", "bun.lock", MANIFEST["source_directory"]):
        if not (source / rel).exists():
            raise ValueError(f"Archon のソースに {rel} がありません")
    if not (source / "node_modules").is_dir():
        raise ValueError("Archon の依存パッケージが不足しています。固定したソースのインストールを完了してください")
    return source


def cli(settings: dict, source: Path) -> list[str]:
    # 環境にある別の archon バイナリやプロバイダーコマンドの上書きは使わない。
    return [settings.get("bun", "bun"), str(source / ENTRY)]


def native_json(settings: dict, source: Path, args: list[str], cwd: Path) -> dict:
    raw = checked([*cli(settings, source), *args, "--cwd", str(cwd), "--json"], cwd)
    data = json.loads(raw)  # 整形済みの JSON も一つの文書全体として読む。
    if not isinstance(data, dict) or data.get("ok") is False:
        raise ValueError(f"ネイティブ CLI の応答が無効です: {raw[:1000]}")
    return data


def source_workflows(source: Path) -> dict[str, Path]:
    # ここで作るのは出所の索引だけ。定義の解析と検証は Archon が担う。
    found = {}
    for path in (source / MANIFEST["source_directory"]).rglob("*"):
        if path.suffix not in (".yaml", ".yml"):
            continue
        match = re.search(r"^name:\s*['\"]?([a-z][a-z0-9-]*)['\"]?\s*$",
                          path.read_text(encoding="utf-8"), re.M)
        if match:
            name = match[1]
            if name in found:
                raise ValueError(f"ソース内でワークフロー名が重複しています: {name}")
            found[name] = path
    # 別のプロジェクト定義によって、選択したパックの要素が隠されてはならない。
    for path in (source / ".archon/workflows").rglob("*"):
        if path.suffix not in (".yaml", ".yml") or path in found.values():
            continue
        match = re.search(r"^name:\s*['\"]?([a-z][a-z0-9-]*)['\"]?\s*$",
                          path.read_text(encoding="utf-8"), re.M)
        if match and match[1] in found:
            raise ValueError(f"Conflicting workflow outside the SDLC pack: {path}")
    # Archon はパッケージのリソースを、その作成元パッケージからの相対位置で解決する。
    # リテラル参照の実体がそこにあることを確認し、ホームや同梱リソースへのフォールバックで
    # 不完全なソースが導入可能に見えることを防ぐ。
    # 構文解析、型、グラフの検証は後段のネイティブ検証が引き続き担当する。
    for name, path in found.items():
        for kind, value in re.findall(r"^\s+(command|script|include):\s*([^\n#]+)",
                                      path.read_text(encoding="utf-8"), re.M):
            value = value.strip().strip("'\"")
            # インラインスクリプトは、この固定された YAML に既に含まれている。
            # ネイティブの isInlineScript の判定に従う。解決する外部ファイルはない。
            if kind == "script" and re.search(r"[;(){}&|<>$`\"' ]", value):
                continue
            if not re.fullmatch(r"[a-zA-Z0-9_./-]+", value):
                raise ValueError(f"{name} 内の {kind} はリテラルではないため出所を検証できません: {value}")
            if kind == "include":
                if value not in found:
                    raise ValueError(f"Include {value} is missing from pinned SDLC source")
                continue
            directory = path.parent / ("commands" if kind == "command" else "scripts")
            candidates = [directory / (value + ".md")] if kind == "command" else [
                directory / (value + suffix) for suffix in ("", ".py", ".ts", ".js", ".sh")]
            if not any(p.is_file() and p.resolve().is_relative_to(source) for p in candidates):
                raise ValueError(f"Missing source-owned {kind} {value} for {name}")
    return found


def discover(settings: dict, source: Path) -> dict[str, Path]:
    local = source_workflows(source)
    data = native_json(settings, source, ["workflow", "list"], source)
    if data.get("errors"):
        raise ValueError(f"ネイティブ CLI のワークフロー検出エラー: {data['errors']}")
    rows = data.get("workflows")
    if not isinstance(rows, list):
        raise ValueError("ネイティブ CLI のワークフロー一覧の応答に workflows 配列がありません")
    names = {row["name"] for row in rows}
    return {name: path for name, path in local.items() if name in names}


def validate(settings: dict, source: Path, name: str) -> None:
    data = native_json(settings, source, ["validate", "workflows", name], source)
    results = data.get("results", [])
    if (not results or any(row.get("valid") is not True for row in results)
            or data.get("summary", {}).get("errors", 0)):
        raise ValueError(f"Workflow/command validation failed for {name}: {data}")


def fence_report(root: Path) -> str:
    """登録されたフック自体を実行し、このチェックアウトでホールドアウトが保護されることを検証する。"""
    if not (root / ".factory/holdout/HOLDOUT.md").exists():
        return "ホールドアウトのシナリオがありません"
    try:
        settings = json.loads((root / ".claude/settings.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        settings = {}
    registered = {hook.get("command") for entries in settings.get("hooks", {}).values()
                  for entry in entries for hook in entry.get("hooks", [])}
    missing = [event for event, mode in FENCE_HOOKS.items() if fence_command(mode) not in registered]
    if missing:
        raise ValueError(f"{', '.join(missing)} のホールドアウト保護フックが .claude/settings.json に登録されていません。factory init を再実行してください")
    ignore = root / ".ignore"
    if not ignore.exists() or FENCE_IGNORE not in ignore.read_text(encoding="utf-8").splitlines():
        raise ValueError(f".ignore に {FENCE_IGNORE} がありません。factory init を再実行してください")
    # ラップは push 済みブランチの新しいチェックアウトで動くので、未コミットの保護設定はそこでは効かない。
    uncommitted = [tracked for tracked in (FENCE_SCRIPT, ".claude/settings.json", ".ignore", ".factory/holdout/HOLDOUT.md")
                   if execute(["git", "ls-files", "--error-unmatch", tracked], root).returncode != 0]
    shell = shutil.which("bash") or shutil.which("sh")
    if shell is None:
        raise ValueError("Claude Code と同じ方法でホールドアウト保護フックを実行するための bash がありません")
    scenarios = str(root / ".factory/holdout/HOLDOUT.md")
    cases = [("ビルダーがシナリオを読む場合", {"file_path": scenarios}, None, 2),
             ("検証担当がシナリオを読む場合", {"file_path": scenarios}, "holdout-verifier", 0),
             ("ビルダーがソースファイルを読む場合", {"file_path": str(root / "README.md")}, None, 0)]
    for name, tool_input, agent, expected in cases:
        event = {"tool_name": "Read", "tool_input": tool_input, "cwd": str(root),
                 **({"agent_type": agent} if agent else {})}
        result = subprocess.run([shell, "-c", fence_command("pre")], input=json.dumps(event), text=True,
                                capture_output=True, cwd=root, timeout=60,
                                env={**os.environ, "CLAUDE_PROJECT_DIR": str(root)})
        if result.returncode != expected:
            raise ValueError(f"ホールドアウトの保護が開いています: {name} の終了コードは {result.returncode} でした (期待値は {expected})。 "
                             f"{result.stderr.strip()[:300]} （bash の `python` が Python 3 を起動するか確認してください）")
    if uncommitted:
        return "このチェックアウトでは保護されていますが、次のファイルをコミットして push するまでラップには反映されません: " + ", ".join(uncommitted)
    return "holds"


def doctor(settings: dict, root: Path | None = None) -> dict:
    source = verify_source(settings)
    # --json などのグローバルフラグは、最上位の workflow ヘルプに一度だけ記載され、
    # 各サブコマンドには繰り返し載らない。`workflow status --help` にあるのは --events と
    # --all だけだが、`status --json` は動く。どちらかのヘルプに載っていれば記載済みとみなす。
    # サブコマンド自体は存在しなければならない。
    shared_help = checked([*cli(settings, source), "workflow", "--help"], source)
    for command, flags in MANIFEST["capabilities"].items():
        help_text = checked([*cli(settings, source), "workflow", command, "--help"], source)
        if f"workflow {command}" not in help_text or any(
                flag not in help_text and flag not in shared_help for flag in flags):
            raise ValueError(f"Pinned CLI missing workflow {command} capability: {flags}")
    discovered = discover(settings, source)
    missing = sorted(set(MANIFEST["entries"]) - discovered.keys())
    if missing:
        raise ValueError("Incomplete integration source; missing shared workflows: " + ", ".join(missing))
    for name in discovered:
        validate(settings, source, name)
    return {"source": str(source), "revision": settings["revision"],
            "workflows": sorted(discovered), "automation": "supervised integration",
            **({"holdout_fence": fence_report(root)} if root is not None else {}),
            "provider_configuration": "ネイティブの設定を維持しています。認証は実際には試していません"}


def harness_inputs(root: Path, name: str) -> dict:
    # プロジェクトのマージポリシーは、ゲートが読み取る確定事項である。
    # エージェントが決めるものではない。ブランチ保護のないプランでは GitHub から必須チェックを取得できず、
    # 人だけが変更してよいファイルも GitHub には記されていない。
    # そのためプロジェクトがこの二つをハーネス設定に一度だけ宣言する。
    if name not in MERGING_WORKFLOWS:
        return {}
    try:
        config = json.loads((root / "harness/harness.config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(config, dict):
        return {}
    return {key: config[key] for key in MERGING_WORKFLOWS[name]
            if isinstance(config.get(key), str) and config[key].strip()}


def workflow_defaults(root: Path, name: str) -> dict:
    return {**MANIFEST.get("default_inputs", {}).get(name, {}), **harness_inputs(root, name)}


def with_default_inputs(name: str, args: list[str], options: list[str], root: Path) -> list[str]:
    supplied = set()
    for index, arg in enumerate(options):
        if arg == "--input" and index + 1 < len(options):
            supplied.add(options[index + 1].split("=", 1)[0])
        elif arg.startswith("--input="):
            supplied.add(arg.removeprefix("--input=").split("=", 1)[0])
    defaults = workflow_defaults(root, name)
    injected = [item for key, value in defaults.items() if key not in supplied
                for item in ("--input", f"{key}={value}")]
    return [args[0], *injected, *args[1:]]


def invoke(root: Path, action: str, args: list[str]) -> int:
    args = list(args)
    runtime_config = None
    options = args[:args.index("--")] if "--" in args else args[:]
    runtime_flags = [a for a in options if a.split("=", 1)[0] == "--runtime-host"]
    if runtime_flags:
        if action != "run" or len(runtime_flags) != 1:
            raise ValueError("--runtime-host はフォアグラウンドでの単一実行にのみ対応します")
        if any(a.split("=", 1)[0] in {"--detach", "--resume", "-d"} for a in options):
            raise ValueError("Detached/resumed runtime-host mode is unsupported: no public durable ownership contract. 新しい実行をフォアグラウンドで始めてください")
        flag = runtime_flags[0]
        index = args.index(flag)
        if "=" in flag:
            runtime_config = flag.split("=", 1)[1]
            del args[index]
        else:
            if index + 1 >= len(options) or options[index + 1].startswith("--"):
                raise ValueError("--runtime-host には信頼できるプロジェクト設定のパスが必要です")
            runtime_config = args.pop(index + 1)
            args.pop(index)
        if not runtime_config:
            raise ValueError("--runtime-host には設定ファイルのパスが必要です")
    if action == "schedule":
        return schedule(root, args)
    if action not in {"run", "list", "get", "status", "runs", "abandon", "approve", "reject", "respond",
                      "cancel", "resume", "doctor", "halt", "unhalt", "pause", "unpause"}:
        raise ValueError(f"Unknown command '{action}'. 利用可能なコマンドは factory --help で確認してください。")
    stop = shared_root(root) / ".factory/STOP"
    if action in {"halt", "unhalt"}:
        if args:
            raise ValueError(f"{action} は引数を取りません。稼働中の実行を取り消すには実行 ID を指定してください。")
        if action == "halt":
            stop.parent.mkdir(parents=True, exist_ok=True)
            stop.write_text("運用者がファクトリーの新規起動を止めました。\n", encoding="utf-8")
        else:
            stop.unlink(missing_ok=True)
        print("ローカルの起動ブレーキを" + ("設定しました。実行中の処理を止めるには cancel <run-id> が必要です。" if action == "halt" else "解除しました。"))
        return 0
    if action in {"pause", "unpause"}:
        # 止めるのは新規起動のみ。タイマーは永続化された待機を解除し続けるので、CI や使用量制限で
        # 一時停止した実行中のラップも完了できる。ホストのタイマーを無効にすると取り残される。
        # halt は設計どおり、その再開も止める。
        if args:
            raise ValueError(f"{action} は引数を取りません")
        pause = shared_root(root) / PAUSE
        if action == "pause":
            pause.parent.mkdir(parents=True, exist_ok=True)
            pause.write_text("運用者がスケジュールによる起動を一時停止しました。wake は続きます。\n", encoding="utf-8")
        else:
            pause.unlink(missing_ok=True)
        print("Scheduled launches " + ("paused. タイマーは実行中の処理を引き続き再開させます。明示的な実行は今までどおり起動します。"
                                       if action == "pause" else "resumed."))
        return 0
    for arg in args:
        if arg.split("=", 1)[0] in {"--cwd", "--workflow-source"}:
            raise ValueError("--cwd と --workflow-source はファクトリーが設定します。作業ディレクトリでアプリケーションを選んでください")
    if action in {"run", "resume", "approve", "respond"} and stop.exists():
        raise ValueError("Local STOP is set. unhalt で起動と継続を許可できます。cancel は引き続き使えます")
    settings = read_settings(root)
    source = verify_source(settings)
    if action == "doctor":
        print(json.dumps(doctor(settings, root), indent=2))
        return 0
    if action == "list":
        print(json.dumps({"source": str(source), "revision": settings["revision"],
                          "workflows": sorted(discover(settings, source))}, indent=2))
        return 0
    # これらのフラグは呼び出し元の引数より前に置く。呼び出し元の `--` の後に足すと
    # メッセージ本文として扱われ、環境からソースを探索する既定の挙動に黙って戻ってしまう。
    native = ["workflow", action, "--cwd", str(root)]
    if action == "run":
        if not args:
            raise ValueError("使い方: factory run <shared-workflow> [ネイティブのオプションとメッセージ]")
        name = args[0]
        if name not in discover(settings, source):
            raise ValueError(f"Shared workflow '{name}' is absent from the pinned SDLC source; no fallback")
        validate(settings, source, name)
        options = args[:args.index("--")] if "--" in args else args
        resuming = any(arg.split("=", 1)[0] == "--resume" for arg in options)
        if not resuming:
            native += ["--workflow-source", str(source)]
            args = with_default_inputs(name, args, options, root)
    native += args
    if action == "status":
        print(f"Factory source={source} revision={settings['revision']} local_STOP={stop.exists()}", file=sys.stderr)
    # ネイティブの出力、終了コード、入力、実行主体、ゲートをそのまま渡す。
    # サブプロセスの期限やリトライから、ネイティブ実行が生きているかは推測できない。
    if runtime_config:
        from runtime_host import RuntimeHost
        with RuntimeHost(root / runtime_config) as host:
            return execute([*cli(settings, source), *native], root, capture=False,
                           timeout=None, env={**os.environ, **host.environment()}).returncode
    return execute([*cli(settings, source), *native], root,
                   capture=False, timeout=None).returncode


def repository_slug(root: Path) -> str:
    remote = checked(["git", "remote", "get-url", "origin"], root).strip()
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", remote)
    if not match:
        raise ValueError("origin の URL から単一の owner/repo を特定できません")
    return match[1]


def whoami(settings: dict, source: Path, root: Path) -> str:
    # CLI は JSON 応答より先にログを出すので、最後のオブジェクトを読む。
    raw = checked([*cli(settings, source), "trigger", "whoami"], root)
    for start in reversed([m.start() for m in re.finditer(r"^\{", raw, re.M)]):
        try:
            data = json.loads(raw[start:])
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("runAsUserId"), str):
            return data["runAsUserId"]
    raise ValueError("archon trigger whoami の応答に runAsUserId がありません")


def base_branch(root: Path, schedule_data: dict) -> str | None:
    """ラップの起点となるブランチを返す。schedule.json の `base_branch` を優先し、未指定なら
    最後の fetch が記録した remote の既定ブランチ（origin/HEAD）をローカルで読む。
    両方とも不明なら None を返し、起動時に remote へ直接問い合わせる。"""
    named = schedule_data.get("base_branch")
    if named is not None:
        if not isinstance(named, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", named):
            raise ValueError("schedule.json の base_branch にはブランチ名を指定してください")
        return named
    result = execute(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], root)
    head = result.stdout.strip() if result.returncode == 0 else ""
    return head.split("/", 1)[1] if head.startswith("origin/") else None


def trigger_config(root: Path, settings: dict, source: Path, interval: int) -> dict:
    """スケジュール対象の共有ワークフローに Archon トリガーを結び付ける。受け付けの制御、
    永続キュー、実行の重複は Archon が扱う。ここでは起動対象だけを指定する。"""
    schedule_file = shared_root(root) / SCHEDULE
    schedule_data = json.loads(schedule_file.read_text(encoding="utf-8"))
    workflow = schedule_data.get("workflow", "archon-loop")
    inputs = schedule_data.get("inputs")
    if not isinstance(workflow, str) or not isinstance(inputs, dict):
        raise ValueError("schedule.json には共有ワークフローと inputs オブジェクトが必要です")
    if "runtime_host" in schedule_data:
        raise ValueError("トリガーの内部でランタイムホストを起動することはできません。独立したサービスとして起動し"
                         "(python factory/runtime_host.py serve --config <runtime.json> "
                         "--connection-file <connection.json>)、schedule.json から runtime_host を削除してください")
    if workflow not in discover(settings, source):
        raise ValueError(f"Shared workflow '{workflow}' is absent from the pinned SDLC source")
    for key in inputs:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("スケジュール対象ワークフローの入力名が無効です")
    values = {**workflow_defaults(root, workflow),
              **{k: v if isinstance(v, str) else json.dumps(v) for k, v in inputs.items()}}
    parallel = schedule_data.get("parallel", 1)
    if not isinstance(parallel, int) or isinstance(parallel, bool) or not 1 <= parallel <= 8:
        raise ValueError("schedule.json parallel must be a whole number of laps from 1 to 8")
    slug = repository_slug(root)
    # Archon は初回使用時にスロットの容量を決めるため、
    # 同時実行するラップ数が変われば別のスロットになる。
    resource = f"github:{slug}:{workflow}" + (f":x{parallel}" if parallel > 1 else "")
    # ベースブランチが不明だと起動のたびに remote の既定値を問い合わせる（ls-remote を一度、再試行なし）。
    # それに失敗すると実行は開始しないままスロットを保持する。
    base = base_branch(root, schedule_data)
    isolation = {"kind": "worktree", "baseOverride": base} if base else {"kind": "default"}
    return {
        "version": 1,
        "sourceInstanceId": "factory-" + slug.replace("/", "-"),
        "binding": {
            "bindingId": "factory-" + workflow,
            "bindingRevision": None,
            "hostId": "factory-" + re.sub(r"[^A-Za-z0-9-]", "-", platform.node() or "host"),
            "runAsUserId": whoami(settings, source, root),
            # リポジトリとワークフローの組に一つのリソースを割り当て、最大 `parallel` ラップを実行する。
            # 全ラップ稼働中に来た tick はスキップし、次の tick が次のラップを開始する。
            # 並列ラップが同じ仕事に取り掛からないよう、ワークフローが調整する。
            "resource": resource,
            "capacity": parallel,
            "overlap": "skip",
            "launch": {"cwd": str(root), "workflowName": workflow, "inputs": values,
                       "isolation": isolation},
        },
        "schedule": {"intervalSeconds": interval, "runAtLoad": False},
    }


def task_xml(python: Path, root: Path, minutes: int) -> str:
    """N 分ごとに `schedule fire` を非表示で実行するタスクスケジューラ定義を返す。"""
    def text(value: object) -> str:
        return (str(value).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))
    start = time.strftime("%Y-%m-%dT%H:%M:%S")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Archon factory trigger for {text(root)}</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition><Interval>PT{minutes}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Hidden>true</Hidden>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{text(python)}</Command>
      <Arguments>factory\\consumer.py schedule fire</Arguments>
      <WorkingDirectory>{text(root)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def timer_commands(root: Path, interval: int) -> dict:
    """ホストのスケジューラが実行する設定を返す。launchd ジョブは Archon 自身が登録する。
    ほかの環境では `factory schedule fire` を呼び、ローカルの STOP に従う。"""
    fire = [sys.executable, str(root / "factory/consumer.py"), "schedule", "fire"]
    name = "ArchonFactory-" + re.sub(r"[^A-Za-z0-9-]", "-", root.name)
    if sys.platform == "win32":
        # pythonw はコンソールを持たず、tick のたびにウィンドウが現れることもない。
        # schtasks /TR では作業ディレクトリを設定できないのでタスクを XML で定義する。
        # 画面描画を伴う実行時検証にはデスクトップセッションが要るため、ユーザーのログオン中だけ動かす。
        # 長いラップの再開が次の起動を妨げないよう、tick は並列に走らせる。
        # 各段階は `schedule fire` 自身が同時に一つに限るため、tick が積み重なることはない。
        pythonw = Path(fire[0]).with_name("pythonw.exe")
        task = shared_root(root) / ".factory/schedule-task.xml"
        return {"task_xml": {task: task_xml(pythonw if pythonw.exists() else Path(fire[0]),
                                            root, max(1, interval // 60))},
                "install": [["schtasks", "/Create", "/TN", name, "/XML", str(task), "/F"]],
                "remove": [["schtasks", "/Delete", "/TN", name, "/F"]]}
    if sys.platform == "darwin":
        return {"install": [], "remove": []}  # launchd ジョブは Archon 自身がインストールする。
    unit = name.lower()
    # タイマーには手動実行と同じツールの PATH、リポジトリ外のモード 600 のファイルにある認証情報、
    # root で動く Claude Code 用の IS_SANDBOX を渡す。
    return {"systemd": {
        f"{unit}.service": "[Unit]\nDescription=Archon factory trigger\n\n[Service]\nType=oneshot\n"
                           f"WorkingDirectory={root}\nEnvironment=PATH={os.environ.get('PATH', '')}\n"
                           "Environment=IS_SANDBOX=1\nEnvironmentFile=-%h/.factory-env\n"
                           f"ExecStart={' '.join(fire)}\n",
        f"{unit}.timer": f"[Unit]\nDescription=Archon factory trigger\n\n[Timer]\nOnBootSec=2min\n"
                         f"OnUnitInactiveSec={interval}s\n\n[Install]\nWantedBy=timers.target\n"},
        "install": [["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "--now", f"{unit}.timer"]],
        "remove": [["systemctl", "--user", "disable", "--now", f"{unit}.timer"]]}


@contextlib.contextmanager
def single_flight(path: Path):
    """`path` の OS ロックを保持する一つのプロセスに True、それ以外に False を yield する。
    プロセスの終了とともにロックも消えるので、tick が強制終了しても以後の tick は詰まらない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        handle.close()


ENDED = {"completed", "failed", "cancelled"}
ACTIVE = {"pending", "running", "paused"}
# 実行開始前に起動が失敗したなら、この時点でログの最終行は書き終わっている。
STUCK_AFTER_SECONDS = 600


def archon_home() -> Path:
    return Path(os.environ.get("ARCHON_HOME") or Path.home() / ".archon")


def sweep_stuck(root: Path, settings: dict, source: Path, runs: list[dict], write) -> set[str]:
    """実行が始まる前に起動に失敗したものを放棄する。放棄されるまでスロットを保持して
    `pending` に留まり、起動ログの最終行にはエラーが残る。"""
    swept = set()
    for run in runs:
        if run.get("status") != "pending" or not isinstance(run.get("id"), str):
            continue
        launch_log = archon_home() / "logs" / f"trigger-run-{run['id']}.log"
        try:
            if time.time() - launch_log.stat().st_mtime < STUCK_AFTER_SECONDS:
                continue
            lines = [line for line in launch_log.read_text(encoding="utf-8", errors="replace").splitlines()
                     if line.strip()]
        except OSError:
            continue
        if not lines or not lines[-1].startswith("Error:"):
            continue
        result = execute([*cli(settings, source), "workflow", "abandon", run["id"], "--cwd", str(root)], root)
        if result.returncode == 0:
            swept.add(run["id"])
            write(f"Abandoned run {run['id']}, whose launch failed before it started: {lines[-1][:300]}\n")
        else:
            write(f"停止した実行 {run['id']} を放棄できませんでした: {(result.stderr or result.stdout)[-300:]}\n")
    return swept


# 稼働中のラップは少なくとも 30 分に一度ログを書く（2026-10-25 に 150 実行を測定し、
# 正常時にログが途切れた最長時間は 29.5 分）。1 時間無出力なら終了処理をせずに死んでいる。
# 実際にレビュー段階で落ちたラップが唯一のスロットを 5 時間保持した。
SILENT_AFTER_SECONDS = 3600


def sweep_silent(root: Path, settings: dict, source: Path, runs: list[dict], write) -> set[str]:
    """running のまま SILENT_AFTER_SECONDS 秒間ログが途絶えた実行を放棄する。
    放棄するとスロットを解放し、worktree は削除するが実行ブランチは保持する。
    コミット済みの作業だけが次のラップに引き継がれる。"""
    swept = set()
    for run in runs:
        if run.get("status") != "running" or not isinstance(run.get("id"), str):
            continue
        logs = list((archon_home() / "workspaces").glob(f"*/*/logs/{run['id']}.jsonl"))
        try:
            newest = max(log.stat().st_mtime for log in logs) if logs else None
        except OSError:
            continue
        if newest is None or time.time() - newest < SILENT_AFTER_SECONDS:
            continue
        result = execute([*cli(settings, source), "workflow", "abandon", run["id"], "--cwd", str(root)], root)
        if result.returncode == 0:
            swept.add(run["id"])
            write(f"Abandoned run {run['id']}: its log has been silent for "
                  f"{int((time.time() - newest) // 60)} min\n")
        else:
            write(f"ログが途絶えた実行 {run['id']} を放棄できませんでした: {(result.stderr or result.stdout)[-300:]}\n")
    return swept


def tick_state(root: Path, **updates) -> dict:
    """tick の間で保持する少量の状態（.factory/gate.json）をその場で更新する。"""
    path = shared_root(root) / ".factory/gate.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if updates:
        state.update(updates)
        path.write_text(json.dumps(state) + "\n", encoding="utf-8")
    return state


EMPTY_LAP_SECONDS = 300
CHOOSING_SECONDS = 1800


def _when(value) -> float | None:
    try:
        return calendar.timegm(time.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def empty_lap_since(runs: list[dict], since: float) -> bool:
    """`since` 以降に開始して数分以内に完了した、作業を選ばなかったラップがあるかを返す。
    使用量制限や障害で失敗したラップからはボードの状態を推測できない。"""
    for run in runs:
        start, end = _when(run.get("started_at")), _when(run.get("completed_at"))
        if start is None or end is None or start + 60 < since:
            continue
        if run.get("status") == "completed" and end - start < EMPTY_LAP_SECONDS:
            return True
    return False


def launch_decision(root: Path, settings: dict, source: Path, write) -> bool:
    """この tick でラップを起動すべきかを返す。起動ゲートがないワークフローは常に起動する。
    ゲートがあれば、まず開始しなかった実行と、終了した実行が残した作業項目の確保を解除する。
    次に、着手可能な仕事があり、ほかに項目を選択中のラップがない場合だけ起動する。
    加えて、アイドル期間ごとに一度、記録の整理のために起動する。ボードが読めなければ、
    ゲート導入前と同じく起動する。"""
    try:
        binding = json.loads((shared_root(root) / TRIGGER).read_text(encoding="utf-8"))["binding"]
        launch_config = binding["launch"]
        workflow, inputs = launch_config["workflowName"], launch_config.get("inputs") or {}
        capacity = binding.get("capacity")
    except (OSError, ValueError, KeyError, TypeError):
        return True
    try:
        schedule_data = json.loads((shared_root(root) / SCHEDULE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        schedule_data = {}
    gate = MANIFEST.get("launch_gates", {}).get(workflow)
    script = source / MANIFEST["source_directory"] / gate if gate else None
    if script is None or not script.is_file():
        return True
    state = tick_state(root)

    signature = ""

    def launch(reason: str) -> bool:
        write(f"Launch: {reason}\n")
        tick_state(root, last_launch=time.time(), last_launch_board=signature)
        return True

    try:
        listed = native_json(settings, source, ["workflow", "runs", "--limit", "100"], root)
        runs = [run for run in listed.get("runs", []) if run.get("workflow_name") == workflow]
        swept = sweep_stuck(root, settings, source, runs, write) | sweep_silent(root, settings, source, runs, write)
        ended = [run["id"] for run in runs if run.get("status") in ENDED]
        active = {run["id"] for run in runs if run.get("status") in ACTIVE} - swept
        result = execute([settings.get("bun", "bun"), str(script)], root, timeout=180, env={
            **os.environ,
            "INPUTS_CLAIM_HOURS": str(inputs.get("claim_hours", "")),
            "INPUTS_ENDED_RUNS": ",".join(ended)})
        if result.returncode:
            raise ValueError((result.stderr or result.stdout)[-500:])
        board = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        return launch(f"the launch gate could not read the board ({str(error)[:300]}).")
    for claim in board.get("ended", []):
        write(f"Released the claim run {claim['run']} left on #{claim['number']} when it ended.\n")
    workable = board.get("workable", [])
    # 作業を選んでいるのはラップの初期だけで、選択は数分で終わる。確保した作業項目の有効期限が
    # 切れた長時間のラップは選択中ではない。これを選択中と数えてすべての起動を遅らせたことがある
    # （実環境で 8 時間のラップが二つ目のスロットを 1 時間以上塞いだ）。
    recent = {run["id"] for run in runs if run.get("id") in active and
              (_when(run.get("started_at")) or time.time()) >= time.time() - CHOOSING_SECONDS}
    choosing = recent - {claim["run"] for claim in board.get("claims", [])}
    names = ", ".join(f"#{item['number']}" for item in workable) or "nothing"
    # ラップから見えるボードの状態。開始後数分で完了したラップは何も選んでいない
    # （実際の作業にはずっと時間がかかる）。その起動からボードに変化がなければ、
    # 次のラップにも同じ状態が見え、同様に何も選ばないので変化を待つ。
    # ゲートが着手可能と数えても、別の実行中のラップが確保して変更している部分に触れるため、
    # 作業選択で見送られる項目が実際にあった。
    # どのオープンな項目でも最新の変更時刻を表す `updated` により、回答も状態変化と数える。
    # 所有者の判断で保留を解いても、着手可能項目と確保状況は保留前と同じに見える。
    # 実際に所有者が項目を解放した後、ファクトリーが 40 分アイドルになった。
    signature = json.dumps([sorted(f"{item.get('kind')}:{item.get('number')}" for item in workable),
                            sorted(claim["run"] for claim in board.get("claims", [])),
                            str(board.get("updated", ""))])
    # 全スロットが使用中なら Archon は起動を見送る。起動したと記録せず、その事実を伝える。
    if isinstance(capacity, int) and len(active) >= capacity:
        write(f"No launch: all {capacity} slot(s) busy; workable {names}.\n")
        return False
    if workable and not choosing and signature == state.get("last_launch_board") and \
            empty_lap_since(runs, state.get("last_launch", 0)):
        write(f"No launch: the last lap found nothing to start on this same board (workable {names}).\n")
        return False
    # 作業選択は同時に一つだけ。二つのラップが同時に選ぶと同じ項目に行き着き、後のラップは確保に失敗した（実測）。
    # 次の tick では先のラップが確保した項目が見える。
    if workable and not choosing:
        return launch(f"workable {names}; no lap still choosing.")
    idle = schedule_data.get("idle_pick_minutes", 60)
    if not isinstance(idle, (int, float)) or isinstance(idle, bool) or idle < 0:
        raise ValueError("schedule.json の idle_pick_minutes に分数を数値で指定してください（0 で無効）")
    if idle and not active and time.time() - state.get("last_launch", 0) >= idle * 60:
        return launch(f"着手できる項目はありませんが、{idle} 分間どのラップもボードを見ていません (bookkeeping)。")
    write(f"No launch: workable {names}; {len(active)} lap(s) active, {len(choosing)} still choosing.\n")
    return False


def schedule(root: Path, args: list[str]) -> int:
    action = args[0] if args else ""
    flags = args[1:]
    apply = "--apply" in flags
    interval = 300
    if "--interval" in flags:
        interval = int(flags[flags.index("--interval") + 1])
        if interval < 60:
            raise ValueError("--interval には 60 秒以上を指定してください")
    settings = read_settings(root)
    source = verify_source(settings)
    config_path = shared_root(root) / TRIGGER
    stop = shared_root(root) / ".factory/STOP"
    native = cli(settings, source)
    if action == "install":
        config = trigger_config(root, settings, source, interval)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        plan = timer_commands(root, interval)
        print(f"{config_path} に {config['binding']['launch']['workflowName']} 用の Archon トリガー設定を書き込みました。")
        base = config["binding"]["launch"]["isolation"].get("baseOverride")
        print(f"Laps start from {base}." if base else
              "No default branch is known locally, so every launch asks the remote for it. "
              "`git remote set-head origin --auto` を実行するか、schedule.json で base_branch を指定してください。")
        if sys.platform == "darwin":
            plan = {"install": [[*native, "trigger", "schedule", "install", "--config", str(config_path)],
                                [*native, "workflow", "wake", "schedule", "install", "--interval", "60"]]}
        for task_file, body in plan.get("task_xml", {}).items():
            print(f"--- {task_file}\n{body}")
            if apply:
                task_file.parent.mkdir(parents=True, exist_ok=True)
                task_file.write_text(body, encoding="utf-16")
        if "systemd" in plan:
            unit_dir = Path.home() / ".config/systemd/user"
            for file_name, body in plan["systemd"].items():
                print(f"--- {unit_dir / file_name}\n{body}")
                if apply:
                    unit_dir.mkdir(parents=True, exist_ok=True)
                    (unit_dir / file_name).write_text(body, encoding="utf-8")
        for command in plan["install"]:
            print(("Running: " if apply else "Run to enable: ") + subprocess.list2cmdline(command))
            if apply:
                checked(command, root)
        return 0
    if action == "fire":
        if stop.exists():
            print("Local STOP is set. トリガーは発火していません。再開するには unhalt を使ってください。", file=sys.stderr)
            return 0
        if not config_path.is_file():
            raise ValueError("トリガー設定がありません。先に factory schedule install を実行してください")
        # ウィンドウのないタイマーの tick（pythonw）はコンソールがないため、出力をログに送る。
        log = ((shared_root(root) / ".factory/schedule.log").open("a", encoding="utf-8")
               if windowless() else None)
        try:
            if log:
                log.write(f"=== schedule fire {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                log.flush()
            write = log.write if log else sys.stderr.write
            fired = 0
            if (shared_root(root) / PAUSE).exists():
                write("Scheduled launches paused. トリガーは発火せず、wake だけを行います。再開するには unpause を使ってください。\n")
            else:
                # 長い再開処理が次の起動を止めないよう tick は並列に動く。各段階には一度に一つの tick だけを通し、
                # その段階が使用中なら、到着した tick はそこを飛ばす。
                with single_flight(shared_root(root) / ".factory/fire.lock") as held:
                    if not held:
                        write("Another tick is still launching. 今回のティックは起動を行いません。\n")
                    elif launch_decision(root, settings, source, write):
                        fired = execute([*native, "trigger", "fire", "--config", str(config_path)], root,
                                        capture=False, timeout=None, output=log).returncode
            if log:
                log.flush()
            # 同じ tick で、再開時刻が来た永続待機（CI による停止や使用量制限後の再開）を起こす。
            woken = 0
            with single_flight(shared_root(root) / ".factory/wake.lock") as held:
                if held:
                    woken = execute([*native, "workflow", "wake"], root, capture=False, timeout=None,
                                    output=log).returncode
                else:
                    write("前のティックの wake がまだ実行中のため、今回は wake を行いません。\n")
        finally:
            if log:
                log.close()
        return fired or woken
    if action == "remove":
        plan = timer_commands(root, interval)
        if sys.platform == "darwin" and config_path.is_file():
            plan = {"remove": [[*native, "trigger", "schedule", "remove", "--config", str(config_path)]]}
        for command in plan["remove"]:
            print(("Running: " if apply else "Run to disable: ") + subprocess.list2cmdline(command))
            if apply:
                execute(command, root)
        if apply:
            config_path.unlink(missing_ok=True)
        return 0
    if action == "status":
        return execute([*native, "trigger", "list"], root, capture=False, timeout=None).returncode
    raise ValueError("使い方: factory schedule install|fire|remove|status [--interval <seconds>] [--apply]")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"--help", "-h"}:
        print("factory run <shared-workflow> [ネイティブ CLI の引数]\n"
              "factory run <shared-workflow> --runtime-host <config.json> [フォアグラウンド実行用のネイティブ CLI 引数]\n"
              "ランタイムホストでは通常のアプリを新規に起動します。切り離し・再開には非対応です。手動での操作方法: python factory/runtime_host.py serve --help\n"
              "factory schedule install [--interval <seconds>] [--apply] | fire | remove [--apply] | status\n"
              "factory list | doctor | status | runs | get <run-id>\n"
              "factory approve | reject | respond | cancel | resume | abandon <run-id>\n"
              "factory halt | unhalt （ローカルでの起動停止・解除のみ）\n"
              "factory pause | unpause （定時起動を一時停止・再開。タイマーによる実行中ラップの再開は継続）")
        return 0
    try:
        return invoke(project_root(), args[0], args[1:])
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"Factory refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
