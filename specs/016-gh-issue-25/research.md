# Research: Static Digest Archive (HTML)

No `NEEDS CLARIFICATION` remained after `/speckit-clarify`; the points below are design decisions.

## R1. Where the archive step runs

- **Decision**: inside `_deliver()` (`invio.notify.email`), after the payload is built and before
  the notification rows/mail are rendered. It runs for every stored non-empty digest of an
  archive-enabled job, independent of SMTP settings or recipients.
- **Rationale**: the issue says "after `persist`"; the `notify` graph stage is exactly that
  point (not run for dry runs or failed runs, which also satisfies "successful run" and FR-014
  without extra checks; `partial` runs that produce a digest are archived like they are mailed).
  Doing it before rendering the mail gives FR-011 (link only if written) without a second pass.
- **Alternatives**: a separate graph stage and `RunDeps.archive` callable (more wiring, and the
  mail would need the result passed through state); a post-send step (mail could not carry the
  link).

## R2. Where the code lives

- **Decision**: new module `invio/notify/archive.py` plus templates in the existing template
  package.
- **Rationale**: `notify` may not import upward and the archive needs `markdown_to_safe_html`
  and the Jinja environment; a sibling top-level package importing `notify.render` while
  `notify.email` imports it back would create an import cycle through `notify/__init__.py`.
- **Alternatives**: `invio/archive/` package (cycle risk, more files).

## R3. Slug and collision rule

- **Decision**: NFKD-normalise, drop non-ASCII marks, lower-case, replace every run of
  characters outside `[a-z0-9]` by one `-`, trim `-`, cut at 48 characters, fall back to `job`
  when empty. Each job directory holds a marker file `.job-name` (UTF-8 original name). If the
  target directory exists with a marker naming a *different* job, use
  `<slug>-<first 8 hex of sha256(name)>` instead. The result only ever contains `[a-z0-9-]`, so
  it has no separators, dots or control characters; additionally the resolved path is asserted
  to be inside `archive_dir`.
- **Rationale**: readable URLs in the common case, collision-safe and deterministic for the
  unusual one; the marker also gives the global index the original display name.
- **Alternatives**: always append the hash (ugly URLs); store a name→slug table in the DB
  (migration, spec forbids no state but simplicity says no).

## R4. Page names, time zone, same-minute collisions

- **Decision**: `YYYY-MM-DD-HHMM` from the run's `started_at` (digest `created_at` if the digest
  has no run), in UTC. The page is first written to a temp file in the target directory, then
  published with `os.link(tmp, final)` which fails with `FileExistsError` instead of
  overwriting; on that, try `…-2`, `…-3`, … Finally the temp file is removed.
- **Rationale**: race-free no-overwrite publish without locks (clarification Q4/Q5). Local file
  systems on the target (ext4/xfs/APFS/tmpfs) support hard links.
- **Alternatives**: `O_EXCL` create of the final name then write (readers could see a partial
  file — violates atomicity); `os.rename` after an `exists()` check (race).

## R5. Atomic writes and permissions

- **Decision**: temp file in the same directory (`.<name>.<random>.tmp`), write, flush,
  `fsync`, `chmod 0644`, publish (`os.link`/`os.replace`), `fsync` the directory (POSIX); on any
  exception remove the temp file. Directories are created with `0755`. Indexes use
  `os.replace`.
- **Rationale**: mirrors the existing `write_yaml` pattern in `config/job.py`, but with
  world-readable mode because the web server usually runs as another user. `Settings` already
  makes `/var/lib/invio` the only writable path of the systemd service.
- **Alternatives**: reusing the private helpers of `config/job.py` (they force `0600` and live
  in a different layer's private API) — rejected; ~15 lines are duplicated deliberately.

## R6. Index generation

- **Decision**: the job index lists `*.html` files of the job directory except `index.html`,
  parsed by file name (`YYYY-MM-DD-HHMM[-n].html`; others ignored), sorted newest first
  (time descending, suffix descending). The global index lists every sub-directory that has a
  `.job-name` marker and at least one page, with page count and newest date, sorted by name.
  Both are rebuilt completely after each archived digest.
- **Rationale**: spec FR-007 / US2 scenario 4: indexes reflect directory content, survive
  restarts and manual copies; no page parsing keeps it fast.
- **Alternatives**: incremental index append (drifts, breaks edge case "page not in index").

## R7. Rendering, sanitizing, no external assets

- **Decision**: three Jinja2 templates (base + page + index) with one inline `<style>` block,
  `<meta name="viewport">`, `prefers-color-scheme` support, fluid layout (`max-width`,
  `overflow-wrap`, tables scroll inside a wrapper). Digest body via the existing
  `markdown_to_safe_html` (same allowlist as mail, images excluded, relative links denied).
  Autoescape is on for all `.j2` HTML templates (`select_autoescape` already matches
  `html.j2`). Pages carry `<meta name="referrer" content="no-referrer">` and
  `<meta name="robots" content="noindex">` as a privacy default.
- **Rationale**: FR-005/FR-006; images stay excluded so the page never makes third-party
  requests on its own. Links inside digest text are user-activated navigations, not asset loads.
- **Alternatives**: separate CSS file (second file to serve/cache-bust); a larger allowlist
  (not requested).

## R8. Mail link, payload and retry

- **Decision**: `NotificationPayload` gets `archive_page: str | None = None` (relative path
  `<job-slug>/<name>.html`); schema version stays 1 (older stored rows lack the key and load as
  `None`, `extra="forbid"` still rejects unknown keys). `render_mail(payload, body,
  archive_url=None)` adds the footer line when a URL is given. `archive_url_for(cfg, settings,
  page)` returns `base_url + "/" + page` only if `cfg.archive.enabled`, `base_url` is set and the
  file exists; it is used at first send and in the retry rebuilder (clarification Q1/Q2).
- **Rationale**: keeps the retry self-contained like the rest of the payload and re-checks the
  page at send time. Path segments are slug/name characters only, so no URL-encoding needed;
  the base URL is stripped of trailing slashes at validation time.
- **Alternatives**: store the full URL in the payload (stale after a `base_url` change);
  recompute the page name on retry (cannot know the `-n` suffix).

## R9. Configuration

- **Decision**: `ArchiveConfig(enabled: StrictBool = False, base_url: str | None = None)` in
  `config/job.py`, `JobConfig.archive` with `default_factory`. `base_url` must parse as
  `http`/`https` with a host, no query or fragment; a trailing `/` is removed. `enabled` without
  `base_url` is valid (archive without mail link). `Settings.archive_dir: Path = Path(
  "/var/lib/invio/archive")`.
- **Rationale**: old stored job configs stay valid (default section); the issue's
  `/var/lib/scout/archive` is its working name, deployment already uses `/var/lib/invio`
  (spec FR-003 amended). The existing test expecting `None` is updated.
- **Alternatives**: keep `archive_dir` optional and treat `None` as "disabled" — rejected: the
  issue asks for a default, and the per-job `enabled` flag already switches the feature.

## R10. Failure handling and logging

- **Decision**: `archive_digest()` raises `OSError`/`ArchiveError`; the caller in `_deliver`
  catches `Exception`, logs `archive.failed` (`error` = class and OS error text, `job`/`run_id`
  from the run context) at WARNING and continues without a page. Temp files are cleaned in
  `finally`; a failed index rebuild after a successfully written page still counts as a
  written page (the page exists; the next run rebuilds the indexes) and is logged separately as
  `archive.index_failed`.
- **Rationale**: FR-012; an index problem must not remove the mail link of a page that exists.
