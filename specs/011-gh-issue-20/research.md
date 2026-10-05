# Research: E-mail Notifier (gh-issue-20)

All Technical Context unknowns are resolved below. Each entry: Decision / Rationale /
Alternatives considered.

## R1. SMTP client

- **Decision**: `aiosmtplib` (`>=4`), with one `aiosmtplib.SMTP` client per delivery batch,
  reused across recipients and reconnected lazily if a send leaves it disconnected.
  - `smtp_security=ssl` → `SMTP(use_tls=True)`.
  - `starttls` → `SMTP(start_tls=True)`, which fails if the server does not offer STARTTLS. There
    is no silent downgrade.
  - `none` → `SMTP(use_tls=False, start_tls=False)`.
  - `login()` runs only when both `smtp_user` and `smtp_password` are set.
  - Every operation gets `timeout=smtp_timeout_seconds`.
- **Rationale**: the issue names it; it is async and fully typed. It raises distinct exceptions
  (`SMTPConnectError`, `SMTPAuthenticationError`, `SMTPRecipientsRefused`,
  `SMTPServerDisconnected`, `SMTPTimeoutError`) that map cleanly to per-recipient errors.
- **Alternatives**:
  - stdlib `smtplib`: blocking, so it would need a thread executor.
  - `aiosmtplib.send()` per message: opens a new connection and login for every recipient.
- **Per-recipient semantics**: each recipient gets its own `send_message(msg)` call.
  - An exception marks only that notification failed.
  - If the client is no longer connected afterwards (timeout or disconnect), the next recipient
    reconnects first, so other recipients are still attempted (spec edge case).
  - A connect or login failure is recorded on the current recipient, and the reconnect is
    attempted again for the next one. Each recipient therefore gets its own error text, at the
    cost of at most N connection attempts. Fine for a handful of recipients.

## R2. Markdown → HTML and sanitizing

- **Decision**: `MarkdownIt("commonmark", {"html": True, "linkify": False}).enable("table")`,
  then `nh3.clean()` with an explicit allowlist (see [contracts/email-message.md](contracts/email-message.md)):
  - tags: `a, p, br, hr, h1–h6, strong, em, b, i, u, s, del, ul, ol, li, blockquote, code, pre,
    table, thead, tbody, tr, th, td`
  - attributes: `a: href, title`; `th, td: align`
  - `url_schemes={"http","https","mailto"}`
  - `link_rel="noopener noreferrer"`
  - `clean_content_tags={"script","style"}`, so their *content* is dropped, not just the tags
  - `strip_comments=True`
- **Rationale**: `html: True` keeps allowed inline HTML (spec edge case `<em>`). The sanitizer,
  not the Markdown renderer, is the security boundary, so every output passes through `nh3`.
  The allowlist matches the spec's "harmless formatting" list.
  - Images (`img`) are excluded: remote images in mail are tracking beacons, and the spec list
    does not include them. Alt text is lost; acceptable.
  - `nh3` (ammonia, Rust) is maintained, fast and typed, whereas bleach is deprecated.
- **Alternatives**: `bleach` (deprecated); `markdown` (Python-Markdown, less strict
  CommonMark); disabling raw HTML entirely (drops the allowed inline HTML the spec keeps).

## R3. Templates

- **Decision**: Jinja2 `Environment` with `PackageLoader("invio.notify", "templates")`,
  `autoescape=select_autoescape(enabled_extensions=("html.j2",), default_for_string=False)`,
  `undefined=StrictUndefined`, `keep_trailing_newline=True`.
  - The sanitized body is passed as `markupsafe.Markup`.
  - Everything else (job name, subject, date, stats) is autoescaped in HTML (FR-007).
  - The text template gets the raw digest Markdown: Markdown is already readable plain text, and
    `text/plain` cannot execute anything.
- **Rationale**: the issue names Jinja2 and the template file names. `StrictUndefined` turns
  template/context mismatches into test failures.
- **Packaging**: `uv_build` packages every file under the module directory, so `templates/*.j2`
  ships in the wheel. A test loads the templates through `PackageLoader` to guard this.
- **Alternatives**: string formatting (no escaping guarantees); a single HTML template with a
  plaintext derived via html2text (an extra dependency, and worse text).

## R4. Message construction

- **Decision**: `email.message.EmailMessage(policy=email.policy.SMTP)`.
  - Content is set with `set_content(text, subtype="plain", charset="utf-8")` then
    `add_alternative(html, subtype="html", charset="utf-8")`, giving `multipart/alternative`
    with plain text first, as RFC 2046 requires.
  - Headers: `From` = `smtp_from`, `To` = the single recipient, `Subject`,
    `Date` = `email.utils.format_datetime(now)`, `Message-ID` = `make_msgid(domain=<from domain>)`.
- **Subject**: `str.replace` for exactly `{job_name}` and `{date}` (not `str.format`, so other
  braces stay verbatim). Then every `\r`/`\n` is replaced with a space and the result is
  stripped. `EmailMessage` with policy SMTP also refuses header injection, as defence in depth.
- **Non-ASCII**: handled by the policy (RFC 2047 header encoding, UTF-8 body parts).

## R5. Empty digest, date, statistics

- **Empty digest**: `len(digest.item_ids) == 0`.
  - With `send_if_empty=False`, `deliver_digest` returns
    `DeliveryOutcome(skipped_empty=True)` without creating rows or connecting.
  - With `True`, the templates render the "no new items" variant (`is_empty` flag).
- **Digest date**: `run.started_at` converted with `ZoneInfo(job_config.schedule.timezone)`,
  formatted `YYYY-MM-DD`. If the run row is gone, `digest.created_at` is used instead.
- **Statistics footer** (`DigestStats`), snapshotted into the payload at first delivery:
  - `items_found`: `run.stats["items_found"]` if it is an int, else `None`, shown as "–".
  - `items_included`: `len(digest.item_ids)`.
  - `duration_seconds`: `now - run.started_at` at delivery time.
  - **Rationale**: `runs.stats` has no fixed shape yet, so only one optional key is read. The
    snapshot keeps retries byte-for-byte reproducible (FR-015).

## R6. Attempts, claiming, stale `pending`

- **Decision**: migration `0003` adds `notifications.attempts` (`Integer`, not null,
  `server_default 0`) and `notifications.last_attempt_at` (`UTCDateTime`, nullable).
  - **First delivery**: `add()` creates a `pending` row with attempts 0. `begin_attempt()` sets
    `attempts += 1` and `last_attempt_at = now` and commits *before* the SMTP send. After the
    send, `mark()` sets `sent` or `failed`, or `skipped` if attempts ≥ 5.
  - **Retry selection** (`retry_candidates(now)`): rows with `status = failed`, or
    `status = pending AND COALESCE(last_attempt_at, created_at) < now - 1h`. Ordered by id.
  - **Claim** (`claim_for_retry(id, now)`): a single `UPDATE notifications SET status='pending',
    attempts=attempts+1, last_attempt_at=:now WHERE id=:id AND (<same predicate>)`, committed
    immediately. `rowcount == 1` means this process owns the row. Otherwise another process
    took it and the row is skipped silently.
  - A claimed row's `last_attempt_at` is now fresh, so it is not stale for another retry for an
    hour. This guarantees one send per notification per retry invocation, and no
    double-sending between concurrent invocations within that hour.
  - **Stale `pending` already at the limit**: an interrupted attempt was already counted by
    `begin_attempt`. If `attempts >= 5`, retry marks the row `skipped` with error
    `interrupted: attempt limit reached` instead of sending. The interrupted send thus counts
    as one failed attempt (clarification Q4).
- **Rationale**: atomic, portable (plain columns, works on SQLite and MariaDB `READ COMMITTED`),
  and cheap.
- **Alternatives**:
  - Attempts in the JSON payload: no portable atomic update or filtering.
  - `SELECT … FOR UPDATE SKIP LOCKED`: not available on SQLite.
  - A lock file: not multi-host safe.

## R7. Error text and secrets

- **Decision**: `scrub_secret(text, settings)` replaces the non-empty values of `smtp_password`
  and `smtp_user` with `***`. Error format: `"<ExceptionClass>: <message>"`.
  - Truncated to 2,000 characters, with the suffix `… [truncated]`.
  - Missing settings produce `"smtp not configured: missing INVIO_SMTP_HOST"` (and likewise for
    `INVIO_SMTP_FROM`).
  - The digest was deleted: `"digest no longer available"`.
- **Rationale**: FR-014 / constitution V. Bounded size keeps the `Text` column and logs sane.

## R8. Run status

- **Decision**: delivery returns
  `DeliveryOutcome(sent, failed, skipped_empty, notification_ids, error)`, and
  `run_status_after_delivery(planned, outcome)` returns `PARTIAL` iff `planned is SUCCEEDED`
  and (`outcome.failed > 0` or `outcome.error is not None`); otherwise it returns `planned`.
  Here `failed` counts rows that ended `failed` or `skipped`.
  - Rows are created as early as possible: right after the digest, job config and payload are
    known, and *before* rendering or connecting. A render or SMTP error therefore always lands
    on rows.
  - If delivery fails before the recipients are known (digest missing, stored job config or
    payload invalid), no row can be written. `outcome.error` carries the scrubbed reason so
    the run is not left `succeeded` while nobody got the mail.
  - The pipeline (later issue) calls the helper before `RunRepository.finish`.
  - `deliver_digest` catches every exception per recipient and around the whole batch, and logs
    it with `logger.exception` (message scrubbed).
- **Rationale**: keeps the notifier free of orchestration concerns (layering), while FR-013 is
  testable as a pure function plus "deliver never raises" tests. A silent `failed=0` for a
  delivery that never happened would hide the problem from run status and health checks.

## R9. Retry CLI

- **Decision**: `invio notify retry` (no arguments).
  1. Validate config first: the database URL (`MissingSettingError` → exit 2), then
     `smtp_host` / `smtp_from` (missing → exit 2 *without touching rows*, so a config mistake
     does not burn attempts).
  2. Run `asyncio.run(retry_failed(...))`.
  3. Print one line per outcome plus the summary line
     `retried N, sent S, failed F, given up G` to stdout.
  4. Exit 0 if `F + G == 0`, else 1.
  - "Nothing to retry" prints `nothing to retry` and exits 0.
- **Rationale**: constitution II (exit codes, stdout/stderr). This differs from pipeline
  delivery, where missing SMTP settings mark rows failed (spec US3 #4). Retry is
  operator-initiated, so failing fast is more useful there.
- **Alternatives**: `--job` filter (spec: optional, not needed); `--dry-run` (not requested).

## R10. Test SMTP server

- **Decision**: dev dependency `aiosmtpd`. A `smtp_server` fixture starts
  `aiosmtpd.controller.Controller` on `127.0.0.1:0` with a recording handler.
  - Variants: plain, STARTTLS (`tls_context`, `require_starttls=True`), implicit TLS
    (`ssl_context`), and a handler that rejects a given recipient with `550`.
  - The certificate is created per session with the dev dependency `trustme`. The client trusts it through an injectable `ssl.SSLContext` in `SmtpMailer` (tests only;
    production uses the default context).
  - "Unreachable server": a closed port from a just-bound-then-closed socket.
- **Rationale**: in-process and deterministic, with no network (constitution III). It verifies
  the real wire format, including `multipart/alternative` (issue acceptance criterion 1).
- **Alternatives**: mocking `aiosmtplib` (does not verify the MIME output); MailHog in Docker
  (external process).
