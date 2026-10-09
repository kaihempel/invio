"""``invio source``: find sources for a research job.

``invio source discover URL`` looks for the feeds, sitemaps and YouTube channels a website
offers and prints them as numbered entries with a paste-ready YAML snippet. Results go to
stdout; diagnostics go to stderr. Exit codes: 0 at least one source found, 1 none found, 2
invalid usage or configuration (see ``specs/019-gh-issue-36/contracts/cli-source-discover.md``).
The module-level ``_make_*``, ``_discovery_deps`` and ``_is_interactive`` functions are test
seams.
"""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import typer
import yaml

from invio.cli.errors import fail, mapped_errors
from invio.cli.prompts import Prompter, QuestionaryPrompter, WizardAborted
from invio.config.job import YoutubeChannelSource, YoutubePlaylistSource
from invio.config.settings import get_settings
from invio.services.jobs import JobNameError, JobService, SourceExistsError, check_job_name
from invio.sources.discover import (
    DiscoveredSource,
    DiscoveryReport,
    DiscoveryTarget,
    Rejection,
    discover,
    parse_target,
)
from invio.sources.http import HttpClientConfig, SafeHttpClient
from invio.sources.urls import redact_url, without_query
from invio.sources.youtube import YoutubeSource
from invio.textsafe import strip_control

app = typer.Typer(help="Find and add sources.", no_args_is_help=True)

_CHECKED = "page links, common locations, robots.txt sitemaps, YouTube links"
_SNIPPET_INDENT = " " * 4
_CANCEL = "Cancel"
_SHOWN_LIMIT = 200  # characters of a remote title, locator or URL that are printed


# --- test seams ------------------------------------------------------------------------------


def _make_service() -> JobService:
    return JobService.from_settings()


def _make_prompter() -> Prompter:
    return QuestionaryPrompter()


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


@asynccontextmanager
async def _discovery_deps() -> AsyncIterator[tuple[SafeHttpClient, YoutubeSource]]:
    """The production HTTP client and YouTube adapter, closed on exit; tests replace this."""
    settings = get_settings()
    youtube = YoutubeSource.from_settings(settings)
    try:
        async with SafeHttpClient(HttpClientConfig.from_settings(settings)) as client:
            yield client, youtube
    finally:
        youtube.close()


# --- discover --------------------------------------------------------------------------------


@app.command("discover")
def discover_sources(
    url: Annotated[
        str,
        typer.Argument(
            metavar="URL",
            help="Website, page, feed, sitemap or YouTube address (http or https).",
        ),
    ],
    add_to: Annotated[
        str | None,
        typer.Option("--add-to", metavar="JOB", help="Append one found source to this job."),
    ] = None,
    pick: Annotated[
        int | None,
        typer.Option("--pick", min=1, metavar="N", help="With --add-to: the source N to add."),
    ] = None,
) -> None:
    """Find the feeds, sitemaps and YouTube channels that URL offers.

    Prints each source as a numbered entry with a YAML snippet for the sources list of a job.
    With --add-to, one of them is appended to an existing job after the job is re-validated.
    """
    try:
        target = parse_target(url)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="URL") from exc
    if pick is not None and add_to is None:
        raise typer.BadParameter("--pick needs --add-to", param_hint="--pick")
    job = (_load_job(add_to), add_to) if add_to is not None else None
    with mapped_errors(config_exit=2):
        report = asyncio.run(_discover(target))
    _print_report(report)
    if not report.sources:
        raise fail(f"No source found for {_shown(target.url)} (checked: {_CHECKED}).", 1)
    if job is not None:
        _add(*job, report.sources, pick)


def _load_job(name: str) -> JobService:
    """The job service, after checking that job ``name`` exists and is valid (no network yet)."""
    try:
        check_job_name(name)
    except JobNameError as exc:
        raise fail(f"Error: invalid job name '{exc.name}': {exc.rule}", 1) from exc
    with mapped_errors(config_exit=2):
        service = _make_service()
        service.get_by_name(name)
    return service


async def _discover(target: DiscoveryTarget) -> DiscoveryReport:
    async with _discovery_deps() as (client, youtube):
        return await discover(target, client=client, youtube=youtube)


# --- --add-to --------------------------------------------------------------------------------


def _add(
    service: JobService, job: str, sources: tuple[DiscoveredSource, ...], pick: int | None
) -> None:
    """Choose one of ``sources`` (``--pick``, the only one or a prompt) and append it to ``job``."""
    number = _choose(job, sources, pick)
    if number is None:
        typer.echo(f"Nothing added to job '{job}'.")
        return
    chosen = sources[number - 1]
    try:
        with mapped_errors(config_exit=2):
            service.append_source(job, chosen.source)
    except SourceExistsError:
        typer.echo(f"Job '{job}' already contains this source; nothing changed.")
        return
    typer.echo(f"Added {_label(number, chosen)} to job '{job}'.")


def _choose(job: str, sources: tuple[DiscoveredSource, ...], pick: int | None) -> int | None:
    """The 1-based number of the source to add; ``None`` when the operator cancels."""
    count = len(sources)
    if pick is not None:
        if pick > count:
            raise fail(f"--pick {pick} is out of range; choose 1–{count}", 1)  # noqa: RUF001
        return pick
    if not _is_interactive():
        if count == 1:
            return 1
        raise fail("several sources found; choose one with --pick N", 1)
    labels = [_label(number, found) for number, found in enumerate(sources, start=1)]
    try:
        answer = _make_prompter().select(f"Add which source to job '{job}'?", [*labels, _CANCEL])
    except (WizardAborted, KeyboardInterrupt, EOFError):
        return None
    return None if answer == _CANCEL else labels.index(answer) + 1


def _label(number: int, found: DiscoveredSource) -> str:
    return f"[{number}] {found.source.type} {_locator(found)}"


# --- output ----------------------------------------------------------------------------------


def _print_report(report: DiscoveryReport) -> None:
    """The numbered entries on stdout, then the diagnostics on stderr."""
    for number, found in enumerate(report.sources, start=1):
        typer.echo(_entry(number, found))
        typer.echo("")
    if report.page_problem is not None:
        problem = report.page_problem
        typer.echo(f"Start page not usable: {problem.reason}: {_shown(problem.locator)}", err=True)
    for rejection in report.rejected:
        typer.echo(_not_offered(rejection), err=True)
    for note in report.skipped:
        typer.echo(f"Skipped: {strip_control(note)}", err=True)


def _entry(number: int, found: DiscoveredSource) -> str:
    """One numbered entry: headline, summary line and the indented YAML list item."""
    marker = " (comments)" if found.is_comment_feed else ""
    headline = f"[{number}] {found.source.type}{marker}  {_locator(found)}"
    return "\n".join([headline, f"{_SNIPPET_INDENT}{_summary(found)}", _snippet(found)])


def _locator(found: DiscoveredSource) -> str:
    """What identifies the source: its URL, or the channel/playlist value of a YouTube source."""
    source = found.source
    if isinstance(source, YoutubeChannelSource):
        return strip_control(source.channel_id, limit=_SHOWN_LIMIT)
    if isinstance(source, YoutubePlaylistSource):
        return strip_control(source.playlist_id, limit=_SHOWN_LIMIT)
    return strip_control(found.final_url, limit=_SHOWN_LIMIT)


def _summary(found: DiscoveredSource) -> str:
    """``title: … · newest entry: … · found: …``; sitemaps count their URLs instead of a title."""
    if found.source.type == "sitemap":
        count = found.entry_count
        kind = (
            f"sitemap index: {count} sitemaps" if found.sitemap_index else f"sitemap: {count} URLs"
        )
        parts = [kind]
    else:
        parts = (
            []
            if found.title is None
            else [f"title: {strip_control(found.title, limit=_SHOWN_LIMIT)}"]
        )
    parts.append(f"newest entry: {_newest(found)}")
    parts.append(f"found: {found.origin.name.lower()}")
    return " · ".join(parts)


def _newest(found: DiscoveredSource) -> str:
    if found.entry_count == 0:
        return "no entries"
    return found.newest.date().isoformat() if found.newest is not None else "unknown"


def _snippet(found: DiscoveredSource) -> str:
    """The source as an entry for the ``sources:`` list of a job file (required fields only)."""
    data = found.source.model_dump(mode="json", exclude_defaults=True)
    text = yaml.safe_dump([data], sort_keys=False)
    return "\n".join(f"{_SNIPPET_INDENT}{line}" for line in text.splitlines())


def _not_offered(rejection: Rejection) -> str:
    line = f"Not offered: {_shown(rejection.locator)}: {rejection.reason}"
    return line if rejection.status is None else f"{line} (HTTP {rejection.status})"


def _shown(text: str) -> str:
    """``text`` (a URL) without credentials, query string and control characters."""
    return strip_control(without_query(redact_url(text)), limit=_SHOWN_LIMIT)
