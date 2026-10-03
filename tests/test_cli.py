"""Tests for the root CLI: ``--version`` and sub-command auto-discovery."""

import importlib
import sys
import textwrap
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest
import typer
from typer.testing import CliRunner

from scout import __version__
from scout.cli.main import app, create_app, discover_commands

runner = CliRunner()

HELLO_MODULE = """
import typer

app = typer.Typer()


@app.command()
def greet(name: str = "world") -> None:
    typer.echo(f"hello {name}")
"""


@pytest.fixture
def make_commands_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[dict[str, str]], ModuleType]]:
    """Create throw-away commands packages on ``sys.path`` containing the given modules."""
    created: list[str] = []

    def factory(modules: dict[str, str]) -> ModuleType:
        package_name = f"fake_commands_{uuid.uuid4().hex}"
        package_dir = tmp_path / package_name
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text("")
        for name, source in modules.items():
            (package_dir / f"{name}.py").write_text(textwrap.dedent(source))
        monkeypatch.syspath_prepend(str(tmp_path))
        created.append(package_name)
        return importlib.import_module(package_name)

    yield factory

    for name in list(sys.modules):
        if any(name == pkg or name.startswith(f"{pkg}.") for pkg in created):
            del sys.modules[name]


def test_version_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"scout {__version__}"


def test_version_is_not_placeholder() -> None:
    assert __version__ != "0.0.0+unknown"


def test_discovery_registers_dropped_in_module(
    make_commands_package: Callable[[dict[str, str]], ModuleType],
) -> None:
    package = make_commands_package({"hello_world": HELLO_MODULE})
    cli = create_app(package)

    result = runner.invoke(cli, ["hello-world", "greet", "--name", "scout"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "hello scout"


def test_discovery_skips_private_modules_and_modules_without_app(
    make_commands_package: Callable[[dict[str, str]], ModuleType],
) -> None:
    package = make_commands_package(
        {
            "hello": HELLO_MODULE,
            "_private": HELLO_MODULE,
            "helpers": "def helper() -> int:\n    return 1\n",
            "not_typer": "app = object()\n",
        }
    )

    registered = discover_commands(typer.Typer(), package)

    assert registered == ["hello"]
    result = runner.invoke(create_app(package), ["_private"])
    assert result.exit_code != 0
