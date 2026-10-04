"""Tests for the stdlib editor launcher (a real child process, never a real editor)."""

import os
import sys
import textwrap
from pathlib import Path

import pytest

from invio.cli import editor
from invio.cli.editor import EditorError, edit_text


def _script(tmp_path: Path, body: str) -> str:
    """Write a Python 'editor' script and return an EDITOR command line running it."""
    script = tmp_path / "fake_editor.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def test_returns_edited_text(tmp_path: Path) -> None:
    cmd = _script(
        tmp_path,
        """
        import sys
        open(sys.argv[1], "w", encoding="utf-8").write("changed: ü\\n")
        """,
    )

    assert edit_text("a: 1\n", env={"EDITOR": cmd}) == "changed: ü\n"


def test_passes_original_text_and_yaml_suffix(tmp_path: Path) -> None:
    seen = tmp_path / "seen.txt"
    cmd = _script(
        tmp_path,
        f"""
        import sys
        text = open(sys.argv[1], encoding="utf-8").read()
        open({str(seen)!r}, "w", encoding="utf-8").write(sys.argv[1] + "|" + text)
        """,
    )

    assert edit_text("a: 1\n", env={"EDITOR": cmd}) is None

    path, _, content = seen.read_text(encoding="utf-8").partition("|")
    assert path.endswith(".yaml")
    assert content == "a: 1\n"
    assert not os.path.exists(path)


def test_unchanged_content_is_none(tmp_path: Path) -> None:
    cmd = _script(tmp_path, "pass\n")

    assert edit_text("a: 1\n", env={"EDITOR": cmd}) is None


def test_visual_wins_over_editor(tmp_path: Path) -> None:
    good = _script(
        tmp_path,
        """
        import sys
        open(sys.argv[1], "w").write("visual\\n")
        """,
    )

    assert edit_text("x\n", env={"VISUAL": good, "EDITOR": "does-not-exist-xyz"}) == "visual\n"


def test_nonzero_exit_is_error_and_cleans_up(tmp_path: Path) -> None:
    seen = tmp_path / "path.txt"
    cmd = _script(
        tmp_path,
        f"""
        import sys
        open({str(seen)!r}, "w").write(sys.argv[1])
        sys.exit(3)
        """,
    )

    with pytest.raises(EditorError, match="exited with status 3"):
        edit_text("a: 1\n", env={"EDITOR": cmd})

    assert not os.path.exists(seen.read_text())


def test_missing_editor_is_error() -> None:
    with pytest.raises(EditorError, match="cannot start editor"):
        edit_text("a: 1\n", env={"EDITOR": "definitely-not-an-editor-xyz"})


def test_empty_editor_command_is_error() -> None:
    with pytest.raises(EditorError, match="cannot start editor"):
        edit_text("a: 1\n", env={"EDITOR": "   "})


def test_unparsable_editor_command_is_error() -> None:
    with pytest.raises(EditorError, match="cannot parse"):
        edit_text("a: 1\n", env={"EDITOR": '"unterminated'})


def test_fallback_editor_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    assert editor.default_editor(env={}, name="nt") == "notepad"
    assert editor.default_editor(env={}, name="posix") == "vi"
    assert editor.default_editor(env={"EDITOR": "nano"}, name="posix") == "nano"
    assert editor.default_editor(env={"VISUAL": "code -w", "EDITOR": "nano"}, name="posix") == (
        "code -w"
    )
    assert editor.default_editor(env={"VISUAL": "", "EDITOR": "nano"}, name="posix") == "nano"


def test_temp_file_is_private_and_removed_on_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[str] = []

    def interrupted(cmd: list[str], **kwargs: object) -> None:
        created.append(cmd[-1])
        if os.name == "posix":
            assert (os.stat(cmd[-1]).st_mode & 0o777) == 0o600
        raise KeyboardInterrupt

    monkeypatch.setattr(editor.subprocess, "run", interrupted)

    with pytest.raises(KeyboardInterrupt):
        edit_text("a: 1\n", env={"EDITOR": "x"})

    assert not os.path.exists(created[0])


def test_non_utf8_result_is_error(tmp_path: Path) -> None:
    cmd = _script(
        tmp_path,
        """
        import sys
        open(sys.argv[1], "wb").write(b"\\xff\\xfe\\x00bad")
        """,
    )

    with pytest.raises(EditorError, match="not valid UTF-8"):
        edit_text("a: 1\n", env={"EDITOR": cmd})
