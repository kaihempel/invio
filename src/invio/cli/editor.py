"""Open text in the operator's editor (``$VISUAL`` / ``$EDITOR``) using only the stdlib.

Typer vendors click without ``click.edit`` and ``click`` is not a direct dependency, so this
small launcher replaces it (blueprint 4.1). The temp file is private (mode 0600 from
``mkstemp``), closed before the editor starts, and always removed.
"""

import os
import shlex
import subprocess
import tempfile
from collections.abc import Mapping

__all__ = ["EditorError", "default_editor", "edit_text"]


class EditorError(Exception):
    """The editor could not be started or did not exit successfully."""


def default_editor(*, env: Mapping[str, str] = os.environ, name: str = os.name) -> str:
    """Return the editor command: ``$VISUAL``, ``$EDITOR``, then ``notepad`` or ``vi``."""
    return env.get("VISUAL") or env.get("EDITOR") or ("notepad" if name == "nt" else "vi")


def edit_text(text: str, *, env: Mapping[str, str] = os.environ) -> str | None:
    """Let the operator edit ``text``; return the new text, or ``None`` if it is unchanged.

    Raises :class:`EditorError` when the editor cannot be started or exits non-zero.
    """
    try:
        command = shlex.split(default_editor(env=env), posix=os.name != "nt")
    except ValueError as exc:
        raise EditorError(f"cannot parse editor command: {exc}") from exc
    if not command:
        raise EditorError("cannot start editor: empty editor command")
    fd, path = tempfile.mkstemp(suffix=".yaml", text=False)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        try:
            completed = subprocess.run([*command, path], check=False)
        except OSError as exc:
            raise EditorError(f"cannot start editor {command[0]!r}: {exc}") from exc
        if completed.returncode != 0:
            raise EditorError(f"editor {command[0]!r} exited with status {completed.returncode}")
        with open(path, encoding="utf-8-sig", newline=None) as handle:
            edited = handle.read()
    except UnicodeDecodeError as exc:
        raise EditorError(f"edited file is not valid UTF-8: {exc}") from exc
    finally:
        os.unlink(path)
    return None if edited == text else edited
