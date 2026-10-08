"""The archive through ``run_job``: only runs that reach the notify stage write pages (FR-004/014).

The pipeline runs over the fakes of ``tests/pipeline_helpers.py``; only the notifier is replaced
by the real ``deliver_digest`` against the in-process SMTP sink and a ``tmp_path`` archive.
"""

import dataclasses
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from invio.config.job import ScheduleConfig
from invio.domain import RunStatus
from invio.graph.ports import DeliveryReport
from invio.llm.base import LLMInvalidRequestError
from invio.notify.email import deliver_digest
from invio.notify.payload import NotificationPayload
from invio.pipeline.run import RunResult, run_job
from invio.sources.errors import FetchError, TooLargeError
from tests.conftest import FakeClock
from tests.pipeline_helpers import (
    Env,
    RoutedFakeProvider,
    build_env,
    make_candidates,
    make_job_config,
    source_key,
)
from tests.smtp_helpers import (
    SmtpServer,
    closed_port,  # noqa: F401
    parts,
    rows,
    smtp_server,  # noqa: F401
    smtp_settings,
)

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_jobs")]

NextRun = Callable[[ScheduleConfig, datetime], datetime]
BASE = "https://digests.example.org/invio"
FEED = "https://example.com/feed.xml"
# ``build_env`` stores the job as ``research``; ``fake_clock`` starts at 2026-10-04 12:00 UTC.
PAGE = "research/2026-10-04-1200.html"


def tree(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every file below ``root`` with its content and mtime: equal snapshots = nothing touched."""
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class ArchivePipeline:
    """A stored job whose notify stage delivers for real into ``archive_dir`` via SMTP."""

    def __init__(
        self,
        engine: Engine,
        clock: FakeClock,
        next_run: NextRun,
        archive_dir: Path,
        smtp_port: int,
        **env_kwargs: Any,
    ) -> None:
        config = env_kwargs.pop("config", None) or make_job_config(
            archive={"enabled": True, "base_url": BASE}
        )
        self.engine = engine
        self.archive_dir = archive_dir
        self.settings = smtp_settings(
            smtp_host="127.0.0.1", smtp_port=smtp_port, archive_dir=archive_dir
        )
        self.env: Env = build_env(engine, clock, next_run, config=config, **env_kwargs)
        self.deliveries: list[int] = []
        self._clock = clock

        async def notify(digest_id: int) -> DeliveryReport:
            self.deliveries.append(digest_id)
            outcome = await deliver_digest(
                self.env.factory, digest_id, settings=self.settings, clock=self._clock
            )
            return DeliveryReport(sent=outcome.sent, failed=outcome.failed, error=outcome.error)

        self.env.deps = dataclasses.replace(self.env.deps, notify=notify)

    async def run(self, *, dry_run: bool = False) -> RunResult:
        return await run_job(self.env.job_id, dry_run=dry_run, deps=self.env.deps)

    def feed(self, prefix: str) -> None:
        """Let the next run see three new candidates."""
        self.env.ports.sources[source_key(FEED)] = make_candidates(3, prefix=prefix)

    def payload_pages(self) -> set[str | None]:
        return {
            NotificationPayload.model_validate(r.payload).archive_page for r in rows(self.engine)
        }


class TestArchiveThroughPipeline:
    """Spec US1 scenarios 1/3/4, FR-004, FR-012 and FR-014 at the run level."""

    @pytest.fixture(autouse=True)
    def setup(
        self,
        db_engine: Engine,
        fake_clock: FakeClock,
        recording_next_run: NextRun,
        tmp_path: Path,
        smtp_server: SmtpServer,
    ) -> None:
        self.engine = db_engine
        self.clock = fake_clock
        self.next_run = recording_next_run
        self.archive_dir = tmp_path / "archive"
        self.server = smtp_server

    def pipeline(self, *, smtp_port: int | None = None, **env_kwargs: Any) -> ArchivePipeline:
        port = self.server.port if smtp_port is None else smtp_port
        return ArchivePipeline(
            self.engine, self.clock, self.next_run, self.archive_dir, port, **env_kwargs
        )

    async def test_succeeded_run_writes_page_indexes_and_mails_the_link(self) -> None:
        pipeline = self.pipeline()

        result = await pipeline.run()

        assert result.status == RunStatus.SUCCEEDED
        page = self.archive_dir / PAGE
        assert page.is_file()
        assert "2026-10-04 12:00 UTC" in page.read_text(encoding="utf-8")
        assert 'href="2026-10-04-1200.html"' in (
            self.archive_dir / "research" / "index.html"
        ).read_text(encoding="utf-8")
        assert 'href="research/index.html"' in (self.archive_dir / "index.html").read_text(
            encoding="utf-8"
        )
        assert pipeline.payload_pages() == {PAGE}
        (message,) = self.server.messages
        text, html = parts(message)
        assert f"Archived version: {BASE}/{PAGE}" in text
        assert f'<a href="{BASE}/{PAGE}">' in html

    async def test_partial_run_with_a_digest_is_archived(self) -> None:
        bad = "https://example.com/post-2"
        pipeline = self.pipeline(pages={bad: TooLargeError(url=bad, limit=1)})

        result = await pipeline.run()

        assert result.status == RunStatus.PARTIAL
        assert (self.archive_dir / PAGE).is_file()

    @pytest.mark.parametrize("send_if_empty", [False, True])
    async def test_failed_run_with_failing_items_creates_nothing(self, send_if_empty: bool) -> None:
        provider = RoutedFakeProvider(
            relevance=LLMInvalidRequestError("bad", provider="fakeco", model="m", status=400)
        )
        config = make_job_config(
            archive={"enabled": True, "base_url": BASE},
            notification__send_if_empty=send_if_empty,
        )
        pipeline = self.pipeline(provider=provider, config=config)

        result = await pipeline.run()

        assert result.status == RunStatus.FAILED
        assert pipeline.deliveries == []
        assert not self.archive_dir.exists()
        assert self.server.messages == []

    async def test_failed_run_with_failing_sources_leaves_an_existing_archive_alone(
        self,
    ) -> None:
        pipeline = self.pipeline()
        await pipeline.run()
        before = tree(self.archive_dir)
        pipeline.env.ports.sources[source_key(FEED)] = FetchError("timeout", url=FEED)

        result = await pipeline.run()

        assert result.status == RunStatus.FAILED
        assert tree(self.archive_dir) == before

    async def test_dry_run_creates_no_archive(self) -> None:
        pipeline = self.pipeline()

        result = await pipeline.run(dry_run=True)

        assert result.status == RunStatus.SUCCEEDED
        assert result.digest is not None and len(result.digest.item_ids) == 3
        assert pipeline.deliveries == []
        assert not self.archive_dir.exists()

    async def test_dry_run_with_a_digest_leaves_pages_and_indexes_untouched(self) -> None:
        pipeline = self.pipeline()
        await pipeline.run()
        before = tree(self.archive_dir)
        pipeline.feed("https://example.com/new")

        result = await pipeline.run(dry_run=True)

        assert result.digest is not None and len(result.digest.item_ids) == 3
        assert tree(self.archive_dir) == before

    async def test_empty_digest_run_leaves_pages_and_indexes_untouched(self) -> None:
        config = make_job_config(
            archive={"enabled": True, "base_url": BASE}, notification__send_if_empty=True
        )
        pipeline = self.pipeline(config=config)
        await pipeline.run()
        before = tree(self.archive_dir)

        result = await pipeline.run()  # same candidates again: nothing new, empty digest

        assert result.status == RunStatus.SUCCEEDED
        assert len(pipeline.deliveries) == 2  # the empty digest was mailed ...
        assert tree(self.archive_dir) == before  # ... but not archived
        text, _ = parts(self.server.messages[-1])
        assert "Archived version" not in text

    async def test_archive_disabled_run_creates_nothing(self) -> None:
        pipeline = self.pipeline(config=make_job_config(archive={"enabled": False}))

        result = await pipeline.run()

        assert result.status == RunStatus.SUCCEEDED
        assert len(pipeline.deliveries) == 1
        assert not self.archive_dir.exists()

    async def test_unwritable_archive_keeps_the_run_succeeded_and_logs_the_cause(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        self.archive_dir.write_text("a file where the directory should be", encoding="utf-8")
        pipeline = self.pipeline()

        with caplog.at_level(logging.WARNING, logger="invio.notify.email"):
            result = await pipeline.run()

        assert result.status == RunStatus.SUCCEEDED
        assert (result.notifications_sent, result.notifications_failed) == (1, 0)
        (record,) = [r for r in caplog.records if r.getMessage() == "archive.failed"]
        assert "Error" in getattr(record, "error", "")  # exception class: the cause is logged
        assert pipeline.payload_pages() == {None}
        assert "Archived version" not in parts(self.server.messages[0])[0]

    async def test_failed_mail_still_archives_and_records_the_page(self, closed_port: int) -> None:
        pipeline = self.pipeline(smtp_port=closed_port)

        result = await pipeline.run()

        assert result.status == RunStatus.PARTIAL  # delivery failed, the run did not
        assert (self.archive_dir / PAGE).is_file()
        assert pipeline.payload_pages() == {PAGE}  # a later retry can still link it
