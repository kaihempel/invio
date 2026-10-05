"""Architecture rules: the graph package sits above the LLM layer and below the entry points."""

import ast
from pathlib import Path

import invio.graph
import invio.llm


def _imported_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def test_graph_package_does_not_import_cli_or_scheduling() -> None:
    banned = ("invio.cli", "invio.scheduling")
    root = Path(invio.graph.__path__[0])
    paths = list(root.rglob("*.py"))
    assert paths  # nodes/ is a subpackage, so a flat glob would miss the nodes
    for path in paths:
        for name in _imported_names(ast.parse(path.read_text(encoding="utf-8"))):
            assert not name.startswith(banned), f"{path.relative_to(root)} imports {name}"


def test_llm_package_does_not_import_graph() -> None:
    for path in Path(invio.llm.__path__[0]).rglob("*.py"):
        for name in _imported_names(ast.parse(path.read_text(encoding="utf-8"))):
            assert not name.startswith("invio.graph"), f"{path.name} imports {name}"
