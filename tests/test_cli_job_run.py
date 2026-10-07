"""CLI tests for ``invio job run`` (contract: contracts/cli-commands.md).

They run the real pipeline over fakes (``run_cli``): no network, no SMTP. stdout and stderr are
read separately.
"""

import asyncio
import contextlib
import re
from collections.abc import Coroutine
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, update

from invio.cli.commands import job as job_module
from invio.cli.progress import RunProgressView
from invio.config.settings import get_settings
from invio.db.models import Item, Job
from invio.db.session import create_db_engine, session_factory, session_scope
from invio.domain import ItemStatus, RunStatus
from invio.graph.state import RunError
from invio.llm.base import ModelRegistryError
from invio.llm.fake import FakeReply
from invio.pipeline import ProgressEvent
from invio.pipeline import deps as deps_module
from invio.pipeline.run import RunResult
from invio.services.jobs import JobRecord, JobService
from invio.sources.errors import FetchError
from tests.pipeline_helpers import DEFAULT_USAGE, make_candidates, relevance_reply, source_key
from tests.run_cli_helpers import FEED_URL, RunCli, run_cli  # noqa: F401

# The production seam, captured before ``run_cli`` replaces it with the fakes.
REAL_RUN_DEPS = job_module._run_deps

DIGEST_TEXT = "An intro about what is new."


def _set_job(run_cli: RunCli, **values: Any) -> None:
    with session_scope(run_cli.env.factory) as session:
        session.execute(update(Job).where(Job.id == run_cli.env.job_id).values(**values))


def _cell(output: str, label: str) -> str:
    match = re.search(rf"^\s*{re.escape(label)}\s+(\S.*?)\s*$", output, re.MULTILINE)
    assert match, f"no row {label!r} in:\n{output}"
    return match.group(1)


# --- dry run (US1) -------------------------------------------------------------------------


def test_dry_run_prints_digest_stats_and_usage_and_sends_nothing(run_cli: RunCli) -> None:
    before = run_cli.job_row().next_run_at

    result = run_cli.invoke(["job", "run", "research", "--dry-run"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("# research\n\n")
    assert DIGEST_TEXT in result.stdout
    assert (_cell(result.stdout, "Found"), _cell(result.stdout, "New")) == ("3", "3")
    assert _cell(result.stdout, "Relevant") == "3"
    assert _cell(result.stdout, "Failed") == "0"
    assert any(line.startswith("Tokens: in") for line in result.stdout.splitlines())
    assert run_cli.notifier_calls == []
    assert run_cli.job_row().next_run_at == before
    (run,) = run_cli.runs()
    assert run.stats is not None and run.stats["dry_run"] is True


def test_dry_run_without_relevant_items_says_so(run_cli: RunCli) -> None:
    run_cli.nothing_relevant()

    result = run_cli.invoke(["job", "run", "research", "--dry-run"])

    assert result.exit_code == 0, result.stderr
    assert "No digest: nothing relevant was found." in result.stdout
    assert "Found" in result.stdout


def test_progress_goes_to_stderr_only(run_cli: RunCli) -> None:
    result = run_cli.invoke(["job", "run", "research", "--dry-run"])

    assert "progress:" not in result.stdout
    assert "progress: deduplicate found=3 new=3" in result.stderr
    assert "progress: keyword_prefilter after_keyword_filter=3" in result.stderr
    assert "progress: items processed=3/3 relevant=3 failed=0" in result.stderr
    assert "progress: finalize" in result.stderr


# --- exit codes (US2) ----------------------------------------------------------------------


def test_a_succeeded_run_exits_0(run_cli: RunCli) -> None:
    assert run_cli.invoke(["job", "run", "research", "--dry-run"]).exit_code == 0


def test_a_partial_run_exits_2(run_cli: RunCli) -> None:
    run_cli.fail_item_at_relevance()

    result = run_cli.invoke(["job", "run", "research", "--dry-run"])

    (run,) = run_cli.runs()
    assert result.exit_code == 2
    assert (
        f"run {run.id} finished partial: 1 error(s); see 'invio run show {run.id}'" in result.stderr
    )
    assert "boom secret" not in result.stdout + result.stderr


def test_a_failed_run_exits_1(run_cli: RunCli) -> None:
    run_cli.fail_every_source()

    result = run_cli.invoke(["job", "run", "research", "--dry-run"])

    (run,) = run_cli.runs()
    assert result.exit_code == 1
    assert f"run {run.id} failed: all sources failed" in result.stderr


def _refusal(result: Any, run_cli: RunCli, text: str) -> None:
    assert result.exit_code == 1, result.stderr
    assert text in result.stderr
    assert result.stdout == ""
    assert run_cli.runs() == []


def test_an_unknown_job_is_refused(run_cli: RunCli) -> None:
    result = run_cli.invoke(["job", "run", "nope"])

    _refusal(result, run_cli, "Error: job 'nope' not found")
    assert len(result.stderr.strip().splitlines()) == 1


def test_a_disabled_job_is_refused(run_cli: RunCli) -> None:
    _set_job(run_cli, enabled=False)

    result = run_cli.invoke(["job", "run", "research"])

    _refusal(result, run_cli, "Error: job 'research' is disabled; enable it with")
    assert len(result.stderr.strip().splitlines()) == 1


def test_an_invalid_stored_config_exits_1_not_2(run_cli: RunCli) -> None:
    config = dict(run_cli.env.config)
    config["schedule"] = {**config["schedule"], "timezone": "Europe/Atlantis"}
    _set_job(run_cli, config=config)

    result = run_cli.invoke(["job", "run", "research"])

    _refusal(result, run_cli, "invalid stored job 'research'")


def test_a_locked_job_is_refused(run_cli: RunCli) -> None:
    _set_job(run_cli, locked_until=run_cli.clock() + timedelta(hours=1))

    result = run_cli.invoke(["job", "run", "research"])

    _refusal(result, run_cli, "Error: job 'research' is running (locked until ")
    assert len(result.stderr.strip().splitlines()) == 1


def test_a_missing_database_url_is_a_configuration_error(
    run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job_module, "_run_deps", REAL_RUN_DEPS)
    monkeypatch.delenv("INVIO_DATABASE_URL")
    get_settings.cache_clear()

    result = run_cli.invoke(["job", "run", "research"])

    assert result.exit_code == 1
    assert "Configuration error:" in result.stderr
    assert "Traceback" not in result.stderr


def test_ctrl_c_exits_1_records_the_run_failed_and_releases_the_lock(run_cli: RunCli) -> None:
    ports = run_cli.env.ports
    ports.gate = asyncio.Event()  # every page fetch blocks: the run is mid-flight

    def interrupt(coro: Coroutine[Any, Any, object]) -> None:
        async def main() -> None:
            task = asyncio.ensure_future(coro)
            await asyncio.wait_for(ports.entered.wait(), timeout=10)
            task.cancel()  # what asyncio.run does to the main task on Ctrl-C
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(main())
        raise KeyboardInterrupt

    run_cli.monkeypatch.setattr(job_module, "_execute", interrupt)

    result = run_cli.invoke(["job", "run", "research"])

    assert result.exit_code == 1
    assert "interrupted; a started run is recorded as failed" in result.stderr
    assert "Traceback" not in result.stderr + result.stdout
    (run,) = run_cli.runs()
    assert run.status == RunStatus.FAILED
    assert run_cli.job_row().locked_until is None
    # The safety net stores the fatal error, so ``run show`` names it (not a legacy run).
    (entry,) = (run.stats or {})["errors"]
    assert (entry["stage"], entry["error_class"]) == ("extract_text", "CancelledError")
    shown = run_cli.invoke(["run", "show", str(run.id)])
    assert "not available" not in shown.stdout
    assert "CancelledError" in shown.stdout


def test_a_hostile_digest_is_printed_without_escapes_but_keeps_its_layout(
    run_cli: RunCli,
) -> None:
    body = "Intro \x1b[31mred\x1b[0m\x1b]0;x\x07 text\n\n## Heading\r\n\n\tindented\tline\x00\nend"
    run_cli.env.provider._routes["synthesize"] = FakeReply(body, DEFAULT_USAGE)

    result = run_cli.invoke(["job", "run", "research", "--dry-run"])

    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert "\x1b" not in out and "\x07" not in out and "\x00" not in out
    assert "Intro red text\n\n## Heading\n\n\tindented\tline\nend" in out


def test_help_documents_the_exit_codes(run_cli: RunCli) -> None:
    result = run_cli.invoke(["job", "run", "--help"])

    assert result.exit_code == 0
    assert "Exit codes: 0 succeeded, 1 failed or could not start, 2 partial." in result.stdout


# --- real run (US3) ------------------------------------------------------------------------


def test_a_real_run_notifies_and_advances_the_schedule(run_cli: RunCli) -> None:
    result = run_cli.invoke(["job", "run", "research"])

    assert result.exit_code == 0, result.stderr
    assert len(run_cli.notifier_calls) == 1
    assert re.search(r"Notifications sent\s+1\b", result.stdout)
    assert re.search(r"Notifications failed\s+0\b", result.stdout)
    assert DIGEST_TEXT not in result.stdout
    (run,) = run_cli.runs()
    assert run.stats is not None and "dry_run" not in run.stats
    assert run_cli.job_row().next_run_at == run_cli.clock() + timedelta(hours=1)


# --- --max-items (US4) ---------------------------------------------------------------------


def test_max_items_caps_the_processed_items(run_cli: RunCli) -> None:
    run_cli.with_candidates(10)

    result = run_cli.invoke(["job", "run", "research", "--dry-run", "--max-items", "2"])

    assert result.exit_code == 0, result.stderr
    assert 0 < int(_cell(result.stdout, "Processed")) <= 2


def test_max_items_above_the_job_limit_is_noted_and_the_run_proceeds(run_cli: RunCli) -> None:
    limit = run_cli.env.config.get("limits", {}).get("max_items_per_run", 100)

    result = run_cli.invoke(["job", "run", "research", "--dry-run", "--max-items", "1000"])

    assert result.exit_code == 0, result.stderr
    assert f"note: --max-items 1000 exceeds the job limit {limit}; using {limit}" in result.stderr
    assert _cell(result.stdout, "Processed") == "3"


def test_max_items_below_one_is_refused(run_cli: RunCli) -> None:
    result = run_cli.invoke(["job", "run", "research", "--max-items", "0"])

    _refusal(result, run_cli, "Error: --max-items must be at least 1")


def test_max_items_that_is_not_a_number_is_a_usage_error(run_cli: RunCli) -> None:
    result = run_cli.invoke(["job", "run", "research", "--max-items", "abc"])

    assert result.exit_code == 2
    assert run_cli.runs() == []


# --- --verbose (US6) -----------------------------------------------------------------------


def _mixed(title: str) -> Any:
    if title == "Article 2":
        return RuntimeError("boom secret")
    return relevance_reply(score=0.0) if title == "Article 3" else relevance_reply()


def test_verbose_prints_one_stderr_line_per_item(run_cli: RunCli) -> None:
    run_cli.env.provider._routes["relevance"] = _mixed

    result = run_cli.invoke(["job", "run", "research", "--dry-run", "-v"])

    lines = result.stderr.splitlines()
    assert "item: relevant  Article 1" in lines
    assert "item: irrelevant  Article 3" in lines
    assert "item: failed  Article 2  RuntimeError: item failed" in lines
    assert "item:" not in result.stdout
    assert "boom secret" not in result.stdout + result.stderr


def test_verbose_prints_the_digest_of_a_real_run(run_cli: RunCli) -> None:
    result = run_cli.invoke(["job", "run", "research", "--verbose"])

    assert result.exit_code == 0, result.stderr
    assert DIGEST_TEXT in result.stdout
    assert len(run_cli.notifier_calls) == 1


# --- stdout / stderr separation (FR-008, SC-006) -------------------------------------------


def test_stdout_is_a_clean_document(run_cli: RunCli) -> None:
    """S11: stdout holds only the digest, the table and the usage line; no progress noise."""
    result = run_cli.invoke(["job", "run", "research", "--dry-run", "-v", "--max-items", "1000"])

    assert result.exit_code == 0, result.stderr
    for marker in ("progress:", "item:", "note:"):
        assert marker not in result.stdout
    assert DIGEST_TEXT not in result.stderr
    assert "Metric" not in result.stderr
    assert "Tokens:" not in result.stderr
    assert result.stdout.rstrip().splitlines()[-1].startswith("Tokens: in ")


def test_a_budget_stop_still_reports_the_item_counts_on_a_pipe(run_cli: RunCli) -> None:
    """FR-007/FR-008: the relevant count appears even when not every selected item ran."""
    config = dict(run_cli.env.config)
    config["limits"] = {"max_llm_tokens_per_run": 20}  # one call costs 15 tokens
    _set_job(run_cli, config=config)
    run_cli.with_candidates(6)

    result = run_cli.invoke(["job", "run", "research", "--dry-run"])

    skipped = int(_cell(result.stdout, "Skipped (budget)"))
    assert skipped > 0, result.stdout
    relevant = _cell(result.stdout, "Relevant")
    lines = [line for line in result.stderr.splitlines() if line.startswith("progress: items")]
    assert len(lines) == 1, result.stderr
    assert re.fullmatch(
        rf"progress: items processed=\d+/6 relevant={relevant} failed=\d+", lines[0]
    )


def _fake_result(**overrides: Any) -> RunResult:
    values: dict[str, Any] = {
        "job_id": 1,
        "run_id": 41,
        "status": RunStatus.FAILED,
        "dry_run": False,
        "digest": None,
        "stats": {},
        "errors": (),
        "notifications_sent": 0,
        "notifications_failed": 0,
        "error": None,
        "started_at": datetime(2026, 10, 7, tzinfo=UTC),
        "finished_at": None,
        "next_run_at": None,
        "retry_scheduled": False,
    }
    values.update(overrides)
    return RunResult(**values)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"error": "Boom\x1b[31m\nsecond line\x07"}, "Boom second line"),
        (
            {"errors": (RunError(stage="persist", error_class="OperationalError"),)},
            "OperationalError",
        ),
        ({}, "run failed"),
    ],
    ids=["stored-error-cleaned", "first-error-class", "fallback"],
)
def test_the_failure_reason_is_one_clean_stderr_line(
    run_cli: RunCli, overrides: dict[str, Any], reason: str
) -> None:
    async def fake_run(name: str, **kwargs: Any) -> RunResult:
        return _fake_result(**overrides)

    run_cli.monkeypatch.setattr(job_module, "run_job_by_name", fake_run)

    result = run_cli.invoke(["job", "run", "research"])

    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1] == f"run 41 failed: {reason}"
    assert "\x1b" not in result.stderr and "\x07" not in result.stderr
    assert _cell(result.stdout, "Duration") == "—"
    assert result.stdout.rstrip().endswith("cost $0.00")


def test_a_partial_run_reports_its_status_on_stderr_only(run_cli: RunCli) -> None:
    run_cli.fail_item_at_relevance()

    result = run_cli.invoke(["job", "run", "research", "--dry-run"])

    assert result.exit_code == 2
    assert "finished partial" not in result.stdout
    assert _cell(result.stdout, "Status") == "partial (dry run)"
    assert _cell(result.stdout, "Failed") == "1"


def test_a_failed_real_run_exits_1_and_sends_nothing(run_cli: RunCli) -> None:
    run_cli.fail_every_source()

    result = run_cli.invoke(["job", "run", "research"])

    assert result.exit_code == 1
    assert run_cli.notifier_calls == []
    assert _cell(result.stdout, "Status") == "failed"
    assert re.search(r"Notifications sent\s+0\b", result.stdout)
    assert "failed:" not in result.stdout


# --- secrets, document text and hostile input never reach the terminal --------------------


def test_a_failed_source_never_leaks_its_url_or_query(run_cli: RunCli) -> None:
    run_cli.env.ports.sources[source_key(FEED_URL)] = FetchError(
        "http_error", url=f"{FEED_URL}?api_key=SECRET-KEY", status=500
    )

    result = run_cli.invoke(["job", "run", "research", "--dry-run", "-v"])
    (run,) = run_cli.runs()
    shown = run_cli.invoke(["run", "show", str(run.id)])

    assert result.exit_code == 1
    assert "FetchError: source failed  (source #1, rss)" in shown.stdout
    everything = result.stdout + result.stderr + shown.stdout + shown.stderr
    assert "SECRET-KEY" not in everything
    assert FEED_URL not in everything


HOSTILE_TITLE = "[bold red]Boom[/bold red] \x1b[31mEvil\x1b[0m\x07\r\nInjected\x1b]0;pwned\x07"


def test_hostile_titles_are_printed_as_plain_single_line_text(run_cli: RunCli) -> None:
    first, *rest = make_candidates(3)
    hostile = replace(first, title=HOSTILE_TITLE, url=f"{first.url}?sig=SIGNED-TOKEN#frag")
    run_cli.env.ports.sources[source_key(FEED_URL)] = [hostile, *rest]

    def route(title: str) -> Any:
        return RuntimeError("secret document text") if "Boom" in title else relevance_reply()

    run_cli.env.provider._routes["relevance"] = route

    result = run_cli.invoke(["job", "run", "research", "--dry-run", "-v"])
    (run,) = run_cli.runs()
    shown = run_cli.invoke(["run", "show", str(run.id)])

    assert result.exit_code == 2
    expected = "[bold red]Boom[/bold red] Evil Injected"
    assert f"item: failed  {expected}  RuntimeError: item failed" in result.stderr.splitlines()
    assert f'"{expected}" — {first.url}' in shown.stdout
    everything = result.stdout + result.stderr + shown.stdout + shown.stderr
    for forbidden in ("\x1b", "\x07", "\r", "pwned", "SIGNED-TOKEN", "#frag", "secret document"):
        assert forbidden not in everything, forbidden


def test_a_raising_display_never_fails_the_run(run_cli: RunCli) -> None:
    def broken(self: RunProgressView, event: ProgressEvent) -> None:
        raise RuntimeError("display bug with secret text")

    run_cli.monkeypatch.setattr(RunProgressView, "__call__", broken)

    result = run_cli.invoke(["job", "run", "research", "--dry-run"])

    assert result.exit_code == 0, result.stderr
    assert DIGEST_TEXT in result.stdout
    assert _cell(result.stdout, "Relevant") == "3"
    assert "secret text" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


# --- could not start: database and settings errors (FR-014) -------------------------------


def test_a_database_error_exits_1_without_details(run_cli: RunCli, tmp_path: Path) -> None:
    empty = create_db_engine(f"sqlite:///{tmp_path}/empty-secretpw.sqlite")  # no schema
    run_cli.monkeypatch.setattr(
        job_module, "_make_service", lambda: JobService(session_factory(empty))
    )

    result = run_cli.invoke(["job", "run", "research", "--dry-run"])

    empty.dispose()
    _refusal(result, run_cli, "Error: database error (OperationalError); is the schema current?")
    assert len(result.stderr.strip().splitlines()) == 1
    assert "secretpw" not in result.stderr
    assert "SELECT" not in result.stderr


def test_invalid_settings_exit_1_and_name_the_field_only(
    run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job_module, "_run_deps", REAL_RUN_DEPS)
    monkeypatch.setenv("INVIO_SMTP_PORT", "SECRET-NOT-A-PORT")
    get_settings.cache_clear()

    result = run_cli.invoke(["job", "run", "research"])

    _refusal(result, run_cli, "Configuration error: invalid settings: smtp_port")
    assert "SECRET-NOT-A-PORT" not in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"INVIO_LOG_LEVEL": "LOUD"}, "invalid log level"),
        ({"INVIO_ENV_FILE": "/does/not/exist.env"}, "INVIO_ENV_FILE points to a missing file"),
    ],
)
def test_a_runtime_configuration_error_exits_1_for_job_run_and_2_elsewhere(
    run_cli: RunCli, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str
) -> None:
    """FR-014: for ``job run`` 2 means only partial; the other commands keep 2."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    ran = run_cli.invoke(["job", "run", "research", "--dry-run"])
    listed = run_cli.invoke(["job", "list"])
    history = run_cli.invoke(["run", "list"])

    _refusal(ran, run_cli, expected)
    assert ran.stderr.startswith("Configuration error:")
    assert (listed.exit_code, history.exit_code) == (2, 2)
    assert expected in listed.stderr
    assert expected in history.stderr


def test_a_settings_error_inside_the_run_wiring_names_the_field_only(run_cli: RunCli) -> None:
    from invio.config.settings import Settings

    def invalid_deps() -> Any:
        return Settings(smtp_port="SECRET-NOT-A-PORT")  # type: ignore[arg-type]

    run_cli.monkeypatch.setattr(job_module, "_run_deps", invalid_deps)

    result = run_cli.invoke(["job", "run", "research"])

    _refusal(result, run_cli, "Configuration error: invalid settings: smtp_port")
    assert "SECRET-NOT-A-PORT" not in result.stderr


def test_an_unusable_database_url_is_a_configuration_error(
    run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job_module, "_run_deps", REAL_RUN_DEPS)
    monkeypatch.setenv("INVIO_DATABASE_URL", "nosuchdialect://user:SECRETPW@host/db")
    get_settings.cache_clear()

    result = run_cli.invoke(["job", "run", "research"])

    _refusal(result, run_cli, "Configuration error: invalid database URL")
    assert "SECRETPW" not in result.stderr


def test_a_broken_model_registry_is_a_configuration_error(
    run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> None:
        raise ModelRegistryError("registry file broken")

    monkeypatch.setattr(deps_module, "default_registry", broken)
    monkeypatch.setattr(job_module, "_run_deps", REAL_RUN_DEPS)
    get_settings.cache_clear()

    result = run_cli.invoke(["job", "run", "research"])

    _refusal(result, run_cli, "Configuration error: registry file broken")


def test_a_job_disabled_after_the_check_is_refused_without_a_run(run_cli: RunCli) -> None:
    """The pipeline re-checks ``enabled`` under the lock (a race with ``job disable``)."""
    record = JobService(run_cli.env.factory).get_by_name("research")
    _set_job(run_cli, enabled=False)

    class StaleService:
        def get_by_name(self, name: str) -> JobRecord:
            return replace(record, enabled=True)

    run_cli.monkeypatch.setattr(job_module, "_make_service", StaleService)

    result = run_cli.invoke(["job", "run", "research"])

    _refusal(result, run_cli, "Error: job 'research' is disabled; enable it with")


# --- --max-items: validation, stored config, baseline mode (FR-010) ------------------------


@pytest.mark.parametrize("value", ["0", "-1", "-100"])
def test_non_positive_max_items_are_refused_before_the_database(
    run_cli: RunCli, value: str
) -> None:
    def no_service() -> JobService:
        raise AssertionError("the service must not be reached")

    run_cli.monkeypatch.setattr(job_module, "_make_service", no_service)

    result = run_cli.invoke(["job", "run", "research", "--max-items", value])

    _refusal(result, run_cli, "Error: --max-items must be at least 1")


def test_max_items_never_changes_the_stored_config(run_cli: RunCli) -> None:
    before = run_cli.job_row().config

    assert (
        run_cli.invoke(["job", "run", "research", "--dry-run", "--max-items", "1"]).exit_code == 0
    )
    assert (
        run_cli.invoke(["job", "run", "research", "--dry-run", "--max-items", "1000"]).exit_code
        == 0
    )

    assert run_cli.job_row().config == before


def test_max_items_caps_a_real_run_in_baseline_mode(run_cli: RunCli) -> None:
    """A new job takes ``baseline_items`` per source; ``--max-items`` cuts that further."""
    run_cli.with_candidates(25)  # more than the default baseline_items (10) and per-source cap

    result = run_cli.invoke(["job", "run", "research", "--max-items", "3"])

    assert result.exit_code == 0, result.stderr
    assert _cell(result.stdout, "Processed") == "3"
    assert _cell(result.stdout, "Relevant") == "3"
    assert len(run_cli.env.provider.requests_for("relevance")) == 3

    def count(*statuses: ItemStatus) -> int | None:
        with session_scope(run_cli.env.factory) as session:
            return session.scalar(
                select(func.count()).select_from(Item).where(Item.status.in_(statuses))
            )

    # Baseline mode was active: 20 reported (max_items_per_source), 10 kept (baseline_items).
    assert count(ItemStatus.SKIPPED_BASELINE) == 10
    assert count(ItemStatus.RELEVANT, ItemStatus.SUMMARIZED) == 3


# --- non-TTY width -------------------------------------------------------------------------


def test_tables_are_not_wrapped_on_a_narrow_pipe(run_cli: RunCli) -> None:
    result = run_cli.runner.invoke(
        job_module.app, ["run", "research", "--dry-run"], env={"COLUMNS": "20"}
    )

    assert result.exit_code == 0, result.stderr
    assert re.search(r"^\s*After keyword filter\s+3\s*$", result.stdout, re.MULTILINE)
    assert re.search(r"^\s*Status\s+succeeded \(dry run\)\s*$", result.stdout, re.MULTILINE)


def test_a_failed_real_run_exits_1_and_schedules_the_retry(run_cli: RunCli) -> None:
    run_cli.fail_every_source()

    result = run_cli.invoke(["job", "run", "research"])

    assert result.exit_code == 1
    assert run_cli.job_row().next_run_at == run_cli.clock() + timedelta(hours=1)
    assert run_cli.job_row().locked_until is None
