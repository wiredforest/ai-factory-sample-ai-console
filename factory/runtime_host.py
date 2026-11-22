"""認証付きでプロジェクトの通常の実行環境を提供し、親プロセスとのパイプが閉じるまで管理します。"""
from __future__ import annotations
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import URLError

from runtime_process import ProcessTree
from runtime_resource import FORBIDDEN, MARKER, owned_destination, resource_digest

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


HTTP = build_opener(ProxyHandler({}), NoRedirect())
def safe_path(root, relative):
    if (not isinstance(relative, str) or not relative or Path(relative).anchor
            or Path(relative).drive):
        raise ValueError("ソースのパスは相対パスで指定してください")
    path = root / relative
    if any(p.lower() in FORBIDDEN or p == ".." for p in Path(relative).parts):
        raise ValueError("非公開パス、またはルート外に出る可能性のあるソースパスは使用できません")
    if not path.resolve().is_relative_to(root):
        raise ValueError("ソースパスが設定済みのルートの外を指しています")
    cursor = path
    while cursor != root:
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            raise ValueError("ソースパスにリンクは使用できません")
        cursor = cursor.parent
    return path


def source_files(root, includes):
    files = {}
    for rel in includes:
        path = safe_path(root, rel)
        if not path.exists():
            raise ValueError("設定で指定したソースが見つかりません")
        candidates = path.rglob("*") if path.is_dir() else [path]
        for child in candidates:
            name = child.relative_to(root).as_posix()
            safe_path(root, name)
            if child.is_file():
                files[name] = child
    if not files:
        raise ValueError("ソースのスナップショットが空です")
    return files


def digest(files):
    result = hashlib.sha256()
    for name, path in sorted(files.items()):
        content = path.read_bytes()
        result.update(name.encode() + b"\0" + str(len(content)).encode() + b"\0" + content)
    return result.hexdigest()


def tree_digest(root):
    return digest(source_files(root, [p.name for p in root.iterdir()]))


def remove_tree(root):
    def writable(function, path, exc):
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        function(path)
    deadline = time.monotonic() + 5
    while root.exists():
        try:
            shutil.rmtree(root, onerror=writable)
        except PermissionError:
            # Windows では、最後のファイルハンドルを解放する直前にジョブ内のプロセス数が 0 になることがある。
            # 待機時間に上限を設け、後始末がそれでも失敗するときはエラーを呼び出し元に返す。
            if time.monotonic() >= deadline:
                raise
            time.sleep(.05)


def command(value, variables):
    if not isinstance(value, list) or not value or not all(isinstance(s, str) and s for s in value):
        raise ValueError("コマンドは空でない引数配列（argv）で指定してください。各要素には空でない文字列が必要です")
    argv = []
    for item in value:
        for key, replacement in variables.items():
            item = item.replace("{" + key + "}", replacement)
        argv.append(item)
    executable = shutil.which(argv[0]) or argv[0]
    # 実行できるのは通常のランタイムに限る。スクリプトは運用者が用意した信頼できるコードとして扱う。
    # 同じユーザー権限での隔離やスクリプトの内容検査は、この制限の対象外。
    if not re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|node)(?:\.exe)?", Path(executable).name.lower()):
        raise ValueError("通常の Python または Node の実行ファイルを指定してください。シェルやプロバイダーの CLI は使用できません")
    argv[0] = executable
    return argv


class Environments:
    def __init__(self, config, stopping):
        if config.get("version") != 1 or not isinstance(config.get("roots"), dict):
            raise ValueError("ランタイム設定には version 1 と名前付きの roots が必要です")
        self.config = config
        self.roots = {}
        self.bindings = {}
        for name, raw in config["roots"].items():
            bound = isinstance(raw, dict)
            path = Path(raw.get("path", "")) if bound else Path(raw)
            if not path.is_absolute() or path.is_symlink() or (not bound and not path.is_dir()):
                raise ValueError("各ルートにはディレクトリの絶対パスを明示してください")
            path = path.resolve()
            if bound:
                if set(raw) != {"path", "binding"} or raw["binding"] != MARKER:
                    raise ValueError("リビジョンに結び付けるルートでは path を指定し、binding を .factory-resource.json に設定してください")
                self.bindings[name] = True
            self.roots[name] = path
        self.stopping = stopping
        self.base = Path(tempfile.mkdtemp(prefix="factory-runtime-"))
        self.active = {}
        self.used_ports = set()

    def teardown(self, slot):
        item = self.active.get(slot)
        if item:
            for proc in reversed(item["processes"]):
                proc.close()
            remove_tree(item["directory"])
            del self.active[slot]
        return {"version": 1, "stopped": True}

    def close(self):
        for slot in list(self.active):
            self.teardown(slot)
        remove_tree(self.base)

    def start(self, request):
        slot = request.get("slot")
        if not isinstance(slot, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", slot):
            raise ValueError("スロットの指定が無効です")
        # 同じスロットを置き換えるときは、再試行のリクエストに不備があっても、既存の環境を先に片付ける。
        self.teardown(slot)
        if set(request) - {"slot", "root", "mutation", "expected_source", "expected_revision"}:
            raise ValueError("start に未定義のフィールドがあります。呼び出し元からコマンドやパスは指定できません")
        root = self.roots.get(request.get("root"))
        if root is None:
            raise ValueError("指定されたルートは設定にありません")
        binding = None
        if self.bindings.get(request.get("root")):
            if not owned_destination(root):
                raise ValueError("指定されたルートは管理下のランタイムリソースではありません")
            binding = json.loads((root / MARKER).read_text(encoding="utf-8"))
            expected_revision = request.get("expected_revision")
            if not isinstance(expected_revision, str) or expected_revision != binding["source_revision"]:
                raise ValueError("期待される候補リビジョンは現在のリビジョンと一致しません")
            if resource_digest(root) != binding["resource_digest"]:
                raise ValueError("準備済みの候補リソースが変更されています")
        cfg = self.config
        if cfg.get("shape") not in {"http", "cli", "library"}:
            raise ValueError("shape に指定できるのは http、cli、library です")
        original = source_files(root, cfg["include"])
        before = digest(original)
        if request.get("expected_source", before) != before:
            raise ValueError("要求されたソースのダイジェストが現在のものと一致しません")
        directory = self.base / secrets.token_hex(16)
        source, state = directory / "source", directory / "state"
        source.mkdir(parents=True)
        state.mkdir()
        item = {"directory": directory, "source": source, "processes": []}
        self.active[slot] = item
        try:
            for rel, path in original.items():
                target = source / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            if tree_digest(source) != before or digest(source_files(root, cfg["include"])) != before:
                raise ValueError("スナップショットの作成中にソースが変更されました")
            mutation = request.get("mutation")
            if mutation:
                matches = [d for d in cfg.get("mutations", []) if d.get("id") == mutation]
                if len(matches) != 1:
                    raise ValueError("指定された変異は未定義か、複数の定義に一致します")
                defect = matches[0]
                target = safe_path(source, defect["file"])
                body = target.read_bytes()
                find, replace = defect["find"].encode(), defect["replace"].encode()
                if not find or find == replace or body.count(find) != 1:
                    raise ValueError("変異の置換元は空でなく、一箇所だけに一致し、置換後の内容と異なる必要があります")
                target.write_bytes(body.replace(find, replace, 1))
            for _ in range(100):
                with socket.socket() as lease:
                    lease.bind(("127.0.0.1", 0))
                    port = lease.getsockname()[1]
                if port not in self.used_ports:
                    self.used_ports.add(port)
                    break
            else:
                raise ValueError("新しいターゲットを割り当てられません")
            variables = {"source": str(source), "state": str(state), "port": str(port),
                         "python": sys.executable}
            env = {k: v for k, v in os.environ.items() if k.upper() in
                   {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG"}}
            env.update({"HOME": str(state), "USERPROFILE": str(state), "TMP": str(state),
                        "TEMP": str(state), "TMPDIR": str(state), "PYTHONDONTWRITEBYTECODE": "1",
                        "FACTORY_RUNTIME_STATE": str(state), "FACTORY_RUNTIME_PORT": str(port)})
            for key, value in cfg.get("env", {}).items():
                if not isinstance(value, str) or key.startswith("FACTORY_RUNTIME_"):
                    raise ValueError("環境変数の設定が無効です")
                for var, replacement in variables.items():
                    value = value.replace("{" + var + "}", replacement)
                env[key] = value
            timeout = float(cfg.get("timeout_s", 30))
            if not 0 < timeout <= 300:
                raise ValueError("timeout_s は 0 より大きく 300 以下の値で指定してください")
            if cfg.get("setup"):
                setup = ProcessTree(command(cfg["setup"], variables), source, env)
                item["processes"].append(setup)
                self.wait_exit(setup, timeout)
                if setup.proc.returncode != 0:
                    raise ValueError("プロジェクトのセットアップに失敗しました（出力は非公開です）")
                setup.close()
                item["processes"].remove(setup)
            actual = tree_digest(source)
            argv = command(cfg["command"], variables)
            identity = "factory-v1:" + hashlib.sha256(json.dumps(
                {"source": actual, "argv": argv, "env": env}, sort_keys=True).encode()).hexdigest()
            env["FACTORY_RUNTIME_CANDIDATE"] = identity
            for path in source.rglob("*"):
                if path.is_file():
                    path.chmod(stat.S_IREAD)
            proc = ProcessTree(argv, source, env)
            item["processes"].append(proc)
            item.update(candidate=identity, digest=actual, proc=proc, shape=cfg["shape"],
                        target=f"http://127.0.0.1:{port}", original=before, binding=binding)
            if cfg["shape"] == "http":
                deadline = time.monotonic() + timeout
                while True:
                    if self.stopping.is_set() or proc.proc.poll() is not None:
                        raise ValueError("起動準備が整う前にアプリが停止しました")
                    try:
                        self.probe(item)
                        break
                    except (URLError, OSError, ValueError):
                        if time.monotonic() >= deadline:
                            raise ValueError("アプリの起動準備または候補との同一性を確認できませんでした") from None
                        self.stopping.wait(.05)
            else:
                self.wait_exit(proc, timeout)
                item["exit_code"] = proc.proc.returncode
                proc.close()  # 実行を終えたコマンドの子孫プロセスも片付ける
            return self.identity(slot)
        except BaseException:
            self.teardown(slot)
            raise

    def wait_exit(self, proc, timeout):
        deadline = time.monotonic() + timeout
        while proc.proc.poll() is None:
            if self.stopping.wait(.05) or time.monotonic() >= deadline:
                raise ValueError("プロジェクトのコマンドがキャンセルされたか、タイムアウトしました")

    def probe(self, item):
        if item["proc"].proc.poll() is not None:
            raise ValueError("管理下のアプリは停止しています")
        for key in ("health_path", "identity_path"):
            path = self.config.get(key)
            if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
                raise ValueError("HTTP には health_path と identity_path が必要です")
            with HTTP.open(item["target"] + path, timeout=.5) as response:
                if response.geturl() != item["target"] + path or response.status != 200:
                    raise ValueError("ターゲットがリダイレクトされたか、正常に応答しませんでした")
                body = response.read(65537).decode("utf-8")
            if key == "identity_path" and body.strip() != item["candidate"]:
                raise ValueError("ターゲットの識別値が候補と一致しません")
        if item["proc"].proc.poll() is not None:
            raise ValueError("確認中に管理下のアプリが停止しました")

    def identity(self, slot):
        item = self.active.get(slot)
        if not item or tree_digest(item["source"]) != item["digest"]:
            raise ValueError("候補のスナップショットが存在しないか、変更されています")
        if item["shape"] == "http":
            self.probe(item)
            if tree_digest(item["source"]) != item["digest"]:
                raise ValueError("確認中に候補が変更されました")
        result = {"version": 1, "slot": slot, "candidate": item["candidate"],
                "source_digest": item["digest"], "input_digest": item["original"],
                "shape": item["shape"], "target": item["target"] if item["shape"] == "http" else None,
                "exit_code": item.get("exit_code"), "snapshot": str(item["source"]),
                "state": str(item["directory"] / "state")}
        if item["binding"]:
            result.update(source_revision=item["binding"]["source_revision"],
                          source_tree=item["binding"]["source_tree"],
                          resource_digest=item["binding"]["resource_digest"])
        return result


def serve(config):
    stopping = threading.Event()
    manager = Environments(config, stopping)
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.connection.settimeout(2)
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise ValueError("リクエストのサイズが無効です")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("リクエストには JSON オブジェクトを指定してください")
                if self.path in {"/setup", "/start"}:
                    # setup で既存の環境を片付け、start で新しい環境を一括して準備する。
                    result = manager.teardown(data["slot"]) if self.path == "/setup" else manager.start(data)
                elif self.path == "/teardown":
                    result = manager.teardown(data["slot"])
                elif self.path == "/identity":
                    result = manager.identity(data["slot"])
                else:
                    raise ValueError("未定義の操作です")
                status = 200
            except Exception:
                # 失敗時の応答には、呼び出し元の入力、プロジェクトの出力、パス、秘密情報を含めない。
                for slot in list(manager.active):
                    manager.teardown(slot)
                status, result = 400, {"error": "ランタイムの要求を拒否しました。稼働中の環境は片付けました"}
            payload = json.dumps(result).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def parent_pipe():
        sys.stdin.buffer.read()
        stopping.set()
    threading.Thread(target=parent_pipe, daemon=True).start()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopping.set())
    server = None
    try:
        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.timeout = .2
        # 親プロセスとの非公開パイプで接続情報を渡す。この時点で親が終了しても後始末する。
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "token": token}), flush=True)
        while not stopping.is_set():
            server.handle_request()
    finally:
        if server:
            server.server_close()
        manager.close()


class RuntimeHost:
    def __init__(self, config):
        self.config = Path(config).resolve()

    def __enter__(self):
        self.proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                      "_serve", str(self.config)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
        line = self.proc.stdout.readline()
        if not line:
            self.proc.stdin.close()
            self.proc.wait(timeout=15)
            self.proc.stdout.close()
            raise ValueError("ランタイムホストが設定を受け付けませんでした")
        self.connection = json.loads(line)
        return self

    def environment(self):
        return {"FACTORY_RUNTIME_URL": self.connection["url"],
                "FACTORY_RUNTIME_TOKEN": self.connection["token"]}

    def __exit__(self, *exc):
        self.proc.stdin.close()
        rc = self.proc.wait(timeout=30)
        self.proc.stdout.close()
        if rc:
            raise RuntimeError("ランタイムの管理プロセスで後始末に失敗しました")


def request(action, data, connection=None):
    connection = connection or {"url": os.environ["FACTORY_RUNTIME_URL"],
                                "token": os.environ["FACTORY_RUNTIME_TOKEN"]}
    # 環境変数は信頼するが、認証情報をリモートホストへ送ってはならない。
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", connection["url"]):
        raise ValueError("ランタイムの制御にはループバックアドレスを使用してください")
    req = Request(connection["url"] + "/" + action, data=json.dumps(data).encode(),
                  headers={"Authorization": "Bearer " + connection["token"],
                           "Content-Type": "application/json"})
    with HTTP.open(req, timeout=310) as response:
        return json.load(response)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    serve_parser = sub.add_parser("_serve", help=argparse.SUPPRESS)
    serve_parser.add_argument("config")
    manual = sub.add_parser("serve", help="フォアグラウンドで実行環境を管理し、認証情報を非公開の接続ファイルに保存します")
    manual.add_argument("--config", required=True)
    manual.add_argument("--connection-file", required=True, type=Path)
    for action in ("setup", "start", "teardown", "identity", "describe"):
        node = sub.add_parser(action)
        node.add_argument("--slot", required=True)
        node.add_argument("--connection-file", type=Path)
        if action == "start":
            node.add_argument("--root", required=True)
            node.add_argument("--mutation")
            node.add_argument("--expected-source")
            node.add_argument("--expected-revision")
    args = parser.parse_args(argv)
    try:
        if args.action == "_serve":
            serve(json.loads(Path(args.config).read_text(encoding="utf-8")))
        elif args.action == "serve":
            with RuntimeHost(args.config) as host:
                # 排他的に新規作成し、ユーザーデータの上書きやリンクの追跡を防ぐ。
                fd = os.open(args.connection_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "w") as stream:
                        json.dump(host.connection, stream)
                    print("Runtime owner ready. このプロセスはフォアグラウンドで動かし続けてください。", flush=True)
                    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
                    while host.proc.poll() is None:
                        time.sleep(.2)
                finally:
                    args.connection_file.unlink(missing_ok=True)
        else:
            connection = json.loads(args.connection_file.read_text()) if args.connection_file else None
            data = {k: v for k, v in vars(args).items()
                    if k not in {"action", "connection_file"} and v is not None}
            result = request("identity" if args.action == "describe" else args.action, data, connection)
            print(result["candidate"] if args.action == "identity" else json.dumps(result))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("ランタイムホストの操作に失敗しました。信頼できるプロジェクト設定と、管理プロセスが稼働中かどうかを確認してください。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
