# Implementation Plan: Static Digest Archive (HTML)

**Branch**: `gh-issue-25` | **Date**: 2026-10-08 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/016-gh-issue-25/spec.md`

## Summary

After a digest is stored, write it as a self-contained HTML page under the archive directory,
regenerate the per-job and global indexes from the directory content, and add a link to the
page in the digest mail when the job configures a `base_url`.

- **Hook point**: `deliver_digest()` in `invio.notify.email` (called by the pipeline's `notify`
  stage after `persist`). The archive write happens *before* the mail is rendered, so the mail
  knows whether a page exists (spec FR-011); it never raises into the delivery (FR-012).
- **New module** `invio.notify.archive`: job-name slugging, page naming, atomic writes, index
  rebuild. Rendering uses new Jinja2 templates (`archive_page.html.j2`, `archive_index.html.j2`,
  shared `archive_base.html.j2` with inline CSS) in the existing `invio.notify` template package
  and the existing sanitizer `markdown_to_safe_html` (#20).
- **Configuration**: new optional `archive: {enabled, base_url}` section on `JobConfig`;
  `Settings.archive_dir` (exists as `Path | None`) gets the default `/var/lib/invio/archive`.
- **Mail link & retry**: the notification payload stores the relative page path
  (`archive_page`, optional, schema stays version 1). First send and `invio notify retry` both
  derive the URL as `base_url` + page path, only if the archive is enabled and the page file
  still exists.
- **No new dependencies, no migration.**

Details: [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.12+ (uv)

**Primary Dependencies**: existing only: Jinja2, markdown-it-py + nh3 (via `markdown_to_safe_html`), pydantic v2 / pydantic-settings.

**Storage**: files under `Settings.archive_dir`; no database change (the page path rides in the existing JSON `notifications.payload`).

**Testing**: pytest with `tmp_path` archive directories; existing SMTP/DB helpers (`tests/smtp_helpers.py`, `tests/db_helpers.py`); Hypothesis for slug safety. No network.

**Target Platform**: Linux server (Debian), files served by an external web server; also macOS for development.

**Project Type**: Python CLI application (single project)

**Performance Goals**: none beyond keeping index rebuilds cheap: they read directory entries and file names only, never page contents.

**Constraints**: no external resources in output; no path outside `archive_dir`; no partial file visible; pages world-readable (`0644`, dirs `0755`) so a separate web-server user can read them; archive failure never fails the run or blocks the mail.

**Scale/Scope**: tens of jobs, thousands of pages; one writer at a time per job (runs are job-locked), concurrent runs of different jobs may rebuild the global index at the same time (last atomic replace wins; each result is complete and built from the directory).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Assessment |
|-----------|------------|
| I. Strict Contracts at Boundaries | `ArchiveConfig` is a strict (`extra=forbid`) model inside `JobConfig`; `base_url` validated with a field-naming message; `archive_dir` is a typed `Path` setting; payload field is a typed optional on the versioned payload. PASS |
| II. CLI-First | No new user-facing command: the archive is a side effect of `invio run` / `invio job run`; documented in README/docs. Nothing requires a command to be reachable only elsewhere. PASS |
| III. Test-Covered | Every spec acceptance scenario and edge case maps to a test (see quickstart). Unit tests use `tmp_path`, no network. PASS |
| IV. Quality Gates | ruff, ruff format, strict mypy, pytest; no suppressions planned. `docs/job.schema.json` regenerated (existing schema test guards it). PASS |
| V. Secrets / Observable | No secrets involved. Failures log `archive.failed` with error class and OS error text (paths only, no credentials) inside the existing run context. PASS |
| Layering | `invio.notify` keeps importing only config/db/domain/log (`tests/test_notify_layering.py`). The archive lives inside `invio.notify`. PASS |
| Simplicity | One module, two templates + base; no new dependency; no DB migration; no generic "publisher" abstraction. PASS |

Post-design re-check: unchanged, PASS. No complexity-tracking entries.

## Project Structure

### Documentation (this feature)

```text
specs/016-gh-issue-25/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── archive-layout.md      # directory, file names, page/index content rules
│   ├── job-config.md          # `archive` section of the job file
│   └── python-api.md          # public functions and changed signatures
├── checklists/requirements.md
└── tasks.md                   # created by /speckit-tasks
```

### Source Code (repository root)

```text
src/invio/
├── config/
│   ├── job.py                 # + ArchiveConfig, JobConfig.archive
│   └── settings.py            # archive_dir default /var/lib/invio/archive
└── notify/
    ├── archive.py             # NEW: slug, page naming, atomic write, indexes, archive_digest()
    ├── email.py               # archive step in _deliver; archive URL in _send_all / _rebuilder
    ├── payload.py             # + archive_page: str | None
    ├── render.py              # render_mail(..., archive_url=); shared stats context; archive page rendering helper
    └── templates/
        ├── archive_base.html.j2   # NEW: inline CSS, responsive, no external assets
        ├── archive_page.html.j2   # NEW
        ├── archive_index.html.j2  # NEW (job index and global index)
        ├── digest.html.j2         # + archive link in footer
        └── digest.txt.j2          # + archive link in footer

docs/job.schema.json, docs/job.example.yaml, docs/deployment.md, README.md   # documentation updates
.env.example, deploy/env/invio.env.example, deploy/ansible/.../invio.env.j2  # unchanged (already INVIO_ARCHIVE_DIR)

tests/
├── test_archive_slug.py       # NEW: slug/containment, collisions, Hypothesis
├── test_archive_write.py      # NEW: pages, indexes, atomicity, same-minute, no-external-assets, sanitizing
├── test_archive_delivery.py   # NEW: deliver_digest/retry integration, footer link rules, failure isolation
├── test_job_config.py         # + archive section cases
├── test_notify_payload.py     # + archive_page
├── test_notify_render.py      # + archive link in footer
└── test_settings.py           # archive_dir default
```

**Structure Decision**: single project; the archive is part of `invio.notify` because it needs the mail sanitizer and templates and must be called by the delivery before the mail is rendered, while the layering rule forbids notify from importing upward.

## Complexity Tracking

No constitution violations.
