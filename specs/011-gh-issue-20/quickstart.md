# Quickstart: Validate the E-mail Notifier (gh-issue-20)

## Prerequisites

```bash
uv sync --locked            # adds aiosmtplib, markdown-it-py, nh3, jinja2; dev: aiosmtpd, trustme
uv run invio db upgrade     # applies migration 0003 (attempts, last_attempt_at)
```

## Automated validation (CI gates)

```bash
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest tests/test_notify_render.py tests/test_notify_email.py \
              tests/test_notify_retry.py tests/test_cli_notify.py \
              tests/test_db_notifications.py tests/test_settings.py
uv run pytest               # full suite, coverage ≥ 95 %
```

## Scenario → test map

| Spec scenario | Test (module::idea) | Expected |
|---------------|--------------------|----------|
| US1 #1–2 (issue AC1) | `test_notify_email::sends_one_multipart_per_recipient` | local SMTP receives 2 messages, each `multipart/alternative` with `text/plain` + `text/html`, job name, date, footer |
| US1 #3–4 | `test_notify_render::subject_placeholders` | table in [email-message.md](contracts/email-message.md) |
| US1 #5 | `test_notify_email::security_modes[starttls,ssl,none]` + `::starttls_required_but_unsupported` | each mode delivers via the matching aiosmtpd variant; `starttls` against a server without STARTTLS fails the row |
| US1 #6 | `test_settings::smtp_security_rejects_unknown` | `tls` → error naming `smtp_security` |
| US2 #1–5 (issue AC2) | `test_notify_render::sanitizer_examples` | normative examples in [email-message.md](contracts/email-message.md) |
| US3 #1 | `test_notify_email::rows_marked_sent` | 2 rows `sent`, `sent_at` set, `error` NULL, `attempts == 1` |
| US3 #2 (issue AC4) | `test_notify_email::unreachable_server_marks_failed` | all rows `failed`, `deliver_digest` returns without raising; `run_status_after_delivery(SUCCEEDED, …) is PARTIAL` |
| US3 #3 | `test_notify_email::one_recipient_rejected` | A `sent`, B `failed` with `550` text |
| US3 #4 | `test_notify_email::missing_smtp_settings` | rows `failed` with `smtp not configured: missing INVIO_SMTP_HOST` |
| US3 #5 | `test_notify_email::password_never_in_error_or_logs` + `test_cli_notify::database_password_not_printed` | SMTP password and DB password absent from `error`, logs and CLI output |
| FR-013 (early failure, logging) | `test_notify_email::digest_missing_reports_error`, `::render_failure_marks_rows_failed`, `::failure_log_carries_job_and_run` | `outcome.error` → run `partial`; render error lands on rows; the JSON log lines (captured via `JsonFormatter`, not `caplog`) carry `job` / `run_id` |
| US4 #1 (issue AC3) | `test_notify_email::empty_digest_not_sent` | 0 messages, 0 rows |
| US4 #2 | `test_notify_email::empty_digest_sent_when_requested` | "No new items" mail, rows `sent` |
| US5 #1 (issue AC5) | `test_notify_retry::failed_become_sent` + `test_cli_notify::summary_exit_0` | rows `sent`, stdout `retried 2, sent 2, failed 0, given up 0`, exit 0 |
| US5 #2 | `test_cli_notify::still_failing_exit_1` | row `failed` with new error, exit 1 |
| US5 #3 | `test_notify_retry::ignores_sent_skipped_fresh_pending` | no messages |
| US5 #4 | `test_cli_notify::nothing_to_retry` | `nothing to retry`, exit 0 |
| US5 #5 | `test_notify_retry::same_subject_and_body` | retried message equals the original apart from `Date` / `Message-ID` |
| US5 #6 | `test_notify_retry::deleted_digest` | that row `failed` with `digest no longer available`, others sent |
| US5 #7 | `test_notify_retry::fifth_attempt_gives_up` | row `skipped`, error kept, next run skips it, exit 0 |
| US5 #8 | `test_notify_retry::stale_pending_recovered` | pending older than 1 h (injected clock) re-sent; attempts incremented |
| US5 #9 | `test_cli_notify::missing_smtp_exit_2` | exit 2, rows unchanged (status and attempts) |
| Edge: concurrent retry | `test_db_notifications::claim_is_exclusive` | second `claim_for_retry` on the same row returns False |
| Edge: duplicate recipients | `test_notify_email::duplicate_recipients_deduped` | 1 row, 1 message |
| Edge: CRLF in job name / subject | `test_notify_render::subject_strips_line_breaks` | single-line `Subject` header |
| Edge: non-ASCII | `test_notify_email::unicode_roundtrip` | decoded subject and parts equal input |
| Migration | `test_db_migrations` (extended) | upgrade/downgrade 0003 on SQLite (and MariaDB if configured) |

## Manual smoke test (optional, local)

```bash
# Terminal 1: debugging SMTP server that prints messages
uv run python -m aiosmtpd -n -l 127.0.0.1:1025

# Terminal 2
export INVIO_SMTP_HOST=127.0.0.1 INVIO_SMTP_PORT=1025 INVIO_SMTP_SECURITY=none \
       INVIO_SMTP_FROM="Invio <invio@localhost>"
uv run invio notify retry       # → "nothing to retry", exit 0
```

To exercise a retry, stop the server, deliver a stored digest (via a test or the pipeline once it
is wired in), then start the server again and run `uv run invio notify retry`. Expected output:
one `sent` line per recipient and exit code 0.
