---

description: "Task list for gh-issue-25: static digest archive (HTML)"
---

# Tasks: Static Digest Archive (HTML)

**Input**: Design documents from `specs/016-gh-issue-25/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: Required (constitution III: every acceptance criterion and rejection path has an
automated test; no network). Within each phase the test tasks come first; write them and check
that they fail before the implementation tasks.

**Organization**: Grouped by user story. Priorities: US1 and US4 (P1), US2 and US3 (P2).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: The user story the task belongs to (US1–US4)

## Path Conventions

Source in `src/invio/`, flat `tests/` directory, docs in `docs/` and `README.md`. All archive
code lives in `src/invio/notify/archive.py`; templates in `src/invio/notify/templates/`.
Gates before every commit: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest`.

---

## Phase 1: Setup

- [x] T001 Run the baseline gates (`uv sync --locked`, ruff, mypy, pytest) on the branch and note that `tests/test_notify_layering.py` and `tests/test_job_schema.py` pass before changes (no files changed)

---

## Phase 2: Foundational (blocks all user stories)

**Purpose**: configuration, settings and payload contracts, plus the safe-path and atomic-write primitives every story uses.

### Tests first

- [x] T002 [P] Add `archive` section cases to `tests/test_job_config.py`: omitted → `enabled=False, base_url=None`; `enabled: true` without `base_url` valid; `base_url: https://h/x/` stored as `https://h/x`; rejected with error naming `archive.base_url`: `ftp://h`, `h/x`, `https://`, `https://h?a=1`, `https://h#f`; `enabled: "yes"` rejected naming `archive.enabled`; unknown key `archive.dir` rejected naming it
- [x] T003 [P] Update `tests/test_settings.py` line 66: `settings.archive_dir == Path("/var/lib/invio/archive")` by default and `INVIO_ARCHIVE_DIR=/x` overrides it
- [x] T004 [P] Add to `tests/test_notify_payload.py`: `archive_page` defaults to `None`, accepts `"slug/2026-10-08-0930.html"`, a stored v1 payload dict without the key still validates, unknown keys are still rejected
- [x] T005 [P] Create `tests/test_archive_slug.py`: `slugify_job_name` examples (`"Weekly AI News"` → `weekly-ai-news`, `"../../etc/x"` → `etc-x`, `"日本語/ニュース"` and `"!!!"` → `job`, 300-char name → ≤ 48 chars, no trailing `-`); a Hypothesis test that for any text the result matches `^[a-z0-9]+(-[a-z0-9]+)*$`; a parametrized list of ≥ 20 hostile names (traversal, separators, NUL/control characters, Windows reserved names, unicode, very long) all resolved by `job_directory(archive_dir, name)` to a path inside `archive_dir`; collision: names `"A b"` and `"a-b"` yield different directories, the second `<slug>-<sha256(name)[:8]>`, and the same name always yields the same directory
- [x] T006 [P] Create `tests/test_archive_write.py` (first part): `write_atomic(path, text)` leaves no `*.tmp` file on success or when the write raises (monkeypatch `os.fsync` to raise `OSError`), result mode is `0o644`, an existing file is replaced whole; `publish_new(dir, stem, text)` returns `stem.html`, then `stem-2.html`, `stem-3.html` for repeated calls and never overwrites; two threads publishing the same stem get distinct names

### Implementation

- [x] T007 [P] Add `ArchiveConfig(_StrictModel)` to `src/invio/config/job.py` with `enabled: StrictBool = False` and `base_url: str | None = None` (validator: scheme in `http`/`https`, non-empty host, no query/fragment, trailing `/` removed, ValueError messages that name the rule) and `JobConfig.archive: ArchiveConfig = Field(default_factory=ArchiveConfig)`; regenerate `docs/job.schema.json` (`uv run python -m invio.config.job` or the command the schema test documents); make T002 pass
- [x] T008 [P] Change `archive_dir` in `src/invio/config/settings.py` to `archive_dir: Path = Path("/var/lib/invio/archive")`; make T003 pass; check `grep -rn archive_dir src` for callers expecting `None`
- [x] T009 [P] Add `archive_page: str | None = None` to `NotificationPayload` in `src/invio/notify/payload.py` (keep `schema_version` Literal[1], `extra="forbid"`); make T004 pass
- [x] T010 Create `src/invio/notify/archive.py` with: `slugify_job_name(name)` (NFKD → ASCII → lower-case → runs of non-`[a-z0-9]` to `-` → trim → max 48 → `job` fallback), `job_directory(archive_dir, name)` (marker `.job-name` collision rule, `<slug>-<sha256(name)[:8]>`, asserts `resolve().is_relative_to(archive_dir.resolve())`), `write_atomic(path, text)` (temp file `.<name>.<token>.tmp` in the same directory, write, flush, fsync, `chmod 0o644`, `os.replace`, directory fsync on POSIX, temp removed on any exception) and `publish_new(directory, stem, text)` (temp file then `os.link` to `<stem>.html`, on `FileExistsError` try `<stem>-2.html`, `-3`…, temp always removed); directories created with mode `0o755`; make T005 and T006 pass

**Checkpoint**: `uv run pytest tests/test_job_config.py tests/test_settings.py tests/test_notify_payload.py tests/test_archive_slug.py tests/test_archive_write.py tests/test_job_schema.py` and mypy pass.

---

## Phase 3: User Story 1 - Browse a past digest as a web page (P1) 🎯 MVP

**Goal**: A non-empty digest of an archive-enabled job becomes a readable page at `<archive_dir>/<job-slug>/<YYYY-MM-DD-HHMM>.html`, created right after persist, without ever failing the run.

**Independent Test**: Deliver a stored digest for an archive-enabled job with a fake mailer and a `tmp_path` archive directory; open the page; it shows the job name, UTC date, stats footer and sanitized digest text.

### Tests for User Story 1

- [x] T011 [P] [US1] In `tests/test_notify_render.py` add tests for `render_archive_page(payload, body, run_started_at=…, index_href=…)`: contains job name (escaped, e.g. `<b>` in a name stays text), `YYYY-MM-DD HH:MM UTC` taken from `run_started_at` converted to UTC (not from `payload.digest_date`, which is job-local), the stats footer `Items found: … · Included: … · Run time: …` without any archive link, the digest body rendered; `<script>`, `onclick=`, `javascript:` hrefs and `<img>` from the Markdown are stripped (reuse the fixtures/cases of the mail sanitizer tests)
- [x] T012 [P] [US1] Create `tests/test_archive_delivery.py` using `tests/smtp_helpers.py` and `tests/db_helpers.py` (see `tests/test_notify_email.py` for the pattern): archive-enabled job + non-empty digest with an `archive_dir` that does not exist yet (created on first run) → page exists at `<slug>/<YYYY-MM-DD-HHMM>.html` (UTC minute of `run.started_at`, falls back to `digest.created_at` without a run) and the notification payload carries `archive_page`; archive disabled or section omitted → archive directory untouched; empty digest (even with `send_if_empty: true`) → no page; failed run and dry run (go through `run_job` fixtures from `tests/pipeline_helpers.py`) → no page, while a `partial` run with a digest → page; unwritable `archive_dir` (a file where the directory should be) → `DeliveryOutcome.sent` unchanged, mail still sent, `archive.failed` logged (caplog), payload `archive_page is None`

### Implementation for User Story 1

- [x] T013 [US1] Create `src/invio/notify/templates/archive_base.html.j2` (HTML5 skeleton, `lang="en"`, charset, viewport, `robots noindex`, `referrer no-referrer`, one inline `<style>`: fluid `max-width` column, system font stack, `overflow-wrap:anywhere`, tables inside a scrolling wrapper, `prefers-color-scheme: dark`) and `archive_page.html.j2` extending it (job name, date line, digest body, stats footer, link to `index_href`); no `<script>`, `<link>`, `<img>`, `src=`, `@import`, `url(`
- [x] T014 [US1] In `src/invio/notify/render.py` factor the footer figures out of `render_mail` into one helper shared with the new `render_archive_page(payload, digest_markdown, *, run_started_at, index_href) -> str` (uses `markdown_to_safe_html`, same `_ENV`; empty digests are not archived so no empty branch); export it in `__all__`; make T011 pass
- [x] T015 [US1] In `src/invio/notify/archive.py` add `ArchivedPage` (frozen dataclass: `job_slug`, `name`, `relative_path`) and `archive_digest(archive_dir, *, job_name, run_started_at, payload, digest_markdown) -> ArchivedPage` which creates the job directory with its `.job-name` marker, names the page `YYYY-MM-DD-HHMM` from `run_started_at` converted to UTC, renders with `render_archive_page(run_started_at=run_started_at, index_href="index.html")` and publishes with `publish_new`; index rebuild is added in US2
- [x] T016 [US1] In `src/invio/notify/email.py` `_deliver()`: after the `NotificationPayload` is built and only when `cfg.archive.enabled` and the digest has items, call `await asyncio.to_thread(archive_digest, settings.archive_dir, …)` in a `try/except Exception`; on success `payload = payload.model_copy(update={"archive_page": page.relative_path})`; on failure log `archive.failed` (WARNING, `error` = exception class and OS error text) and continue; `run_started_at` is `run.started_at` or `digest.created_at`; make T012 pass
- [x] T017 [US1] Run `uv run mypy` and the notify test files (`tests/test_notify_*.py`, `tests/test_archive_*.py`); fix typing (aware `datetime`, `Path` handling)

**Checkpoint**: US1 works alone: pages appear for archive-enabled jobs and nothing else changes.

---

## Phase 4: User Story 4 - Safe, self-contained output (P1)

**Goal**: Pages and indexes make no external requests, job names cannot escape the archive directory, writes are atomic, and narrow screens work.

**Independent Test**: Archive a digest for a job named `../../etc/x`; all files are inside `archive_dir`; scanning all generated files finds no external references.

### Tests for User Story 4

- [x] T018 [P] [US4] Extend `tests/test_archive_write.py`: after `archive_digest` for the ≥ 20 hostile job names of T005 every created path (`rglob`) is inside `archive_dir` and no name contains a path separator; a job name with a symlinked `archive_dir/<slug>` pointing outside is refused (OSError/ArchiveError, nothing written outside); no `*.tmp` files remain after success and after a forced failure (monkeypatch `os.link` to raise `OSError`)
- [x] T019 [P] [US4] Add to `tests/test_archive_write.py` a scan test: for a rendered page (and, after US2, both indexes) assert none of `<script`, `<link`, `<img`, `<iframe`, `<form`, `src=`, `@import`, `url(` appears and the only `http(s)://` occurrences are inside `<a href=…>` of digest links; assert `<meta name="viewport"` is present and the CSS contains `overflow-wrap` and a table wrapper rule (narrow-screen guard, SC-007)
- [x] T020 [P] [US4] Add to `tests/test_archive_write.py`: two `archive_digest` calls with the same job and the same UTC minute yield `…-0930.html` and `…-0930-2.html`, both contain their own digest text; a non-UTC aware `run_started_at` (e.g. `+02:00`) is named by its UTC minute

### Implementation for User Story 4

- [x] T021 [US4] Harden `src/invio/notify/archive.py` until T018–T020 pass: refuse a job directory or `archive_dir` that resolves outside `archive_dir` (symlink check on every path component created), `ArchiveError(OSError)` for such refusals, make sure temp files are removed in `finally` for page publish; keep `os.link` fallback out (documented: hard links required, research R4)
- [x] T022 [US4] Review `archive_base.html.j2` against the contract in `contracts/archive-layout.md` (metas, no external references, `0644` pages) and fix deviations; make T019 pass

**Checkpoint**: P1 stories complete — MVP shippable.

---

## Phase 5: User Story 2 - Navigate digests through indexes (P2)

**Goal**: Each archived digest regenerates the job `index.html` (newest first) and the global `index.html` from directory content.

**Independent Test**: Archive digests for two jobs over several runs; the global index lists both jobs, each job index lists its pages newest first with working relative links.

### Tests for User Story 2

- [x] T023 [P] [US2] Create `tests/test_archive_index.py`: after one archived digest `<slug>/index.html` and `index.html` exist and link to the page; after three digests with different minutes the job index lists them newest first (and `-2` after the base page of the same minute is listed first, i.e. newest); two jobs appear once each in the global index with page count, newest date and link `<slug>/index.html`, sorted by name case-insensitively; indexes are rebuilt from the directory (pre-seed an unlisted `2026-01-01-0000.html`, a foreign `notes.html` that is ignored, and delete the index before the call); a directory without `.job-name` or without pages is not listed; index text is autoescaped (job name `<b>x</b>` renders as text); no `*.tmp` remains; an `OSError` while writing an index (monkeypatch) is logged as `archive.index_failed`, `archive_digest` still returns the page and the page file exists
- [x] T024 [P] [US2] Extend the scan test of T019 to run over `index.html` and `<slug>/index.html`

### Implementation for User Story 2

- [x] T025 [US2] Create `src/invio/notify/templates/archive_index.html.j2` extending `archive_base.html.j2` for both indexes (job variant: heading, list of dated links, link `../index.html`; global variant: list of jobs with page count and newest date)
- [x] T026 [US2] In `src/invio/notify/archive.py` add `rebuild_job_index(job_dir)` (pages matching `^\d{4}-\d{2}-\d{2}-\d{4}(-\d+)?\.html$`, sorted by (stamp, suffix) descending, label `YYYY-MM-DD HH:MM UTC` plus `#n` for suffixes) and `rebuild_global_index(archive_dir)` (directories with `.job-name` and at least one page); both render through the shared `_ENV` and write with `write_atomic`; call them from `archive_digest` after the page is published, catching `OSError` per index and logging `archive.index_failed`; make T023 and T024 pass

**Checkpoint**: archive is browsable from the global index in two clicks (SC-006).

---

## Phase 6: User Story 3 - Jump from the mail to the archived digest (P2)

**Goal**: The mail footer (HTML and text) links to the archived page when the archive is enabled, `base_url` is set and the page exists — on first send and on `invio notify retry`.

**Independent Test**: With `base_url` configured, deliver a digest; the received mail footer contains `<base_url>/<slug>/<name>.html`.

### Tests for User Story 3

- [x] T027 [P] [US3] Add to `tests/test_notify_render.py`: `render_mail(payload, body, archive_url="https://h/x/s/p.html")` puts the URL in the HTML footer as a link and in the text footer on its own line; with `archive_url=None` both outputs are byte-identical to the current output (regression guard for #20 tests); a URL with `&` or `"` is escaped in HTML
- [x] T028 [P] [US3] Add to `tests/test_archive_delivery.py`: with `base_url` set the delivered message (HTML and text part from the SMTP sink) contains the exact page URL; `base_url` with and without trailing slash gives exactly one `/` between segments; enabled without `base_url` → no link; disabled with `base_url` → no link and no page; page write failed → mail sent, no link (spec US3 scenario 4); same-minute second digest → mail links to the `-2` page; retry: a failed notification re-sent via `retry_failed` includes the link when the page still exists and omits it when the page file was deleted or `base_url` was removed from the job config
- [x] T029 [P] [US3] Add `archive_url` tests to `tests/test_archive_write.py` (or `test_archive_delivery.py`): `archive_url(base_url, archive_dir, page_path, enabled=…)` returns `None` unless enabled, `base_url` set, `page_path` set and the file exists; builds `base_url + "/" + page_path`

### Implementation for User Story 3

- [x] T030 [US3] Add `archive_url(base_url, archive_dir, page_path, *, enabled) -> str | None` to `src/invio/notify/archive.py`
- [x] T031 [US3] In `src/invio/notify/render.py` add `archive_url: str | None = None` to `render_mail` and pass it to the templates; edit `src/invio/notify/templates/digest.html.j2` (footer line `Archived version: <a href="{{ archive_url }}">…</a>`, only inside `{% if archive_url %}`) and `digest.txt.j2` (own line, same condition); make T027 pass
- [x] T032 [US3] In `src/invio/notify/email.py` compute `url = archive_url(cfg.archive.base_url, settings.archive_dir, payload.archive_page, enabled=cfg.archive.enabled)` in `_deliver` and pass it through `_send_all` to `render_mail`; in `_rebuilder` load the job's current `JobConfig` (via `row.job_id`) and compute the URL the same way from the stored `payload.archive_page`; make T028 and T029 pass
- [x] T033 [US3] Run `tests/test_notify_*.py` and the full suite; fix regressions in existing mail tests

**Checkpoint**: all four user stories work and are independently testable.

---

## Phase 7: Polish & cross-cutting

- [x] T034 [P] Document the feature: `README.md` (feature list/config), `docs/job.example.yaml` (commented `archive:` section), `docs/deployment.md` (archive directory, serving it with a static web server and basic auth, example nginx `location` with `auth_basic`, file modes `0644`/`0755`, `/var/lib/invio/archive` is writable for the service per `ReadWritePaths`), `.env.example` comment for `INVIO_ARCHIVE_DIR`
- [x] T035 [P] Check `deploy/ansible/` and `deploy/systemd/` that `/var/lib/invio/archive` is created (owner `invio`, mode `0755`) and writable under the service hardening; add the directory task only if missing, and run the deploy tests that cover the role (`tests/test_deploy_*.py`)
- [x] T036 Run the manual end-to-end from `quickstart.md` §2–§3 with the fake LLM and local SMTP sink and tick off each row of the failure table; record deviations in `specs/016-gh-issue-25/quickstart.md` if any
- [x] T037 Final gates: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest`; confirm `tests/test_notify_layering.py` passes; map each spec acceptance scenario (US1–US4) and each FR-001–FR-014 to a test name in the PR description

---

## Dependencies & Execution Order

- Phase 1 → Phase 2 → stories. Phase 2 blocks everything.
- **US1** (Phase 3) needs Phase 2. **US4** (Phase 4) hardens US1 output and needs T015/T016. **US2** (Phase 5) needs US1 (`archive_digest`). **US3** (Phase 6) needs US1 (payload `archive_page`, `_deliver` wiring); it does not need US2.
- Suggested order: Phase 2 → US1 → US4 → US2 → US3 → Polish. US2 and US3 may run in parallel after US1 (different functions, but both edit `archive.py`/`render.py`: coordinate or serialise T026 and T030/T031).
- Within a phase: tests (fail first) → templates/render → archive module → email wiring.

## Parallel Opportunities

- Phase 2: T002–T006 (tests, separate files) together; then T007, T008, T009 together; T010 after T005/T006.
- US1: T011 and T012 together. US4: T018–T020 together. US2: T023 and T024. US3: T027–T029 together.
- Polish: T034 and T035 together.

## Implementation Strategy

- **MVP**: Phases 1–4 (config, safe primitives, page generation, safety guarantees). Ship-ready: pages are written, safe, atomic, self-contained.
- **Increment 2**: US2 indexes (browsability). **Increment 3**: US3 mail link and retry.
- Keep every increment green on all gates; no migration or dependency changes at any point.
