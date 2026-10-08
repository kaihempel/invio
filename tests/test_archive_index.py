"""Tests for the job and global index pages."""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from invio.notify import archive
from invio.notify.archive import archive_digest, rebuild_global_index, rebuild_job_index
from invio.notify.payload import NotificationPayload

STARTED = datetime(2026, 10, 8, 9, 30, tzinfo=UTC)


def _payload(name: str) -> NotificationPayload:
    return NotificationPayload.model_validate(
        {
            "job_name": name,
            "subject": "s",
            "digest_date": "2026-10-08",
            "is_empty": False,
            "stats": {"items_found": 1, "items_included": 1, "duration_seconds": 1},
        }
    )


def _archive(root: Path, name: str, started: datetime = STARTED) -> str:
    page = archive_digest(
        root,
        run_started_at=started,
        payload=_payload(name),
        digest_markdown="body",
    )
    return page.name


def test_first_digest_creates_both_indexes_linking_the_page(tmp_path: Path) -> None:
    name = _archive(tmp_path, "News")

    job_index = (tmp_path / "news" / "index.html").read_text(encoding="utf-8")
    global_index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert f'href="{name}"' in job_index
    assert "2026-10-08 09:30 UTC" in job_index
    assert 'href="../index.html"' in job_index
    assert 'href="news/index.html"' in global_index
    assert "1 page " in global_index
    assert "latest 2026-10-08 09:30 UTC" in global_index


def test_job_index_lists_pages_newest_first(tmp_path: Path) -> None:
    for minutes in (0, 120, 60):
        _archive(tmp_path, "News", STARTED + timedelta(minutes=minutes))
    _archive(tmp_path, "News", STARTED)  # same minute as the first: gets -2

    html = (tmp_path / "news" / "index.html").read_text(encoding="utf-8")

    order = [
        "2026-10-08-1130.html",
        "2026-10-08-1030.html",
        "2026-10-08-0930-2.html",
        "2026-10-08-0930.html",
    ]
    positions = [html.index(f'href="{name}"') for name in order]
    assert positions == sorted(positions)
    assert "#2" in html


def test_global_index_lists_each_job_once_sorted_case_insensitively(tmp_path: Path) -> None:
    _archive(tmp_path, "zebra")
    _archive(tmp_path, "Apple")
    _archive(tmp_path, "apple pie")
    _archive(tmp_path, "Apple", STARTED + timedelta(days=1))

    html = (tmp_path / "index.html").read_text(encoding="utf-8")

    assert html.count('href="apple/index.html"') == 1
    assert html.count('href="zebra/index.html"') == 1
    assert html.index("apple/index.html") < html.index("apple-pie/index.html")
    assert html.index("apple-pie/index.html") < html.index("zebra/index.html")
    assert "2 pages · latest 2026-10-09 09:30 UTC" in html


def test_indexes_are_rebuilt_from_the_directory(tmp_path: Path) -> None:
    _archive(tmp_path, "News")
    job_dir = tmp_path / "news"
    (job_dir / "2026-01-01-0000.html").write_text("old", encoding="utf-8")
    (job_dir / "notes.html").write_text("foreign", encoding="utf-8")
    (job_dir / "index.html").unlink()
    (tmp_path / "index.html").unlink()

    rebuild_job_index(job_dir)
    rebuild_global_index(tmp_path)

    job_index = (job_dir / "index.html").read_text(encoding="utf-8")
    assert 'href="2026-01-01-0000.html"' in job_index
    assert "notes.html" not in job_index
    assert "2 pages" in (tmp_path / "index.html").read_text(encoding="utf-8")


def test_directories_without_marker_or_pages_are_not_listed(tmp_path: Path) -> None:
    _archive(tmp_path, "News")
    (tmp_path / "nomarker").mkdir()
    (tmp_path / "nomarker" / "2026-01-01-0000.html").write_text("x", encoding="utf-8")
    (tmp_path / "nopages").mkdir()
    (tmp_path / "nopages" / ".job-name").write_text("No pages", encoding="utf-8")
    (tmp_path / "stray.txt").write_text("x", encoding="utf-8")

    rebuild_global_index(tmp_path)

    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "nomarker" not in html
    assert "nopages" not in html
    assert "No pages" not in html
    assert 'href="news/index.html"' in html


def test_index_text_is_escaped(tmp_path: Path) -> None:
    _archive(tmp_path, "<b>x</b>")

    for index in (tmp_path / "index.html", next(tmp_path.glob("*/index.html"))):
        html = index.read_text(encoding="utf-8")
        assert "<b>x</b>" not in html
        assert "&lt;b&gt;x&lt;/b&gt;" in html


def test_index_write_failure_is_logged_and_the_page_is_kept(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = archive.write_atomic

    def fail(path: Path, text: str) -> None:
        if path.name == "index.html":
            raise OSError("no space")
        real(path, text)

    monkeypatch.setattr(archive, "write_atomic", fail)
    with caplog.at_level(logging.WARNING, logger="invio.notify.archive"):
        name = _archive(tmp_path, "News")

    assert (tmp_path / "news" / name).is_file()
    assert any(record.getMessage() == "archive.index_failed" for record in caplog.records)
    assert not list(tmp_path.rglob("*.tmp"))
