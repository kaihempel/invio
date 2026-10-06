# Implementation Plan: E-mail Notifier with HTML and Plaintext Templates

**Branch**: `gh-issue-20` | **Date**: 2026-10-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/011-gh-issue-20/spec.md`

## Summary

Add the e-mail delivery step for stored digests (`invio.notify`) and the `invio notify retry`
command. For each digest, `deliver_digest()` creates one `notifications` row per distinct
recipient, renders one multipart message per recipient and sends it over SMTP with
`aiosmtplib`, then marks each row `sent`, `failed` or (after 5 attempts) `skipped`.

- **Rendering**: the digest Markdown goes through `markdown-it-py`, then the `nh3` allowlist
  sanitizer. Two Jinja2 templates (`digest.html.j2` with autoescape, `digest.txt.j2`) wrap the
  body with job name, date and a run statistics footer.
- **Subject**: literal replacement of `{job_name}` / `{date}`, with line breaks removed.
- **Reproducible retries**: the data needed to rebuild the mail (rendered subject, digest date,
  job name, statistics snapshot) is stored with the notification as a versioned, typed
  payload. Retry rebuilds the message from that payload plus the stored digest.
- **Attempts and claiming**: migration `0004` adds `attempts` and `last_attempt_at` to
  `notifications`. Retry claims each row with a conditional `UPDATE` before sending, so
  concurrent retries never double-send. Stale `pending` rows (last attempt more than 1 hour
  ago) are picked up.
- **Settings**: `smtp_starttls` is replaced by `smtp_security` (`starttls` | `ssl` | `none`),
  and a new `smtp_timeout_seconds` is added.
- **Run status**: delivery never raises. It returns a `DeliveryOutcome`, and the pure helper
  `run_status_after_delivery()` turns a would-be `succeeded` run into `partial` when any
  notification did not end `sent`, or when delivery failed before any notification could be
  created (`outcome.error`, e.g. digest missing or stored job config invalid).

Details: [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.12+ (uv)

**Primary Dependencies**: existing: pydantic v2 / pydantic-settings, SQLAlchemy 2.1, Alembic,
Typer. New runtime: `aiosmtplib>=4`, `markdown-it-py>=3`, `nh3>=0.2.18`, `jinja2>=3.1`. New dev:
`aiosmtpd>=1.4` (in-process test SMTP server), `trustme>=1.2` (throwaway TLS certificates for
STARTTLS / implicit-TLS tests).

**Storage**: existing `notifications` table. Migration `0004` adds `attempts` (int, not null,
default 0) and `last_attempt_at` (UTC datetime, nullable). The `payload` JSON holds
`NotificationPayload` v1.

**Testing**: pytest + pytest-asyncio. A local `aiosmtpd` controller is used on 127.0.0.1 with a
random port and records received messages. Implicit TLS and STARTTLS are tested with a
self-signed certificate generated in the test session. `db`-marked tests run on SQLite (and on
MariaDB when `INVIO_TEST_DATABASE_URL` is set). No external network.

**Target Platform**: Linux server, unattended (cron/systemd); macOS for development.

**Project Type**: CLI application / library (`src/invio`).

**Performance Goals**: not critical. A digest has a handful of recipients, and messages are
sent sequentially over one reused SMTP connection.

**Constraints**:

- Delivery never raises into the run.
- Secrets never appear in error text or logs.
- One send per notification per retry invocation.
- Every SMTP operation is bounded by `smtp_timeout_seconds` (default 30 s).

**Scale/Scope**: about 5 new modules, 2 templates, 1 CLI command, 1 migration, 1 settings change.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How this plan complies |
|-----------|--------|------------------------|
| I. Strict contracts at boundaries | PASS | `smtp_security` is a `Literal` that rejects other values and names the field. The notification payload is parsed with a strict Pydantic model (`extra="forbid"`, `schema_version: Literal[1]`) when read back. `NotificationConfig` from #3 is reused, not re-declared. |
| II. CLI-first | PASS | `src/invio/cli/commands/notify.py` exposes `app` and is auto-discovered as `invio notify`, with sub-command `retry`. The summary goes to stdout and logs to stderr. Exit codes: 0 when everything was sent (or nothing was due), 1 when anything was not sent, 2 for configuration errors. |
| III. Test-covered behaviour | PASS | Each acceptance scenario maps to a test ([quickstart.md](quickstart.md)). The SMTP server is local and in-process, the DB is SQLite, and the clock is injected for the 1-hour and attempt rules. |
| IV. Quality gates mirror CI | PASS | All new dependencies ship type information (`aiosmtplib`, `markdown-it-py` and `jinja2` are typed; `nh3` ships `.pyi`). No `# type: ignore` is planned. `uv.lock` is updated. |
| V. Secrets & observability | PASS | `smtp_password` stays `SecretStr`. Error texts pass through `scrub_secret()`, which redacts the password and user before they are stored or logged. Delivery logs use `extra={"recipient", "notification_id", "digest_id"}` inside the caller's `run_context`, and retry opens `run_context(job=<name>)` per job. |
| Layering | PASS | `invio.notify` (adapter) imports only `invio.config`, `invio.db` and `invio.domain`. `invio.cli.commands.notify` imports `invio.notify`. Nothing in `notify` imports `cli`, `graph` or `scheduling`. |
| New runtime dependencies justified | PASS | See research.md R1–R3 (also to go in the PR description). |
| Docs updated | PASS (planned) | README sections on settings, notifications and the `invio notify retry` command; `.env.example` gets `INVIO_SMTP_SECURITY` and `INVIO_SMTP_TIMEOUT_SECONDS`. |

**Spec deviation (recorded)**: the spec's Assumptions said "no schema change is expected".
Claiming rows safely against concurrent retries, and selecting stale `pending` rows by their last
attempt, needs real columns. JSON payload fields cannot be queried or updated atomically and
portably across SQLite and MariaDB. Hence migration `0004`; see research.md R6. The spec
assumption is updated accordingly.

**Post-design re-check**: PASS. The design artifacts introduce no further deviations.

## Project Structure

### Documentation (this feature)

```text
specs/011-gh-issue-20/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── cli-notify-retry.md   # `invio notify retry` command contract
│   ├── python-api.md         # invio.notify public API
│   ├── settings.md           # SMTP settings contract (smtp_security, smtp_timeout_seconds)
│   └── email-message.md      # Shape of the sent message, subject rules, sanitizer allowlist
└── tasks.md             # Phase 2 output (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
src/invio/
├── config/settings.py              # smtp_starttls → smtp_security; + smtp_timeout_seconds
├── db/
│   ├── models.py                   # Notification: + attempts, + last_attempt_at
│   ├── repositories.py             # NotificationRepository: + begin_attempt, claim_for_retry,
│   │                               #   retry_candidates; DigestRepository: + get; mark() unchanged
│   └── migrations/versions/
│       └── 0004_notification_attempts.py
├── notify/
│   ├── __init__.py                 # re-exports public API
│   ├── email.py                    # SmtpMailer (aiosmtplib), deliver_digest, retry_failed,
│   │                               #   DeliveryOutcome, run_status_after_delivery
│   ├── render.py                   # markdown_to_safe_html, render_subject, build_message
│   ├── payload.py                  # NotificationPayload (v1), DigestStats
│   └── templates/
│       ├── digest.html.j2
│       └── digest.txt.j2
└── cli/commands/notify.py          # `invio notify retry`

tests/
├── smtp_helpers.py                 # aiosmtpd controller fixture (plain / STARTTLS / implicit TLS)
├── test_notify_render.py           # Markdown → sanitized HTML, subject, templates, escaping
├── test_notify_email.py            # deliver_digest against local SMTP: parts, per-recipient
│                                   #   rows, failures, empty digest, secrets
├── test_notify_retry.py            # retry_failed: failed/stale pending, limit → skipped,
│                                   #   claiming, deleted digest
├── test_cli_notify.py              # `invio notify retry` summary and exit codes
├── test_db_notifications.py        # repository additions + migration 0004 (db marker)
└── test_settings.py                # updated: smtp_security replaces smtp_starttls
```

**Structure Decision**: single project. Code goes into the existing `invio.notify` package and
the auto-discovered CLI command package, following the README layout. The issue's
`scout/notify/...` paths map to `src/invio/notify/...`.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Schema migration despite the spec assumption of none | Atomic claim (`UPDATE … WHERE status=… AND last_attempt_at …`) against concurrent retries; portable selection of stale `pending` rows | Payload-only attempt tracking cannot be updated or filtered atomically across SQLite and MariaDB, so concurrent retries could double-send (spec edge case) |
