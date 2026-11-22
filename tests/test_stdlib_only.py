"""実行時のコードは標準ライブラリと console だけを import する (実行時の依存パッケージは 0)。"""

import ast
import sys

from conftest import ROOT


def runtime_sources():
    return [ROOT / "app.py", *sorted((ROOT / "console").rglob("*.py"))]


def imported_top_names(source: str) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_runtime_code_imports_only_stdlib_and_console():
    allowed = sys.stdlib_module_names | {"console"}
    found = {
        path.relative_to(ROOT).as_posix(): imported_top_names(path.read_text(encoding="utf-8"))
        for path in runtime_sources()
    }
    assert "console/server.py" in found
    outside = {path: names - allowed for path, names in found.items() if names - allowed}
    assert outside == {}
