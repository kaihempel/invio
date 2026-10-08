# Contract: Python API

## `invio.notify.archive` (new)

```python
def slugify_job_name(name: str) -> str:
    ...
    # pure; result matches ^[a-z0-9]+(-[a-z0-9]+)*$ and is at most 48 characters


@dataclass(frozen=True, slots=True)
class ArchivedPage:
    job_slug: str
    name: str  # e.g. "2026-10-08-0930.html"

    @property
    def relative_path(self) -> str: ...  # "<job_slug>/<name>"


def archive_digest(
    archive_dir: Path,
    *,
    job_name: str,
    run_started_at: datetime,  # aware; converted to UTC for the page name
    payload: NotificationPayload,
    digest_markdown: str,
) -> ArchivedPage:
    ...
    # writes page, then job index and global index; raises OSError on I/O failure of the page.
    # An index failure is logged (archive.index_failed) and does not raise.


def archive_url(
    base_url: str | None, archive_dir: Path, page_path: str | None, *, enabled: bool
) -> str | None:
    ...
    # None unless enabled, base_url set, page_path set and the file exists under archive_dir
```

Internal helpers (module-level, tested directly): `job_directory(archive_dir, name) -> Path`,
`write_atomic(path, text) -> None`, `publish_new(directory, stem, text) -> str` (returns the
file name actually used), `rebuild_job_index(job_dir)`, `rebuild_global_index(archive_dir)`.
`ArchiveError(OSError)` is raised for a path that would leave `archive_dir` (e.g. a symlinked
job directory); the delivery treats it like any other archive failure.

Guarantees: every created path is inside `archive_dir` (asserted on the resolved path); no
temp file remains; an existing page is never overwritten; never touches anything when
`archive_dir` cannot be created (raises `OSError`).

## `invio.notify.render`

```python
def render_mail(
    payload: NotificationPayload, digest_markdown: str, *, archive_url: str | None = None
) -> RenderedMail: ...
def render_archive_page(
    payload: NotificationPayload,
    digest_markdown: str,
    *,
    run_started_at: datetime,  # aware; shown as "YYYY-MM-DD HH:MM UTC" (payload.digest_date is in the job's time zone and is not used for the page)
    index_href: str,
) -> str: ...
```

`archive_url` set → HTML and plain-text footers contain it (HTML: an escaped link; text: the
bare URL on its own line). `None` → footers unchanged from #20.

## `invio.notify.payload`

`NotificationPayload.archive_page: str | None = None` (see data-model).

## `invio.notify.email`

`deliver_digest(...)` signature unchanged. Behaviour added: for an archive-enabled job and a
non-empty digest it calls `archive_digest` (in a worker thread) before rendering; failures are
logged and swallowed; on success the payload stored on the notification rows carries
`archive_page` and the mail carries the link if `base_url` is set. `retry_failed(...)`
rebuilds the mail with `archive_url(...)` computed from the job's current config and the stored
`archive_page`.

## `invio.config.job`

`ArchiveConfig` (strict, frozen like the other sections); `JobConfig.archive: ArchiveConfig`.

## `invio.config.settings`

`Settings.archive_dir: Path = Path("/var/lib/invio/archive")`.
