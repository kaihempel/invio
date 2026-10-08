"""CLI tests for ``invio usage`` (issue #35)."""

import json
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine
from typer.testing import CliRunner, Result

from invio.cli import main as cli_main
from invio.cli.commands import usage as usage_module
from invio.cli.usage_output import format_usage_cost
from invio.config.settings import get_settings
from invio.db.models import Base
from invio.db.session import create_db_engine, session_factory, session_scope
from invio.llm.base import ModelRegistryError
from invio.llm.registry import ModelRegistry, default_registry
from invio.services import usage as usage_service_module
from invio.services.usage import UsageService, UsageTotal
from tests.cli_helpers import make_registry
from tests.db_helpers import make_job, make_llm_usage

# The production seam, captured before the fixture replaces it.
REAL_MAKE_REGISTRY = usage_module._make_registry


def _seed(engine: Engine) -> None:
    with session_scope(session_factory(engine)) as session:
        alpha = make_job(session, "alpha")
        beta = make_job(session, "beta")
        make_job(session, "idle")
        for job, model, given, produced, day in (
            (alpha, "gpt-small", 1_000, 500, 1),
            (alpha, "gpt-small", 2_000, 1_000, 2),
            (beta, "gpt-large", 1_000_000, 1_000_000, 3),
            (beta, "unknown-x", 500, 500, 3),
        ):
            make_llm_usage(
                session,
                job,
                model=model,
                input_tokens=given,
                output_tokens=produced,
                created_at=datetime(2026, 10, day, 12, 0, tzinfo=UTC),
            )


class UsageCli:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.runner = CliRunner()

    def invoke(self, args: Sequence[str]) -> Result:
        return self.runner.invoke(cli_main.app, ["usage", *args], env={"COLUMNS": "200"})


@pytest.fixture
def usage_cli(
    monkeypatch: pytest.MonkeyPatch, db_engine: Engine, clean_jobs: None
) -> Iterator[UsageCli]:
    _seed(db_engine)
    service = UsageService(session_factory(db_engine))
    monkeypatch.setattr(usage_module, "_make_service", lambda: service)
    monkeypatch.setattr(usage_module, "_make_registry", make_registry)
    yield UsageCli(db_engine)


@pytest.fixture
def service_engines(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Engine]]:
    """The engines the real ``_make_service`` creates, disposed at teardown."""
    engines: list[Engine] = []

    def tracked(url: str) -> Engine:
        engines.append(create_db_engine(url))
        return engines[-1]

    monkeypatch.setattr(usage_service_module, "create_db_engine", tracked)
    yield engines
    for engine in engines:
        engine.dispose()


def _row(stdout: str, label: str) -> list[str]:
    (line,) = [line for line in stdout.splitlines() if line.strip().startswith(label)]
    return re.split(r"\s{2,}", line.strip())


def test_table_by_model_with_total(usage_cli: UsageCli) -> None:
    result = usage_cli.invoke([])

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert re.fullmatch(
        r"\s*Model\s+Calls\s+Input tokens\s+Output tokens\s+Cost \(USD\)\s*", lines[0]
    )
    assert len(lines) == 5
    assert _row(result.stdout, "openai/gpt-small") == [
        "openai/gpt-small",
        "2",
        "3,000",
        "1,500",
        "$0.0060",
    ]
    assert _row(result.stdout, "openai/unknown-x")[-1] == "unknown"
    assert _row(result.stdout, "Total") == ["Total", "4", "1,003,500", "1,002,000", "≥ $3.0060"]
    assert "openai/unknown-x" in result.stderr
    assert result.stderr.count("\n") == 1


@pytest.mark.parametrize(
    ("by", "header", "keys"),
    [
        ("provider", "Provider", ["openai"]),
        ("job", "Job", ["alpha", "beta"]),
        ("day", "Day", ["2026-10-01", "2026-10-02", "2026-10-03"]),
    ],
)
def test_each_grouping(usage_cli: UsageCli, by: str, header: str, keys: list[str]) -> None:
    result = usage_cli.invoke(["--by", by])

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].split()[0] == header
    assert [line.split()[0] for line in lines[1:-1]] == keys
    assert _row(result.stdout, "Total")[1:4] == ["4", "1,003,500", "1,002,000"]


def test_filters(usage_cli: UsageCli) -> None:
    result = usage_cli.invoke(["--job", "alpha", "--since", "2026-10-02", "--by", "day"])

    assert result.exit_code == 0, result.stderr
    assert _row(result.stdout, "2026-10-02") == ["2026-10-02", "1", "2,000", "1,000", "$0.0040"]
    assert _row(result.stdout, "Total")[-1] == "$0.0040"
    assert result.stderr == ""


def test_json(usage_cli: UsageCli) -> None:
    result = usage_cli.invoke(["--json", "--by", "job", "--since", "2026-10-01"])

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["by"] == "job"
    assert document["job"] is None
    assert document["since"] == "2026-10-01"
    assert document["unpriced_models"] == ["openai/unknown-x"]
    alpha, beta = document["groups"]
    assert alpha == {
        "key": "alpha",
        "calls": 2,
        "input_tokens": 3_000,
        "output_tokens": 1_500,
        "cost_usd": "0.006000",
        "cost_complete": True,
        "unpriced_models": [],
    }
    assert beta["cost_usd"] == "3.000000"
    assert beta["cost_complete"] is False
    assert document["total"] == {
        "calls": 4,
        "input_tokens": 1_003_500,
        "output_tokens": 1_002_000,
        "cost_usd": "3.006000",
        "cost_complete": False,
        "unpriced_models": ["openai/unknown-x"],
    }
    assert "openai/unknown-x" in result.stderr  # the warning stays off stdout


def test_json_of_an_unpriced_group_has_a_null_cost(usage_cli: UsageCli) -> None:
    result = usage_cli.invoke(["--json"])

    groups = {group["key"]: group for group in json.loads(result.stdout)["groups"]}
    assert groups["openai/unknown-x"]["cost_usd"] is None


def test_no_usage(usage_cli: UsageCli) -> None:
    result = usage_cli.invoke(["--job", "idle"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "No usage."
    assert result.stderr == ""
    empty = json.loads(usage_cli.invoke(["--job", "idle", "--json"]).stdout)
    assert empty["groups"] == []
    assert empty["total"]["cost_usd"] == "0.000000"


def test_unknown_job(usage_cli: UsageCli) -> None:
    result = usage_cli.invoke(["--job", "nope"])

    assert result.exit_code == 1
    assert "Error: job 'nope' not found" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("value", ["2026-13-01", "yesterday", "20261001", "2026-1-5"])
def test_bad_date(usage_cli: UsageCli, value: str) -> None:
    result = usage_cli.invoke(["--since", value])

    assert result.exit_code == 2
    assert "--since" in result.stderr
    assert "YYYY-MM-DD" in result.stderr


def test_bad_grouping(usage_cli: UsageCli) -> None:
    result = usage_cli.invoke(["--by", "week"])

    assert result.exit_code == 2
    assert "--by" in result.stderr


def test_registry_error(usage_cli: UsageCli, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> ModelRegistry:
        raise ModelRegistryError("models.d/x.yaml: invalid registry file")

    monkeypatch.setattr(usage_module, "_make_registry", broken)

    result = usage_cli.invoke([])

    assert result.exit_code == 2
    assert "Configuration error: models.d/x.yaml: invalid registry file" in result.stderr


def test_missing_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage_module, "_make_registry", make_registry)

    result = CliRunner().invoke(cli_main.app, ["usage"])

    assert result.exit_code == 2
    assert "Configuration error" in result.stderr


def test_database_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, service_engines: list[Engine]
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", f"sqlite:///{tmp_path}/empty.sqlite")
    get_settings.cache_clear()

    result = CliRunner().invoke(cli_main.app, ["usage"])  # no schema: the table is missing

    assert result.exit_code == 1
    assert "database error (OperationalError)" in result.stderr


def test_real_seams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, service_engines: list[Engine]
) -> None:
    url = f"sqlite:///{tmp_path}/usage.sqlite"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    _seed(engine)
    engine.dispose()
    monkeypatch.setenv("INVIO_DATABASE_URL", url)
    get_settings.cache_clear()

    assert REAL_MAKE_REGISTRY() is default_registry()
    result = CliRunner().invoke(cli_main.app, ["usage", "--by", "provider"])

    assert result.exit_code == 0, result.stderr
    assert len(service_engines) == 1
    assert _row(result.stdout, "Total")[:2] == ["Total", "4"]


@pytest.mark.parametrize(
    ("cost", "complete", "shown"),
    [
        (Decimal("1.234567"), True, "$1.2346"),
        (Decimal("0.000040"), False, "≥ $0.0000"),
        (None, False, "unknown"),
    ],
)
def test_format_usage_cost(cost: Decimal | None, complete: bool, shown: str) -> None:
    total = UsageTotal(
        calls=1,
        input_tokens=1,
        output_tokens=1,
        cost_usd=cost,
        cost_complete=complete,
        unpriced_models=(),
    )

    assert format_usage_cost(total) == shown
