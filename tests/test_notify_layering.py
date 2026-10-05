"""Architecture rule: the notifier does not depend on the layers that call it."""

import ast
from pathlib import Path

import invio.notify

BANNED = ("invio.cli", "invio.graph", "invio.scheduling", "invio.services")


def _imported_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def test_notify_package_does_not_import_upper_layers() -> None:
    paths = list(Path(invio.notify.__path__[0]).rglob("*.py"))

    assert paths  # the guard must actually see the modules
    for path in paths:
        for name in _imported_names(ast.parse(path.read_text(encoding="utf-8"))):
            assert not name.startswith(BANNED), f"{path.name} imports {name}"
