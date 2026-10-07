"""Tests for the root CLI: ``--version`` and sub-command auto-discovery."""

import importlib
import json
import logging
import sys
import textwrap
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest
import typer
from typer.testing import CliRunner

from invio import __version__
from invio.cli.main import app, create_app, discover_commands
from invio.log import JsonFormatter

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
    assert result.stdout.strip() == f"invio {__version__}"


def test_version_is_not_placeholder() -> None:
    assert __version__ != "0.0.0+unknown"


def test_discovery_registers_dropped_in_module(
    make_commands_package: Callable[[dict[str, str]], ModuleType],
) -> None:
    package = make_commands_package({"hello_world": HELLO_MODULE})
    cli = create_app(package)

    result = runner.invoke(cli, ["hello-world", "greet", "--name", "invio"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "hello invio"


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


def test_subcommand_configures_json_logging_and_warns_about_unknown_keys(
    make_commands_package: Callable[[dict[str, str]], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logger: logging.Logger,
) -> None:
    monkeypatch.setenv("INVIO_OPENAI_APIKEY", "typo")
    cli = create_app(make_commands_package({"hello": HELLO_MODULE}))

    result = runner.invoke(cli, ["hello", "greet"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "hello world"
    assert any(isinstance(h.formatter, JsonFormatter) for h in restore_root_logger.handlers)
    (warning,) = [json.loads(line) for line in result.stderr.splitlines()]
    assert warning["message"] == "unknown setting ignored"
    assert warning["key"] == "INVIO_OPENAI_APIKEY"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"INVIO_LOG_LEVEL": "LOUD"}, "invalid log level"),
        ({"INVIO_ENV_FILE": "/does/not/exist.env"}, "INVIO_ENV_FILE points to a missing file"),
    ],
)
def test_configuration_errors_exit_with_code_2_and_a_clear_message(
    make_commands_package: Callable[[dict[str, str]], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    expected: str,
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    cli = create_app(make_commands_package({"hello": HELLO_MODULE}))

    result = runner.invoke(cli, ["hello", "greet"])

    assert result.exit_code == 2
    assert "Configuration error:" in result.stderr
    assert expected in result.stderr
    assert "Traceback" not in result.stderr


def test_every_self_configuring_group_runs_the_runtime_setup_itself() -> None:
    """The root callback skips ``setup_runtime`` for these groups: their callback must run it."""
    import inspect

    import typer

    from invio.cli.main import SELF_CONFIGURING_GROUPS, app

    groups = {
        info.name: info.typer_instance
        for info in app.registered_groups
        if info.name in SELF_CONFIGURING_GROUPS
    }
    assert set(groups) == set(SELF_CONFIGURING_GROUPS)
    for name, group in groups.items():
        assert isinstance(group, typer.Typer)
        callback = group.registered_callback
        assert callback is not None and callback.callback is not None, name
        assert "setup_runtime(" in inspect.getsource(callback.callback), name
