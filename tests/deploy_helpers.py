"""Shared paths and helpers for the ``tests/test_deploy_*.py`` modules."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
SYSTEMD = DEPLOY / "systemd"
SCRIPTS = DEPLOY / "scripts"
ANSIBLE = DEPLOY / "ansible"
ROLE = ANSIBLE / "roles" / "invio"
MOLECULE = ANSIBLE / "molecule"
ENV_EXAMPLE = DEPLOY / "env" / "invio.env.example"

Unit = dict[str, dict[str, list[str]]]

_SECTION = re.compile(r"^\[([A-Za-z]+)\]$")


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


ROLE_VARS: dict[str, Any] = load_yaml(ROLE / "vars" / "main.yml")
UNITS: list[str] = ROLE_VARS["invio_units"]
TIMERS = [name for name in UNITS if name.endswith(".timer")]
SERVICES = [name for name in UNITS if name.endswith(".service")]
# Keys the env template writes, in template order (INVIO_ENV_FILE is never written).
ENV_KEYS = [key for key in ROLE_VARS["invio_managed_env_keys"] if key != "INVIO_ENV_FILE"]


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


def is_unwanted_deploy_skip(report: pytest.TestReport, path: Path, required: bool) -> bool:
    """Whether a skipped deploy test must count as a failure (``INVIO_REQUIRE_DEPLOY_TOOLS=1``).

    An expected failure (``xfail``) is reported as skipped too and stays untouched.
    """
    return (
        required
        and report.skipped
        and not hasattr(report, "wasxfail")
        and path.name.startswith("test_deploy_")
    )
