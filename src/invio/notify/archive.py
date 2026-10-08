"""Static HTML archive of digests: safe paths, atomic writes, pages and indexes.

The archive is a directory tree of plain files (no database): one directory per job with one
page per archived digest, a job index and a global index. Job names never reach the file system
unchanged: they are reduced to a slug, and every created path is checked to stay inside the
archive directory. Pages are published with a no-overwrite hard link, everything else is
replaced atomically, so a reader never sees a partial file and a page is never modified.
"""

import contextlib
import hashlib
import logging
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from invio.notify.payload import NotificationPayload
from invio.notify.render import (
    IndexEntry,
    render_archive_page,
    render_global_index,
    render_job_index,
)

__all__ = [
    "ArchiveError",
    "ArchivedPage",
    "archive_digest",
    "archive_url",
    "job_directory",
    "publish_new",
    "rebuild_global_index",
    "rebuild_job_index",
    "slugify_job_name",
    "write_atomic",
]

logger = logging.getLogger(__name__)

_MARKER = ".job-name"
_INDEX = "index.html"
_MAX_SLUG = 48
_FALLBACK_SLUG = "job"
_MAX_SUFFIX = 10_000
# [0-9], not \d: \d matches any Unicode digit.
_PAGE_NAME = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4})(?:-([0-9]+))?\.html$")
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_FILE_MODE = 0o644
_DIR_MODE = 0o755


class ArchiveError(OSError):
    """A path would leave the archive directory (for example a symlinked job directory)."""


@dataclass(frozen=True, slots=True)
class ArchivedPage:
    """A page that was written: the job directory name and the page file name."""

    job_slug: str
    name: str

    @property
    def relative_path(self) -> str:
        return f"{self.job_slug}/{self.name}"


def _encode(text: str) -> bytes:
    # surrogatepass: a job name with a lone surrogate must hash and be stored without raising.
    return text.encode("utf-8", "surrogatepass")


def slugify_job_name(name: str) -> str:
    """Reduce ``name`` to ``^[a-z0-9]+(-[a-z0-9]+)*$`` (at most 48 characters, ``job`` if empty)."""
    ascii_text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = _NON_SLUG.sub("-", ascii_text.lower()).strip("-")[:_MAX_SLUG].strip("-")
    return slug or _FALLBACK_SLUG


def _inside(archive_dir: Path, path: Path) -> bool:
    return path.resolve().is_relative_to(archive_dir.resolve())


def _marker_matches(directory: Path, name: str) -> bool | None:
    """``True``/``False`` when the directory has a marker (equal / different), else ``None``."""
    try:
        return (directory / _MARKER).read_bytes() == _encode(name)
    except FileNotFoundError:
        return None


def job_directory(archive_dir: Path, name: str) -> Path:
    """Return the directory for job ``name``; nothing is created.

    The slug is used unless the directory belongs to another job name (its ``.job-name``
    differs, or it holds foreign content without a marker); then ``<slug>-<sha256(name)[:8]>``.
    Raises :class:`ArchiveError` for a symlinked job directory or a path outside the archive.
    """
    slug = slugify_job_name(name)
    candidate = archive_dir / slug
    if candidate.is_symlink():
        raise ArchiveError(f"archive job directory {slug!r} is a symlink")
    if candidate.is_dir():
        owner = _marker_matches(candidate, name)
        taken = owner is False or (owner is None and any(candidate.iterdir()))
        if taken:
            full = hashlib.sha256(_encode(name)).hexdigest()
            for length in (8, 16, 32, 64):
                candidate = archive_dir / f"{slug}-{full[:length]}"
                if candidate.is_symlink():
                    raise ArchiveError(f"archive job directory {candidate.name!r} is a symlink")
                if not candidate.is_dir():
                    break
                owner = _marker_matches(candidate, name)
                if owner is True or (owner is None and not any(candidate.iterdir())):
                    break
            else:
                raise ArchiveError(f"no free archive directory for job {name!r}")
    if candidate.parent != archive_dir or not _inside(archive_dir, candidate):
        raise ArchiveError(f"archive path {candidate} is outside the archive directory")
    return candidate


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temp(directory: Path, name: str, data: bytes) -> Path:
    """Write ``data`` to a fresh temp file next to the target (mode 0644, fsynced)."""
    temp = directory / f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, _FILE_MODE)  # explicit: the umask must not decide who can read it
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    return temp


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step; no temp file remains, even on failure."""
    temp = _write_temp(path.parent, path.name, _encode(text))
    try:
        os.replace(temp, path)
        with contextlib.suppress(OSError):  # already in place; a failed sync is not a failed write
            _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


def publish_new(directory: Path, stem: str, text: str) -> str:
    """Publish ``text`` as ``<stem>.html`` without overwriting; return the name actually used.

    A taken name moves on to ``<stem>-2.html``, ``-3``, ... The no-overwrite step is a hard
    link, which is atomic and fails with ``FileExistsError`` instead of replacing a page.
    """
    temp = _write_temp(directory, f"{stem}.html", _encode(text))
    try:
        for number in range(1, _MAX_SUFFIX):
            name = f"{stem}.html" if number == 1 else f"{stem}-{number}.html"
            try:
                os.link(temp, directory / name)
            except FileExistsError:
                continue
            with contextlib.suppress(OSError):  # the page is published; do not report a failure
                _fsync_directory(directory)
            return name
        raise ArchiveError(f"no free page name for {stem}")
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


def _page_files(job_dir: Path) -> list[tuple[str, int, str]]:
    """Return ``(stamp, suffix, file name)`` of the pages in ``job_dir``, newest first."""
    pages: list[tuple[str, int, str]] = []
    for entry in job_dir.iterdir():
        match = _PAGE_NAME.match(entry.name)
        if match is not None and entry.is_file() and not entry.is_symlink():
            pages.append((match.group(1), int(match.group(2) or 1), entry.name))
    pages.sort(reverse=True)
    return pages


def _label(stamp: str) -> str:
    return f"{stamp[:10]} {stamp[11:13]}:{stamp[13:15]} UTC"


def _read_job_name(job_dir: Path) -> str | None:
    try:
        return (job_dir / _MARKER).read_bytes().decode("utf-8", "surrogatepass")
    except FileNotFoundError:
        return None


def rebuild_job_index(job_dir: Path) -> None:
    """Rewrite ``index.html`` of one job from the pages in its directory (newest first)."""
    name = _read_job_name(job_dir)
    if name is None:
        raise ArchiveError(f"{job_dir} has no {_MARKER} marker")
    entries = [
        IndexEntry(
            href=file_name,
            label=_label(stamp) + (f" #{suffix}" if suffix > 1 else ""),
            detail="",
        )
        for stamp, suffix, file_name in _page_files(job_dir)
    ]
    write_atomic(job_dir / _INDEX, render_job_index(name, entries))


def rebuild_global_index(archive_dir: Path) -> None:
    """Rewrite the top-level ``index.html`` from the job directories that hold pages."""
    jobs: list[tuple[str, IndexEntry]] = []
    for job_dir in archive_dir.iterdir():
        if job_dir.is_symlink() or not job_dir.is_dir():
            continue
        try:
            name = _read_job_name(job_dir)
            pages = _page_files(job_dir) if name is not None else []
        except (OSError, ValueError):
            continue
        if name is None or not pages:
            continue
        count = len(pages)
        detail = f"{count} page{'s' if count != 1 else ''} · latest {_label(pages[0][0])}"
        jobs.append((name, IndexEntry(f"{job_dir.name}/{_INDEX}", name, detail)))
    jobs.sort(key=lambda job: (job[0].casefold(), job[0]))
    write_atomic(archive_dir / _INDEX, render_global_index([entry for _, entry in jobs]))


def _make_directory(path: Path, *, parents: bool = False) -> None:
    """Create ``path`` with mode 0755 regardless of the umask; leave an existing one alone."""
    try:
        path.mkdir(mode=_DIR_MODE, parents=parents)
    except FileExistsError:
        return
    os.chmod(path, _DIR_MODE)


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def archive_digest(
    archive_dir: Path,
    *,
    run_started_at: datetime,
    payload: NotificationPayload,
    digest_markdown: str,
) -> ArchivedPage:
    """Write the page of one digest, then rebuild the job and the global index.

    Raises :class:`OSError` when the page cannot be written (nothing is left behind); a failing
    index is logged as ``archive.index_failed`` and does not raise. ``run_started_at`` names the
    page by its UTC minute; a second digest in the same minute gets a ``-2`` suffix.
    """
    _make_directory(archive_dir, parents=True)
    job_dir = job_directory(archive_dir, payload.job_name)
    _make_directory(job_dir)
    if not _inside(archive_dir, job_dir):
        raise ArchiveError(f"archive path {job_dir} is outside the archive directory")
    if _marker_matches(job_dir, payload.job_name) is None:
        write_atomic(job_dir / _MARKER, payload.job_name)
    moment = run_started_at if run_started_at.tzinfo else run_started_at.replace(tzinfo=UTC)
    html = render_archive_page(payload, digest_markdown, run_started_at=moment, index_href=_INDEX)
    stem = moment.astimezone(UTC).strftime("%Y-%m-%d-%H%M")
    page = ArchivedPage(job_slug=job_dir.name, name=publish_new(job_dir, stem, html))
    try:
        rebuild_job_index(job_dir)
    except (OSError, ValueError) as exc:
        logger.warning("archive.index_failed", extra={"index": page.job_slug, "error": _error(exc)})
    try:
        rebuild_global_index(archive_dir)
    except (OSError, ValueError) as exc:
        logger.warning("archive.index_failed", extra={"index": "global", "error": _error(exc)})
    return page


def archive_url(
    base_url: str | None, archive_dir: Path, page_path: str | None, *, enabled: bool
) -> str | None:
    """The public URL of an archived page, or ``None`` when no link should be shown.

    Needs the archive enabled, a ``base_url``, a stored page path and the page file to exist
    (inside ``archive_dir``) at the time of the call.
    """
    if not enabled or not base_url or not page_path:
        return None
    page = archive_dir / page_path
    try:
        if not page.is_file() or not _inside(archive_dir, page):
            return None
    except OSError:
        return None
    return f"{base_url.rstrip('/')}/{page_path}"
