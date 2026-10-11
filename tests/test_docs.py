"""Consistency checks between the documentation, the job schema and the CLI (issue #38)."""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import typer

from invio.cli.main import app
from tests.job_helpers import SCHEMA

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"
README = REPO_ROOT / "README.md"
CONFIGURATION = DOCS / "configuration.md"
OPERATIONS = DOCS / "operations.md"

REQUIRED_DOCS = [
    README,
    CONFIGURATION,
    OPERATIONS,
    DOCS / "troubleshooting.md",
    DOCS / "backup.md",
    DOCS / "security.md",
]

_FENCED_CODE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
_INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'(][^)]*)?\)")
_REFERENCE_LINK = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$", re.MULTILINE)
_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _markdown_files() -> list[Path]:
    return [README, *sorted(DOCS.glob("*.md"))]


def _all_docs_text() -> str:
    return "\n".join(_read(path) for path in _markdown_files() if path.is_file())


def _schema_properties(node: Any) -> Iterator[str]:
    """Yield every property name in the schema: root, ``$defs`` and any nested object."""
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            yield from properties
        for value in node.values():
            yield from _schema_properties(value)
    elif isinstance(node, list):
        for value in node:
            yield from _schema_properties(value)


def _cli_commands(command: Any, path: list[str]) -> Iterator[str]:
    """Yield ``invio <group> <sub>`` for every leaf command of the click tree."""
    subcommands = getattr(command, "commands", None)
    if subcommands:
        for name, sub in sorted(subcommands.items()):
            if not getattr(sub, "hidden", False):
                yield from _cli_commands(sub, [*path, name])
    else:
        yield " ".join(path)


SCHEMA_PROPERTIES = sorted(set(_schema_properties(json.loads(_read(SCHEMA)))))
CLI_COMMANDS = list(_cli_commands(typer.main.get_command(app), ["invio"]))


def _relative_links(path: Path) -> list[tuple[Path, str]]:
    text = _FENCED_CODE.sub("", _read(path))
    targets = _INLINE_LINK.findall(text) + _REFERENCE_LINK.findall(text)
    links = []
    for target in targets:
        if target.startswith("#") or _EXTERNAL.match(target):
            continue
        links.append((path, target))
    return links


def _all_relative_links() -> list[tuple[Path, str]]:
    return [link for path in _markdown_files() if path.is_file() for link in _relative_links(path)]


def test_schema_properties_are_collected() -> None:
    # Guards the walker: a broken traversal would make the parametrized test vacuous.
    for name in ("schema_version", "schedule", "timezone", "max_items_per_run", "playlist_id"):
        assert name in SCHEMA_PROPERTIES


def test_cli_commands_are_collected() -> None:
    assert CLI_COMMANDS == [
        "invio db upgrade",
        "invio job create",
        "invio job delete",
        "invio job disable",
        "invio job edit",
        "invio job enable",
        "invio job export",
        "invio job import",
        "invio job list",
        "invio job run",
        "invio job show",
        "invio job suggest",
        "invio llm test",
        "invio notify retry",
        "invio run list",
        "invio run show",
        "invio run-due",
        "invio source discover",
        "invio usage",
    ]


@pytest.mark.parametrize("path", REQUIRED_DOCS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_required_doc_exists(path: Path) -> None:
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"


def test_readme_has_mermaid_diagram() -> None:
    assert "```mermaid" in _read(README)


@pytest.mark.parametrize("prop", SCHEMA_PROPERTIES)
def test_schema_property_documented(prop: str) -> None:
    text = _read(CONFIGURATION)
    # `prop` or a dotted path ending in it, e.g. `limits.max_items_per_run`.
    pattern = re.compile(rf"`(?:[\w\[\]]+\.)*{re.escape(prop)}`")
    assert pattern.search(text), f"`{prop}` is not documented in docs/configuration.md"


@pytest.mark.parametrize("command", CLI_COMMANDS)
def test_cli_command_documented(command: str) -> None:
    pattern = re.compile(rf"{re.escape(command)}(?![\w-])")
    assert pattern.search(_all_docs_text()), f"{command!r} does not appear in README/docs"


@pytest.mark.parametrize(
    ("source", "target"),
    _all_relative_links(),
    ids=lambda v: str(v.relative_to(REPO_ROOT)) if isinstance(v, Path) else v,
)
def test_relative_link_resolves(source: Path, target: str) -> None:
    file_part = target.split("#", 1)[0].split("?", 1)[0]
    resolved = (source.parent / file_part).resolve()
    assert resolved.exists(), f"{source.relative_to(REPO_ROOT)} links to missing {target}"


@pytest.mark.parametrize(
    ("topic", "keyword_groups"),
    [
        ("lock handling", (("lock",),)),
        ("notification retry", (("invio notify retry",),)),
        ("provider outage", (("provider",), ("outage", "fallback"))),
    ],
)
def test_operations_runbook_covers(topic: str, keyword_groups: tuple[tuple[str, ...], ...]) -> None:
    # Every group must match; within a group any one keyword suffices (case-insensitive).
    text = _read(OPERATIONS).lower()
    missing = [group for group in keyword_groups if not any(word in text for word in group)]
    assert not missing, f"docs/operations.md does not cover {topic}: missing {missing}"
