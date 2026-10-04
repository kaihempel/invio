"""Static checks for the delivery requirements of the data store (FR-015, FR-017)."""

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ci() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    return data


def test_ci_has_an_optional_mariadb_job_running_the_db_tests() -> None:
    job = _ci()["jobs"]["mariadb"]

    assert job["continue-on-error"] is True
    assert job["services"]["mariadb"]["image"].startswith("mariadb:")
    assert job["env"]["INVIO_TEST_DATABASE_URL"].startswith(("mysql", "mariadb"))
    assert any("pytest -m db" in step.get("run", "") for step in job["steps"])


def test_required_ci_job_stays_on_sqlite() -> None:
    job = _ci()["jobs"]["checks"]

    assert "continue-on-error" not in job
    assert "services" not in job
    assert "INVIO_TEST_DATABASE_URL" not in str(job)


def test_readme_documents_db_upgrade_and_configuration() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    for needle in ("invio db upgrade", "INVIO_DATABASE_URL", "INVIO_TEST_DATABASE_URL"):
        assert needle in readme
