"""T036: the quickstart's manual end-to-end (specs/016-gh-issue-25/quickstart.md §2-§3), scripted.

``invio job run`` and ``invio notify retry`` run through the CLI against a SQLite file, the
real pipeline over the fake LLM and fake sources of ``tests/pipeline_helpers.py``, the real
``deliver_digest`` and the in-process SMTP sink. Settings come from ``INVIO_*`` variables like
in the quickstart. The only sockets are on 127.0.0.1 (SMTP sink, static file server).
"""

import dataclasses
import http.server
import os
import re
import stat
import threading
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from invio.cli import main as cli_main
from invio.cli.commands import job as job_module
from invio.config.settings import get_settings
from invio.db.models import Base
from invio.db.session import create_db_engine
from invio.graph.ports import DeliveryReport, RunDeps
from invio.notify.email import deliver_digest
from invio.services.jobs import JobService
from invio.sources.errors import FetchError
from tests.conftest import FakeClock
from tests.pipeline_helpers import Env, build_env, make_candidates, make_job_config, source_key
from tests.pipeline_helpers import store_job as store_job_row
from tests.smtp_helpers import (
    SmtpServer,
    closed_port,  # noqa: F401
    parts,
    smtp_server,  # noqa: F401
)

BASE = "https://digests.example.org/invio"
FEED = "https://example.com/feed.xml"
STAMP = "2026-10-04-1200"  # ``fake_clock`` (UTC)
# Quickstart §2 step 5, plus the other external-reference markers of the layout contract.
EXTERNAL = re.compile(r"<script|<link|<img|<iframe|src=|@import|url\(", re.IGNORECASE)


class QuickstartSite:
    """The quickstart set-up: one stored job, ``INVIO_*`` settings and a scratch archive."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        clock: FakeClock,
        smtp_port: int,
        archive: dict[str, Any],
    ) -> None:
        self.monkeypatch = monkeypatch
        self.clock = clock
        self.archive_dir = tmp_path / "archive"
        url = f"sqlite:///{tmp_path}/quickstart.sqlite"
        self.engine = create_db_engine(url)
        Base.metadata.create_all(self.engine)
        for key, value in {
            "INVIO_DATABASE_URL": url,
            "INVIO_ARCHIVE_DIR": str(self.archive_dir),
            "INVIO_SMTP_HOST": "127.0.0.1",
            "INVIO_SMTP_FROM": "Invio <invio@localhost>",
            "INVIO_SMTP_SECURITY": "none",
            "INVIO_SMTP_TIMEOUT_SECONDS": "5",
        }.items():
            monkeypatch.setenv(key, value)
        self.use_smtp_port(smtp_port)
        self.config = make_job_config(archive=archive, notification__to=["reader@example.org"])
        self.env: Env = build_env(self.engine, clock, config=self.config)
        self.deliveries: list[int] = []

        async def notify(digest_id: int) -> DeliveryReport:
            # Like ``invio.pipeline.deps.default_deps``: settings read from the environment.
            self.deliveries.append(digest_id)
            outcome = await deliver_digest(
                self.env.factory, digest_id, settings=get_settings(), clock=self.clock
            )
            return DeliveryReport(sent=outcome.sent, failed=outcome.failed, error=outcome.error)

        deps = dataclasses.replace(self.env.deps, notify=notify)
        service = JobService(self.env.factory)

        @asynccontextmanager
        async def run_deps() -> AsyncIterator[RunDeps]:
            yield deps

        monkeypatch.setattr(job_module, "_make_service", lambda: service)
        monkeypatch.setattr(job_module, "_run_deps", run_deps)
        self.runner = CliRunner()

    def use_smtp_port(self, port: int) -> None:
        self.monkeypatch.setenv("INVIO_SMTP_PORT", str(port))
        get_settings.cache_clear()

    def add_job(self, name: str) -> None:
        store_job_row(self.env.factory, self.config, name=name)

    def fresh_candidates(self, prefix: str) -> None:
        self.env.ports.sources[source_key(FEED)] = make_candidates(3, prefix=prefix)

    def invoke(self, *args: str) -> Result:
        return self.runner.invoke(cli_main.app, list(args), env={"COLUMNS": "200"})

    def files(self) -> list[Path]:
        return sorted(p for p in self.archive_dir.rglob("*") if p.is_file())

    def snapshot(self) -> dict[str, tuple[bytes, int]]:
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.files()}

    def close(self) -> None:
        self.engine.dispose()


class StaticServer:
    """Quickstart §2 step 7: ``python -m http.server -d <archive_dir>`` on 127.0.0.1."""

    def __init__(self, root: Path) -> None:
        handler = partial(QuietHandler, directory=str(root))
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "StaticServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def get(self, path: str) -> tuple[int, str]:
        url = f"http://127.0.0.1:{self._server.server_address[1]}/{path.lstrip('/')}"
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode("utf-8")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        """Keep the test output clean."""


class TestQuickstartEndToEnd:
    """Quickstart §2 (happy path) and every row of the §3 failure table."""

    @pytest.fixture(autouse=True)
    def setup(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_clock: FakeClock,
        smtp_server: SmtpServer,
    ) -> Iterator[None]:
        self.monkeypatch, self.tmp_path, self.clock = monkeypatch, tmp_path, fake_clock
        self.server = smtp_server
        self._sites: list[QuickstartSite] = []
        yield
        for site in self._sites:
            site.close()

    def site(
        self, archive: dict[str, Any] | None = None, smtp_port: int | None = None
    ) -> QuickstartSite:
        site = QuickstartSite(
            self.monkeypatch,
            self.tmp_path,
            self.clock,
            self.server.port if smtp_port is None else smtp_port,
            {"enabled": True, "base_url": BASE} if archive is None else archive,
        )
        self._sites.append(site)
        return site

    # --- §2 ---------------------------------------------------------------------------------

    def test_s2_job_run_publishes_a_browsable_self_contained_archive(self) -> None:
        site = self.site()

        result = site.invoke("job", "run", "research")

        assert result.exit_code == 0, result.output
        page = site.archive_dir / "research" / f"{STAMP}.html"
        expected = {page, site.archive_dir / "research" / "index.html"}
        expected.add(site.archive_dir / "index.html")
        # step 3: the three files (plus the job's ``.job-name`` marker), 0644, no temp files
        assert set(site.files()) == expected | {site.archive_dir / "research" / ".job-name"}
        for path in site.files():
            assert stat.S_IMODE(path.stat().st_mode) == 0o644, path
        for directory in (site.archive_dir, site.archive_dir / "research"):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o755, directory
        # step 4/5: nothing external in any generated file
        for path in site.files():
            assert EXTERNAL.search(path.read_text(encoding="utf-8")) is None, path
        # step 6: the mail footer
        (message,) = self.server.messages
        text, html = parts(message)
        link = f"{BASE}/research/{STAMP}.html"
        assert f"Archived version: {link}" in text
        assert f'<a href="{link}">' in html
        # step 7: serve statically, open the mail link path and click through the indexes
        with StaticServer(site.archive_dir) as web:
            status, body = web.get(link.removeprefix(BASE))
            assert status == 200 and "2026-10-04 12:00 UTC" in body
            _, global_index = web.get("/index.html")
            (job_href,) = re.findall(r'href="([^"]+/index\.html)"', global_index)
            _, job_index = web.get(job_href)
            assert f'href="{STAMP}.html"' in job_index
            assert web.get(f"{job_href.rsplit('/', 1)[0]}/{STAMP}.html")[0] == 200

    # --- §3 ---------------------------------------------------------------------------------

    @pytest.mark.parametrize("name", ["../../etc/x", "日本語/ニュース"])
    def test_s3_hostile_job_name_stays_inside_the_archive(self, name: str) -> None:
        site = self.site()
        site.add_job(name)

        result = site.invoke("job", "run", name)

        assert result.exit_code == 0, result.output
        root = site.archive_dir.resolve()
        assert site.files()
        for path in site.files():
            assert path.resolve().is_relative_to(root)
            assert len(path.relative_to(site.archive_dir).parts) <= 2
        assert not (self.tmp_path.parent / "etc").exists()

    def test_s3_colliding_slugs_get_distinct_directories(self) -> None:
        site = self.site()
        site.add_job("A b")
        site.add_job("a-b")

        assert site.invoke("job", "run", "A b").exit_code == 0
        assert site.invoke("job", "run", "a-b").exit_code == 0

        dirs = sorted(p.name for p in site.archive_dir.iterdir() if p.is_dir())
        assert dirs[0] == "a-b"
        assert re.fullmatch(r"a-b-[0-9a-f]{8}", dirs[1])
        for directory in dirs:
            assert (site.archive_dir / directory / f"{STAMP}.html").is_file()

    def test_s3_two_runs_in_the_same_minute_keep_both_pages(self) -> None:
        site = self.site()
        assert site.invoke("job", "run", "research").exit_code == 0
        site.fresh_candidates("https://example.com/second")

        assert site.invoke("job", "run", "research").exit_code == 0

        job_dir = site.archive_dir / "research"
        assert (job_dir / f"{STAMP}.html").is_file()
        assert (job_dir / f"{STAMP}-2.html").is_file()
        text, _ = parts(self.server.messages[-1])
        assert f"{BASE}/research/{STAMP}-2.html" in text

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    def test_s3_unwritable_archive_dir_still_sends_without_link(self) -> None:
        site = self.site()
        site.archive_dir.mkdir()
        site.archive_dir.chmod(0o555)
        try:
            result = site.invoke("job", "run", "research")
        finally:
            site.archive_dir.chmod(0o755)

        assert result.exit_code == 0, result.output
        assert "archive.failed" in result.output
        assert list(site.archive_dir.iterdir()) == []
        (message,) = self.server.messages
        assert "Archived version" not in parts(message)[0]

    def test_s3_enabled_without_base_url_writes_the_page_but_no_link(self) -> None:
        site = self.site(archive={"enabled": True})

        assert site.invoke("job", "run", "research").exit_code == 0

        assert (site.archive_dir / "research" / f"{STAMP}.html").is_file()
        (message,) = self.server.messages
        assert "Archived version" not in parts(message)[0]

    def test_s3_empty_failed_and_dry_runs_change_nothing(self) -> None:
        site = self.site()
        assert site.invoke("job", "run", "research").exit_code == 0
        before = site.snapshot()

        # empty digest: the same candidates again are not new
        site.invoke("job", "run", "research")
        # dry run with new items (a digest that would be archived on a real run)
        site.fresh_candidates("https://example.com/dry")
        dry = site.invoke("job", "run", "research", "--dry-run")
        # failed run
        site.env.ports.sources[source_key(FEED)] = FetchError("timeout", url=FEED)
        site.invoke("job", "run", "research")

        assert dry.exit_code == 0, dry.output
        assert site.snapshot() == before
        assert len(site.deliveries) == 1

    def test_s3_notify_retry_after_a_failed_send_carries_the_link(self, closed_port: int) -> None:
        site = self.site(smtp_port=closed_port)
        site.invoke("job", "run", "research")  # the send fails; the page is written anyway
        assert self.server.messages == []
        page = site.archive_dir / "research" / f"{STAMP}.html"
        assert page.is_file()
        site.use_smtp_port(self.server.port)

        result = site.invoke("notify", "retry")

        assert result.exit_code == 0, result.output
        (message,) = self.server.messages
        assert f"Archived version: {BASE}/research/{STAMP}.html" in parts(message)[0]
