"""Shared error output for CLI commands."""

import typer

__all__ = ["fail"]


def fail(message: str, code: int) -> typer.Exit:
    """Print ``message`` to stderr and return the ``Exit`` to raise (``raise fail(...)``)."""
    typer.echo(message, err=True)
    return typer.Exit(code=code)
