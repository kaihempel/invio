"""Architecture rule: the LLM layer does not depend on the layers built on top of it."""

import ast
from pathlib import Path

import invio.llm


def _imported_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def test_llm_package_does_not_import_upper_layers() -> None:
    banned = ("invio.db", "invio.services", "invio.cli")
    for path in Path(invio.llm.__path__[0]).glob("*.py"):
        for name in _imported_names(ast.parse(path.read_text(encoding="utf-8"))):
            assert not name.startswith(banned), f"{path.name} imports {name}"
