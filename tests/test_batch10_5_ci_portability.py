from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_batch10_3_review_subprocess_uses_active_interpreter() -> None:
    source = (PROJECT_ROOT / "tests" / "test_ui_review_batch10_3.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    subprocess_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "run"
    ]
    assert subprocess_calls
    command = subprocess_calls[0].args[0]
    assert isinstance(command, ast.List)
    first = command.elts[0]
    assert (
        isinstance(first, ast.Attribute)
        and isinstance(first.value, ast.Name)
        and first.value.id == "sys"
        and first.attr == "executable"
    )


def test_python_tests_do_not_assume_repository_local_venv() -> None:
    offenders: list[str] = []
    needles = (
        ".venv/scripts/python.exe",
        ".venv\\scripts\\python.exe",
        ".venv/bin/python",
        "appdata/local/programs/python",
        "appdata\\local\\programs\\python",
    )
    for path in sorted((PROJECT_ROOT / "tests").glob("*.py")):
        if path == Path(__file__):
            continue
        source = path.read_text(encoding="utf-8").casefold()
        if any(needle in source for needle in needles):
            offenders.append(path.name)
    assert offenders == []
