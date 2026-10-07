"""Pure formatters of ``invio.cli.run_output`` (cost, tokens, durations, stats rows)."""

import io
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from rich.console import Console
from rich.table import Table

from invio.cli.run_output import (
    format_cost,
    format_duration,
    format_errors,
    format_time,
    format_tokens,
    runs_table,
    stats_rows,
    stats_table,
)
from invio.domain import RunStatus
from invio.services.runs import RunErrorView, RunSummary

START = datetime(2026, 10, 7, 4, 0, tzinfo=UTC)


def test_cost_without_llm_calls_is_zero() -> None:
    assert format_cost({"llm_calls": 0}) == "$0.00"


def test_cost_of_a_complete_run_has_four_decimals() -> None:
    stats = {"llm_calls": 3, "cost_complete": True, "estimated_cost_usd": "0.010305"}
    assert format_cost(stats) == "$0.0103"


def test_cost_with_some_unpriced_calls_is_a_lower_bound() -> None:
    stats = {
        "llm_calls": 5,
        "llm_calls_unpriced": 2,
        "cost_complete": False,
        "estimated_cost_usd": "0.005",
    }
    assert format_cost(stats) == "≥ $0.0050 (2 calls without price)"


def test_cost_with_only_unpriced_calls_is_unknown() -> None:
    stats = {
        "llm_calls": 4,
        "llm_calls_unpriced": 4,
        "cost_complete": False,
        "estimated_cost_usd": "0",
    }
    assert format_cost(stats) == "unknown"


def test_cost_of_a_legacy_run_without_the_unpriced_count() -> None:
    stats = {"llm_calls": 5, "cost_complete": False, "estimated_cost_usd": "0.005"}
    assert format_cost(stats) == "≥ $0.0050 (some calls without price)"


def test_cost_never_reports_zero_calls_without_price_for_an_incomplete_run() -> None:
    stats = {
        "llm_calls": 5,
        "llm_calls_unpriced": 0,  # disagrees with cost_complete: the count says nothing
        "cost_complete": False,
        "estimated_cost_usd": "0.005",
    }
    assert format_cost(stats) == "≥ $0.0050 (some calls without price)"


def test_cost_tolerates_missing_and_garbage_values() -> None:
    assert format_cost({}) == "$0.00"
    assert format_cost({"llm_calls": 2, "estimated_cost_usd": "oops"}) == "unknown"


def test_tokens() -> None:
    stats = {"input_tokens": 48211, "output_tokens": 5120, "tokens": 53331}
    assert format_tokens(stats) == "in 48,211 · out 5,120 · total 53,331"


def test_tokens_default_to_zero() -> None:
    assert format_tokens({}) == "in 0 · out 0 · total 0"


def test_duration() -> None:
    assert format_duration(START, START + timedelta(seconds=12.34)) == "12.3 s"


def test_duration_of_an_unfinished_run_is_a_dash() -> None:
    assert format_duration(START, None) == "—"
    assert format_duration(None, START) == "—"


def test_duration_is_never_negative() -> None:
    assert format_duration(START, START - timedelta(seconds=5)) == "0.0 s"


def test_time_is_shown_in_the_zone() -> None:
    assert format_time(START, "Europe/Berlin") == "2026-10-07 06:00 CEST"
    assert format_time(None, "UTC") == "—"


STATS: dict[str, Any] = {
    "found": 12,
    "new": 5,
    "after_keyword_filter": 4,
    "processed": 4,
    "relevant": 2,
    "summarized": 2,
    "failed": 1,
    "skipped_budget": 0,
    "sources": 2,
    "sources_failed": 0,
}


def _rows(**overrides: Any) -> dict[str, str]:
    args: dict[str, Any] = {
        "run_id": 7,
        "status": RunStatus.SUCCEEDED,
        "dry_run": False,
        "stats": STATS,
        "started_at": START,
        "finished_at": START + timedelta(seconds=2),
        "notifications": None,
    }
    args.update(overrides)
    return dict(stats_rows(**args))


def test_rows_in_contract_order() -> None:
    rows = stats_rows(
        run_id=7,
        status=RunStatus.PARTIAL,
        dry_run=True,
        stats=STATS,
        started_at=START,
        finished_at=START + timedelta(seconds=2),
        notifications=None,
    )

    assert rows == [
        ("Run", "7"),
        ("Status", "partial (dry run)"),
        ("Found", "12"),
        ("New", "5"),
        ("After keyword filter", "4"),
        ("Processed", "4"),
        ("Relevant", "2"),
        ("Summarized", "2"),
        ("Failed", "1"),
        ("Duration", "2.0 s"),
    ]


def test_optional_rows_appear_only_when_positive() -> None:
    rows = _rows(stats={**STATS, "skipped_budget": 3, "sources_failed": 1})

    assert rows["Skipped (budget)"] == "3"
    assert rows["Sources failed"] == "1/2"
    assert "Skipped (budget)" not in _rows()
    assert "Sources failed" not in _rows()


def test_notification_rows_only_for_real_runs() -> None:
    rows = _rows(notifications=(1, 0))

    assert rows["Notifications sent"] == "1"
    assert rows["Notifications failed"] == "0"
    assert "Notifications sent" not in _rows(notifications=None)


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        ({"after_keyword_filter": 5, "skipped_budget": 2}, "3"),
        ({"after_keyword_filter": 5}, "5"),
        ({}, "—"),
        ({"processed": 0, "after_keyword_filter": 5}, "0"),
    ],
)
def test_processed_falls_back_for_legacy_stats(stats: dict[str, Any], expected: str) -> None:
    assert _rows(stats=stats)["Processed"] == expected


def test_missing_keys_render_as_a_dash() -> None:
    rows = _rows(stats={}, finished_at=None)

    assert rows["Found"] == rows["Relevant"] == rows["Duration"] == "—"


def test_the_table_has_metric_and_value_columns() -> None:
    table = stats_table([("Run", "7")])

    assert [c.header for c in table.columns] == ["Metric", "Value"]


# --- format_errors / runs_table ------------------------------------------------------------


def _render(table: Table) -> str:
    console = Console(file=io.StringIO(), width=300, markup=False, highlight=False)
    console.print(table)
    return console.file.getvalue()  # type: ignore[attr-defined]


def test_errors_of_a_legacy_run_and_an_empty_list() -> None:
    assert format_errors(None, 0) == ["Errors: item errors are not available for this run"]
    assert format_errors((), 0) == ["Errors: none"]


def test_error_lines_with_title_url_source_and_omitted() -> None:
    errors = (
        RunErrorView(
            stage="score_relevance",
            error_class="LLMInvalidOutputError",
            message="LLMInvalidOutputError: bad answer",
            item_id=1,
            title="A title",
            url="https://example.org/a",
        ),
        RunErrorView(stage="extract_text", error_class="KeyError", message="KeyError", item_id=2),
        RunErrorView(
            stage="summarize_item", error_class="E", message="lost", item_id=3, title="Only title"
        ),
        RunErrorView(
            stage="fetch_sources",
            error_class="FetchError",
            message="FetchError: source failed",
            source="0:",
        ),
    )

    lines = format_errors(errors, omitted=2)

    indent = " " * 19
    assert lines == [
        "Errors (4)",
        "  score_relevance  LLMInvalidOutputError: bad answer",
        f'{indent}"A title" — https://example.org/a',
        "  extract_text     KeyError",
        "  summarize_item   E: lost",
        f'{indent}"Only title"',
        "  fetch_sources    FetchError: source failed  (source #1, ?)",
        "  … and 2 more not stored",
    ]


def test_error_lines_never_carry_control_characters() -> None:
    error = RunErrorView(
        stage="persist\x1b[2J",
        error_class="E",
        message="E: a\nb",
        item_id=1,
        title="\x1b]0;x\x07T",
        url="https://example.org/\x07p",
        source=None,
    )

    text = "\n".join(format_errors((error,), 0))

    assert "\x1b" not in text and "\x07" not in text
    assert "E: a b" in text
    assert '"T" — https://example.org/ p' in text


def _summary(**overrides: Any) -> RunSummary:
    values: dict[str, Any] = {
        "id": 3,
        "job_name": "daily",
        "status": RunStatus.SUCCEEDED,
        "dry_run": False,
        "started_at": START,
        "finished_at": START + timedelta(seconds=63.24),
        "found": 12,
        "new": 5,
        "relevant": 2,
        "timezone": "Europe/Berlin",
    }
    values.update(overrides)
    return RunSummary(**values)


def test_runs_table_rows() -> None:
    table = runs_table(
        [
            _summary(),
            _summary(id=2, dry_run=True, status=RunStatus.PARTIAL),
            _summary(id=1, finished_at=None, found=None, new=None, relevant=None, timezone="UTC"),
        ]
    )

    lines = [line.split() for line in _render(table).splitlines()]
    assert lines[0] == ["ID", "Job", "Started", "Duration", "Status", "Found", "New", "Relevant"]
    assert lines[1] == [
        "3", "daily", "2026-10-07", "06:00", "CEST", "63.2", "s", "succeeded", "12", "5", "2"
    ]  # fmt: skip
    assert lines[2][-5:] == ["partial", "(dry)", "12", "5", "2"]
    assert lines[3][2:] == ["2026-10-07", "04:00", "UTC", "—", "succeeded", "—", "—", "—"]


def test_runs_table_prints_names_as_plain_text() -> None:
    rendered = _render(runs_table([_summary(job_name="[red]x[/red]\x1b[2J")]))

    assert "[red]x[/red]" in rendered
    assert "\x1b" not in rendered
