"""Contract tests for the systemd units, env example and Ansible role files in ``deploy/``.

The expectations come from ``specs/015-gh-issue-24/contracts``. The unit parser is deliberately
small: it supports exactly what the shipped units use and raises on anything else, so a unit
cannot silently use syntax that these tests would misread.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from invio.config.settings import unknown_env_keys

REPO = Path(__file__).resolve().parents[1]
SYSTEMD = REPO / "deploy" / "systemd"
ROLE = REPO / "deploy" / "ansible" / "roles" / "invio"
ENV_EXAMPLE = REPO / "deploy" / "env" / "invio.env.example"

Unit = dict[str, dict[str, list[str]]]
Expected = dict[str, str | set[str]]

_SECTION = re.compile(r"^\[([A-Za-z]+)\]$")


def parse_unit(path: Path) -> Unit:
    """Parse a systemd unit: section -> key -> list of values (one entry per assignment).

    Full-line ``#`` and ``;`` comments and blank lines are skipped, repeated keys accumulate and an
    empty assignment (``Key=``) is one empty value. Line continuations are not supported and
    raise ``ValueError``.
    """
    unit: Unit = {}
    section: str | None = None
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.endswith("\\"):
            raise ValueError(f"{path}:{number}: line continuations are not supported")
        match = _SECTION.match(line)
        if match:
            section = match.group(1)
            unit.setdefault(section, {})
            continue
        if section is None or "=" not in line:
            raise ValueError(f"{path}:{number}: cannot parse {line!r}")
        key, _, value = line.partition("=")
        unit[section].setdefault(key.strip(), []).append(value.strip())
    return unit


def assert_directives(unit: Unit, section: str, expected: Expected) -> None:
    """Assert that ``unit[section]`` has each expected key with exactly the expected value.

    A ``str`` is compared with the single value of the key. A ``set`` is compared with the
    whitespace-split values of all assignments, ignoring order (an empty string means an empty
    set).
    """
    assert section in unit, f"missing section [{section}]"
    actual_section = unit[section]
    for key, want in expected.items():
        assert key in actual_section, f"[{section}] {key} is missing"
        values = actual_section[key]
        if isinstance(want, set):
            got = {word for value in values for word in value.split()}
            assert got == want, f"[{section}] {key}: {sorted(got)} != {sorted(want)}"
        else:
            assert values == [want], f"[{section}] {key}: {values} != [{want!r}]"


# --- parser and helper behaviour ---------------------------------------------------------------


@pytest.fixture
def unit_file(tmp_path: Path) -> Path:
    path = tmp_path / "x.service"
    path.write_text(
        "# top comment\n"
        "[Unit]\n"
        "Description=Thing\n"
        "; semicolon comment\n"
        "\n"
        "[Service]\n"
        "Environment=A=1 B=2\n"
        "Environment=C=3\n"
        "CapabilityBoundingSet=\n"
        "ExecStart=/bin/true --flag=1\n",
        encoding="utf-8",
    )
    return path


def test_parse_unit_sections_and_values(unit_file: Path) -> None:
    unit = parse_unit(unit_file)
    assert list(unit) == ["Unit", "Service"]
    assert unit["Unit"] == {"Description": ["Thing"]}
    assert unit["Service"]["ExecStart"] == ["/bin/true --flag=1"]


def test_parse_unit_keeps_repeated_keys(unit_file: Path) -> None:
    assert parse_unit(unit_file)["Service"]["Environment"] == ["A=1 B=2", "C=3"]


def test_parse_unit_skips_comments(unit_file: Path) -> None:
    unit = parse_unit(unit_file)
    assert all(not key.startswith(("#", ";")) for keys in unit.values() for key in keys)


def test_parse_unit_empty_assignment_is_one_empty_value(unit_file: Path) -> None:
    assert parse_unit(unit_file)["Service"]["CapabilityBoundingSet"] == [""]


def test_parse_unit_rejects_line_continuations(tmp_path: Path) -> None:
    path = tmp_path / "x.service"
    path.write_text("[Service]\nExecStart=/bin/true \\\n  --flag\n", encoding="utf-8")
    with pytest.raises(ValueError, match="continuation"):
        parse_unit(path)


def test_parse_unit_rejects_assignment_outside_a_section(tmp_path: Path) -> None:
    path = tmp_path / "x.service"
    path.write_text("Key=value\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot parse"):
        parse_unit(path)


def test_assert_directives_compares_strings_exactly(unit_file: Path) -> None:
    unit = parse_unit(unit_file)
    assert_directives(unit, "Unit", {"Description": "Thing"})
    with pytest.raises(AssertionError):
        assert_directives(unit, "Unit", {"Description": "Other"})
    with pytest.raises(AssertionError, match="missing"):
        assert_directives(unit, "Unit", {"After": "x"})
    with pytest.raises(AssertionError, match="missing section"):
        assert_directives(unit, "Install", {"WantedBy": "x"})


def test_assert_directives_compares_sets_ignoring_order(unit_file: Path) -> None:
    unit = parse_unit(unit_file)
    assert_directives(unit, "Service", {"Environment": {"C=3", "B=2", "A=1"}})
    assert_directives(unit, "Service", {"CapabilityBoundingSet": set()})
    with pytest.raises(AssertionError):
        assert_directives(unit, "Service", {"Environment": {"A=1"}})


# --- env example (FR-012) ----------------------------------------------------------------------


def test_env_example_keys_are_known_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("INVIO_"):
            monkeypatch.delenv(key)
    assert ENV_EXAMPLE.is_file()
    assert unknown_env_keys(ENV_EXAMPLE) == []


def _env_example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        assert value.startswith('"') and value.endswith('"'), f'{key} must be KEY="value"'
        values[key] = value[1:-1]
    return values


def test_env_example_has_only_placeholders() -> None:
    allowed_markers = ("change-me", "example.", "/var/lib/invio")
    allowed_exact = {"587", "starttls", "INFO"}
    for key, value in _env_example_values().items():
        assert (
            value == ""
            or value in allowed_exact
            or any(marker in value for marker in allowed_markers)
        ), f"{key} does not look like a placeholder"


def test_env_example_lists_every_documented_key_and_not_env_file() -> None:
    keys = set(_env_example_values())
    assert "INVIO_ENV_FILE" not in keys
    assert keys == {
        "INVIO_DATABASE_URL",
        "INVIO_SMTP_HOST",
        "INVIO_SMTP_PORT",
        "INVIO_SMTP_SECURITY",
        "INVIO_SMTP_FROM",
        "INVIO_SMTP_USER",
        "INVIO_SMTP_PASSWORD",
        "INVIO_MISTRAL_API_KEY",
        "INVIO_OPENAI_API_KEY",
        "INVIO_ANTHROPIC_API_KEY",
        "INVIO_GOOGLE_API_KEY",
        "INVIO_LOG_LEVEL",
        "INVIO_HTTP_CONTACT",
        "INVIO_ARCHIVE_DIR",
        "INVIO_HEALTHCHECK_URL",
    }
