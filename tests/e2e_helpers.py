"""End-to-end fixtures: local feed server, SMTP sink, fake provider, network guard and scenario
environment (not shipped). Contract: specs/022-gh-issue-26/contracts/e2e-harness.md.

Nothing here touches the internet: the feed, the SMTP sink and the Ollama stub bind ``127.0.0.1``
and the ``no_external_network`` fixture turns any other connection into a test failure.
"""

import asyncio
import dataclasses
import http.server
import ipaddress
import json
import os
import re
import socket
import socketserver
import threading
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4
from xml.sax.saxutils import escape

import pytest
import yaml
from sqlalchemy import Engine, delete, select, update
from typer.testing import CliRunner, Result

from invio.cli import main as cli_main
from invio.cli.commands import run_due as run_due_module
from invio.config.job import JobConfig
from invio.config.settings import get_settings
from invio.db.models import Digest, Item, Job, Notification, Run
from invio.db.session import create_db_engine, session_factory, session_scope
from invio.notify.archive import slugify_job_name
from invio.pipeline.deps import default_deps
from invio.pipeline.due import DueReport, run_due
from invio.scheduling.next_run import compute_next_run
from tests.conftest import FakeClock
from tests.db_helpers import TEST_DATABASE_URL, uses_sqlite
from tests.http_helpers import LOOPBACK, LoopbackServer, Route
from tests.pipeline_helpers import (
    RoutedFakeProvider,
    article_html,
    digest_reply,
    relevance_reply,
    summary_reply,
)
from tests.smtp_helpers import SmtpServer, _serve, parts

__all__ = [
    "RECIPIENTS",
    "E2EArticle",
    "E2EEnv",
    "E2EFeed",
    "ExternalNetworkError",
    "FakeOllamaServer",
    "Observed",
    "archive_pages",
    "assert_no_external_resources",
    "e2e_env",
    "initial_articles",
    "mail_texts",
    "no_external_network",
    "run_scenario_a",
    "scenario_provider",
    "write_job_yaml",
]

# ``.test`` is rejected by the job schema's e-mail validation (reserved name), so use example.com;
# the mails only ever reach the local SMTP sink.
RECIPIENTS: tuple[str, str] = ("reader-1@example.com", "reader-2@example.com")
ARCHIVE_BASE_URL = "https://archive.example.test/digests"
SEMANTIC_DESCRIPTION = "Orchard keepers share field notes about growing apples."
_LOOPBACK_NAMES = frozenset({"localhost", ""})


# --- Articles and the feed ---------------------------------------------------------------------


@dataclass(frozen=True)
class E2EArticle:
    """One article that the feed publishes.

    ``title`` is unique and plain alphanumeric: the fake provider routes by it.
    ``relevant=False`` scores 0.1 (below ``min_relevance``); ``fail_at`` makes the provider
    raise for this article at that stage.
    """

    slug: str
    title: str
    published_at: datetime
    relevant: bool = True
    fail_at: Literal["relevance"] | None = None


def initial_articles(now: datetime) -> list[E2EArticle]:
    """A1, A2, A3 (relevant) and the off-topic X1, published 1 to 4 hours before ``now``."""
    return [
        E2EArticle("a1", "Alpha Orchard Notes", now - timedelta(hours=1)),
        E2EArticle("a2", "Bravo Lantern Report", now - timedelta(hours=2)),
        E2EArticle("a3", "Charlie Meadow Review", now - timedelta(hours=3)),
        E2EArticle("x1", "Xray Cooking Gadgets", now - timedelta(hours=4), relevant=False),
    ]


class E2EFeed:
    """A local RSS feed with one page per article, on a :class:`LoopbackServer`."""

    def __init__(self, articles: Sequence[E2EArticle]) -> None:
        self.server = LoopbackServer()
        self.articles: list[E2EArticle] = list(articles)
        self.server.routes["/robots.txt"] = Route(
            headers={"Content-Type": "text/plain"}, body=b"User-agent: *\nAllow: /\n"
        )
        self._render()

    @property
    def feed_url(self) -> str:
        return f"{self.server.base_url}/feed.xml"

    @property
    def requests(self) -> list[str]:
        """The paths requested so far, in arrival order."""
        return self.server.paths()

    def start(self) -> None:
        self.server.start()

    def stop(self) -> None:
        self.server.stop()

    def add(self, article: E2EArticle) -> None:
        """Publish one more article."""
        self.articles.append(article)
        self._render()

    def replace(self, articles: Sequence[E2EArticle]) -> None:
        """Publish exactly ``articles`` (the list object stays the same)."""
        self.articles[:] = articles
        self._render()

    def _render(self) -> None:
        base = self.server.base_url
        newest_first = sorted(self.articles, key=lambda a: a.published_at, reverse=True)
        items = "".join(
            f"<item><title>{escape(a.title)}</title><link>{base}/articles/{a.slug}.html</link>"
            f"<guid>{base}/articles/{a.slug}.html</guid>"
            f"<pubDate>{format_datetime(a.published_at, usegmt=True)}</pubDate>"
            f"<description>{escape(a.title)}</description></item>"
            for a in newest_first
        )
        feed = (
            '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            f"<title>E2E feed</title><link>{base}/</link><description>E2E</description>"
            f"{items}</channel></rss>"
        )
        self.server.routes["/feed.xml"] = Route(
            headers={"Content-Type": "application/rss+xml"}, body=feed.encode()
        )
        for article in self.articles:
            self.server.routes[f"/articles/{article.slug}.html"] = Route(
                headers={"Content-Type": "text/html; charset=utf-8"},
                body=article_html(article.title).encode(),
            )


# --- Fake provider -----------------------------------------------------------------------------


def scenario_provider(articles: Sequence[E2EArticle]) -> RoutedFakeProvider:
    """A routed provider that answers by article title.

    ``articles`` is read at call time, so articles added to the same list later (Scenario C) are
    known too. Relevance follows ``relevant`` / ``fail_at``; the summary headline is the title, so
    a title appears in a mail only when its item made it into the digest.
    """

    def article(title: str) -> E2EArticle:
        for candidate in articles:
            if candidate.title == title:
                return candidate
        raise AssertionError(f"provider asked about an unknown title: {title!r}")

    def relevance(title: str) -> Any:
        found = article(title)
        if found.fail_at == "relevance":
            return RuntimeError("boom secret")
        return relevance_reply(0.9 if found.relevant else 0.1)

    def summary(title: str) -> Any:
        article(title)
        reply = summary_reply()
        text = json.loads(reply.text) | {"headline": title}
        return dataclasses.replace(reply, text=json.dumps(text))

    return RoutedFakeProvider(relevance=relevance, summarize=summary)


# --- Network guard -----------------------------------------------------------------------------


class ExternalNetworkError(AssertionError):
    """A test tried to reach a host outside loopback."""


def _is_local(host: object) -> bool:
    if host is None:
        return True  # getaddrinfo(None, port) resolves the local host
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture
def no_external_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Fail on any connect or DNS lookup outside loopback; drop provider and SMTP credentials.

    Yields the list of blocked attempts: product code that swallows the error still fails the
    test at teardown, unless the test clears the list on purpose.
    """
    blocked: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def check(host: object) -> None:
        if not _is_local(host):
            blocked.append(str(host))
            raise ExternalNetworkError(f"external network access blocked: {host}")

    def check_address(sock: socket.socket, address: Any) -> None:
        if sock.family != socket.AF_UNIX:
            check(address[0])

    def connect(self: socket.socket, address: Any) -> None:
        check_address(self, address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        check_address(self, address)
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        check(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    for key in list(os.environ):
        if key.endswith("_API_KEY"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("INVIO_SMTP_USER", raising=False)
    monkeypatch.delenv("INVIO_SMTP_PASSWORD", raising=False)
    yield blocked
    assert not blocked, f"external network access was attempted: {blocked}"


# --- Job file ----------------------------------------------------------------------------------


def write_job_yaml(path: Path, *, feed_url: str, recipients: Sequence[str] = RECIPIENTS) -> Path:
    """Write the scenario job (daily 07:00 UTC, one RSS source, archive on) to ``path``."""
    job = {
        "schedule": {"frequency": "daily", "time": "07:00", "timezone": "UTC"},
        "notification": {"to": list(recipients), "subject": "invio e2e digest"},
        "sources": [{"type": "rss", "url": feed_url}],
        "search": {"semantic_description": SEMANTIC_DESCRIPTION},
        "llm": {
            "provider": "ollama",
            "models": {"fast": "llama3.2:3b", "smart": "qwen2.5:14b"},
        },
        "archive": {"enabled": True, "base_url": ARCHIVE_BASE_URL},
    }
    path.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")
    return path


# --- Scenario environment ----------------------------------------------------------------------


@dataclass(frozen=True)
class Observed:
    """Everything a scenario compares: product records, mails, files and provider calls."""

    runs: tuple[tuple[str, tuple[dict[str, Any], ...]], ...]  # (status, errors), oldest first
    items: dict[str, tuple[str, int]]  # title -> (status, id)
    digests: tuple[tuple[int, ...], ...]  # item ids per digest, oldest first
    notifications: int
    mails: int
    pages: tuple[str, ...]
    next_run_at: datetime | None
    locked_until: datetime | None
    relevance_calls: Counter[str]
    summarize_calls: Counter[str]


@dataclass
class E2EEnv:
    """One scenario's world: database, feed, SMTP sink, provider, clock and the CLI."""

    tmp: Path
    database_url: str
    archive_dir: Path
    job_name: str
    job_yaml: Path
    feed: E2EFeed
    mail: SmtpServer
    provider: RoutedFakeProvider
    clock: FakeClock
    assert_engine: Engine
    runner: CliRunner = field(default_factory=CliRunner)

    @property
    def slug(self) -> str:
        return slugify_job_name(self.job_name)

    def cli(self, *args: str) -> Result:
        """Invoke the real ``invio`` application in-process."""
        return self.runner.invoke(cli_main.app, list(args), env={"COLUMNS": "200"})

    def _ok(self, *args: str) -> Result:
        result = self.cli(*args)
        assert result.exit_code == 0, f"invio {' '.join(args)} failed:\n{result.output}"
        return result

    def set_articles(self, articles: Sequence[E2EArticle]) -> None:
        """Replace the published articles (before ``setup_job``)."""
        self.feed.replace(articles)

    def setup_job(self) -> None:
        """Create the schema (SQLite only; MariaDB is migrated once) and the job."""
        if uses_sqlite():
            self._ok("db", "upgrade")
        self._ok("job", "create", "--from-file", str(self.job_yaml), "--name", self.job_name)

    def make_due(self) -> None:
        """Make the job due one minute ago and release any lock."""
        with session_scope(session_factory(self.assert_engine)) as session:
            session.execute(
                update(Job)
                .where(Job.name == self.job_name)
                .values(next_run_at=self.clock() - timedelta(minutes=1), locked_until=None)
            )

    def run_due(self) -> Result:
        """Run ``invio run-due``."""
        return self.cli("run-due")

    def expected_next_run(self) -> datetime:
        """The regular slot ``release_lock`` sets after a run that finished at ``clock()``."""
        config = JobConfig.model_validate(yaml.safe_load(self.job_yaml.read_text("utf-8")))
        return compute_next_run(config.schedule, self.clock())

    def job_schedule(self) -> tuple[datetime | None, datetime | None]:
        """The job's ``(next_run_at, locked_until)``, aware UTC."""
        with session_scope(session_factory(self.assert_engine)) as session:
            job = session.scalars(select(Job).where(Job.name == self.job_name)).one()
            return job.next_run_at, job.locked_until

    def job_id(self) -> int:
        with session_scope(session_factory(self.assert_engine)) as session:
            return session.scalars(select(Job.id).where(Job.name == self.job_name)).one()

    def snapshot(self) -> Observed:
        """Read every observable outcome."""
        job_id = self.job_id()
        with session_scope(session_factory(self.assert_engine)) as session:
            runs = tuple(
                (run.status.value, tuple((run.stats or {}).get("errors", [])))
                for run in session.scalars(
                    select(Run).where(Run.job_id == job_id).order_by(Run.id)
                )
            )
            items = {
                item.title: (item.status.value, item.id)
                for item in session.scalars(select(Item).where(Item.job_id == job_id))
            }
            digests = tuple(
                tuple(digest.item_ids)
                for digest in session.scalars(
                    select(Digest).where(Digest.job_id == job_id).order_by(Digest.id)
                )
            )
            notifications = len(
                session.scalars(select(Notification).where(Notification.job_id == job_id)).all()
            )
        next_run_at, locked_until = self.job_schedule()
        return Observed(
            runs=runs,
            items=items,
            digests=digests,
            notifications=notifications,
            mails=len(self.mail.messages),
            pages=tuple(str(page.relative_to(self.archive_dir)) for page in archive_pages(self)),
            next_run_at=next_run_at,
            locked_until=locked_until,
            relevance_calls=Counter(self.provider.titles_for("relevance")),
            summarize_calls=Counter(self.provider.titles_for("summarize")),
        )


def database_url_for(tmp_path: Path) -> str:
    """The MariaDB test URL when configured, else a SQLite file in ``tmp_path``."""
    if TEST_DATABASE_URL is not None and not uses_sqlite():
        return TEST_DATABASE_URL
    return f"sqlite:///{tmp_path}/e2e.sqlite"


@pytest.fixture
def e2e_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_clock: FakeClock,
    patched_providers: Callable[[str, Any], None],
    no_external_network: list[str],
    _server_engine: Engine | None,
    clean_jobs: None,
) -> Iterator[E2EEnv]:
    """A fresh scenario environment; nothing is shared between tests."""
    feed = E2EFeed(initial_articles(fake_clock()))
    feed.start()
    provider = scenario_provider(feed.articles)
    database_url = database_url_for(tmp_path)
    archive_dir = tmp_path / "archive"
    engine = _server_engine if _server_engine is not None else create_db_engine(database_url)
    if _server_engine is not None:
        # run-due runs every due job of the shared database: start without leftovers.
        with session_scope(session_factory(engine)) as session:
            session.execute(delete(Job))
    try:
        with _serve() as mail:
            monkeypatch.setenv("INVIO_DATABASE_URL", database_url)
            monkeypatch.setenv("INVIO_ARCHIVE_DIR", str(archive_dir))
            monkeypatch.setenv("INVIO_SMTP_HOST", mail.host)
            monkeypatch.setenv("INVIO_SMTP_PORT", str(mail.port))
            monkeypatch.setenv("INVIO_SMTP_SECURITY", "none")
            monkeypatch.setenv("INVIO_SMTP_FROM", "invio@example.test")
            monkeypatch.setenv("INVIO_HTTP_HOST_INTERVAL_SECONDS", "0.01")
            get_settings.cache_clear()
            patched_providers("ollama", provider)

            async def due(limit: int | None, parallel: int) -> DueReport:
                async with default_deps(get_settings(), allow_networks=LOOPBACK) as deps:
                    fixed = dataclasses.replace(deps, clock=fake_clock)
                    return await run_due(limit=limit, parallel=parallel, deps=fixed)

            monkeypatch.setattr(
                run_due_module,
                "_run_due",
                lambda limit, parallel: asyncio.run(due(limit, parallel)),
            )
            job_name = f"e2e-{uuid4().hex[:8]}"
            yield E2EEnv(
                tmp=tmp_path,
                database_url=database_url,
                archive_dir=archive_dir,
                job_name=job_name,
                job_yaml=write_job_yaml(tmp_path / f"{job_name}.yaml", feed_url=feed.feed_url),
                feed=feed,
                mail=mail,
                provider=provider,
                clock=fake_clock,
                assert_engine=engine,
            )
    finally:
        feed.stop()
        if _server_engine is None:
            engine.dispose()


# --- Scenario steps and assertions -------------------------------------------------------------


def run_scenario_a(env: E2EEnv) -> Result:
    """Set the job up, make it due and run it once; the run must exit 0."""
    env.setup_job()
    env.make_due()
    result = env.run_due()
    assert result.exit_code == 0, f"run-due failed:\n{result.output}"
    return result


def mail_texts(env: E2EEnv, recipient: str) -> list[str]:
    """The plain-text bodies of the mails sent to ``recipient``."""
    return [
        parts(mail.message)[0] for mail in env.mail.handler.received if recipient in mail.rcpt_tos
    ]


def archive_pages(env: E2EEnv) -> list[Path]:
    """The digest pages of the job under the archive (without ``index.html``), sorted."""
    directory = env.archive_dir / env.slug
    if not directory.is_dir():
        return []
    return sorted(page for page in directory.glob("*.html") if page.name != "index.html")


_EXTERNAL = re.compile(
    r"<script[^>]*\ssrc\s*=|<link[^>]*\shref\s*=\s*[\"']?https?:|url\(\s*[\"']?https?:|@import",
    re.IGNORECASE,
)


def assert_no_external_resources(path: Path) -> None:
    """Fail if an archive file loads a script, stylesheet, font or image from elsewhere.

    Links to article URLs (``<a href>``) are allowed.
    """
    found = _EXTERNAL.search(path.read_text(encoding="utf-8"))
    assert found is None, f"{path} references an external resource: {found and found.group(0)}"


# --- Ollama stub for the separate-process test -------------------------------------------------


class _QuietServer(http.server.ThreadingHTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)  # skip the reverse DNS lookup of HTTPServer
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


class FakeOllamaServer:
    """A stub of Ollama's ``POST /api/chat`` that answers by the requested ``format`` schema.

    ``score`` in the schema gets a relevance answer (always relevant), ``headline`` an item
    summary, anything else the digest text. ``requests`` records ``(purpose, model)``.
    """

    def __init__(self) -> None:
        self._requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                data = json.dumps(owner._answer(body)).encode()
                self.send_response(200 if self.path == "/api/chat" else 404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

        self._httpd = _QuietServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=lambda: self._httpd.serve_forever(poll_interval=0.01), daemon=True
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    @property
    def requests(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._requests)

    def purposes(self) -> set[str]:
        """The purposes that got at least one request."""
        return {purpose for purpose, _model in self.requests}

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=5)
        self._httpd.server_close()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _answer(self, body: dict[str, Any]) -> dict[str, Any]:
        fmt = body.get("format")
        properties = fmt.get("properties", {}) if isinstance(fmt, dict) else {}
        if "score" in properties:
            purpose, text = "relevance", relevance_reply(0.9).text
        elif "headline" in properties:
            purpose, text = "summarize", summary_reply().text
        else:
            purpose, text = "synthesize", digest_reply().text
        with self._lock:
            self._requests.append((purpose, str(body.get("model", ""))))
        return {
            "model": body.get("model", ""),
            "created_at": "2026-10-09T12:00:00.000000Z",
            "message": {"role": "assistant", "content": text},
            "done_reason": "stop",
            "done": True,
            "prompt_eval_count": 12,
            "eval_count": 3,
        }
