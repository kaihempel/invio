"""Invariants of the Ansible role ``deploy/ansible/roles/invio`` (contracts/ansible-role.md).

The static tests parse the role's YAML and compare it with the contract, the shipped units and
the docs. The ``validate.yml`` tests run the real task file with ``ansible-playbook`` against
localhost (it only contains ``assert`` tasks) and are skipped when the ``deploy`` dependency
group is not installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

from tests.deploy_helpers import (
    ANSIBLE,
    ENV_EXAMPLE,
    ENV_KEYS,
    REPO,
    ROLE,
    ROLE_VARS,
    SERVICES,
    SYSTEMD,
    TIMERS,
    UNITS,
    load_yaml,
    parse_unit,
)

CONTRACT = REPO / "specs" / "015-gh-issue-24" / "contracts" / "ansible-role.md"
DOCS = REPO / "docs" / "deployment.md"

SECRET_VARS = (
    "invio_db_password",
    "invio_db_admin_password",
    "invio_smtp_password",
    "invio_llm_api_keys",
    "invio_healthcheck_url",
    "invio_env_extra",
)


DEFAULTS: dict[str, Any] = load_yaml(ROLE / "defaults" / "main.yml")
# Contract rows whose default column is prose rather than one value per variable.
PROSE_DEFAULT_ROWS = frozenset({"invio_uv_version"})


def _contract_section(title: str) -> list[tuple[list[str], str]]:
    """Rows of the first table below ``title``: (backticked names in col 1, raw col 2)."""
    text = CONTRACT.read_text(encoding="utf-8")
    start = text.index(title)
    rows: list[tuple[list[str], str]] = []
    for line in text[start:].splitlines()[1:]:
        if line.startswith("#"):
            break
        if not line.startswith("|") or set(line) <= {"|", "-", " "}:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        names = re.findall(r"`(invio_[a-z0-9_]+)`", cells[0])
        if names:
            rows.append((names, cells[1]))
    return rows


def _iter_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten ``block``/``rescue``/``always`` into a list of plain tasks."""
    flat: list[dict[str, Any]] = []
    for task in tasks:
        nested = [task.get(key) or [] for key in ("block", "rescue", "always")]
        if any(nested):
            flat.extend(_iter_tasks([sub for part in nested for sub in part]))
        else:
            flat.append(task)
    return flat


def _task_file(name: str) -> list[dict[str, Any]]:
    return _iter_tasks(load_yaml(ROLE / "tasks" / name))


# --- defaults and required variables vs the contract -------------------------------------------


def test_contract_tables_parse() -> None:
    assert len(_contract_section("### Defaults")) >= 10
    assert len(_contract_section("### Required")) >= 5


def test_defaults_match_the_contract_table() -> None:
    for names, raw in _contract_section("### Defaults"):
        if raw.startswith("unset"):
            for name in names:
                assert name not in DEFAULTS, f"{name} must stay undefined (contract: unset)"
            continue
        for name in names:
            assert name in DEFAULTS, f"{name} is in the contract but not in defaults/main.yml"
        if names[0] in PROSE_DEFAULT_ROWS:
            continue  # "pinned uv release (matches setup-uv in CI)", compared elsewhere
        # The values are the backticked words before an optional "(explanation)".
        values = re.findall(r"`([^`]*)`", raw.split("(", 1)[0])
        if len(values) == 1:
            values *= len(names)  # "`invio_user` / `invio_group` | `invio`"
        assert len(values) == len(names), f"cannot read the defaults of {names} from {raw!r}"
        for name, value in zip(names, values, strict=True):
            assert DEFAULTS[name] == yaml.safe_load(value), name


def test_defaults_define_nothing_beyond_the_contract() -> None:
    documented = {name for names, _ in _contract_section("### Defaults") for name in names}
    assert set(DEFAULTS) <= documented, set(DEFAULTS) - documented


def test_required_variables_have_no_default_and_are_validated() -> None:
    # Parsed tasks, not the raw file: a mention in a comment does not count.
    validate = yaml.safe_dump(_task_file("validate.yml"))
    required = {name for names, _ in _contract_section("### Required") for name in names}
    assert "invio_db_password" in required
    for name in required:
        assert name not in DEFAULTS, f"required {name} must not have a default (FR-016)"
        assert re.search(rf"\b{name}\b", validate), f"validate.yml does not check {name}"


def test_example_playbook_sets_every_unconditionally_required_variable() -> None:
    (play,) = load_yaml(ANSIBLE / "playbook.example.yml")
    assert play["become"] is True
    assert "invio" in play["roles"]
    for names, rule in _contract_section("### Required"):
        if "only" in rule:
            continue
        for name in names:
            assert name in play["vars"], f"playbook.example.yml lacks {name}"


# --- cross-file invariants ---------------------------------------------------------------------


def test_role_unit_lists_match_the_shipped_units() -> None:
    shipped = sorted(path.name for path in SYSTEMD.iterdir())
    assert sorted(UNITS) == shipped
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None  # Ansible's
    for var, names in (("invio_timers", TIMERS), ("invio_services", SERVICES)):
        rendered = env.from_string(ROLE_VARS[var]).render(invio_units=UNITS)
        assert rendered == str(names), var
    for timer in TIMERS:
        unit = parse_unit(SYSTEMD / timer)
        assert unit["Timer"]["Unit"] == [timer.replace(".timer", ".service")]


@pytest.mark.parametrize(
    ("service", "default_var"),
    [
        ("invio-run-due.service", "invio_run_due_memory_max"),
        ("invio-notify-retry.service", "invio_notify_retry_memory_max"),
    ],
)
def test_memory_defaults_equal_the_unit_so_no_drop_in_is_written_by_default(
    service: str, default_var: str
) -> None:
    # G9/R11: a default install must not write a drop-in, so the three values must agree.
    (shipped,) = parse_unit(SYSTEMD / service)["Service"]["MemoryMax"]
    (override,) = [o for o in ROLE_VARS["invio_memory_overrides"] if o["unit"] == service]
    assert override["default"] == shipped
    assert str(DEFAULTS[default_var]) == shipped
    assert override["value"] == "{{ " + default_var + " }}"


def test_service_account_matches_the_units() -> None:
    for service in SERVICES:
        unit = parse_unit(SYSTEMD / service)["Service"]
        assert unit["User"] == [DEFAULTS["invio_user"]]
        assert unit["Group"] == [DEFAULTS["invio_group"]]


def test_update_wait_outlasts_the_run_due_start_timeout() -> None:
    (timeout,) = parse_unit(SYSTEMD / "invio-run-due.service")["Service"]["TimeoutStartSec"]
    assert timeout == "3h"
    assert int(DEFAULTS["invio_update_wait_timeout"]) > 3 * 3600


def test_llm_providers_match_the_env_example() -> None:
    example = ENV_EXAMPLE.read_text(encoding="utf-8")
    keys = re.findall(r"^INVIO_([A-Z]+)_API_KEY=", example, flags=re.MULTILINE)
    assert sorted(ROLE_VARS["invio_llm_providers"]) == sorted(key.lower() for key in keys)


def test_uv_checksums_are_sha256_per_architecture() -> None:
    checksums = DEFAULTS["invio_uv_sha256"]
    assert set(checksums) == {"x86_64", "aarch64"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in checksums.values())
    assert re.fullmatch(r"\d+\.\d+\.\d+", DEFAULTS["invio_uv_version"])


def test_docs_use_the_role_pins() -> None:
    docs = DOCS.read_text(encoding="utf-8")
    python = DEFAULTS["invio_python_version"]
    assert f"uv python install --no-bin {python}" in docs
    assert set(re.findall(r"UV_PYTHON=(\S+)", docs)) == {python}


def test_meta_platforms_match_the_validated_debian_versions() -> None:
    meta = load_yaml(ROLE / "meta" / "main.yml")
    (debian,) = meta["galaxy_info"]["platforms"]
    assert debian["name"] == "Debian"
    assert sorted(debian["versions"]) == ["bookworm", "trixie"]  # Debian 12 and 13
    # validate.yml accepting exactly 12 and 13 is run in test_validate_accepts_debian_13 and the
    # debian-11 case of test_validate_rejects_bad_input_naming_the_variable.


# --- task order, tags and secrets (FR-011, FR-016, FR-017) -------------------------------------


def test_task_order_and_tags_follow_the_contract() -> None:
    main = load_yaml(ROLE / "tasks" / "main.yml")
    imports = [task["ansible.builtin.import_tasks"] for task in main]
    assert imports == [
        "validate.yml",
        "mariadb.yml",
        "account.yml",
        "config.yml",
        "install.yml",
        "units.yml",
    ]
    assert "always" in main[0]["tags"], "validation must run with every tag selection"
    # An update with --tags invio:install stops the timers; the unit tasks must start them again.
    assert "invio:install" in main[-1]["tags"]
    tags = {tag for task in main for tag in task["tags"]}
    contract_tags = set(re.findall(r"`(invio(?::[a-z]+)?)`", _contract_text("## Tags")))
    assert contract_tags <= tags, contract_tags - tags


def _contract_text(title: str) -> str:
    text = CONTRACT.read_text(encoding="utf-8")
    start = text.index(title) + len(title)
    end = text.find("\n## ", start)
    return text[start : end if end != -1 else None]


def test_validate_only_asserts_and_changes_nothing() -> None:
    for task in _task_file("validate.yml"):
        modules = set(task) - {"name", "loop", "loop_control", "when", "tags", "vars"}
        assert modules == {"ansible.builtin.assert"}, task["name"]


@pytest.mark.parametrize(
    "name", ["mariadb.yml", "account.yml", "config.yml", "install.yml", "units.yml"]
)
def test_every_task_that_touches_a_secret_has_no_log(name: str) -> None:
    for task in _task_file(name):
        text = yaml.safe_dump(task)
        used = [var for var in SECRET_VARS if re.search(rf"\b{var}\b", text)]
        if used:
            assert task.get("no_log") is True, f"{name}: {task['name']!r} uses {used}"


def test_env_file_task_is_secret_and_has_the_contract_permissions() -> None:
    tasks = {task["name"]: task for task in _task_file("config.yml")}
    directory = tasks["Create the configuration directory"]["ansible.builtin.file"]
    assert (directory["path"], directory["owner"], directory["mode"]) == (
        "/etc/invio",
        "root",
        "0750",
    )
    assert directory["group"] == "{{ invio_group }}"
    env = tasks["Write the env file"]
    assert env["no_log"] is True
    template = env["ansible.builtin.template"]
    assert (template["dest"], template["mode"]) == ("/etc/invio/invio.env", "0640")
    # root owns the file: the service account can read its settings but not rewrite them.
    assert (template["owner"], template["group"]) == ("root", "{{ invio_group }}")


def test_account_is_a_system_user_without_login_or_home_content() -> None:
    (group, user) = _task_file("account.yml")
    assert group["ansible.builtin.group"]["system"] is True
    spec = user["ansible.builtin.user"]
    assert spec["system"] is True
    assert spec["shell"] == "/usr/sbin/nologin"
    assert spec["home"] == "/var/lib/invio"
    assert spec["create_home"] is False


def test_units_are_copied_verbatim_and_timers_enabled_last() -> None:
    tasks = _task_file("units.yml")
    copy = tasks[0]["ansible.builtin.copy"]
    assert copy["src"] == "{{ item }}"
    assert copy["dest"] == "/etc/systemd/system/{{ item }}"
    assert "content" not in copy
    assert tasks[0]["loop"] == "{{ invio_units }}"
    last = tasks[-1]["ansible.builtin.systemd_service"]
    assert (last["enabled"], last["state"]) == (True, "started")
    assert tasks[-1]["loop"] == "{{ invio_timers }}"


def test_role_never_drops_anything() -> None:
    # G8: no database, user or table is removed.
    for name in ("mariadb.yml", "install.yml"):
        for task in _task_file(name):
            for module in ("ansible.mysql.mysql_db", "ansible.mysql.mysql_user"):
                if module in task:
                    assert task[module]["state"] == "present"
            text = yaml.safe_dump(task)
            assert not re.search(r"\bDROP\s+(DATABASE|SCHEMA|USER|TABLE)\b", text, re.I)
            assert "mysql_query" not in text


# --- validate.yml with ansible-playbook (FR-016, US5 sc.6) -------------------------------------

ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")
needs_ansible = pytest.mark.skipif(
    ANSIBLE_PLAYBOOK is None, reason="ansible-playbook not installed (uv sync --group deploy)"
)

VALID: dict[str, Any] = {
    "ansible_distribution": "Debian",
    "ansible_distribution_major_version": "12",
    "ansible_architecture": "x86_64",
    "invio_git_repo": "https://example.com/invio.git",
    "invio_git_version": "v1.2.3",
    "invio_db_password": "qa-secret-db-1",
    "invio_smtp_host": "smtp.example.com",
    "invio_smtp_from": "Invio <invio@example.com>",
    "invio_http_contact": "ops@example.com",
    "invio_llm_api_keys": {"mistral": "qa-secret-llm-1"},
}
PLAYBOOK = """\
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.include_role:
        name: invio
        tasks_from: validate
"""


def run_validate(tmp_path: Path, variables: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    assert ANSIBLE_PLAYBOOK is not None
    playbook = tmp_path / "validate.yml"
    playbook.write_text(PLAYBOOK, encoding="utf-8")
    config = tmp_path / "ansible.cfg"
    config.write_text("[defaults]\n", encoding="utf-8")
    extra = tmp_path / "vars.json"
    extra.write_text(
        json.dumps({**variables, "ansible_python_interpreter": sys.executable}), encoding="utf-8"
    )
    env = {
        **os.environ,
        "ANSIBLE_ROLES_PATH": str(ROLE.parent),
        "ANSIBLE_HOME": str(tmp_path / "ansible-home"),
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-tmp"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_FORCE_COLOR": "0",
        "ANSIBLE_LOCALHOST_WARNING": "False",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False",
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
        "ANSIBLE_CONFIG": str(config),
    }
    return subprocess.run(
        [ANSIBLE_PLAYBOOK, "-i", "localhost,", "-c", "local", "-e", f"@{extra}", str(playbook)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )


def _without(name: str) -> dict[str, Any]:
    return {key: value for key, value in VALID.items() if key != name}


def _assert_fails_naming(result: subprocess.CompletedProcess[str], *names: str) -> None:
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "failed=1" in out, out
    for name in names:
        assert name in out, f"{name} not named in:\n{out}"
    for secret in ("qa-secret-db-1", "qa-secret-llm-1", "qa-secret-admin-1"):
        assert secret not in out, "a secret value leaked into the output (FR-017)"


@needs_ansible
def test_validate_accepts_the_minimal_variables(tmp_path: Path) -> None:
    result = run_validate(tmp_path, VALID)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "qa-secret-db-1" not in result.stdout


@needs_ansible
def test_validate_accepts_all_optional_variables(tmp_path: Path) -> None:
    result = run_validate(
        tmp_path,
        {
            **VALID,
            "invio_git_version": "0123456789abcdef0123456789abcdef01234567",
            "invio_smtp_user": "mailer",
            "invio_smtp_password": "qa-secret-smtp-1",
            "invio_healthcheck_url": "https://hc.example.com/x",
            "invio_git_key_file": "/root/.ssh/deploy",
            "invio_llm_api_keys": {"mistral": "a", "openai": "b", "anthropic": "", "google": "d"},
            "invio_env_extra": {"INVIO_MAX_PARALLEL_ITEMS": "4"},
            "invio_mariadb_manage_server": False,
            "invio_db_host": "db.example.com",
            "invio_db_admin_user": "root",
            "invio_db_admin_password": "qa-secret-admin-1",
            "invio_run_due_memory_max": "2G",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_ansible
@pytest.mark.parametrize(
    "override",
    [
        {"ansible_distribution_major_version": "13"},
        {"invio_git_repo": "ssh://git@example.com/invio.git"},
        {"invio_git_repo": "git@example.com:org/invio.git"},
        {"invio_git_repo": "/srv/invio-src"},
    ],
    ids=["debian-13", "ssh-url-with-user", "scp-like", "local-path"],
)
def test_validate_accepts(tmp_path: Path, override: dict[str, Any]) -> None:
    result = run_validate(tmp_path, {**VALID, **override})
    assert result.returncode == 0, result.stdout + result.stderr


@needs_ansible
@pytest.mark.parametrize(
    "name",
    [
        "invio_git_repo",
        "invio_git_version",
        "invio_db_password",
        "invio_smtp_host",
        "invio_smtp_from",
        "invio_http_contact",
    ],
)
def test_validate_names_a_missing_required_variable(tmp_path: Path, name: str) -> None:
    _assert_fails_naming(run_validate(tmp_path, _without(name)), f"{name} is required")


@needs_ansible
@pytest.mark.parametrize(
    ("override", "named"),
    [
        ({"invio_db_password": ""}, "invio_db_password is required"),
        ({"invio_db_password": "qa-secret-db-1\nINVIO_X=1"}, "invio_db_password is required"),
        ({"invio_db_password": "qa-secret-db-1\rINVIO_X=1"}, "invio_db_password is required"),
        ({"invio_git_repo": "https://user:tok@example.com/i.git"}, "invio_git_repo must not"),
        ({"invio_git_repo": "https://tok@example.com/i.git"}, "invio_git_repo must not"),
        ({"invio_env_extra": {"INVIO_X": True}}, "invio_env_extra"),
        ({"invio_db_host": "db.example.com"}, "invio_mariadb_manage_server installs a local"),
        ({"invio_git_version": "0123abc"}, "abbreviated commit SHA"),
        ({"invio_llm_api_keys": {"mistral": ""}}, "invio_llm_api_keys"),
        ({"invio_llm_api_keys": {"cohere": "qa-secret-llm-1"}}, "invio_llm_api_keys"),
        ({"invio_llm_api_keys": "qa-secret-llm-1"}, "invio_llm_api_keys"),
        ({"invio_env_extra": {"PATH": "/tmp"}}, "invio_env_extra"),
        ({"invio_env_extra": {"INVIO_X": "a\nb"}}, "invio_env_extra"),
        ({"invio_smtp_user": "a\nb"}, "invio_smtp_user must be a single-line string"),
        ({"invio_db_name": "in\nvio"}, "invio_db_name must be a single-line string"),
        ({"invio_run_due_memory_max": "2 GB"}, "invio_run_due_memory_max"),
        ({"invio_update_wait_timeout": 0}, "invio_update_wait_timeout"),
        ({"ansible_distribution_major_version": "11"}, "Debian 12 and 13"),
        ({"ansible_architecture": "ppc64le"}, "invio_uv_sha256"),
        (
            {"invio_mariadb_manage_server": False, "invio_db_host": "db.example.com"},
            "invio_db_admin_user and invio_db_admin_password are required",
        ),
    ],
    ids=[
        "empty-password",
        "newline-password",
        "carriage-return-password",
        "repo-with-password",
        "repo-with-token",
        "extra-boolean",
        "managed-server-remote-host",
        "short-sha",
        "no-llm-key",
        "unknown-provider",
        "llm-keys-not-mapping",
        "extra-not-invio",
        "extra-newline",
        "optional-newline",
        "db-name-newline",
        "memory-format",
        "zero-wait",
        "debian-11",
        "unknown-arch",
        "external-db-without-admin",
    ],
)
def test_validate_rejects_bad_input_naming_the_variable(
    tmp_path: Path, override: dict[str, Any], named: str
) -> None:
    _assert_fails_naming(run_validate(tmp_path, {**VALID, **override}), named)


@needs_ansible
@pytest.mark.parametrize(
    ("name", "source", "named"),
    [
        ("invio_db_password", "", "invio_db_password is required"),
        ("invio_smtp_from", "a@example.com\nINVIO_X=1", "invio_smtp_from is required"),
        ("invio_smtp_password", "qa-secret-db-1\nINVIO_X=1", "invio_smtp_password must be"),
    ],
    ids=["empty-vault-password", "newline-vault-from", "newline-vault-optional"],
)
def test_validate_checks_values_behind_vault_style_references(
    tmp_path: Path, name: str, source: str, named: str
) -> None:
    variables = {**VALID, name: "{{ vault_value }}", "vault_value": source}
    _assert_fails_naming(run_validate(tmp_path, variables), named)


@needs_ansible
@pytest.mark.parametrize(
    "key",
    [
        "INVIO_DATABASE_URL",
        "INVIO_ARCHIVE_DIR",
        "INVIO_ENV_FILE",
        "INVIO_SMTP_HOST",
        "INVIO_LOG_LEVEL",
        "INVIO_MISTRAL_API_KEY",
        "INVIO_HEALTHCHECK_URL",
    ],
)
def test_validate_rejects_env_extra_keys_the_role_manages(tmp_path: Path, key: str) -> None:
    result = run_validate(tmp_path, {**VALID, "invio_env_extra": {key: "x"}})
    _assert_fails_naming(result, "invio_env_extra")


def test_managed_env_keys_list_matches_what_the_template_writes() -> None:
    """validate.yml must reject exactly the keys that invio.env.j2 writes itself."""
    template = (ROLE / "templates" / "invio.env.j2").read_text(encoding="utf-8")
    written = set(re.findall(r"^(INVIO_[A-Z_]+)=", template, flags=re.M))
    written.discard("INVIO_")
    providers = ROLE_VARS["invio_llm_providers"]
    written |= {f"INVIO_{provider.upper()}_API_KEY" for provider in providers}
    assert set(ENV_KEYS) == written
    assert ROLE_VARS["invio_managed_env_keys"] == [*ENV_KEYS, "INVIO_ENV_FILE"]


def _login_arguments() -> list[tuple[str, dict[str, Any]]]:
    tasks = yaml.safe_load((ROLE / "tasks" / "mariadb.yml").read_text(encoding="utf-8"))
    found = []
    for task in tasks:
        for module, args in task.items():
            if module.startswith("ansible.mysql.") and isinstance(args, dict):
                found.append((task["name"], args))
    return found


@pytest.mark.parametrize(
    ("manage", "host", "uses_admin"),
    # A managed server on a remote host is rejected by validate.yml.
    [(True, "localhost", False), (False, "localhost", False), (False, "db.example.com", True)],
)
def test_database_login_needs_the_admin_account_only_for_a_remote_host(
    manage: bool, host: str, uses_admin: bool
) -> None:
    """Contract: invio_db_admin_* are required only for a non-localhost host.

    The login arguments are rendered with the admin variables undefined: for a local server
    (managed or not) rendering must not need them and must fall back to the root socket.
    """
    omit = "__omit__"
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    variables: dict[str, Any] = {
        "omit": omit,
        "invio_mariadb_manage_server": manage,
        "invio_db_host": host,
        "invio_db_port": 3306,
        "invio_db_name": "invio",
        "invio_db_user": "invio",
        "invio_db_user_host": "localhost",
        "invio_db_password": "x",
    }
    tasks = _login_arguments()
    assert tasks
    for name, args in tasks:
        logins = {k: v for k, v in args.items() if k.startswith("login_")}
        if uses_admin:
            variables.update(invio_db_admin_user="root", invio_db_admin_password="pw")
        rendered = {k: env.from_string(str(v)).render(**variables) for k, v in logins.items()}
        if uses_admin:
            assert rendered["login_user"] == "root", name
            assert rendered["login_unix_socket"] == omit, name
        else:
            assert rendered["login_unix_socket"] == "/run/mysqld/mysqld.sock", name
            assert {rendered[k] for k in rendered if k != "login_unix_socket"} == {omit}, name
