"""CliRunner tests for ``invio source discover`` (the web is a scripted ``Site``; no network)."""

import re
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
import yaml
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner, Result

from invio.cli import main as cli_main
from invio.cli.commands import source as source_module
from invio.config.job import JobConfigError, validate_job
from invio.config.settings import MissingSettingError
from invio.db.models import Job
from invio.db.session import session_factory, session_scope
from invio.scheduling.next_run import compute_next_run
from invio.services.jobs import JobService
from invio.sources.http import SafeHttpClient
from invio.sources.youtube import YoutubeSource
from tests.cli_helpers import FakePrompter
from tests.conftest import FakeClock
from tests.discover_helpers import RSS, TEXT, XML, Site, fixture, make_client
from tests.youtube_helpers import FakeExtractor

db = pytest.mark.db  # tests that use the database (--add-to)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CHECKED = "page links, common locations, robots.txt sitemaps, YouTube links"


@dataclass
class SourceCli:
    """What the ``source_cli`` fixture hands to a test."""

    site: Site
    runner: CliRunner
    monkeypatch: pytest.MonkeyPatch

    def invoke(self, args: Sequence[str]) -> Result:
        return self.runner.invoke(cli_main.app, ["source", *args], env={"COLUMNS": "200"})


@pytest.fixture
def source_cli(monkeypatch: pytest.MonkeyPatch) -> Iterator[SourceCli]:
    """A CLI runner whose discovery runs against a scripted site."""
    site = Site()

    # The client is built inside the async context manager: each CLI invocation runs
    # ``asyncio.run`` on a fresh event loop, and a client must live on the loop that uses it.
    @asynccontextmanager
    async def deps() -> AsyncIterator[tuple[SafeHttpClient, YoutubeSource]]:
        youtube = YoutubeSource(extract=FakeExtractor())
        try:
            async with make_client(site) as client:
                yield client, youtube
        finally:
            youtube.close()

    monkeypatch.setattr(source_module, "_discovery_deps", deps)
    yield SourceCli(site, CliRunner(), monkeypatch)


def snippets(stdout: str) -> list[Any]:
    """The YAML list items printed after the summaries (4-space indent removed)."""
    items: list[Any] = []
    for block in re.split(r"\n\n", stdout.strip()):
        lines = block.splitlines()[2:]
        items.extend(yaml.safe_load("\n".join(line.removeprefix("    ") for line in lines)))
    return items


def assert_valid_job_sources(items: list[Any], job_data: dict[str, Any]) -> None:
    """SC-003: the printed snippets are valid entries of a job's ``sources`` list."""
    assert items
    validate_job(job_data | {"sources": items})


# --- help and wiring -------------------------------------------------------------------------


def test_source_group_lists_discover(source_cli: SourceCli) -> None:
    result = source_cli.invoke(["--help"])

    assert result.exit_code == 0
    assert re.search(r"^\s*│?\s*discover\s", ANSI_RE.sub("", result.output), re.MULTILINE)


# --- US1: announced feeds ----------------------------------------------------------------------


def test_discover_prints_the_announced_feed(
    source_cli: SourceCli, job_data: dict[str, Any]
) -> None:
    source_cli.site.add("/", fixture("page_rss_link.html"))
    source_cli.site.add("/blog/feed.xml", fixture("feed_rss.xml"), RSS)

    result = source_cli.invoke(["discover", "example.com"])

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "[1] rss  https://example.com/blog/feed.xml\n"
        "    title: Example Blog · newest entry: 2026-10-01 · found: announced\n"
        "    - type: rss\n"
        "      url: https://example.com/blog/feed.xml\n"
        "\n"
    )
    assert_valid_job_sources(snippets(result.stdout), job_data)


def test_discover_renders_unknown_and_missing_dates(source_cli: SourceCli) -> None:
    source_cli.site.add(
        "/",
        '<link rel="alternate" type="application/rss+xml" href="/empty">'
        '<link rel="alternate" type="application/rss+xml" href="/undated">',
    )
    source_cli.site.add("/empty", fixture("feed_empty.xml"), RSS)
    source_cli.site.add(
        "/undated",
        b'<rss version="2.0"><channel><title>Undated</title><item><title>A</title>'
        b"<link>https://example.com/a</link></item></channel></rss>",
        RSS,
    )

    result = source_cli.invoke(["discover", "https://example.com/"])

    assert result.exit_code == 0, result.output
    assert "title: Empty Blog · newest entry: no entries · found: announced" in result.stdout
    assert "title: Undated · newest entry: unknown · found: announced" in result.stdout


def test_discover_marks_comment_feeds_and_lists_them_last(
    source_cli: SourceCli, job_data: dict[str, Any]
) -> None:
    source_cli.site.add("/", fixture("page_comments.html"))
    source_cli.site.add("/comments/feed/", fixture("feed_rss.xml"), RSS)
    source_cli.site.add("/feed/", fixture("feed_atom.xml"), RSS)

    result = source_cli.invoke(["discover", "example.com"])

    assert result.exit_code == 0, result.output
    headlines = [line for line in result.stdout.splitlines() if line.startswith("[")]
    assert headlines == [
        "[1] rss  https://example.com/feed/",
        "[2] rss (comments)  https://example.com/comments/feed/",
    ]
    assert_valid_job_sources(snippets(result.stdout), job_data)


def test_discover_reports_rejected_findings_on_stderr_without_secrets(
    source_cli: SourceCli,
) -> None:
    source_cli.site.add(
        "/",
        '<link rel="alternate" type="application/rss+xml" href="/good">'
        '<link rel="alternate" type="application/rss+xml" href="/gone">'
        '<link rel="alternate" type="application/rss+xml" href="/secret-feed?token=hunter2">'
        '<link rel="alternate" type="application/rss+xml" href="/page">',
    )
    source_cli.site.add("/good", fixture("feed_rss.xml"), RSS)
    source_cli.site.add("/secret-feed", status=500)
    source_cli.site.add("/page", fixture("not_a_feed.html"))

    result = source_cli.invoke(["discover", "example.com"])

    assert result.exit_code == 0, result.output
    assert "Not offered: https://example.com/gone: http_status (HTTP 404)" in result.stderr
    assert "Not offered: https://example.com/secret-feed: http_status (HTTP 500)" in result.stderr
    assert "Not offered: https://example.com/page: not_a_feed_or_sitemap" in result.stderr
    # (httpx2's own INFO request log, which the logging setup lets through, is not ours)
    diagnostics = [line for line in result.stderr.splitlines() if line.startswith("Not offered")]
    assert diagnostics
    assert all("hunter2" not in line for line in diagnostics)
    assert "gone" not in result.stdout


def test_discover_prints_the_start_page_problem_and_skipped_notes(source_cli: SourceCli) -> None:
    source_cli.site.add("/", status=500)
    source_cli.site.add("/robots.txt", fixture("robots_many_sitemaps.txt"), TEXT)
    for i in range(1, 8):
        source_cli.site.add(f"/map-{i}.xml", fixture("sitemap_urlset.xml"), XML)

    result = source_cli.invoke(["discover", "example.com"])

    assert result.exit_code == 0, result.output
    assert "Start page not usable: http_status: https://example.com/" in result.stderr
    assert "Skipped: 2 more robots.txt sitemaps (limit 5)" in result.stderr


def test_discover_exits_1_when_nothing_is_found(source_cli: SourceCli) -> None:
    source_cli.site.add("/", fixture("page_plain.html"))

    result = source_cli.invoke(["discover", "example.com"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert f"No source found for https://example.com/ (checked: {CHECKED})." in result.stderr


def test_discover_exits_1_when_the_start_page_and_every_probe_fail(source_cli: SourceCli) -> None:
    source_cli.site.add("/", status=503)

    result = source_cli.invoke(["discover", "example.com"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Start page not usable: http_status: https://example.com/" in result.stderr
    assert "Not offered: https://example.com/sitemap.xml: http_status (HTTP 404)" in result.stderr
    assert f"No source found for https://example.com/ (checked: {CHECKED})." in result.stderr


def test_discover_invalid_settings_exit_2_before_any_request(source_cli: SourceCli) -> None:
    source_cli.monkeypatch.setenv("INVIO_HTTP_MAX_RESPONSE_BYTES", "abc")

    result = source_cli.invoke(["discover", "example.com"])

    assert result.exit_code == 2
    assert "Configuration error" in result.stderr
    assert source_cli.site.requested == []


def test_discover_configuration_error_while_opening_the_clients_exits_2(
    source_cli: SourceCli,
) -> None:
    @asynccontextmanager
    async def missing() -> AsyncIterator[tuple[SafeHttpClient, YoutubeSource]]:
        raise MissingSettingError("youtube_cookies_file")
        yield  # pragma: no cover

    source_cli.monkeypatch.setattr(source_module, "_discovery_deps", missing)

    result = source_cli.invoke(["discover", "example.com"])

    assert result.exit_code == 2
    assert "Configuration error" in result.stderr
    assert result.stdout == ""


def test_discover_no_source_message_hides_the_query_string(source_cli: SourceCli) -> None:
    result = source_cli.invoke(["discover", "https://example.com/page?token=hunter2"])

    assert result.exit_code == 1
    (line,) = [x for x in result.stderr.splitlines() if x.startswith("No source found")]
    assert line.startswith("No source found for https://example.com/page (checked")


@pytest.mark.parametrize("url", ["ftp://example.com/", "https://", "file:///etc/passwd", "  "])
def test_discover_rejects_an_invalid_url_before_any_request(
    source_cli: SourceCli, url: str
) -> None:
    result = source_cli.invoke(["discover", url])

    assert result.exit_code == 2
    assert "URL" in result.stderr
    assert source_cli.site.requested == []


def test_discover_a_private_address_is_not_requested(source_cli: SourceCli) -> None:
    result = source_cli.invoke(["discover", "http://10.0.0.5/"])

    assert result.exit_code == 1
    assert "Start page not usable: non_public_address: http://10.0.0.5/" in result.stderr
    assert source_cli.site.requested == []


def test_discover_strips_control_characters_from_remote_titles(source_cli: SourceCli) -> None:
    source_cli.site.add("/", '<link rel="alternate" type="application/rss+xml" href="/f">')
    source_cli.site.add(
        "/f",
        b'<rss version="2.0"><channel><title>Evil\x1b[31mRED</title>'
        b"<item><title>A</title><link>https://example.com/a</link></item></channel></rss>",
        RSS,
    )

    result = source_cli.invoke(["discover", "example.com"])

    assert "\x1b" not in result.stdout


# --- US2: sitemaps --------------------------------------------------------------------------------


def test_discover_prints_a_probed_sitemap(source_cli: SourceCli, job_data: dict[str, Any]) -> None:
    source_cli.site.add("/", fixture("page_plain.html"))
    source_cli.site.add("/sitemap.xml", fixture("sitemap_urlset.xml"), XML)

    result = source_cli.invoke(["discover", "example.com"])

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "[1] sitemap  https://example.com/sitemap.xml\n"
        "    sitemap: 3 URLs · newest entry: 2026-09-30 · found: probed\n"
        "    - type: sitemap\n"
        "      url: https://example.com/sitemap.xml\n"
        "\n"
    )
    assert_valid_job_sources(snippets(result.stdout), job_data)


def test_discover_prints_a_sitemap_index_and_numbers_in_engine_order(
    source_cli: SourceCli, job_data: dict[str, Any]
) -> None:
    source_cli.site.add("/", '<link rel="alternate" type="application/rss+xml" href="/f.xml">')
    source_cli.site.add("/f.xml", fixture("feed_rss.xml"), RSS)
    source_cli.site.add("/sitemap.xml", fixture("sitemap_index.xml"), XML)
    source_cli.site.add("/robots.txt", fixture("robots_sitemaps.txt"), TEXT)
    source_cli.site.add("/sitemap_index.xml", fixture("sitemap_urlset.xml"), XML)

    result = source_cli.invoke(["discover", "example.com"])

    assert result.exit_code == 0, result.output
    headlines = [line for line in result.stdout.splitlines() if line.startswith("[")]
    assert headlines == [
        "[1] rss  https://example.com/f.xml",
        "[2] sitemap  https://example.com/sitemap.xml",
        "[3] sitemap  https://example.com/sitemap_index.xml",
    ]
    assert (
        "    sitemap index: 2 sitemaps · newest entry: 2026-09-20 · found: probed" in result.stdout
    )
    assert "found: robots" in result.stdout
    assert_valid_job_sources(snippets(result.stdout), job_data)


# --- production wiring ---------------------------------------------------------------------------


async def test_discovery_deps_build_the_adapters_from_settings_and_close_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INVIO_YOUTUBE_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("INVIO_HTTP_MAX_RESPONSE_BYTES", "4321")
    source_module.get_settings.cache_clear()

    async with source_module._discovery_deps() as (client, youtube):
        assert isinstance(client, SafeHttpClient)
        assert client.max_response_bytes == 4321
        assert isinstance(youtube, YoutubeSource)
        assert youtube._timeout == 12.5

    assert youtube._closed
    assert client._client.is_closed


# --- US3: --add-to and --pick ---------------------------------------------------------------

JOB = "news"
FEED_A = "https://example.com/blog/feed.xml"
FEED_B = "https://example.com/other.xml"


@dataclass
class AddCli:
    """A :class:`SourceCli` with a stored job ``news`` behind ``_make_service``."""

    cli: SourceCli
    service: JobService
    job_data: dict[str, Any]

    @property
    def site(self) -> Site:
        return self.cli.site

    def invoke(self, args: Sequence[str]) -> Result:
        return self.cli.invoke(["discover", "example.com", *args])

    def answers(self, *answers: str | bool, interactive: bool = True) -> FakePrompter:
        prompter = FakePrompter(answers)
        self.cli.monkeypatch.setattr(source_module, "_make_prompter", lambda: prompter)
        self.cli.monkeypatch.setattr(source_module, "_is_interactive", lambda: interactive)
        return prompter

    def sources(self) -> list[dict[str, Any]]:
        return list(self.service.stored_config(JOB)["sources"])


@pytest.fixture
def add_cli(
    source_cli: SourceCli,
    db_engine: Engine,
    clean_jobs: None,
    fake_clock: FakeClock,
    job_data: dict[str, Any],
) -> AddCli:
    service = JobService(session_factory(db_engine), next_run=compute_next_run, clock=fake_clock)
    service.create(JOB, job_data)
    monkeypatch = source_cli.monkeypatch
    monkeypatch.setattr(source_module, "_make_service", lambda: service)
    monkeypatch.setattr(source_module, "_is_interactive", lambda: False)
    return AddCli(source_cli, service, job_data)


def one_feed(site: Site) -> None:
    site.add("/", '<link rel="alternate" type="application/rss+xml" href="/blog/feed.xml">')
    site.add("/blog/feed.xml", fixture("feed_rss.xml"), RSS)


def two_feeds(site: Site) -> None:
    link = '<link rel="alternate" type="application/rss+xml" href="{}">'
    site.add("/", link.format(FEED_A) + link.format(FEED_B))
    site.add("/blog/feed.xml", fixture("feed_rss.xml"), RSS)
    site.add("/other.xml", fixture("feed_atom.xml"), RSS)


@db
def test_cli_add_to_appends_and_revalidates(add_cli: AddCli) -> None:
    one_feed(add_cli.site)
    before = add_cli.sources()

    result = add_cli.invoke(["--add-to", JOB])

    assert result.exit_code == 0, result.output
    assert f"Added [1] rss {FEED_A} to job '{JOB}'." in result.stdout
    after = add_cli.sources()
    assert after[:-1] == before
    assert after[-1]["type"] == "rss"
    assert after[-1]["url"] == FEED_A
    validate_job(add_cli.service.stored_config(JOB))


@db
def test_cli_add_to_interactive_select_picks_one(add_cli: AddCli) -> None:
    two_feeds(add_cli.site)
    prompter = add_cli.answers(f"[2] rss {FEED_B}")

    result = add_cli.invoke(["--add-to", JOB])

    assert result.exit_code == 0, result.output
    assert f"Added [2] rss {FEED_B} to job '{JOB}'." in result.stdout
    assert add_cli.sources()[-1]["url"] == FEED_B
    [choices] = prompter.choices.values()
    assert choices == [f"[1] rss {FEED_A}", f"[2] rss {FEED_B}", "Cancel"]


@db
def test_cli_add_to_interactive_asks_even_for_a_single_candidate(add_cli: AddCli) -> None:
    one_feed(add_cli.site)
    before = add_cli.service.stored_config(JOB)
    prompter = add_cli.answers("Cancel")

    result = add_cli.invoke(["--add-to", JOB])

    assert result.exit_code == 0, result.output
    assert f"Nothing added to job '{JOB}'." in result.stdout
    [choices] = prompter.choices.values()
    assert choices == [f"[1] rss {FEED_A}", "Cancel"]
    assert add_cli.service.stored_config(JOB) == before


@db
def test_cli_add_to_interactive_cancel_changes_nothing(add_cli: AddCli) -> None:
    two_feeds(add_cli.site)
    before = add_cli.service.stored_config(JOB)
    add_cli.answers("Cancel")

    result = add_cli.invoke(["--add-to", JOB])

    assert result.exit_code == 0, result.output
    assert f"Nothing added to job '{JOB}'." in result.stdout
    assert add_cli.service.stored_config(JOB) == before


@db
def test_cli_add_to_interactive_ctrl_c_changes_nothing(add_cli: AddCli) -> None:
    two_feeds(add_cli.site)
    before = add_cli.service.stored_config(JOB)
    add_cli.answers()  # out of answers: WizardAborted

    result = add_cli.invoke(["--add-to", JOB])

    assert result.exit_code == 0, result.output
    assert f"Nothing added to job '{JOB}'." in result.stdout
    assert add_cli.service.stored_config(JOB) == before


class InterruptingPrompter(FakePrompter):
    """A prompter whose selection ends with ``error`` (Ctrl+C or a closed stdin)."""

    def __init__(self, error: BaseException) -> None:
        super().__init__([])
        self.error = error

    def select(self, message: str, choices: Sequence[str], *, default: str | None = None) -> str:
        self.asked.append(message)
        raise self.error


@db
@pytest.mark.parametrize("error", [KeyboardInterrupt(), EOFError()], ids=["ctrl-c", "eof"])
def test_cli_add_to_interactive_interrupt_changes_nothing(
    add_cli: AddCli, error: BaseException
) -> None:
    two_feeds(add_cli.site)
    before = add_cli.service.stored_config(JOB)
    prompter = InterruptingPrompter(error)
    add_cli.cli.monkeypatch.setattr(source_module, "_make_prompter", lambda: prompter)
    add_cli.cli.monkeypatch.setattr(source_module, "_is_interactive", lambda: True)

    result = add_cli.invoke(["--add-to", JOB])

    assert result.exit_code == 0, result.output
    assert prompter.asked == [f"Add which source to job '{JOB}'?"]
    assert f"Nothing added to job '{JOB}'." in result.stdout
    assert add_cli.service.stored_config(JOB) == before


@db
def test_cli_add_to_database_error_exits_1_and_changes_nothing(add_cli: AddCli) -> None:
    def broken(name: str, source: object) -> None:
        raise OperationalError("UPDATE jobs SET config=...", {}, Exception("secret-dsn"))

    one_feed(add_cli.site)
    before = add_cli.service.stored_config(JOB)
    add_cli.cli.monkeypatch.setattr(add_cli.service, "append_source", broken)

    result = add_cli.invoke(["--add-to", JOB])

    assert result.exit_code == 1
    assert "database error (OperationalError)" in result.stderr
    assert "secret-dsn" not in result.output
    assert "Added" not in result.stdout
    assert add_cli.service.stored_config(JOB) == before


@db
def test_cli_add_to_non_interactive_several_candidates_need_pick(add_cli: AddCli) -> None:
    two_feeds(add_cli.site)
    before = add_cli.service.stored_config(JOB)

    result = add_cli.invoke(["--add-to", JOB])

    assert result.exit_code == 1
    assert "[1] rss" in result.stdout
    assert "[2] rss" in result.stdout
    assert "several sources found; choose one with --pick N" in result.stderr
    assert add_cli.service.stored_config(JOB) == before


@db
def test_cli_add_to_pick_chooses_without_prompting(add_cli: AddCli) -> None:
    two_feeds(add_cli.site)
    prompter = add_cli.answers()

    result = add_cli.invoke(["--add-to", JOB, "--pick", "2"])

    assert result.exit_code == 0, result.output
    assert add_cli.sources()[-1]["url"] == FEED_B
    assert prompter.asked == []


@db
def test_cli_add_to_pick_out_of_range_changes_nothing(add_cli: AddCli) -> None:
    two_feeds(add_cli.site)
    before = add_cli.service.stored_config(JOB)

    result = add_cli.invoke(["--add-to", JOB, "--pick", "9"])

    assert result.exit_code == 1
    assert "--pick 9 is out of range; choose 1–2" in result.stderr  # noqa: RUF001
    assert add_cli.service.stored_config(JOB) == before


@db
def test_cli_add_to_unknown_job_makes_no_request(add_cli: AddCli) -> None:
    one_feed(add_cli.site)

    result = add_cli.invoke(["--add-to", "missing"])

    assert result.exit_code == 1
    assert "missing" in result.stderr
    assert add_cli.site.requested == []


@db
def test_cli_add_to_invalid_job_name_makes_no_request(add_cli: AddCli) -> None:
    one_feed(add_cli.site)

    result = add_cli.invoke(["--add-to", " padded"])

    assert result.exit_code == 1
    assert "invalid job name" in result.stderr
    assert add_cli.site.requested == []


@db
def test_cli_add_to_invalid_stored_job_exits_2_without_request(add_cli: AddCli) -> None:
    one_feed(add_cli.site)
    with session_scope(add_cli.service._session_factory) as session:
        session.add(Job(name="broken", config={"bogus": 1}))

    result = add_cli.invoke(["--add-to", "broken"])

    assert result.exit_code == 2
    assert "invalid stored job 'broken'" in result.stderr
    assert add_cli.site.requested == []


@db
def test_cli_add_to_without_database_exits_2_without_request(add_cli: AddCli) -> None:
    def no_database() -> JobService:
        raise MissingSettingError("database_url")

    add_cli.cli.monkeypatch.setattr(source_module, "_make_service", no_database)
    one_feed(add_cli.site)

    result = add_cli.invoke(["--add-to", JOB])

    assert result.exit_code == 2
    assert "Configuration error" in result.stderr
    assert add_cli.site.requested == []


@db
def test_cli_add_to_a_source_the_job_has_changes_nothing(add_cli: AddCli) -> None:
    one_feed(add_cli.site)
    add_cli.invoke(["--add-to", JOB])
    before = add_cli.service.stored_config(JOB)

    result = add_cli.invoke(["--add-to", JOB])

    assert result.exit_code == 0, result.output
    assert f"Job '{JOB}' already contains this source; nothing changed." in result.stdout
    assert add_cli.service.stored_config(JOB) == before


@db
def test_cli_add_to_invalid_result_is_not_saved(
    add_cli: AddCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def only_one_source(config: Any) -> Any:
        if len(config["sources"]) > 1:
            raise JobConfigError(None, ["sources: too many sources"])
        return validate_job(config)

    one_feed(add_cli.site)
    before = add_cli.service.stored_config(JOB)
    monkeypatch.setattr("invio.services.jobs._validate", only_one_source)

    result = add_cli.invoke(["--add-to", JOB])

    assert result.exit_code == 2
    assert "sources: too many sources" in result.stderr
    assert add_cli.service.stored_config(JOB) == before


@db
def test_cli_add_to_without_candidates_changes_nothing(add_cli: AddCli) -> None:
    add_cli.site.add("/", fixture("page_plain.html"))
    before = add_cli.service.stored_config(JOB)

    result = add_cli.invoke(["--add-to", JOB])

    assert result.exit_code == 1
    assert "No source found" in result.stderr
    assert add_cli.service.stored_config(JOB) == before


def test_cli_pick_without_add_to_is_a_usage_error(source_cli: SourceCli) -> None:
    result = source_cli.invoke(["discover", "example.com", "--pick", "1"])

    assert result.exit_code == 2
    assert "--add-to" in result.stderr
    assert source_cli.site.requested == []


def test_cli_pick_zero_is_a_usage_error(source_cli: SourceCli) -> None:
    result = source_cli.invoke(["discover", "example.com", "--add-to", JOB, "--pick", "0"])

    assert result.exit_code == 2
    assert source_cli.site.requested == []


# --- US4: YouTube --------------------------------------------------------------------------------

CREATOR_LISTING = {
    "title": "Example Creator",
    "entries": [{"id": "abc", "title": "T", "timestamp": 1_772_000_000}],
}


def with_youtube(cli: SourceCli, info: dict[str, Any]) -> None:
    """Let the scripted site's YouTube adapter answer every listing with ``info``."""
    site = cli.site

    @asynccontextmanager
    async def deps() -> AsyncIterator[tuple[SafeHttpClient, YoutubeSource]]:
        youtube = YoutubeSource(extract=FakeExtractor(info))
        try:
            async with make_client(site) as client:
                yield client, youtube
        finally:
            youtube.close()

    cli.monkeypatch.setattr(source_module, "_discovery_deps", deps)


def test_discover_prints_a_linked_youtube_channel(
    source_cli: SourceCli, job_data: dict[str, Any]
) -> None:
    with_youtube(source_cli, CREATOR_LISTING)
    source_cli.site.add("/", '<a href="https://www.youtube.com/@somecreator/videos">YouTube</a>')

    result = source_cli.invoke(["discover", "example.com"])

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "[1] youtube_channel  @somecreator\n"
        "    title: Example Creator · newest entry: 2026-02-25 · found: linked\n"
        "    - type: youtube_channel\n"
        "      channel_id: '@somecreator'\n"
        "\n"
    )
    assert_valid_job_sources(snippets(result.stdout), job_data)


def test_discover_prints_a_direct_youtube_playlist_without_any_request(
    source_cli: SourceCli, job_data: dict[str, Any]
) -> None:
    with_youtube(source_cli, CREATOR_LISTING)

    result = source_cli.invoke(["discover", "https://www.youtube.com/playlist?list=PL123"])

    assert result.exit_code == 0, result.output
    assert "[1] youtube_playlist  PL123" in result.stdout
    assert "found: direct" in result.stdout
    assert source_cli.site.requested == []
    assert_valid_job_sources(snippets(result.stdout), job_data)


@db
def test_cli_add_to_appends_a_youtube_candidate(
    add_cli: AddCli,
) -> None:
    with_youtube(add_cli.cli, CREATOR_LISTING)
    add_cli.site.add("/", '<a href="https://www.youtube.com/@somecreator">YouTube</a>')

    result = add_cli.invoke(["--add-to", JOB])

    assert result.exit_code == 0, result.output
    assert f"Added [1] youtube_channel @somecreator to job '{JOB}'." in result.stdout
    assert add_cli.sources()[-1]["channel_id"] == "@somecreator"
    validate_job(add_cli.service.stored_config(JOB))


# --- output hygiene -------------------------------------------------------------------------------


def test_discover_cuts_a_huge_remote_title(source_cli: SourceCli) -> None:
    source_cli.site.add("/", '<link rel="alternate" type="application/rss+xml" href="/f">')
    source_cli.site.add(
        "/f",
        b'<rss version="2.0"><channel><title>' + b"T" * 3000 + b"</title>"
        b"<item><title>A</title><link>https://example.com/a</link></item></channel></rss>",
        RSS,
    )

    result = source_cli.invoke(["discover", "example.com"])

    assert result.exit_code == 0, result.output
    assert max(len(line) for line in result.stdout.splitlines()) < 400


def test_discover_strips_escape_sequences_from_rejected_urls(source_cli: SourceCli) -> None:
    source_cli.site.add("/", '<link rel="alternate" type="application/rss+xml" href="/a%1B[31mb">')

    result = source_cli.invoke(["discover", "example.com"])

    assert "\x1b" not in result.stderr
    assert "Not offered:" in result.stderr
