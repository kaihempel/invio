"""Entry point of the ``invio`` CLI with automatic sub-command discovery."""

import importlib
import pkgutil
from types import ModuleType
from typing import Annotated

import typer

from invio import __version__
from invio.cli import commands
from invio.cli.runtime import setup_runtime

# Groups that run ``setup_runtime`` in their own callback, where the sub-command is known:
# ``invio job run`` exits 1 on a configuration error, because its exit code 2 means
# ``partial`` (specs/015-gh-issue-22, FR-014). Every other command exits 2.
SELF_CONFIGURING_GROUPS = frozenset({"job"})


def discover_commands(app: typer.Typer, package: ModuleType) -> list[str]:
    """Register every sub-module of ``package`` that exposes a Typer ``app``.

    Modules whose name starts with ``_`` and modules without a ``typer.Typer`` named ``app``
    are skipped. The command name is the module name with ``_`` replaced by ``-``.

    Returns the names of the registered commands.
    """
    registered: list[str] = []
    for module_info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        sub_app = getattr(module, "app", None)
        if not isinstance(sub_app, typer.Typer):
            continue
        command_name = module_info.name.replace("_", "-")
        app.add_typer(sub_app, name=command_name)
        registered.append(command_name)
    return registered


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"invio {__version__}")
        raise typer.Exit()


def create_app(package: ModuleType = commands) -> typer.Typer:
    """Build the root Typer app and register all sub-commands found in ``package``."""
    root = typer.Typer(
        name="invio",
        help="Invio: AI research system.",
        no_args_is_help=True,
        pretty_exceptions_show_locals=False,  # locals may hold the database URL
    )

    @root.callback()
    def main(
        ctx: typer.Context,
        version: Annotated[
            bool,
            typer.Option(
                "--version",
                "-V",
                help="Show the version and exit.",
                callback=_version_callback,
                is_eager=True,
            ),
        ] = False,
    ) -> None:
        """Invio: AI research system."""
        if ctx.invoked_subcommand not in SELF_CONFIGURING_GROUPS:
            setup_runtime()

    discover_commands(root, package)
    return root


app = create_app()

if __name__ == "__main__":  # pragma: no cover
    app()
