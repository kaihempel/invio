"""Layering and prompt-seam guards for the ``invio.cli`` package."""

import ast
from pathlib import Path

import pytest

import invio
from invio.cli import prompts
from invio.cli.prompts import WizardAborted, adapt_validator

CLI_DIR = Path(invio.__file__).parent / "cli"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def test_cli_does_not_import_the_database_layer() -> None:
    offenders = {
        str(path.relative_to(CLI_DIR)): sorted(
            m for m in _imported_modules(path) if m == "invio.db" or m.startswith("invio.db.")
        )
        for path in CLI_DIR.rglob("*.py")
        if path.name != "db.py"  # ``invio db upgrade`` is the migration command and needs it
    }

    assert {k: v for k, v in offenders.items() if v} == {}


@pytest.mark.parametrize("name", ["prompts.py", "source_check.py"])
def test_leaf_helpers_do_not_import_typer_or_services(name: str) -> None:
    modules = _imported_modules(CLI_DIR / name)

    assert not [m for m in modules if m.split(".")[0] == "typer" or m.startswith("invio.services")]


# --- prompt seam -----------------------------------------------------------------------------


def test_adapt_validator_maps_false_to_a_message() -> None:
    assert adapt_validator(None) is None
    adapted = adapt_validator(lambda value: value == "ok")
    assert adapted is not None
    assert adapted("ok") is True
    assert adapted("no") == "invalid value"
    keeps_message = adapt_validator(lambda value: "custom")
    assert keeps_message is not None and keeps_message("x") == "custom"


def test_questionary_none_answer_aborts() -> None:
    with pytest.raises(WizardAborted):
        prompts._answer(None)
    assert prompts._answer("x") == "x"
    assert prompts._answer(False) is False


def test_source_discovery_stays_below_the_upper_layers() -> None:
    banned = ("invio.cli", "invio.services", "invio.pipeline", "invio.db")
    path = Path(invio.__file__).parent / "sources" / "discover.py"

    offenders = [m for m in _imported_modules(path) if m.startswith(banned)]

    assert offenders == []
