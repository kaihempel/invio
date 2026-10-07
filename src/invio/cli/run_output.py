"""Pure formatting for ``invio job run`` and ``invio run``: no I/O, no database.

Every function returns text or a Rich table; the commands decide where it is printed. Titles,
URLs and messages come from the network or the database, so they are stripped of control
characters and ANSI escapes before they are shown (and tables use ``Text``, never markup).
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from zoneinfo import ZoneInfo

from rich.console import Group
from rich.table import Table
from rich.text import Text

from invio.domain import RunStatus
from invio.services.runs import RunErrorView, RunSummary, stat_count
from invio.textsafe import strip_control

# The building blocks of ``stats_block`` (``stats_rows``, ``stats_table``, ``format_cost``,
# ``format_tokens``) are module-level for unit tests only; the commands use what is listed here.
__all__ = [
    "DASH",
    "format_duration",
    "format_errors",
    "format_time",
    "runs_table",
    "stats_block",
]

DASH: Final = "—"
_COST_STEP: Final = Decimal("0.0001")
_STAGE_WIDTH: Final = 17


def format_cost(stats: Mapping[str, Any]) -> str:
    """The cost of a run (research R10): exact, a lower bound, ``unknown`` or ``$0.00``."""
    calls = stat_count(stats, "llm_calls") or 0
    if calls == 0:
        return "$0.00"
    try:
        cost = Decimal(str(stats.get("estimated_cost_usd"))).quantize(_COST_STEP)
    except (InvalidOperation, ValueError):
        return "unknown"
    if stats.get("cost_complete") is True:
        return f"${cost}"
    unpriced = stat_count(stats, "llm_calls_unpriced")
    if unpriced is not None and unpriced >= calls:
        return "unknown"
    if not unpriced:  # unknown, or a row whose keys disagree: the count says nothing
        return f"≥ ${cost} (some calls without price)"
    noun = "call" if unpriced == 1 else "calls"
    return f"≥ ${cost} ({unpriced} {noun} without price)"


def format_tokens(stats: Mapping[str, Any]) -> str:
    """``in 48,211 · out 5,120 · total 53,331``."""
    given, produced, total = (
        stat_count(stats, k) or 0 for k in ("input_tokens", "output_tokens", "tokens")
    )
    return f"in {given:,} · out {produced:,} · total {total:,}"


def format_duration(start: datetime | None, end: datetime | None) -> str:
    """``12.3 s``; a dash while the run has not finished. Never negative."""
    if start is None or end is None:
        return DASH
    return f"{max((end - start).total_seconds(), 0.0):.1f} s"


def format_time(moment: datetime | None, tz: str) -> str:
    """``YYYY-MM-DD HH:MM <TZ abbr>`` in ``tz`` (a dash if unset)."""
    if moment is None:
        return DASH
    return moment.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")


def _processed(stats: Mapping[str, Any]) -> str:
    processed = stat_count(stats, "processed")
    if processed is not None:
        return str(processed)
    after_filter = stat_count(stats, "after_keyword_filter")
    if after_filter is None:
        return DASH
    return str(max(after_filter - (stat_count(stats, "skipped_budget") or 0), 0))


def stats_rows(
    *,
    run_id: int,
    status: RunStatus,
    dry_run: bool,
    stats: Mapping[str, Any],
    started_at: datetime | None,
    finished_at: datetime | None,
    notifications: tuple[int, int] | None,
) -> list[tuple[str, str]]:
    """The ``Metric | Value`` rows of a run, in the order of contracts/cli-commands.md."""

    def count(key: str) -> str:
        value = stat_count(stats, key)
        return str(value) if value is not None else DASH

    rows = [
        ("Run", str(run_id)),
        ("Status", f"{status.value} (dry run)" if dry_run else status.value),
        ("Found", count("found")),
        ("New", count("new")),
        ("After keyword filter", count("after_keyword_filter")),
        ("Processed", _processed(stats)),
        ("Relevant", count("relevant")),
        ("Summarized", count("summarized")),
        ("Failed", count("failed")),
    ]
    if (stat_count(stats, "skipped_budget") or 0) > 0:
        rows.append(("Skipped (budget)", count("skipped_budget")))
    failed_sources = stat_count(stats, "sources_failed") or 0
    if failed_sources > 0:
        rows.append(("Sources failed", f"{failed_sources}/{stat_count(stats, 'sources') or DASH}"))
    rows.append(("Duration", format_duration(started_at, finished_at)))
    if notifications is not None:
        rows.append(("Notifications sent", str(notifications[0])))
        rows.append(("Notifications failed", str(notifications[1])))
    return rows


def stats_table(rows: Sequence[tuple[str, str]]) -> Table:
    """Two columns, ``Metric`` and ``Value``."""
    table = Table(box=None)
    table.add_column("Metric", no_wrap=True)
    table.add_column("Value", no_wrap=True)
    for metric, value in rows:
        table.add_row(Text(metric), Text(value))
    return table


def stats_block(
    *,
    run_id: int,
    status: RunStatus,
    dry_run: bool,
    stats: Mapping[str, Any],
    started_at: datetime | None,
    finished_at: datetime | None,
    notifications: tuple[int, int] | None,
) -> Group:
    """The stats table, a blank line and the ``Tokens: ... · cost ...`` line of a run."""
    rows = stats_rows(
        run_id=run_id,
        status=status,
        dry_run=dry_run,
        stats=stats,
        started_at=started_at,
        finished_at=finished_at,
        notifications=notifications,
    )
    usage = f"Tokens: {format_tokens(stats)} · cost {format_cost(stats)}"
    return Group(stats_table(rows), Text(), Text(usage))


def _source_label(source: str) -> str:
    index, _, kind = source.partition(":")
    if index.isdecimal():
        return f"(source #{int(index) + 1}, {strip_control(kind) or '?'})"
    return f"(source {strip_control(source)})"


def format_errors(errors: tuple[RunErrorView, ...] | None, omitted: int) -> list[str]:
    """The ``Errors`` block of ``run show``, as lines."""
    if errors is None:
        return ["Errors: item errors are not available for this run"]
    if not errors:
        return ["Errors: none"]
    indent = " " * (2 + _STAGE_WIDTH)
    lines = [f"Errors ({len(errors)})"]
    for error in errors:
        text = error.message
        if not text.startswith(error.error_class):
            text = f"{error.error_class}: {text}"
        message = strip_control(text)
        head = f"  {strip_control(error.stage):<{_STAGE_WIDTH}}{message}"
        if error.source is not None:
            lines.append(f"{head}  {_source_label(error.source)}")
            continue
        lines.append(head)
        if error.title is not None or error.url is not None:
            title = strip_control(error.title or "")
            url = strip_control(error.url or "")
            item = f"#{error.item_id} " if error.item_id is not None else ""
            ref = f'{item}"{title}"'
            lines.append(f"{indent}{ref} — {url}" if url else f"{indent}{ref}")
    if omitted > 0:
        lines.append(f"  … and {omitted} more not stored")
    return lines


def runs_table(summaries: Sequence[RunSummary]) -> Table:
    """The ``run list`` table; a missing count is a dash."""

    def count(value: int | None) -> str:
        return str(value) if value is not None else DASH

    table = Table(box=None)
    for column in ("ID", "Job", "Started", "Duration", "Status", "Found", "New", "Relevant"):
        table.add_column(column, no_wrap=True)
    for run in summaries:
        status = f"{run.status.value} (dry)" if run.dry_run else run.status.value
        table.add_row(
            Text(str(run.id)),
            Text(strip_control(run.job_name)),
            Text(format_time(run.started_at, run.timezone)),
            Text(format_duration(run.started_at, run.finished_at)),
            Text(status),
            Text(count(run.found)),
            Text(count(run.new)),
            Text(count(run.relevant)),
        )
    return table
