"""CLI tests for ``invio run-due`` (contract: specs/016-gh-issue-23/contracts/cli-run-due.md).

The seams ``_run_due`` (the report) and ``_ping`` (the health check) are replaced, so no
database and no network are involved. stdout and stderr are read separately.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import httpx2
import pytest
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner, Result

from invio.cli import main as cli_main
from invio.cli.commands import run_due as run_due_module
from invio.config.settings import MissingSettingError, get_settings
from invio.domain import RunStatus
from invio.pipeline import healthcheck
from invio.pipeline.due import DueOutcome, DueReport

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
BERLIN = "Europe/Berlin"
NEXT = datetime(2026, 10, 8, 5, 0, tzinfo=UTC)  # 07:00 CEST


def ran(
    name: str, status: RunStatus, *, run_id: int = 1, retry: bool = False, tz: str = BERLIN
) -> DueOutcome:
    return DueOutcome(
        1, name, "ran", run_id=run_id, status=status, next_run_at=NEXT, retry=retry, timezone=tz
    )


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.report = DueReport(NOW, 0, ())
        self.error: BaseException | None = None
        self.calls: list[tuple[int | None, int]] = []
        self.pings: list[tuple[str, bool]] = []
        self.on_print: Callable[[], None] = lambda: None
        self.on_ping: Callable[[], None] = lambda: None
        monkeypatch.setattr(run_due_module, "_run_due", self._run_due)
        monkeypatch.setattr(run_due_module, "_ping", self._ping)
        real_print = run_due_module._print_report

        def printing(report: DueReport) -> None:
            real_print(report)
            self.on_print()

        monkeypatch.setattr(run_due_module, "_print_report", printing)
        self.runner = CliRunner()

    def _ping(self, url: str, failed: bool) -> None:
        self.pings.append((url, failed))
        self.on_ping()

    def _run_due(self, limit: int | None, parallel: int) -> DueReport:
        self.calls.append((limit, parallel))
        if self.error is not None:
            raise self.error
        return self.report

    def invoke(self, *args: str) -> Result:
        return self.runner.invoke(cli_main.app, ["run-due", *args], env={"COLUMNS": "200"})


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(monkeypatch)


def test_the_report_lines_follow_the_contract(harness: Harness) -> None:
    harness.report = DueReport(
        NOW,
        6,
        (
            ran("daily-ai", RunStatus.SUCCEEDED, run_id=412),
            ran("weekly-rust", RunStatus.FAILED, run_id=413, retry=True),
            DueOutcome(3, "papers", "busy", locked_until=NOW, timezone=BERLIN),
            DueOutcome(4, "old-feed", "skipped", reason="no longer due"),
            DueOutcome(5, "news-de", "error", reason="OperationalError"),
            ran("mixed", RunStatus.PARTIAL, run_id=414),
        ),
    )

    result = harness.invoke()

    lines = [" ".join(line.split()) for line in result.stdout.splitlines()]
    assert lines == [
        "ran daily-ai run 412 succeeded next 2026-10-08 07:00 CEST",
        "ran weekly-rust run 413 failed next 2026-10-08 07:00 CEST (retry)",
        "busy papers locked until 2026-10-07 14:00 CEST",
        "skipped old-feed no longer due",
        "error news-de OperationalError",
        "ran mixed run 414 partial next 2026-10-08 07:00 CEST",
        "due 6 · ran 3 (1 succeeded, 1 partial, 1 failed) · busy 1 · skipped 1 · error 1",
    ]
    assert result.exit_code == 1


def test_columns_are_aligned(harness: Harness) -> None:
    harness.report = DueReport(
        NOW,
        2,
        (ran("a", RunStatus.SUCCEEDED, run_id=7), ran("long-name", RunStatus.FAILED, run_id=12)),
    )

    first, second, _ = harness.invoke().stdout.splitlines()

    assert first.index("run 7") == second.index("run 12")


def test_the_error_count_is_left_out_when_zero(harness: Harness) -> None:
    harness.report = DueReport(NOW, 1, (ran("a", RunStatus.SUCCEEDED),))

    summary = harness.invoke().stdout.splitlines()[-1]

    assert summary == "due 1 · ran 1 (1 succeeded, 0 partial, 0 failed) · busy 0 · skipped 0"


def test_jobs_deferred_by_the_limit_are_counted_in_the_summary(harness: Harness) -> None:
    harness.report = DueReport(NOW, 5, (ran("a", RunStatus.SUCCEEDED),), deferred=4)

    result = harness.invoke("--limit", "1")

    assert result.stdout.splitlines()[-1].endswith(" · deferred 4 (--limit)")
    assert harness.calls == [(1, 1)]


def test_nothing_due_prints_exactly_that(harness: Harness) -> None:
    result = harness.invoke()

    assert (result.stdout, result.exit_code) == ("nothing due\n", 0)


@pytest.mark.parametrize(
    ("outcomes", "code"),
    [
        ((ran("a", RunStatus.SUCCEEDED), ran("b", RunStatus.PARTIAL)), 0),
        ((DueOutcome(1, "a", "busy"), DueOutcome(2, "b", "skipped", reason="deleted")), 0),
        ((ran("a", RunStatus.SUCCEEDED), ran("b", RunStatus.FAILED)), 1),
        ((DueOutcome(1, "a", "error", reason="OperationalError"),), 1),
    ],
)
def test_exit_codes(harness: Harness, outcomes: tuple[DueOutcome, ...], code: int) -> None:
    harness.report = DueReport(NOW, len(outcomes), outcomes)

    assert harness.invoke().exit_code == code


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--limit", "0"), "Error: --limit must be at least 1"),
        (("--limit", "-3"), "Error: --limit must be at least 1"),
        (("--parallel", "0"), "Error: --parallel must be at least 1"),
        (("--parallel", "-2"), "Error: --parallel must be at least 1"),
        (("--limit", "2", "--parallel", "0"), "Error: --parallel must be at least 1"),
    ],
)
def test_invalid_options_exit_2_without_running_or_pinging(
    harness: Harness, args: tuple[str, ...], message: str
) -> None:
    result = harness.invoke(*args)

    assert result.exit_code == 2
    assert message in result.stderr
    assert (harness.calls, harness.pings) == ([], [])


@pytest.mark.parametrize(
    ("option", "value"), [("--limit", "abc"), ("--parallel", "two"), ("--parallel", "1.5")]
)
def test_a_non_numeric_option_is_a_usage_error(harness: Harness, option: str, value: str) -> None:
    result = harness.invoke(option, value)

    assert result.exit_code == 2
    assert option in result.stderr
    assert (harness.calls, harness.pings) == ([], [])


def test_limit_and_parallel_reach_the_pipeline(harness: Harness) -> None:
    harness.invoke("--limit", "2", "--parallel", "3")

    assert harness.calls == [(2, 3)]


def test_without_options_every_due_job_runs_one_after_another(harness: Harness) -> None:
    harness.invoke()

    assert harness.calls == [(None, 1)]


def test_a_database_error_exits_1_with_the_class_name_only(harness: Harness) -> None:
    harness.error = OperationalError("SELECT secret", {}, Exception("mysql://u:pw@h/db"))

    result = harness.invoke()

    assert result.exit_code == 1
    assert "OperationalError" in result.stderr
    assert "pw" not in result.stderr + result.stdout


def test_a_missing_setting_exits_2(harness: Harness) -> None:
    harness.error = MissingSettingError("database_url")

    result = harness.invoke()

    assert result.exit_code == 2
    assert "Configuration error" in result.stderr


def test_job_names_lose_their_control_characters(harness: Harness) -> None:
    harness.report = DueReport(
        NOW, 1, (DueOutcome(1, "evil\x1b[31mname\x07", "skipped", reason="deleted\x00"),)
    )

    out = harness.invoke().stdout

    assert "\x1b" not in out and "\x07" not in out and "\x00" not in out


def test_run_due_is_listed_in_the_help() -> None:
    runner = CliRunner()

    top = runner.invoke(cli_main.app, ["--help"])
    sub = runner.invoke(cli_main.app, ["run-due", "--help"])

    assert "run-due" in top.stdout
    assert "--limit" in sub.stdout and "--parallel" in sub.stdout
    assert "INVIO_MAX_PARALLEL_ITEMS" in " ".join(sub.stdout.split())


# --- the health-check signal ----------------------------------------------------------------------

HC_URL = "https://hc.example.com/ping/0a1b2c3d"


@pytest.fixture
def monitored(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setenv("INVIO_HEALTHCHECK_URL", HC_URL)
    get_settings.cache_clear()
    return harness


def test_a_clean_invocation_pings_success_once_after_the_report(monitored: Harness) -> None:
    monitored.report = DueReport(NOW, 1, (ran("a", RunStatus.SUCCEEDED),))
    order: list[str] = []
    monitored.on_print = lambda: order.append("report")
    monitored.on_ping = lambda: order.append("ping")

    result = monitored.invoke()

    assert result.exit_code == 0
    assert monitored.pings == [(HC_URL, False)]
    assert order == ["report", "ping"]


def test_a_failed_run_pings_fail_and_exits_1(monitored: Harness) -> None:
    monitored.report = DueReport(NOW, 1, (ran("a", RunStatus.FAILED),))

    result = monitored.invoke()

    assert result.exit_code == 1
    assert monitored.pings == [(HC_URL, True)]


def test_nothing_due_pings_success(monitored: Harness) -> None:
    assert monitored.invoke().exit_code == 0
    assert monitored.pings == [(HC_URL, False)]


def test_a_database_error_pings_fail(monitored: Harness) -> None:
    monitored.error = OperationalError("SELECT", {}, Exception("gone"))

    result = monitored.invoke()

    assert result.exit_code == 1
    assert monitored.pings == [(HC_URL, True)]


def test_a_configuration_error_after_loading_the_settings_pings_fail(monitored: Harness) -> None:
    monitored.error = MissingSettingError("database_url")

    result = monitored.invoke()

    assert result.exit_code == 2
    assert monitored.pings == [(HC_URL, True)]


def test_an_unexpected_exception_pings_fail_and_is_logged_by_class(monitored: Harness) -> None:
    monitored.error = RuntimeError("secret mysql://u:pw@h/db")

    result = monitored.invoke()

    assert result.exit_code == 1
    assert monitored.pings == [(HC_URL, True)]
    assert "pw@h" not in result.stderr + result.stdout
    assert "run_due.aborted" in result.stderr
    assert "RuntimeError" in result.stderr


def test_ctrl_c_pings_fail_and_exits_1(monitored: Harness) -> None:
    monitored.error = KeyboardInterrupt()

    result = monitored.invoke()

    assert result.exit_code == 1
    assert monitored.pings == [(HC_URL, True)]


@pytest.mark.parametrize("args", [("--parallel", "0"), ("--limit", "0"), ("--limit", "abc")])
def test_usage_errors_send_no_ping(monitored: Harness, args: tuple[str, ...]) -> None:
    monitored.invoke(*args)

    assert monitored.pings == []


@pytest.mark.parametrize("value", ["ftp://hc.example.com/0a1b2c3d", "hc.example.com/0a1b2c3d"])
def test_an_invalid_url_exits_2_without_running_pinging_or_echoing_it(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("INVIO_HEALTHCHECK_URL", value)
    get_settings.cache_clear()

    result = harness.invoke()

    assert result.exit_code == 2
    assert "healthcheck_url" in result.stderr
    assert "0a1b2c3d" not in result.stderr + result.stdout
    assert (harness.calls, harness.pings) == ([], [])


def test_unreadable_settings_exit_2_without_a_signal(
    monitored: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INVIO_LOG_LEVEL", "LOUD")
    get_settings.cache_clear()

    result = monitored.invoke()

    assert result.exit_code == 2
    assert (monitored.calls, monitored.pings) == ([], [])


def test_a_job_error_outcome_pings_fail(monitored: Harness) -> None:
    monitored.report = DueReport(NOW, 1, (DueOutcome(1, "a", "error", reason="RuntimeError"),))

    assert monitored.invoke().exit_code == 1
    assert monitored.pings == [(HC_URL, True)]


def test_busy_and_partial_runs_ping_success(monitored: Harness) -> None:
    monitored.report = DueReport(
        NOW, 2, (DueOutcome(1, "a", "busy", locked_until=NOW), ran("b", RunStatus.PARTIAL))
    )

    assert monitored.invoke().exit_code == 0
    assert monitored.pings == [(HC_URL, False)]


@pytest.mark.parametrize("outcome", ["clean", "failed", "aborted"])
def test_without_a_url_nothing_is_pinged(harness: Harness, outcome: str) -> None:
    if outcome == "failed":
        harness.report = DueReport(NOW, 1, (ran("a", RunStatus.FAILED),))
    elif outcome == "aborted":
        harness.error = OperationalError("SELECT", {}, Exception("gone"))

    harness.invoke()

    assert harness.pings == []


def test_a_failing_ping_leaves_the_exit_code_and_the_report_alone(
    monitored: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitored.report = DueReport(NOW, 1, (ran("a", RunStatus.SUCCEEDED),))
    transport = httpx2.MockTransport(lambda request: httpx2.Response(500))
    monkeypatch.setattr(
        run_due_module,
        "_ping",
        lambda url, failed: healthcheck.ping(url, failed=failed, transport=transport),
    )

    result = monitored.invoke()

    assert result.exit_code == 0
    assert result.stdout.splitlines()[0].startswith("ran")
    assert "healthcheck.failed" in result.stderr
    assert "0a1b2c3d" not in result.stderr
