"""The Rich consoles of the CLI: plain text only (no markup, highlighting or emoji)."""

from typing import Final

from rich.console import Console

__all__ = ["PIPE_WIDTH", "stderr_console", "stdout_console"]

PIPE_WIDTH: Final = 1_000


def stdout_console() -> Console:
    """A console on stdout; piped output gets a wide fixed width (not 80) so rows never wrap."""
    console = Console(markup=False, highlight=False, emoji=False)
    if not console.is_terminal:
        # Piped output defaults to 80 columns; never wrap or truncate a row for grep/awk.
        console = Console(markup=False, highlight=False, emoji=False, width=PIPE_WIDTH)
    return console


def stderr_console() -> Console:
    """A console on stderr, for progress and notes."""
    return Console(stderr=True, markup=False, highlight=False, emoji=False)
