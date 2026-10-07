"""Contract tests for the systemd units, env example and Ansible role files in ``deploy/``.

The expectations come from ``specs/015-gh-issue-24/contracts``. The unit parser
(``tests/deploy_helpers.py``) is deliberately small: it supports exactly what the shipped units
use and raises on anything else, so a unit cannot silently use syntax that these tests would
misread.
"""

from __future__ import annotations

import os
from pathlib import Path

import jinja2
import pytest
from sqlalchemy.engine import make_url

from invio.config.settings import unknown_env_keys
from tests.deploy_helpers import (
    ENV_EXAMPLE,
    ENV_KEYS,
    ROLE,
    SERVICES,
    SYSTEMD,
    Unit,
    parse_unit,
)

Expected = dict[str, str | set[str]]


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
    assert keys == set(ENV_KEYS)


# --- unit contracts (contracts/systemd-units.md) -----------------------------------------------

SERVICE_UNIT: Expected = {
    "Documentation": "file:///opt/invio/docs/deployment.md",
    "Wants": "network-online.target",
    "After": {"network-online.target", "mariadb.service"},
}
SERVICE_BASE: Expected = {
    "Type": "oneshot",
    "User": "invio",
    "Group": "invio",
    "EnvironmentFile": "/etc/invio/invio.env",
    "WorkingDirectory": "/var/lib/invio",
    "StateDirectory": "invio",
    "StateDirectoryMode": "0750",
    "ReadWritePaths": "/var/lib/invio",
    "UMask": "0027",
}
JOURNAL_ENV = frozenset(
    {
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONUNBUFFERED=1",
        "HOME=/var/lib/invio",
        "XDG_CACHE_HOME=/var/lib/invio/cache",
    }
)
HARDENING: Expected = {
    # required by the issue (FR-006)
    "NoNewPrivileges": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "PrivateTmp": "yes",
    # additional low-risk set (research R8)
    "PrivateDevices": "yes",
    "ProtectKernelTunables": "yes",
    "ProtectKernelModules": "yes",
    "ProtectKernelLogs": "yes",
    "ProtectControlGroups": "yes",
    "ProtectClock": "yes",
    "ProtectHostname": "yes",
    "RestrictSUIDSGID": "yes",
    "RestrictRealtime": "yes",
    "RestrictNamespaces": "yes",
    "LockPersonality": "yes",
    "SystemCallArchitectures": "native",
    "RestrictAddressFamilies": {"AF_UNIX", "AF_INET", "AF_INET6"},
    "CapabilityBoundingSet": set(),
}
# Everything else is rejected, among others MemoryDenyWriteExecute, PrivateNetwork and
# DynamicUser, and directives that could run another command (ExecStartPre, ExecStop, ...).
ALLOWED_SERVICE_KEYS = frozenset(
    {"Environment", "ExecStart", "SyslogIdentifier", "MemoryMax", "TimeoutStartSec"}
    | set(SERVICE_BASE)
    | set(HARDENING)
)
ALLOWED_UNIT_KEYS = frozenset({"Description", *SERVICE_UNIT})
PER_SERVICE_KEYS = ("ExecStart", "SyslogIdentifier", "MemoryMax", "TimeoutStartSec")
RUN_DUE = {
    "ExecStart": "/opt/invio/.venv/bin/invio run-due",
    "SyslogIdentifier": "invio-run-due",
    "MemoryMax": "1G",
    "TimeoutStartSec": "3h",
}


def load(name: str) -> Unit:
    path = SYSTEMD / name
    assert path.is_file(), f"{path} does not exist"
    return parse_unit(path)


def _assert_service_contract(name: str, per_service: dict[str, str]) -> None:
    unit = load(name)
    assert set(unit) == {"Unit", "Service"}, "services have no [Install] section (FR-004)"
    assert unit["Unit"]["Description"][0] != ""
    assert_directives(unit, "Unit", SERVICE_UNIT)
    expected: Expected = {**SERVICE_BASE, **HARDENING, "Environment": set(JOURNAL_ENV)}
    assert_directives(unit, "Service", {**expected, **per_service})
    service = unit["Service"]
    # stdout and stderr go to the journal by default (research R9): no override
    assert "StandardOutput" not in service
    assert "StandardError" not in service
    assert "Restart" not in service  # FR-005
    assert "SuccessExitStatus" not in service  # exit codes pass through (FR-005)
    assert not service["EnvironmentFile"][0].startswith("-")


NOTIFY_RETRY = {
    "ExecStart": "/opt/invio/.venv/bin/invio notify retry",
    "SyslogIdentifier": "invio-notify-retry",
    "MemoryMax": "256M",
    "TimeoutStartSec": "15min",
}


def test_run_due_service_contract() -> None:
    _assert_service_contract("invio-run-due.service", RUN_DUE)


def test_notify_retry_service_contract() -> None:
    _assert_service_contract("invio-notify-retry.service", NOTIFY_RETRY)


def _assert_timer_contract(name: str, timer: dict[str, str], unit_name: str) -> None:
    unit = load(name)
    assert set(unit) == {"Unit", "Timer", "Install"}
    assert unit["Unit"]["Description"][0] != ""
    assert_directives(unit, "Timer", {**timer, "Unit": unit_name})
    assert_directives(unit, "Install", {"WantedBy": "timers.target"})


def test_run_due_timer_contract() -> None:
    # RandomizedDelaySec=60 plus AccuracySec=1s keeps a start within about 60 s of the quarter
    # hour (SC-002); the default AccuracySec of 1 min would add up to a minute on top of it.
    _assert_timer_contract(
        "invio-run-due.timer",
        {
            "OnCalendar": "*:0/15",
            "Persistent": "true",
            "RandomizedDelaySec": "60",
            "AccuracySec": "1s",
        },
        "invio-run-due.service",
    )


# --- Ansible role files (contracts/ansible-role.md, contracts/env-file.md) ---------------------


def test_role_unit_files_are_symlinks_to_canonical_units() -> None:
    canonical = sorted(path.name for path in SYSTEMD.iterdir())
    assert canonical, "deploy/systemd is empty"
    in_role = sorted(path.name for path in (ROLE / "files").iterdir() if path.name != ".gitkeep")
    assert in_role == canonical, "role files/ must hold exactly the canonical units"
    for name in canonical:
        link = ROLE / "files" / name
        assert link.is_symlink(), f"{link} must be a symlink"
        assert not Path(os.readlink(link)).is_absolute(), f"{link} must be a relative symlink"
        assert link.resolve() == (SYSTEMD / name).resolve()


def _render_template(name: str, **variables: object) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROLE / "templates"),
        trim_blocks=True,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
        autoescape=False,  # config files, not HTML
    )
    return env.get_template(name).render(**variables)


ENV_VARS: dict[str, object] = {
    "invio_db_user": "invio",
    "invio_db_password": "pw",
    "invio_db_host": "localhost",
    "invio_db_port": 3306,
    "invio_db_name": "invio",
    "invio_mariadb_manage_server": True,
    "invio_smtp_host": "smtp.example.com",
    "invio_smtp_port": 587,
    "invio_smtp_security": "starttls",
    "invio_smtp_from": "Invio <invio@example.com>",
    "invio_llm_api_keys": {"openai": "k-openai", "mistral": "k-mistral"},
    "invio_log_level": "INFO",
    "invio_http_contact": "ci@example.invalid",
    "invio_env_extra": {},
}
ALL_OPTIONAL: dict[str, object] = {
    "invio_smtp_user": "mailer",
    "invio_smtp_password": "mail-pw",
    "invio_healthcheck_url": "https://hc-ping.example.com/uuid",
    "invio_llm_api_keys": {
        "google": "k-g",
        "anthropic": "k-a",
        "openai": "k-o",
        "mistral": "k-m",
    },
    "invio_env_extra": {"INVIO_MAX_PARALLEL_ITEMS": "4", "INVIO_HTTP_RESPECT_ROBOTS": "true"},
}


def _systemd_unquote(value: str) -> str:
    """Decode a ``"..."`` EnvironmentFile value the way systemd does for ``\\\\`` and ``\\"``."""
    assert value.startswith('"') and value.endswith('"') and len(value) >= 2, value
    inner = value[1:-1]
    out: list[str] = []
    chars = iter(inner)
    for char in chars:
        if char == "\\":
            out.append(next(chars))
        else:
            assert char != '"', f"unescaped quote in {value!r}"
            out.append(char)
    return "".join(out)


def _env_pairs(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        assert key not in pairs, f"duplicate key {key}"
        pairs[key] = _systemd_unquote(value)
    return pairs


def test_env_template_renders_the_documented_keys_in_fixed_order() -> None:
    text = _render_template("invio.env.j2", **{**ENV_VARS, **ALL_OPTIONAL})
    assert text.isascii()
    assert text.endswith("\n")
    keys = [ln.partition("=")[0] for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert keys == [*ENV_KEYS, "INVIO_HTTP_RESPECT_ROBOTS", "INVIO_MAX_PARALLEL_ITEMS"]
    assert "Managed by Ansible role invio" in text.splitlines()[0]
    assert "INVIO_ENV_FILE" not in keys


def test_env_template_omits_optional_keys_when_undefined_or_empty() -> None:
    text = _render_template("invio.env.j2", **{**ENV_VARS, "invio_smtp_user": ""})
    pairs = _env_pairs(text)
    assert not {"INVIO_SMTP_USER", "INVIO_SMTP_PASSWORD", "INVIO_HEALTHCHECK_URL"} & set(pairs)
    assert set(pairs) >= {"INVIO_DATABASE_URL", "INVIO_OPENAI_API_KEY", "INVIO_ARCHIVE_DIR"}
    assert pairs["INVIO_ARCHIVE_DIR"] == "/var/lib/invio/archive"


def test_env_template_skips_empty_llm_keys() -> None:
    text = _render_template(
        "invio.env.j2", **{**ENV_VARS, "invio_llm_api_keys": {"openai": "k", "google": ""}}
    )
    assert "INVIO_GOOGLE_API_KEY" not in text


def test_env_template_output_has_only_known_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in list(os.environ):
        if key.startswith("INVIO_"):
            monkeypatch.delenv(key)
    path = tmp_path / "invio.env"
    path.write_text(
        _render_template("invio.env.j2", **{**ENV_VARS, **ALL_OPTIONAL}), encoding="utf-8"
    )
    assert unknown_env_keys(path) == []


@pytest.mark.parametrize(
    ("host", "managed", "socket"),
    [
        ("localhost", True, True),
        ("localhost", False, True),  # local server managed by another tool: same socket
        ("dbserver", False, False),
    ],
)
def test_env_template_uses_the_unix_socket_for_a_local_server(
    host: str, managed: bool, socket: bool
) -> None:
    text = _render_template(
        "invio.env.j2",
        **{**ENV_VARS, "invio_db_host": host, "invio_mariadb_manage_server": managed},
    )
    url = _env_pairs(text)["INVIO_DATABASE_URL"]
    assert ("unix_socket=/run/mysqld/mysqld.sock" in url) is socket
    assert url.endswith("charset=utf8mb4&unix_socket=/run/mysqld/mysqld.sock") is socket
    assert f"@{host}:3306/invio?charset=utf8mb4" in url


def test_env_template_escaping_round_trips_through_systemd_and_sqlalchemy() -> None:
    password = 'p"a\\ss/@:%'
    user = "us:er@x"
    text = _render_template(
        "invio.env.j2", **{**ENV_VARS, "invio_db_password": password, "invio_db_user": user}
    )
    # the raw line must not contain the password unescaped
    assert password not in text
    url = make_url(_env_pairs(text)["INVIO_DATABASE_URL"])
    assert url.password == password
    assert url.username == user
    assert url.host == "localhost"
    assert url.database == "invio"
    assert url.query["charset"] == "utf8mb4"


def test_env_template_escapes_backslash_and_quote_in_plain_values_and_keeps_dollar_literal() -> (
    None
):
    value = 'a"b\\c $HOME `id` ${X}'
    text = _render_template("invio.env.j2", **{**ENV_VARS, "invio_http_contact": value})
    assert _env_pairs(text)["INVIO_HTTP_CONTACT"] == value
    assert '"a\\"b\\\\c $HOME `id` ${X}"' in text


def test_notify_retry_timer_contract() -> None:
    unit = load("invio-notify-retry.timer")
    assert "AccuracySec" not in unit["Timer"]  # the 1 min default is fine for an hourly retry
    _assert_timer_contract(
        "invio-notify-retry.timer",
        {"OnCalendar": "hourly", "Persistent": "true", "RandomizedDelaySec": "300"},
        "invio-notify-retry.service",
    )


@pytest.mark.parametrize("name", SERVICES)
def test_services_have_no_unlisted_directives(name: str) -> None:
    unit = load(name)
    assert set(unit["Unit"]) <= ALLOWED_UNIT_KEYS, set(unit["Unit"]) - ALLOWED_UNIT_KEYS
    assert set(unit["Service"]) <= ALLOWED_SERVICE_KEYS, set(unit["Service"]) - ALLOWED_SERVICE_KEYS


def test_services_differ_only_in_the_per_service_keys() -> None:
    run_due, notify = (load(name)["Service"] for name in SERVICES)
    assert set(run_due) == set(notify)
    differing = {key for key in run_due if run_due[key] != notify[key]}
    assert differing == set(PER_SERVICE_KEYS)
    assert load(SERVICES[0])["Unit"].keys() == load(SERVICES[1])["Unit"].keys()


def test_systemd_directory_has_no_drop_ins_or_stray_files() -> None:
    entries = sorted(path.name for path in SYSTEMD.iterdir())
    assert not [name for name in entries if name.endswith(".d")]
    assert all(name.endswith((".service", ".timer")) for name in entries)


def test_memory_override_template(tmp_path: Path) -> None:
    text = _render_template("memory-override.conf.j2", memory_max="2G")
    path = tmp_path / "50-invio-role.conf"
    path.write_text(text, encoding="utf-8")
    unit = parse_unit(path)
    assert unit == {"Service": {"MemoryMax": ["2G"]}}
    assert "Managed by Ansible role invio" in text.splitlines()[0]


@pytest.mark.parametrize(
    "override",
    [
        {"invio_http_contact": "a\nINVIO_X=1"},
        {"invio_smtp_from": "a\rb"},
        {"invio_llm_api_keys": {"openai": "k\nINVIO_Y=2"}},
        {"invio_env_extra": {"INVIO_Z": "v\nINVIO_W=3"}},
        {"invio_db_host": "local\nINVIO_V=4"},
        {"invio_db_name": "in\rvio"},
    ],
)
def test_env_template_refuses_values_with_newlines(override: dict[str, object]) -> None:
    """Defense in depth: validate.yml checks the inputs, the template refuses them too."""
    with pytest.raises(jinja2.UndefinedError, match="newline"):
        _render_template("invio.env.j2", **{**ENV_VARS, **override})


def test_env_template_url_encodes_a_newline_in_the_database_credentials() -> None:
    # User and password are URL-encoded, so a newline cannot start a new line in the env file.
    text = _render_template("invio.env.j2", **{**ENV_VARS, "invio_db_password": "p\nINVIO_V=4"})
    assert "INVIO_V" not in _env_pairs(text)
    assert make_url(_env_pairs(text)["INVIO_DATABASE_URL"]).password == "p\nINVIO_V=4"
