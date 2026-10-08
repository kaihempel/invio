"""Tests for the archive file primitives, page publishing, safety and the mail URL."""

import logging
import os
import re
import stat
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from invio.notify import archive
from invio.notify.archive import (
    ArchiveError,
    archive_digest,
    archive_url,
    publish_new,
    write_atomic,
)
from invio.notify.payload import NotificationPayload
from tests.test_archive_slug import HOSTILE_NAMES

STARTED = datetime(2026, 10, 8, 9, 30, 5, tzinfo=UTC)


def _payload(job_name: str = "ai-news") -> NotificationPayload:
    return NotificationPayload.model_validate(
        {
            "job_name": job_name,
            "subject": "invio: digest",
            "digest_date": "2026-10-08",
            "is_empty": False,
            "stats": {"items_found": 7, "items_included": 2, "duration_seconds": 192.4},
        }
    )


def _archive(archive_dir: Path, name: str = "ai-news", **kwargs: Any) -> archive.ArchivedPage:
    values: dict[str, Any] = {
        "run_started_at": STARTED,
        "payload": _payload().model_copy(update={"job_name": name}),
        "digest_markdown": "# News\n\n- [item](https://example.com/a?x=1&y=2)",
    }
    values.update(kwargs)
    return archive_digest(archive_dir, **values)


def _tmp_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.name.endswith(".tmp")]


def test_write_atomic_writes_with_mode_0644_and_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "index.html"
    previous = os.umask(0o077)
    try:
        write_atomic(target, "héllo")
    finally:
        os.umask(previous)

    assert target.read_text(encoding="utf-8") == "héllo"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert _tmp_files(tmp_path) == []


def test_write_atomic_replaces_an_existing_file_whole(tmp_path: Path) -> None:
    target = tmp_path / "index.html"
    target.write_text("old content that is longer", encoding="utf-8")

    write_atomic(target, "new")

    assert target.read_text(encoding="utf-8") == "new"


def test_write_atomic_failure_leaves_no_temp_file_and_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "index.html"
    target.write_text("old", encoding="utf-8")

    def boom(_fd: int) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="disk on fire"):
        write_atomic(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert _tmp_files(tmp_path) == []


def test_publish_new_never_overwrites(tmp_path: Path) -> None:
    names = [publish_new(tmp_path, "2026-10-08-0930", f"page {n}") for n in range(3)]

    assert names == ["2026-10-08-0930.html", "2026-10-08-0930-2.html", "2026-10-08-0930-3.html"]
    assert (tmp_path / names[0]).read_text(encoding="utf-8") == "page 0"
    assert (tmp_path / names[2]).read_text(encoding="utf-8") == "page 2"
    assert stat.S_IMODE((tmp_path / names[0]).stat().st_mode) == 0o644
    assert _tmp_files(tmp_path) == []


def test_publish_new_threads_get_distinct_names(tmp_path: Path) -> None:
    results: list[str] = []
    barrier = threading.Barrier(8, timeout=10)

    def work(n: int) -> None:
        barrier.wait()
        results.append(publish_new(tmp_path, "stem", f"text {n}"))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(set(results)) == 8
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(results)


# --- archive_digest: layout, modes, safety ---------------------------------------------------


def test_archive_digest_creates_the_layout_with_explicit_modes(tmp_path: Path) -> None:
    archive_dir = tmp_path / "not" / "yet" / "there"
    previous = os.umask(0o077)
    try:
        page = _archive(archive_dir)
    finally:
        os.umask(previous)

    job_dir = archive_dir / "ai-news"
    assert page.relative_path == "ai-news/2026-10-08-0930.html"
    assert (job_dir / "2026-10-08-0930.html").is_file()
    assert (job_dir / ".job-name").read_text(encoding="utf-8") == "ai-news"
    assert stat.S_IMODE(job_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(archive_dir.stat().st_mode) == 0o755
    for path in archive_dir.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o644, path
    assert _tmp_files(archive_dir) == []


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_hostile_job_names_create_only_safe_paths_inside_the_archive(
    tmp_path: Path, name: str
) -> None:
    archive_dir = tmp_path / "archive"

    _archive(archive_dir, name)

    assert [path.name for path in tmp_path.iterdir()] == ["archive"]
    for path in archive_dir.rglob("*"):
        assert path.resolve().is_relative_to(archive_dir.resolve())
        assert "/" not in path.name
        assert "\\" not in path.name
        assert "\x00" not in path.name
    assert _tmp_files(archive_dir) == []


def test_symlinked_job_directory_is_refused(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (archive_dir / "ai-news").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArchiveError):
        _archive(archive_dir)

    assert list(outside.iterdir()) == []


def test_no_temp_file_remains_after_a_failed_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("link refused")

    monkeypatch.setattr(os, "link", boom)
    with pytest.raises(OSError, match="link refused"):
        _archive(tmp_path / "archive")

    assert _tmp_files(tmp_path) == []
    assert not list((tmp_path / "archive").rglob("*.html"))


def test_unusable_archive_dir_raises_oserror(tmp_path: Path) -> None:
    blocker = tmp_path / "archive"
    blocker.write_text("a file where the directory should be", encoding="utf-8")

    with pytest.raises(OSError):
        _archive(blocker)


FORBIDDEN = ("<script", "<link", "<img", "<iframe", "<form", "src=", "@import", "url(")


def _assert_self_contained(html: str) -> None:
    lowered = html.lower()
    for needle in FORBIDDEN:
        assert needle not in lowered, needle
    # External addresses may only appear as link targets.
    for match in re.finditer(r"https?://", html):
        assert html[: match.start()].endswith('href="'), html[match.start() - 20 : match.start()]
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert "overflow-wrap" in html
    assert "overflow-x:auto" in html


def test_generated_files_are_self_contained_and_narrow_screen_ready(tmp_path: Path) -> None:
    _archive(tmp_path)

    files = [tmp_path / "ai-news" / "2026-10-08-0930.html"]
    files += [tmp_path / "ai-news" / "index.html", tmp_path / "index.html"]
    for file in files:
        _assert_self_contained(file.read_text(encoding="utf-8"))
    page = files[0].read_text(encoding="utf-8")
    assert 'href="https://example.com/a?x=1&amp;y=2"' in page


def test_same_minute_digests_get_a_suffix_and_keep_their_own_text(tmp_path: Path) -> None:
    first = _archive(tmp_path, digest_markdown="first digest")
    second = _archive(tmp_path, digest_markdown="second digest")

    assert (first.name, second.name) == ("2026-10-08-0930.html", "2026-10-08-0930-2.html")
    assert "first digest" in (tmp_path / "ai-news" / first.name).read_text(encoding="utf-8")
    assert "second digest" in (tmp_path / "ai-news" / second.name).read_text(encoding="utf-8")


def test_page_is_named_by_its_utc_minute(tmp_path: Path) -> None:
    local = datetime(2026, 10, 9, 0, 45, 59, tzinfo=timezone(timedelta(hours=2)))

    page = _archive(tmp_path, run_started_at=local)

    assert page.name == "2026-10-08-2245.html"


def test_naive_start_is_taken_as_utc(tmp_path: Path) -> None:
    page = _archive(tmp_path, run_started_at=datetime(2026, 1, 2, 3, 4))

    assert page.name == "2026-01-02-0304.html"


def test_colliding_job_names_use_separate_directories(tmp_path: Path) -> None:
    first = _archive(tmp_path, "A b")
    second = _archive(tmp_path, "a-b")
    again = _archive(tmp_path, "A b", run_started_at=STARTED + timedelta(minutes=1))

    assert first.job_slug == "a-b"
    assert second.job_slug != first.job_slug
    assert again.job_slug == first.job_slug


def test_index_failure_is_logged_and_the_page_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = archive.write_atomic

    def fail_for_indexes(path: Path, text: str) -> None:
        if path.name == "index.html":
            raise OSError("index disk full")
        real(path, text)

    monkeypatch.setattr(archive, "write_atomic", fail_for_indexes)
    with caplog.at_level(logging.WARNING, logger="invio.notify.archive"):
        page = _archive(tmp_path)

    assert (tmp_path / "ai-news" / page.name).is_file()
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("archive.index_failed") == 2
    assert _tmp_files(tmp_path) == []


# --- archive_url ---------------------------------------------------------------------------


def test_archive_url_needs_every_condition(tmp_path: Path) -> None:
    page = _archive(tmp_path)
    path = page.relative_path
    base = "https://h/x"

    assert archive_url(base, tmp_path, path, enabled=True) == f"{base}/{path}"
    assert archive_url(base + "/", tmp_path, path, enabled=True) == f"{base}/{path}"
    assert archive_url(base, tmp_path, path, enabled=False) is None
    assert archive_url(None, tmp_path, path, enabled=True) is None
    assert archive_url(base, tmp_path, None, enabled=True) is None
    assert archive_url(base, tmp_path, "ai-news/2020-01-01-0000.html", enabled=True) is None


def test_archive_url_is_none_once_the_page_is_deleted(tmp_path: Path) -> None:
    page = _archive(tmp_path)
    (tmp_path / "ai-news" / page.name).unlink()

    assert archive_url("https://h", tmp_path, page.relative_path, enabled=True) is None


def test_archive_url_does_not_follow_a_path_out_of_the_archive(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (tmp_path / "secret.html").write_text("x", encoding="utf-8")

    assert archive_url("https://h", archive_dir, "../secret.html", enabled=True) is None
