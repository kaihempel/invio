"""Architecture rules: the graph package sits above the LLM layer and below the entry points."""

import ast
from pathlib import Path

import invio
import invio.graph
import invio.llm

SRC = Path(invio.__path__[0]).parent


def _imported_names(source: str, package: str) -> list[str]:
    """Absolute names of all modules ``source`` (in ``package``) imports, relative ones resolved.

    ``from x import y`` yields ``x`` and ``x.y``, since ``y`` may be a submodule.
    """
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package.rsplit(".", node.level - 1)[0] if node.level else ""
            module = ".".join(part for part in (base, node.module) if part)
            names.append(module)
            names.extend(f"{module}.{alias.name}" for alias in node.names)
    return names


def _imports_of(path: Path) -> list[str]:
    package = ".".join(path.relative_to(SRC).parts[:-1])  # also right for __init__.py
    return _imported_names(path.read_text(encoding="utf-8"), package)


def _is_within(name: str, packages: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(f"{p}.") for p in packages)


def test_graph_package_does_not_import_cli_or_scheduling() -> None:
    banned = ("invio.cli", "invio.scheduling")
    root = Path(invio.graph.__path__[0])
    paths = list(root.rglob("*.py"))
    assert paths  # nodes/ is a subpackage, so a flat glob would miss the nodes
    for path in paths:
        for name in _imports_of(path):
            assert not _is_within(name, banned), f"{path.relative_to(root)} imports {name}"


def test_graph_package_does_not_import_notify() -> None:
    # The digest node mirrors notify's run-status rule instead of importing it.
    root = Path(invio.graph.__path__[0])
    paths = list(root.rglob("*.py"))
    assert any(path.name == "synthesize.py" for path in paths)
    for path in paths:
        for name in _imports_of(path):
            assert not _is_within(name, ("invio.notify",)), (
                f"{path.relative_to(root)} imports {name}"
            )


def test_llm_package_does_not_import_graph() -> None:
    for path in Path(invio.llm.__path__[0]).rglob("*.py"):
        for name in _imports_of(path):
            assert not _is_within(name, ("invio.graph",)), f"{path.name} imports {name}"


def test_imported_names_resolves_relative_and_submodule_imports() -> None:
    source = "import a.b\nfrom .. import cli\nfrom .x import y\nfrom invio import scheduling\n"
    names = _imported_names(source, "invio.graph.nodes")
    assert {"a.b", "invio.graph.cli", "invio.graph.nodes.x.y", "invio.scheduling"} <= set(names)
    assert _imported_names("from ... import cli", "invio.graph.nodes") == ["invio", "invio.cli"]


def test_is_within_matches_whole_package_names_only() -> None:
    assert _is_within("invio.cli", ("invio.cli",))
    assert _is_within("invio.cli.main", ("invio.cli",))
    assert not _is_within("invio.client", ("invio.cli",))
