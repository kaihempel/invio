"""Sanity checks for the deployment CI workflows (FR-022, FR-023, quickstart #14).

The workflows themselves only run on GitHub; these tests catch drift between them and the
repository (paths, scenario names, pinned versions, the secrets the leak check looks for).
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
import yaml

from tests.deploy_helpers import ANSIBLE, MOLECULE, REPO, ROLE, is_unwanted_deploy_skip, load_yaml

WORKFLOWS = REPO / ".github" / "workflows"
DEFAULTS = ROLE / "defaults" / "main.yml"


def load_workflow(name: str) -> dict[str, Any]:
    data: dict[Any, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    # YAML 1.1 reads the bare key ``on`` as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


DEPLOY = load_workflow("deploy.yml")
CI = load_workflow("ci.yml")
SCENARIOS = sorted(path.parent.name for path in MOLECULE.glob("*/molecule.yml"))


def _steps(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = workflow["jobs"][job]["steps"]
    return steps


def _step(workflow: dict[str, Any], job: str, name: str) -> dict[str, Any]:
    (step,) = [step for step in _steps(workflow, job) if step.get("name") == name]
    return step


def _run_lines(step: dict[str, Any]) -> list[str]:
    """Shell commands of a step, with folded (``>-``) blocks already joined by YAML."""
    return [line.strip() for line in step["run"].splitlines() if line.strip()]


# --- deploy.yml (FR-023) -----------------------------------------------------------------------


def test_deploy_workflow_is_opt_in_and_triggered_by_deploy_changes() -> None:
    triggers = DEPLOY["on"]
    assert "workflow_dispatch" in triggers
    for event in ("pull_request", "push"):
        paths = triggers[event]["paths"]
        assert "deploy/**" in paths
        assert ".github/workflows/deploy.yml" in paths
    assert triggers["pull_request"]["paths"] == triggers["push"]["paths"]


def test_deploy_trigger_paths_match_existing_files() -> None:
    for pattern in DEPLOY["on"]["push"]["paths"]:
        # Path.glob treats "*" and "**" like GitHub's filter: "*" stays within one directory.
        assert any(path.is_file() for path in REPO.glob(pattern)), f"{pattern} matches nothing"


def test_deploy_runs_when_the_cli_output_checked_by_molecule_changes() -> None:
    # verify.yml greps "database at revision" and "nothing to retry" from src/invio/cli.
    assert "src/invio/cli/**" in DEPLOY["on"]["push"]["paths"]


def test_deploy_matrix_covers_every_molecule_scenario() -> None:
    (job,) = DEPLOY["jobs"].values()
    assert sorted(job["strategy"]["matrix"]["scenario"]) == SCENARIOS == ["default", "external-db"]
    assert job["strategy"]["fail-fast"] is False


def test_deploy_checks_out_full_history_for_the_update_test() -> None:
    (checkout,) = [
        s for s in _steps(DEPLOY, "molecule") if s.get("uses", "").startswith("actions/checkout")
    ]
    assert checkout["with"]["fetch-depth"] == 0


def test_deploy_uv_version_matches_the_role_pin() -> None:
    (setup,) = [
        s for s in _steps(DEPLOY, "molecule") if s.get("uses", "").startswith("astral-sh/setup-uv")
    ]
    pinned = yaml.safe_load(DEFAULTS.read_text(encoding="utf-8"))["invio_uv_version"]
    assert setup["with"]["version"] == pinned


def test_deploy_collection_requirement_files_exist() -> None:
    step = _step(DEPLOY, "molecule", "Install Ansible collections")
    paths = re.findall(r"-r (\S+)", step["run"])
    assert paths == ["deploy/ansible/requirements.yml", "deploy/ansible/molecule/requirements.yml"]
    assert all((REPO / path).is_file() for path in paths)


def test_collections_are_pinned_exactly_and_consistently() -> None:
    pins: dict[str, str] = {}
    for path in (ANSIBLE / "requirements.yml", MOLECULE / "requirements.yml"):
        for collection in load_yaml(path)["collections"]:
            version = collection["version"]
            assert re.fullmatch(r"==\d+\.\d+\.\d+", version), f"{path}: {collection}"
            assert pins.setdefault(collection["name"], version) == version, collection["name"]


def _leak_check_command() -> list[str]:
    converge = _step(DEPLOY, "molecule", "Converge verbosely")
    assert (
        converge["shell"] == "bash"
    )  # GitHub runs bash with -eo pipefail, so tee keeps the status
    (line,) = _run_lines(converge)
    assert "molecule converge" in line and "-v" in shlex.split(line)
    assert "| tee converge.log" in line
    step = _step(DEPLOY, "molecule", "Check the converge log for secrets")
    assert step["working-directory"] == converge["working-directory"] == "deploy/ansible"
    (line,) = [ln for ln in _run_lines(step) if "check-no-secrets.sh" in ln]
    argv = shlex.split(line)
    assert (ANSIBLE / argv[0]).resolve() == REPO / "deploy" / "scripts" / "check-no-secrets.sh"
    assert argv[1] == "converge.log"
    return argv[2:]


def test_leak_check_and_destroy_run_even_when_converge_fails() -> None:
    names = [step.get("name") for step in _steps(DEPLOY, "molecule")]
    converge = names.index("Converge verbosely")
    assert names[converge + 1 : converge + 3] == [
        "Check the converge log for secrets",
        "Destroy the converge instances",
    ]
    for name in names[converge + 1 : converge + 3]:
        assert "always()" in _step(DEPLOY, "molecule", name)["if"]


@pytest.mark.parametrize("workflow", ["deploy.yml", "ci.yml"])
def test_workflow_token_is_read_only(workflow: str) -> None:
    assert load_workflow(workflow)["permissions"] == {"contents": "read"}


def _scenario_secrets(scenario: str) -> set[str]:
    data = yaml.safe_load((MOLECULE / scenario / "molecule.yml").read_text(encoding="utf-8"))
    group = data["provisioner"]["inventory"]["group_vars"]["all"]
    secrets = {
        group.get(name)
        for name in ("invio_db_password", "invio_db_admin_password", "invio_smtp_password")
    }
    secrets |= set(group.get("invio_llm_api_keys", {}).values())
    return {secret for secret in secrets if secret}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_leak_check_looks_for_every_secret_of_the_scenario(scenario: str) -> None:
    # Otherwise the FR-017 check on the verbose converge log would pass vacuously.
    secrets = _scenario_secrets(scenario)
    assert secrets
    assert secrets <= set(_leak_check_command())


def test_leak_check_secrets_are_used_by_a_scenario_and_survive_url_encoding() -> None:
    used = set().union(*(_scenario_secrets(scenario) for scenario in SCENARIOS))
    for secret in _leak_check_command():
        assert secret in used, f"{secret} is checked but no scenario sets it"
        # The DB password is URL-encoded in INVIO_DATABASE_URL; the raw grep must still match.
        assert quote(secret, safe="") == secret


# --- ci.yml deploy-static (FR-022) -------------------------------------------------------------


def test_deploy_static_verifies_units_strictly() -> None:
    step = _step(CI, "deploy-static", "Verify systemd units")
    assert _run_lines(step) == ["deploy/scripts/verify-units.sh"]  # never --if-available


def test_deploy_static_lints_the_role_in_its_directory() -> None:
    step = _step(CI, "deploy-static", "Lint the Ansible role")
    assert step["working-directory"] == "deploy/ansible"
    assert "ansible-lint" in step["run"]
    assert (
        "--group deploy"
        in _step(CI, "deploy-static", "Install dependencies (with the deploy group)")["run"]
    )


def test_deploy_static_runs_every_deploy_test_module() -> None:
    step = _step(CI, "deploy-static", "Unit contract tests")
    args = [arg for arg in shlex.split(step["run"]) if arg.startswith("tests/")]
    modules = sorted(
        p.relative_to(REPO).as_posix() for p in (REPO / "tests").glob("test_deploy_*.py")
    )
    assert len(modules) >= 4
    for module in modules:
        assert any(fnmatch.fnmatch(module, arg) for arg in args), (
            f"{module} not run in deploy-static"
        )


def test_deploy_static_is_not_in_the_opt_in_workflow() -> None:
    # The Molecule job must stay out of the normal PR checks (FR-023): ci.yml runs no molecule.
    for job in CI["jobs"].values():
        for step in job["steps"]:
            assert not re.search(r"\bmolecule (test|converge|verify|create)\b", step.get("run", ""))


def test_deploy_static_fails_instead_of_skipping_deploy_tests() -> None:
    step = _step(CI, "deploy-static", "Unit contract tests")
    assert step["env"]["INVIO_REQUIRE_DEPLOY_TOOLS"] == "1"
    assert "--group deploy" in step["run"]


def _report(outcome: str, **extra: str) -> pytest.TestReport:
    report = pytest.TestReport("t", ("t.py", 0, "t"), {}, outcome, None, "call")  # type: ignore[arg-type]
    for key, value in extra.items():
        setattr(report, key, value)
    return report


@pytest.mark.parametrize(
    ("report", "name", "required", "unwanted"),
    [
        (_report("skipped"), "test_deploy_x.py", True, True),
        (_report("skipped"), "test_deploy_x.py", False, False),
        (_report("skipped"), "test_other.py", True, False),
        (_report("skipped", wasxfail=""), "test_deploy_x.py", True, False),
        (_report("passed"), "test_deploy_x.py", True, False),
    ],
    ids=["skip", "not-required", "other-module", "xfail", "passed"],
)
def test_only_real_skips_of_deploy_tests_fail_when_tools_are_required(
    report: pytest.TestReport, name: str, required: bool, unwanted: bool
) -> None:
    assert is_unwanted_deploy_skip(report, Path(name), required) is unwanted
