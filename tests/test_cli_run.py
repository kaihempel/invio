"""CLI tests for ``invio run list`` and ``invio run show`` (US5)."""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from invio.cli import main as cli_main
from invio.cli.commands import run as run_module
from invio.config.settings import get_settings
from invio.db.repositories import RunRepository
from invio.db.session import create_db_engine, session_factory, session_scope
from invio.domain import RunStatus
from invio.graph.ports import DeliveryReport
from invio.services.runs import RunService
from tests.pipeline_helpers import add_run, make_job_config, store_job
from tests.run_cli_helpers import RunCli, run_cli  # noqa: F401

# The production seam, captured before ``run_cli`` replaces it with the fixture's service.
REAL_MAKE_SERVICE = run_module._make_service


def _stats_of(run_cli: RunCli, run_id: int) -> dict[str, Any]:
    with session_scope(run_cli.env.factory) as session:
        run = RunRepository(session).get(run_id)
        assert run is not None
        return dict(run.stats or {})


def _set_stats(run_cli: RunCli, run_id: int, stats: dict[str, Any] | None) -> None:
    with session_scope(run_cli.env.factory) as session:
        run = RunRepository(session).get(run_id)
        assert run is not None
        run.stats = stats


def _add_run(run_cli: RunCli, status: RunStatus, stats: dict[str, Any] | None = None) -> int:
    started = datetime(2026, 10, 1, tzinfo=UTC)
    return add_run(run_cli.env.factory, run_cli.env.job_id, status, stats=stats, started_at=started)


# --- run list ------------------------------------------------------------------------------


def test_list_shows_a_row_per_run_and_marks_dry_runs(run_cli: RunCli) -> None:
    run_cli.invoke(["job", "run", "research", "--dry-run"])
    run_cli.invoke(["job", "run", "research"])

    result = run_cli.invoke(["run", "list"])

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert re.fullmatch(
        r"\s*ID\s+Job\s+Started\s+Duration\s+Status\s+Found\s+New\s+Relevant\s*", lines[0]
    )
    assert len(lines) == 3
    newest, oldest = lines[1], lines[2]
    assert "research" in newest and "(dry)" not in newest
    assert "succeeded (dry)" in oldest


def test_list_of_an_unknown_job_fails(run_cli: RunCli) -> None:
    result = run_cli.invoke(["run", "list", "--job", "nope"])
    assert result.exit_code == 1
    assert "Error: job 'nope' not found" in result.stderr


def test_list_without_runs(run_cli: RunCli) -> None:
    result = run_cli.invoke(["run", "list"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "No runs."
    named = run_cli.invoke(["run", "list", "--job", "research"])
    assert named.stdout.strip() == "No runs for job 'research'."


def test_list_limit(run_cli: RunCli) -> None:
    for _ in range(3):
        run_cli.invoke(["job", "run", "research", "--dry-run"])

    assert len(run_cli.invoke(["run", "list", "--limit", "2"]).stdout.splitlines()) == 3
    bad = run_cli.invoke(["run", "list", "--limit", "0"])
    assert bad.exit_code == 1
    assert "--limit must be between 1 and 1000" in bad.stderr
    too_many = run_cli.invoke(["run", "list", "--limit", "1001"])
    assert too_many.exit_code == 1
    assert "--limit must be between 1 and 1000" in too_many.stderr


# --- run show ------------------------------------------------------------------------------


def test_show_lists_the_error_of_a_partial_run(run_cli: RunCli) -> None:
    run_cli.fail_item_at_relevance()
    run_cli.invoke(["job", "run", "research", "--dry-run"])
    (run,) = run_cli.runs()

    result = run_cli.invoke(["run", "show", str(run.id)])

    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert f"Run {run.id} · job research · partial (dry run)" in out
    assert "Errors (1)" in out
    assert "score_relevance" in out
    assert "RuntimeError: item failed" in out
    assert '"Article 2" — https://example.com/post-2' in out
    assert "boom secret" not in out + result.stderr
    assert "Tokens: in" in out


def test_show_keeps_the_error_after_a_later_successful_run(run_cli: RunCli) -> None:
    run_cli.fail_item_at_relevance()
    run_cli.invoke(["job", "run", "research"])
    (old,) = run_cli.runs()
    run_cli.heal_items()
    assert run_cli.invoke(["job", "run", "research"]).exit_code == 0

    result = run_cli.invoke(["run", "show", str(old.id)])

    assert "Errors (1)" in result.stdout
    assert '"Article 2"' in result.stdout


def test_show_a_run_with_an_empty_error_list(run_cli: RunCli) -> None:
    run_cli.invoke(["job", "run", "research", "--dry-run"])
    (run,) = run_cli.runs()

    assert "Errors: none" in run_cli.invoke(["run", "show", str(run.id)]).stdout


def test_show_a_running_run(run_cli: RunCli) -> None:
    run_id = _add_run(run_cli, RunStatus.RUNNING)

    result = run_cli.invoke(["run", "show", str(run_id)])

    assert result.exit_code == 0
    assert f"Run {run_id} · job research · running" in result.stdout
    assert "Duration —" in result.stdout
    assert "Metric" not in result.stdout
    assert "Errors" not in result.stdout  # no error list until the run has finished


def test_show_an_unknown_or_malformed_id(run_cli: RunCli) -> None:
    missing = run_cli.invoke(["run", "show", "999"])
    assert missing.exit_code == 1
    assert "Error: run 999 not found" in missing.stderr
    assert run_cli.invoke(["run", "show", "abc"]).exit_code == 2


def test_show_renders_the_cost_by_completeness(run_cli: RunCli) -> None:
    base = {"version": 1, "llm_calls": 5, "input_tokens": 1, "output_tokens": 2, "tokens": 3}
    cases = {
        "complete": ({**base, "cost_complete": True, "estimated_cost_usd": "0.010305"}, "$0.0103"),
        "partial": (
            {
                **base,
                "cost_complete": False,
                "llm_calls_unpriced": 2,
                "estimated_cost_usd": "0.005",
            },
            "≥ $0.0050 (2 calls without price)",
        ),
        "unpriced": (
            {
                **base,
                "cost_complete": False,
                "llm_calls_unpriced": 5,
                "estimated_cost_usd": "0",
            },
            "unknown",
        ),
    }
    for stats, expected in cases.values():
        run_id = _add_run(run_cli, RunStatus.SUCCEEDED, stats)
        assert f"cost {expected}" in run_cli.invoke(["run", "show", str(run_id)]).stdout


def test_show_strips_control_characters_from_stored_text(run_cli: RunCli) -> None:
    entry = {
        "stage": "persist",
        "error_class": "E",
        "message": "E: \x1b[31mred\x07",
        "item_id": 1,
        "title": "T\nitle",
        "url": "https://example.org/x",
    }
    run_id = _add_run(run_cli, RunStatus.PARTIAL, {"version": 1, "errors": [entry]})

    out = run_cli.invoke(["run", "show", str(run_id)]).stdout

    assert "\x1b" not in out and "\x07" not in out
    assert '"T itle"' in out


# --- QA additions ----------------------------------------------------------------------------


def _row(stdout: str, run_id: int) -> str:
    (line,) = [line for line in stdout.splitlines() if re.match(rf"\s*{run_id}\s", line)]
    return line


def test_list_spans_jobs_newest_first_and_filters(run_cli: RunCli) -> None:
    """US5 scenarios 1 and 2 with two jobs."""
    other_id = store_job(run_cli.env.factory, make_job_config(), name="other")
    with session_scope(run_cli.env.factory) as session:
        runs = RunRepository(session)
        old = runs.start(run_cli.env.job_id, started_at=datetime(2026, 10, 1, tzinfo=UTC))
        runs.finish(old, RunStatus.SUCCEEDED, stats={"version": 1, "found": 1})
        new = runs.start(other_id, started_at=datetime(2026, 10, 2, tzinfo=UTC))
        runs.finish(new, RunStatus.FAILED, stats={"version": 1, "found": 2})
        old_id, new_id = old.id, new.id

    everything = run_cli.invoke(["run", "list"]).stdout.splitlines()
    only_other = run_cli.invoke(["run", "list", "--job", "other"]).stdout

    assert re.match(rf"\s*{new_id}\s+other\s", everything[1])
    assert re.match(rf"\s*{old_id}\s+research\s", everything[2])
    assert "research" not in only_other
    assert len(only_other.splitlines()) == 2


def test_list_shows_legacy_and_running_runs_with_dashes(run_cli: RunCli) -> None:
    null_stats = _add_run(run_cli, RunStatus.FAILED, None)
    no_counts = _add_run(run_cli, RunStatus.SUCCEEDED, {"version": 1})
    running = _add_run(run_cli, RunStatus.RUNNING)

    result = run_cli.invoke(["run", "list"])

    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    for run_id, status in ((null_stats, "failed"), (no_counts, "succeeded")):
        cells = _row(result.stdout, run_id).split()
        assert cells[-4:] == [status, "—", "—", "—"]
    assert _row(result.stdout, running).split()[-5:] == ["—", "running", "—", "—", "—"]


def test_list_times_are_in_the_job_timezone(run_cli: RunCli) -> None:
    run_id = _add_run(run_cli, RunStatus.SUCCEEDED, {"version": 1})

    assert "2026-10-01 02:00 CEST" in _row(run_cli.invoke(["run", "list"]).stdout, run_id)


def test_show_a_legacy_run_without_errors_or_processed(run_cli: RunCli) -> None:
    stats = {"version": 1, "found": 5, "new": 4, "after_keyword_filter": 3, "skipped_budget": 1}
    with session_scope(run_cli.env.factory) as session:
        runs = RunRepository(session)
        run = runs.start(run_cli.env.job_id, started_at=datetime(2026, 10, 1, tzinfo=UTC))
        runs.finish(run, RunStatus.FAILED, stats=stats, error="LockExpiredError")
        run_id = run.id

    result = run_cli.invoke(["run", "show", str(run_id)])

    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert "Error     LockExpiredError" in out
    assert re.search(r"^\s*Processed\s+2\s*$", out, re.MULTILINE)  # 3 after filter minus 1 budget
    assert "Errors: item errors are not available for this run" in out
    assert "cost $0.00" in out


def test_show_a_run_without_any_stats(run_cli: RunCli) -> None:
    run_id = _add_run(run_cli, RunStatus.FAILED, None)

    result = run_cli.invoke(["run", "show", str(run_id)])

    assert result.exit_code == 0, result.stderr
    assert re.search(r"^\s*Found\s+—\s*$", result.stdout, re.MULTILINE)
    assert "Tokens: in 0 · out 0 · total 0 · cost $0.00" in result.stdout
    assert "Errors: item errors are not available for this run" in result.stdout


def test_show_lists_source_errors_and_the_omitted_count(run_cli: RunCli) -> None:
    errors = [
        {
            "stage": "fetch_sources",
            "error_class": "FetchError",
            "message": "FetchError: source failed",
            "source": "1:web",
        },
        {
            "stage": "fetch_sources",
            "error_class": "FetchError",
            "message": "FetchError: source failed",
            "source": "odd-key",
        },
        {
            "stage": "summarize_item",
            "error_class": "LLMError",
            "message": "no prefix",
            "item_id": 3,
        },
        "garbage entry",
    ]
    stats = {"version": 1, "errors": errors, "errors_omitted": 7}
    run_id = _add_run(run_cli, RunStatus.PARTIAL, stats)

    out = run_cli.invoke(["run", "show", str(run_id)]).stdout

    assert "Errors (4)" in out
    assert "FetchError: source failed  (source #2, web)" in out
    assert "FetchError: source failed  (source odd-key)" in out
    assert "LLMError: no prefix" in out
    assert "<unreadable error entry>" in out
    assert "  … and 7 more not stored" in out


def test_show_of_an_unknown_id_is_one_stderr_line(run_cli: RunCli) -> None:
    result = run_cli.invoke(["run", "show", "4242"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip().splitlines() == ["Error: run 4242 not found"]


def test_show_times_are_in_the_job_timezone(run_cli: RunCli) -> None:
    run_id = _add_run(run_cli, RunStatus.SUCCEEDED, {"version": 1})

    assert "Started   2026-10-01 02:00 CEST" in run_cli.invoke(["run", "show", str(run_id)]).stdout


def test_history_commands_write_nothing(run_cli: RunCli) -> None:
    """FR-019: list and show are read-only."""
    run_cli.invoke(["job", "run", "research", "--dry-run"])
    (run,) = run_cli.runs()
    before = (run.stats, run.status, run.finished_at, run_cli.job_row().next_run_at)

    run_cli.invoke(["run", "list"])
    run_cli.invoke(["run", "show", str(run.id)])

    (after,) = run_cli.runs()
    assert (after.stats, after.status, after.finished_at, run_cli.job_row().next_run_at) == before


@pytest.mark.parametrize("args", [["run", "list"], ["run", "show", "1"]])
def test_history_without_a_database_url_is_a_configuration_error(
    run_cli: RunCli, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setattr(run_module, "_make_service", REAL_MAKE_SERVICE)
    monkeypatch.delenv("INVIO_DATABASE_URL")
    get_settings.cache_clear()

    result = run_cli.invoke(args)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.strip() == "Configuration error: INVIO_DATABASE_URL is not set"


@pytest.mark.parametrize("args", [["run", "list"], ["run", "show", "1"]])
def test_history_on_an_old_schema_is_a_database_error(
    run_cli: RunCli, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: list[str]
) -> None:
    empty = create_db_engine(f"sqlite:///{tmp_path}/empty-secretpw.sqlite")  # no schema
    monkeypatch.setattr(run_module, "_make_service", lambda: RunService(session_factory(empty)))

    result = run_cli.invoke(args)

    empty.dispose()
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == (
        "Error: database error (OperationalError); is the schema current? run 'invio db upgrade'"
    )


def test_list_is_not_wrapped_on_a_narrow_pipe(run_cli: RunCli) -> None:
    for _ in range(2):
        run_cli.invoke(["job", "run", "research", "--dry-run"])

    result = run_cli.runner.invoke(cli_main.app, ["run", "list"], env={"COLUMNS": "20"})

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 3
    assert all("succeeded (dry)" in line for line in lines[1:])


def test_show_explains_a_partial_run_caused_by_delivery(run_cli: RunCli) -> None:
    run_cli.env.ports.notifier.report = DeliveryReport(sent=2, failed=1, error="smtp secret")
    run_cli.invoke(["job", "run", "research"])
    (run,) = run_cli.runs()

    result = run_cli.invoke(["run", "show", str(run.id)])

    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert "Errors (1)" in out
    assert "1 of 3 notification(s) not sent" in out
    assert "smtp secret" not in out
