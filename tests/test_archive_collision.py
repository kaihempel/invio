"""FR-009 corner: the hashed fallback directory may itself belong to another job."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from invio.notify.archive import archive_digest
from invio.notify.payload import NotificationPayload

STARTED = datetime(2026, 10, 8, 9, 30, tzinfo=UTC)


class TestHashedDirectoryOwnership:
    """``job_directory`` checks the marker of ``<slug>`` but not of ``<slug>-<hash8>``."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.payload = NotificationPayload.model_validate(
            {
                "job_name": "x",
                "subject": "s",
                "digest_date": "2026-10-08",
                "is_empty": False,
                "stats": {"items_found": 1, "items_included": 1, "duration_seconds": 1},
            }
        )

    def archive(self, name: str) -> str:
        page = archive_digest(
            self.root,
            job_name=name,
            run_started_at=STARTED,
            payload=self.payload,
            digest_markdown=f"digest of {name}",
        )
        return page.job_slug

    def test_job_named_like_a_hashed_directory_does_not_absorb_the_colliding_job(self) -> None:
        crafted = "a-b-" + hashlib.sha256(b"A b").hexdigest()[:8]
        slugs = [self.archive(name) for name in ("a-b", crafted, "A b")]

        assert len(set(slugs)) == 3
