---

description: "Task list for the e-mail notifier with HTML and plaintext templates (gh-issue-20)"
---

# Tasks: E-mail Notifier with HTML and Plaintext Templates

**Input**: Design documents from `/specs/011-gh-issue-20/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/cli-notify-retry.md,
contracts/python-api.md, contracts/settings.md, contracts/email-message.md, quickstart.md

**Tests**: Required. Constitution Principle III demands at least one test per acceptance
criterion, including rejection paths, with no external network. Within each story, write the
tests first and confirm they fail before implementing. The scenario → test map is in
`quickstart.md`.

**Organization**: Tasks are grouped by user story so each story can be implemented and tested
on its own.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: User story from spec.md (US1–US5)
- Paths are relative to the repository root (single project: `src/invio/`, `tests/`)

## Conventions for every task

- **Style**: Python 3.12, `mypy --strict` clean, ruff clean, line length 100, double quotes.
  Match the docstring/comment density of `src/invio/db/repositories.py` and
  `src/invio/graph/nodes/keyword_filter.py`.
- **Fixed contracts**:
  - Public names and signatures: `contracts/python-api.md`.
  - CLI output and exit codes: `contracts/cli-notify-retry.md`.
  - Settings: `contracts/settings.md`.
  - MIME shape, subject rules and sanitizer allowlist: `contracts/email-message.md`.
  - Columns, payload and state transitions: `data-model.md`.
  - Decisions: `research.md` R1–R10.
- **Layering**: `invio.notify` may import only `invio.config`, `invio.db`, `invio.domain`, the
  stdlib and the new third-party libraries. It must never import `invio.cli`, `invio.graph`,
  `invio.scheduling` or `invio.services`.
- **Tests**:
  - Async tests rely on `asyncio_mode = "auto"`.
  - DB tests use the `db_engine` / `db_session` fixtures and the factories in
    `tests/db_helpers.py`, and carry `@pytest.mark.db` where they test persistence behaviour.
  - Time-dependent tests use the `fake_clock` fixture from `tests/conftest.py`.
- **Secrets**: no secret value may appear in `error`, log output or `repr`. Use `SecretStr` and
  `scrub_secret`.
- **Transactions**: commit before every network send and right after it. Never keep a DB
  transaction open across an SMTP call (`contracts/python-api.md` § Transactions).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Dependencies, package skeleton, lint configuration

- [x] T001 Update `pyproject.toml`:
  - add runtime dependencies `"aiosmtplib>=4"`, `"jinja2>=3.1"`, `"markdown-it-py>=3"`,
    `"nh3>=0.2.18"` (alphabetical within `dependencies`);
  - add dev dependencies `"aiosmtpd>=1.4"` and `"trustme>=1.2"` to `[dependency-groups].dev`;
  - add `"tests/test_notify*.py" = ["F811"]` and `"tests/test_cli_notify.py" = ["F811"]` to
    `[tool.ruff.lint.per-file-ignores]` (fixtures from `tests/smtp_helpers.py` are imported and
    used as parameters).

  Then run `uv lock` and `uv sync --locked`, and confirm `uv run mypy` still passes.
- [x] T002 [P] Create the package skeleton:
  - `src/invio/notify/templates/` with empty placeholder files `digest.html.j2` and
    `digest.txt.j2`;
  - empty modules `src/invio/notify/payload.py`, `src/invio/notify/render.py` and
    `src/invio/notify/email.py`, each with a one-line module docstring.

  Keep `src/invio/notify/__init__.py` docstring-only for now; the re-exports are added in T030.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Settings change, schema/repository additions, typed payload, SMTP test server.
All user stories depend on these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [x] T003 [P] Update `tests/test_settings.py` (contracts/settings.md):
  - replace the `INVIO_SMTP_STARTTLS` / `smtp_starttls` assertions (currently around lines 24,
    31 and 63) with `INVIO_SMTP_SECURITY`; the default is `"starttls"`, and `"ssl"` and
    `"none"` are accepted;
  - add `test_smtp_security_rejects_unknown`: `INVIO_SMTP_SECURITY=tls` raises
    `ValidationError`, and the message contains `smtp_security` and `'starttls', 'ssl' or 'none'`;
  - add tests that `smtp_timeout_seconds` defaults to `30.0` and rejects `0`, `-1`, `inf` and
    `nan`;
  - add a test that a leftover `INVIO_SMTP_STARTTLS` is listed by `unknown_env_keys()`.
- [x] T004 Change `src/invio/config/settings.py` (data-model.md § Settings):
  - remove `smtp_starttls`;
  - add `smtp_security: Literal["starttls", "ssl", "none"] = "starttls"`;
  - add `smtp_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)` under
    the `# E-mail notifications` block.

  Grep `src/` and `tests/` for `smtp_starttls` and fix every remaining use.
  `uv run pytest tests/test_settings.py` must pass. (Depends on T003)
- [x] T005 [P] Write `tests/test_db_notifications.py` (`@pytest.mark.db`; uses `make_job`,
  `make_run`, `make_digest`, `make_notification` from `tests/db_helpers.py`):
  - `attempts` defaults to `0` and `last_attempt_at` to `None` on new rows;
  - `NotificationRepository.begin_attempt(n, now=t)` sets `attempts == 1` and
    `last_attempt_at == t`, and the status stays `pending`;
  - `mark(n, SENT)` clears a previous `error`;
  - `retry_candidates(now=…, stale_after=timedelta(hours=1))` returns `failed` rows, plus
    `pending` rows whose `COALESCE(last_attempt_at, created_at)` is more than 1 h old. It
    excludes `sent`, `skipped` and fresh `pending` rows, and returns them ordered by id;
  - `claim_for_retry(id, now=…, stale_after=…)` returns `True` once and `False` on a second
    call for the same row (exclusive claim). After a claim the row is `pending`, `attempts` is
    +1 and `last_attempt_at == now`;
  - `DigestRepository.get(id)` returns the digest or `None`.
- [x] T006 [P] Extend `tests/test_db_migrations.py`: upgrading to `0004` adds
  `notifications.attempts` (not null, server default `0`, existing rows read `0`) and
  `notifications.last_attempt_at` (nullable). Downgrading to `0003` removes both. Follow the
  existing migration test pattern in that file.
- [x] T007 Add the columns to `Notification` in `src/invio/db/models.py`:
  - `attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))`;
  - `last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)`.

  Create `src/invio/db/migrations/versions/0004_notification_attempts.py` (revision `"0004"`,
  `down_revision="0003"`, docstring in the style of `0002_job_name_binary_collation.py`):
  - upgrade: `op.add_column` for both columns, with `server_default="0"` and `nullable=False`
    for `attempts` and `nullable=True` for `last_attempt_at` using `UTCDateTime`;
  - downgrade: drop both columns (use `batch_alter_table` for SQLite).

  `uv run pytest tests/test_db_migrations.py` must pass. (Depends on T006)
- [x] T008 Extend `src/invio/db/repositories.py` per contracts/python-api.md § Repository
  additions:
  - `DigestRepository.get(digest_id) -> Digest | None`.
  - `NotificationRepository.begin_attempt(notification, *, now)`.
  - `NotificationRepository.retry_candidates(*, now, stale_after)`: one `select` with
    `or_(status == FAILED, and_(status == PENDING, func.coalesce(last_attempt_at, created_at) < now - stale_after))`,
    ordered by id.
  - `NotificationRepository.claim_for_retry(notification_id, *, now, stale_after) -> bool`: a
    single `update(Notification).where(id == …, <same predicate>).values(status=PENDING, attempts=Notification.attempts + 1, last_attempt_at=now)`;
    return `result.rowcount == 1`, then `session.flush()`.
  - Change `mark()` to also set `error = None` when the status is `SENT`.

  `uv run pytest tests/test_db_notifications.py` must pass. (Depends on T005, T007)
- [x] T009 [P] Write `tests/test_notify_payload.py` for `NotificationPayload` / `DigestStats`
  (data-model.md):
  - a round trip through `model_dump(mode="json")` / `model_validate` preserves every field;
  - it rejects unknown keys, `schema_version` other than `1`, empty `job_name` or `subject`, and
    a negative `items_included`, `items_found` or `duration_seconds`;
  - `digest_date` serializes as `"YYYY-MM-DD"`.
- [x] T010 Implement `src/invio/notify/payload.py`:
  - `DigestStats` and `NotificationPayload`: frozen Pydantic models with
    `ConfigDict(extra="forbid", frozen=True)`;
  - fields exactly as in data-model.md, e.g. `schema_version: Literal[1] = 1`,
    `job_name: str = Field(min_length=1)`, `subject: str = Field(min_length=1)`,
    `digest_date: date`, `is_empty: bool`, `stats: DigestStats`;
  - in `DigestStats`: `items_found: int | None = Field(default=None, ge=0)`,
    `items_included: int = Field(ge=0)`,
    `duration_seconds: float | None = Field(default=None, ge=0)`.

  (Depends on T009)
- [x] T011 [P] Create `tests/smtp_helpers.py` (research R10). It provides:
  - **`RecordingHandler`**: an aiosmtpd handler storing `(mail_from, rcpt_tos, raw bytes)`.
    A `reject` set of addresses makes `handle_RCPT` return `"550 mailbox unavailable"` for
    them.
  - **Fixtures**:
    - `smtp_server` (plain): `aiosmtpd.controller.Controller(handler, hostname="127.0.0.1", port=0)`;
      it yields an object with `host`, `port`, `handler` and `messages` (parsed with
      `email.message_from_bytes(…, policy=email.policy.default)`);
    - `smtp_server_starttls`: `tls_context` from a session-scoped `trustme.CA`, with
      `require_starttls=True`;
    - `smtp_server_ssl`: `ssl_context`, implicit TLS;
    - `client_tls_context`: an `ssl.SSLContext` that trusts the CA;
    - `closed_port`: bind a socket to `127.0.0.1:0`, read the port and close the socket.
  - **`smtp_settings(server, **overrides) -> Settings`**: builds
    `Settings(smtp_host=…, smtp_port=…, smtp_from="Invio <invio@localhost>", smtp_security="none", smtp_timeout_seconds=5, _env_file=None, **overrides)`.
  - **`seed_digest(session, *, job_name="ai-news", to=[...], subject="invio: {job_name} – {date}", send_if_empty=False, body="# News\n\n- item", item_ids=[1, 2], timezone="Europe/Berlin", run_stats={"items_found": 7}) -> Digest`**:
    creates a `Job` whose `config` is a valid `JobConfig` dump (build it from the `job_data`
    shape in `tests/conftest.py` with the notification fields overridden), a finished `Run`
    and a `Digest`, and commits.

---

**Checkpoint**: Settings, schema, repositories, payload and test SMTP infrastructure are ready.
`uv run pytest tests/test_settings.py tests/test_db_notifications.py tests/test_db_migrations.py tests/test_notify_payload.py`
is green.

---

## Phase 3: User Story 1 - Receive the digest by e-mail (Priority: P1) 🎯 MVP

**Goal**: Each distinct recipient receives one `multipart/alternative` mail (plain text + HTML)
with job name, date, digest content and stats footer. The subject placeholders are replaced,
and the connection uses `smtp_security`.

**Independent Test**: Seed a job with two recipients, call `deliver_digest` against
`smtp_server`, and inspect the two received messages (quickstart rows US1 #1–6).

### Tests for User Story 1 ⚠️

- [x] T012 [P] [US1] Write `tests/test_notify_render.py` (render part, no network):
  - `render_subject` matches the table in contracts/email-message.md § Subject rendering
    (placeholders, no placeholders, `{unknown}` and a lone `{` left verbatim);
  - `render_mail(payload, markdown)`:
    - the text part contains the job name, the date, the Markdown body verbatim and the footer
      `Items found: 7 · Included: 2 · Run time: …`;
    - the HTML part contains `<h1>` for `# News`, the job name and the footer;
    - `items_found=None` and `duration_seconds=None` render as `–`;
  - `build_message`:
    - `get_content_type() == "multipart/alternative"`;
    - the parts are `text/plain` then `text/html`, both `utf-8`;
    - `From`, `To` (single recipient), `Subject`, `Date` and `Message-ID` are set;
  - the templates load via `PackageLoader("invio.notify", "templates")` (packaging guard).
- [x] T013 [P] [US1] Write `tests/test_notify_email.py` (US1 part; uses `smtp_helpers`
  fixtures and `db_engine`):
  - `sends_one_multipart_per_recipient`:
    - seed `to=["a@example.org", "b@example.org"]`; `await deliver_digest(session_factory(db_engine), digest.id, settings=smtp_settings(smtp_server))`;
    - exactly 2 messages arrive, each `rcpt_tos` is a single address, each message is
      `multipart/alternative` with both parts;
    - the subject is `invio: ai-news – <date>`, where `<date>` is the run's `started_at` in
      `Europe/Berlin`;
  - `duplicate_recipients_deduped`: `to` contains the same address twice → 1 message and 1
    row;
  - `security_modes`, parametrized:
    - `starttls` against `smtp_server_starttls` and `ssl` against `smtp_server_ssl` (both with
      `mailer_factory=lambda s: SmtpMailer(s, tls_context=client_tls_context)`), and `none`
      against `smtp_server`: each delivers (the "STARTTLS not offered" failure case is tested
      in T020, once error handling exists);
  - `login_only_when_credentials_set`: with `smtp_user`/`smtp_password` set against a server
    requiring auth, the mail is delivered; without them, no `AUTH` command is sent;
  - `unicode_roundtrip`: a job name, subject and body with `ä`, `ß` and an emoji decode equal
    to the input.

### Implementation for User Story 1

- [x] T014 [P] [US1] Write the templates (contracts/email-message.md § Template context):
  - `src/invio/notify/templates/digest.html.j2`: a minimal inline-styled HTML document with
    `<title>{{ subject }}</title>`, a heading `{{ job_name }}`, a line with `{{ date }}`, then
    either `{{ body_html }}` or, when `is_empty`, the text "No new items were found for this
    run.", and a `<footer>` with
    `Items found: {{ stats.items_found }} · Included: {{ stats.items_included }} · Run time: {{ stats.duration }}`;
  - `src/invio/notify/templates/digest.txt.j2`: the same structure as plain text, with
    `{{ body_text }}` and the same footer line.
- [x] T015 [US1] Implement `src/invio/notify/render.py` (research R2–R4):
  - **Environment**: a module-level Jinja2 `Environment(loader=PackageLoader("invio.notify", "templates"), autoescape=select_autoescape(enabled_extensions=("html.j2",), default_for_string=False), undefined=StrictUndefined, keep_trailing_newline=True)`.
  - **`markdown_to_safe_html(markdown)`**: for now, `MarkdownIt("commonmark", {"html": False}).enable("table").render(markdown)`,
    so raw HTML is escaped. US2 (T019) switches to `html: True` plus `nh3`.
  - **`render_subject(template, *, job_name, date)`**: `str.replace` for `{job_name}` and
    `{date}` (`date.isoformat()`), then replace every `\r` and `\n` with a space and strip.
  - **`_format_duration(seconds) -> str`**: `"3m 12s"`, `"45s"` or `"1h 02m"`; `–` for `None`.
  - **`render_mail(payload, digest_markdown) -> RenderedMail`**: renders both templates with
    the context table from contracts/email-message.md; `body_html` is wrapped in
    `markupsafe.Markup`.
  - **`build_message(mail, *, sender, recipient, now)`** (research R4): `EmailMessage(policy=email.policy.SMTP)`,
    `set_content(text, subtype="plain", charset="utf-8")`, then
    `add_alternative(html, subtype="html", charset="utf-8")`. Headers: `From`, `To`, `Subject`,
    `Date=format_datetime(now)`, and `Message-ID=make_msgid(domain=<domain part of sender's addr-spec, fallback "localhost">)`.
  - **`RenderedMail`**: a frozen dataclass (`subject`, `text`, `html`) defined in this module.

  `uv run pytest tests/test_notify_render.py` must pass. (Depends on T002, T010, T012, T014)
- [x] T016 [US1] Implement `SmtpMailer` in `src/invio/notify/email.py` (research R1,
  contracts/python-api.md):
  - **`__init__(settings, *, tls_context=None)`**: stores the settings. The client is created
    lazily as `aiosmtplib.SMTP(hostname=settings.smtp_host, port=settings.smtp_port, use_tls=(security == "ssl"), start_tls=(security == "starttls"), timeout=settings.smtp_timeout_seconds, tls_context=tls_context)`.
    With `"none"`, both TLS flags are `False`.
  - **`_ensure_connected()`**: if the client is not connected, call `connect()`; then, when
    both `smtp_user` and `smtp_password` are set, call
    `login(user, password.get_secret_value())`.
  - **`send(message)`**: `_ensure_connected()`, then `send_message(message)`. Exceptions
    propagate.
  - **`__aexit__`**: `quit()`, wrapped in `contextlib.suppress(Exception)`.
- [x] T017 [US1] Implement the success path of `deliver_digest` in `src/invio/notify/email.py`
  (contracts/python-api.md; data-model.md § Rules):
  1. In one session: load the digest (`DigestRepository.get`), the job (`session.get(Job, digest.job_id)`)
     and `JobConfig.model_validate(job.config)`, and the run (`RunRepository.get`, may be
     `None`).
  2. Compute `digest_date`: the run's `started_at` (or `digest.created_at`) as
     `.astimezone(ZoneInfo(cfg.schedule.timezone)).date()`.
  3. Build the `DigestStats` snapshot (research R5): `items_found` comes from
     `run.stats.get("items_found")` only if it is an `int`; `items_included = len(digest.item_ids)`;
     `duration_seconds = (clock() - run.started_at).total_seconds()` when the run exists.
  4. Build the `NotificationPayload`.
  5. De-duplicate `cfg.notification.to` by exact string, keeping order. Create one row per
     recipient with `NotificationRepository.add(job.id, CHANNEL_EMAIL, recipient, run_id=…, digest_id=…, payload=payload.model_dump(mode="json"))`,
     then commit. Rows are created *before* rendering, so a later render or SMTP error always
     lands on rows (research R8).
  5a. Build the `RenderedMail` once with `render_mail(payload, digest.body)`.
  6. Open `async with mailer_factory(settings) as mailer:`. For each row: `begin_attempt(now=clock())`
     and commit; `await mailer.send(build_message(...))`; `mark(SENT)` and commit.
  7. Return `DeliveryOutcome(sent=…, failed=0, skipped_empty=False, notification_ids=…)`.

  Define `MAX_ATTEMPTS`, `STALE_PENDING_AFTER`, `CHANNEL_EMAIL` and the frozen dataclass
  `DeliveryOutcome` (fields per data-model.md, including `error: str | None = None`) in this
  module. Error handling is added in US3 (T024).
  `uv run pytest tests/test_notify_email.py -k "multipart or dedup or security or login or unicode"`
  must pass. (Depends on T008, T011, T013, T015, T016)

**Checkpoint**: Digest mails arrive with both parts (issue acceptance criterion 1). US1 is
demonstrable on its own.

---

## Phase 4: User Story 2 - Digest content cannot inject active or unsafe content (Priority: P1)

**Goal**: Allowed raw HTML is kept and everything outside the allowlist is stripped from the
HTML part (FR-006/FR-007).

**Independent Test**: Render the normative examples of contracts/email-message.md and inspect
the HTML (quickstart row US2 #1–5).

### Tests for User Story 2 ⚠️

- [x] T018 [P] [US2] Add to `tests/test_notify_render.py`:
  - **`sanitizer_examples`**: parametrized over every row of the "Normative examples" table in
    contracts/email-message.md, asserting the "must not contain" and "must contain" substrings
    on `markdown_to_safe_html(input)`.
  - **Additional cases**:
    - `onerror` on any tag is removed;
    - `data:` and `vbscript:` links are removed;
    - `<form>` is removed;
    - links get `rel="noopener noreferrer"`;
    - `<img src=…>` is removed;
    - comments are removed.
  - **`escaping`**: job name `<b>&` and subject `"<script>"` appear escaped (`&lt;b&gt;&amp;`)
    in the HTML part and verbatim in the text part.
  - **Regression**: the existing US1 render tests still pass with raw HTML enabled.

### Implementation for User Story 2

- [x] T019 [US2] In `src/invio/notify/render.py`, switch `markdown_to_safe_html` to
  `MarkdownIt("commonmark", {"html": True, "linkify": False}).enable("table")` and pass the
  output through `nh3.clean()` with module-level constants that match the allowlist in
  contracts/email-message.md verbatim:
  - `_ALLOWED_TAGS = {"a","p","br","hr","h1","h2","h3","h4","h5","h6","strong","em","b","i","u","s","del","ul","ol","li","blockquote","code","pre","table","thead","tbody","tr","th","td"}`
  - `_ALLOWED_ATTRIBUTES = {"a": {"href","title"}, "th": {"align"}, "td": {"align"}}`
  - `url_schemes={"http","https","mailto"}`, `link_rel="noopener noreferrer"`,
    `clean_content_tags={"script","style"}`, `strip_comments=True`

  Add a docstring stating that the sanitizer is the security boundary and that images are
  deliberately excluded (research R2). `uv run pytest tests/test_notify_render.py` must pass.
  (Depends on T015, T018)

**Checkpoint**: Issue acceptance criterion 2 is met. US1 tests still green.

---

## Phase 5: User Story 3 - Delivery failures are recorded without failing the run (Priority: P1)

**Goal**: Per-recipient `sent`/`failed` rows with scrubbed error text. `deliver_digest` never
raises, missing SMTP settings fail the rows, and the run becomes `partial` instead of
`succeeded` (FR-010–FR-015, clarification Q1).

**Independent Test**: Deliver against `closed_port`, against a server rejecting one recipient,
and with no `smtp_host`; inspect the rows and `run_status_after_delivery` (quickstart rows
US3 #1–5).

### Tests for User Story 3 ⚠️

- [x] T020 [P] [US3] Add to `tests/test_notify_email.py`:
  - **`rows_marked_sent`**: 2 rows, each with `status == SENT`, `sent_at` set, `error is None`,
    `attempts == 1`, `channel == "email"`, `run_id`/`digest_id` set, and
    `NotificationPayload.model_validate(row.payload)` succeeding.
  - **`unreachable_server_marks_failed`**:
    - `smtp_port=closed_port`; `deliver_digest` returns (does not raise);
    - all rows are `FAILED`, `error` starts with an exception class name, and `attempts == 1`;
    - `outcome.failed == 2`.
  - **`one_recipient_rejected`**: `handler.reject={"b@example.org"}` → A is `SENT`; B is
    `FAILED` with `"550"` in `error`.
  - **`mid_batch_disconnect_reconnects`**: a handler that drops the connection on the first
    `DATA` → the first row is `FAILED` and the second row is `SENT` (reconnect, research R1).
  - **`missing_smtp_settings`**: `smtp_host=None` → every row is `FAILED` with
    `"smtp not configured: missing INVIO_SMTP_HOST"`; likewise for `smtp_from=None` and
    `INVIO_SMTP_FROM`; no connection attempt is made.
  - **`password_never_in_error_or_logs`**:
    - a server requiring AUTH rejects the login, with `smtp_password="s3cr3t-pw"` and
      `smtp_user="bob@x"`;
    - `"s3cr3t-pw"` appears neither in any `error` nor in `capsys`/`caplog` output, and
      neither does `"bob@x"`;
    - errors longer than 2,000 chars end with `"… [truncated]"`.
  - **`unexpected_error_never_raises`**: a `mailer_factory` whose `send` raises `RuntimeError`
    → the row is `FAILED` and the function returns.
  - **`digest_missing_reports_error`**: `deliver_digest(…, digest_id=999_999, …)` returns
    without raising; `outcome.error` contains `"not found"`; no rows exist; and
    `run_status_after_delivery(SUCCEEDED, outcome) is PARTIAL`.
  - **`invalid_job_config_reports_error`**: the job's stored `config` is `{"x": 1}` →
    `outcome.error` is set, no rows exist, and no connection is made.
  - **`render_failure_marks_rows_failed`**: monkeypatch `invio.notify.email.render_mail` to
    raise `RuntimeError("boom")` → both rows exist and are `FAILED` with `"RuntimeError: boom"`,
    and `outcome.failed == 2`.
  - **`starttls_required_but_unsupported`** (moved from T013): `smtp_security="starttls"`
    against the plain `smtp_server` → the row is `FAILED` and no message is received.
  - **`failure_log_carries_job_and_run`** (FR-013): `job` and `run_id` are added by
    `JsonFormatter.format()` from context variables, not stored on the `LogRecord`, so
    `caplog` cannot see them. Instead, attach a `logging.StreamHandler(io.StringIO())` with
    `invio.log.JsonFormatter()` to `logging.getLogger("invio.notify")` (level `DEBUG`,
    removed in teardown), as the `log_stream` fixture in `tests/test_log.py` does. Parse
    the JSON lines; the `"notification failed"` line has `job == "ai-news"`,
    `run_id == str(run.id)`, `recipient` and `notification_id`.
- [x] T021 [P] [US3] Add `tests/test_notify_run_status.py`: `run_status_after_delivery`
  returns `PARTIAL` for `(SUCCEEDED, failed>0)`, `SUCCEEDED` for `(SUCCEEDED, failed=0)`, and
  the planned status unchanged for `PARTIAL`/`FAILED` with any outcome. A `skipped_empty`
  outcome never changes the status. An outcome with `failed=0` but `error` set turns
  `SUCCEEDED` into `PARTIAL`.

### Implementation for User Story 3

- [x] T022 [US3] In `src/invio/notify/email.py`, implement:
  - **`scrub_secret(text, settings)`** (research R7): replace the non-empty
    `smtp_password.get_secret_value()` and `smtp_user` with `"***"`. If the result is longer
    than 2,000 chars, cut it to 2,000 chars and append `"… [truncated]"`.
  - **`_error_text(exc, settings)`**: returns `scrub_secret(f"{type(exc).__name__}: {exc}", settings)`.
  - **`missing_smtp_settings(settings)`**: returns `["INVIO_SMTP_HOST"]` and/or
    `["INVIO_SMTP_FROM"]` for the missing or blank values, in that order.
- [x] T023 [US3] In `src/invio/notify/email.py`, implement
  `run_status_after_delivery(planned, outcome)` per research R8 (`failed > 0` **or** `error is not None`). `uv run pytest tests/test_notify_run_status.py`
  must pass. (Depends on T021)
- [x] T024 [US3] Harden `deliver_digest` in `src/invio/notify/email.py` (FR-011–FR-014):
  - **Missing settings**: if `missing_smtp_settings` is non-empty, create the rows, call
    `begin_attempt`, then `mark(FAILED, error="smtp not configured: missing <first name>")` for
    each one, without creating a mailer.
  - **Per-recipient failures**: wrap each recipient's send in `try/except Exception`. On error,
    `mark(FAILED or SKIPPED if attempts >= MAX_ATTEMPTS, error=_error_text(exc))`, commit, log
    `logger.warning("notification failed", extra={"notification_id", "recipient", "digest_id", "error"})`
    and continue. Log `logger.info("notification sent", extra=…)` on success.
  - **Log context (FR-013)**: run the per-recipient loop inside
    `run_context(job=job.name, run_id=str(digest.run_id) if digest.run_id else None)` from
    `invio.log`, so every record carries `job` and `run_id` even when the caller has not
    opened a run context.
  - **Whole-function failures**: wrap the whole function body.
    - An exception before the rows exist (digest not found, stored job config or payload
      invalid) is logged with `logger.exception` and returns
      `DeliveryOutcome(sent=0, failed=0, skipped_empty=False, notification_ids=(), error=_error_text(exc))`.
      A missing digest uses the error `"digest <id> not found"`.
    - An exception after the rows exist (e.g. in `render_mail` or the mailer context) marks
      every row still `pending` as `FAILED` with `_error_text(exc)` and counts it in `failed`.
  - **Outcome counts**: `DeliveryOutcome.failed` counts rows that ended `FAILED` or `SKIPPED`.

  `uv run pytest tests/test_notify_email.py` must pass. (Depends on T017, T020, T022)

**Checkpoint**: Issue acceptance criterion 4 is met. US1/US2 tests still green.

---

## Phase 6: User Story 4 - No mail for empty digests unless requested (Priority: P2)

**Goal**: An empty digest with `send_if_empty=false` sends nothing and creates no rows; with
`true`, it sends a "no new items" mail (FR-008/FR-009).

**Independent Test**: Seed `item_ids=[]` with both flag values against `smtp_server`
(quickstart rows US4 #1–2).

### Tests for User Story 4 ⚠️

- [x] T025 [P] [US4] Add to `tests/test_notify_email.py`:
  - **`empty_digest_not_sent`**: `item_ids=[]`, `send_if_empty=False` → `outcome.skipped_empty is True`,
    0 messages, and 0 rows for the digest (`NotificationRepository.list_for_job`). No
    connection is attempted, even with `smtp_port=closed_port`.
  - **`empty_digest_sent_when_requested`**: `send_if_empty=True` → each recipient gets a mail
    whose text and HTML parts contain "No new items were found for this run." and the footer
    `Included: 0`; the rows are `SENT` and `payload["is_empty"] is True`.
  - Add to `tests/test_notify_run_status.py`: `skipped_empty` keeps `SUCCEEDED`, if not already
    covered by T021.

### Implementation for User Story 4

- [x] T026 [US4] In `deliver_digest` (`src/invio/notify/email.py`), right after loading the
  digest and config: if `len(digest.item_ids) == 0` and not `cfg.notification.send_if_empty`,
  log `logger.info("empty digest not sent", extra={"digest_id": …})` and return
  `DeliveryOutcome(sent=0, failed=0, skipped_empty=True, notification_ids=())` before any row
  or mailer is created. Otherwise set `is_empty=len(digest.item_ids) == 0` on the payload. Make
  sure `render_mail` and both templates honour `is_empty` (T014/T015).
  `uv run pytest tests/test_notify_email.py -k empty` must pass. (Depends on T024, T025)

**Checkpoint**: Issue acceptance criterion 3 is met.

---

## Phase 7: User Story 5 - Retry failed notifications from the CLI (Priority: P2)

**Goal**: `invio notify retry` re-sends `failed` and stale `pending` notifications from the
stored digest and payload. It claims rows atomically, enforces the 5-attempt limit (→
`skipped`), prints a summary and exits 0/1/2 (FR-016–FR-021, clarifications Q2/Q4).

**Independent Test**: Create failed rows with the server down, start `smtp_server`, run the
command through `typer.testing.CliRunner`, and inspect output, exit code, rows and received
mail (quickstart rows US5 #1–8).

### Tests for User Story 5 ⚠️

- [x] T027 [P] [US5] Write `tests/test_notify_retry.py` (`retry_failed`, `db_engine`,
  `fake_clock`, `smtp_helpers`):
  - **`failed_become_sent`**: deliver with `closed_port` (2 rows `FAILED`), then
    `retry_failed` against `smtp_server`. Both rows end `SENT` with `error is None` and
    `attempts == 2`; the outcome is `retried=2, sent=2, failed=0, given_up=0`.
  - **`same_subject_and_body`**: the retried message's `Subject`, text part and HTML part equal
    those of a direct delivery of the same digest. The job is renamed and its subject changed
    in between, to prove the payload is used. No new `Digest`/`Run` row is created.
  - **`still_failing`**: the server is still down → the row stays `FAILED` with the new error
    and `attempts == 2`.
  - **`ignores_sent_skipped_fresh_pending`**: rows in `SENT`, `SKIPPED`, and `PENDING` with
    `last_attempt_at = now - 59min` are not sent.
  - **`stale_pending_recovered`**: a `PENDING` row with `attempts=1` and
    `last_attempt_at = now - 61min` is sent and ends with `attempts == 2`.
  - **`stale_pending_at_limit`**: a `PENDING` row with `attempts=5`, stale → `SKIPPED` with
    `"interrupted: attempt limit reached"`, nothing sent, and counted in `given_up`.
  - **`fifth_attempt_gives_up`**: a `FAILED` row with `attempts=4` and the server down → after
    the retry it is `SKIPPED`, the error is kept and `given_up == 1`. A second `retry_failed`
    finds nothing.
  - **`deleted_digest`**: the digest row is deleted (`digest_id` becomes NULL) → that row is
    `FAILED` with `"digest no longer available"` and the other rows are still sent.
  - **`invalid_payload`**: `payload={"x": 1}` → `FAILED` with
    `"invalid notification payload: …"`.
  - **`claim_skips_taken_row`**: monkeypatch `claim_for_retry` to return `False` for one id →
    that row is neither sent nor counted.
- [x] T028 [P] [US5] Write `tests/test_cli_notify.py` (`CliRunner` on
  `invio.cli.main.create_app()`; set `INVIO_DATABASE_URL` to the test SQLite URL and the SMTP
  env vars via `monkeypatch`):
  - **`summary_exit_0`**: 2 failed rows and the server up → stdout has two `sent` lines and
    `retried 2, sent 2, failed 0, given up 0`; exit code 0.
  - **`still_failing_exit_1`**: a `failed` line and exit code 1.
  - **`given_up_exit_1`**: `given up` line, exit code 1.
  - **`nothing_to_retry`**: stdout is exactly `nothing to retry\n`; exit code 0.
  - **`missing_smtp_exit_2`**: no `INVIO_SMTP_HOST` → `Configuration error: smtp not configured: missing INVIO_SMTP_HOST`
    on stderr, exit code 2, and the rows are unchanged (status and attempts).
  - **`missing_database_exit_2`**: exit code 2 with `Configuration error`.
  - **`unknown_security_exit_2`**: `INVIO_SMTP_SECURITY=tls` → exit code 2.
  - **`password_not_printed`**: no secret in stdout/stderr.
  - **`database_password_not_printed`**: `INVIO_DATABASE_URL` with password `db-s3cr3t`, and
    `retry_failed` monkeypatched to raise
    `sqlalchemy.exc.OperationalError("connect", {}, Exception("… db-s3cr3t …"))` → exit code 1,
    `Error: OperationalError` on stderr, and `db-s3cr3t` appears nowhere in stdout or stderr.
  - **`command_registered`**: `invio notify --help` lists `retry`.

### Implementation for User Story 5

- [x] T029 [US5] Implement `retry_failed` in `src/invio/notify/email.py` (contracts/cli-notify-retry.md
  steps 3–6; data-model.md § State transitions):
  1. `now = clock()`; get candidates via `retry_candidates(now=now, stale_after=STALE_PENDING_AFTER)`.
  2. For each candidate:
     - a stale `pending` row with `attempts >= MAX_ATTEMPTS` → `mark(SKIPPED, error="interrupted: attempt limit reached")`,
       commit, and count it as given up;
     - otherwise `claim_for_retry` and commit; if it returns `False`, skip the row.
  3. Load the digest (`None` → the error `"digest no longer available"`) and
     `NotificationPayload.model_validate(row.payload)` (`ValidationError` →
     `"invalid notification payload: <first error msg>"`). Rebuild with
     `render_mail(payload, digest.body)` and send with
     `build_message(..., recipient=row.recipient)` over one shared mailer.
  4. Mark the row `SENT`, or `FAILED`/`SKIPPED` (`attempts >= MAX_ATTEMPTS`), with
     `_error_text`.
  5. Log inside `run_context(job=payload.job_name or str(row.job_id))`.
  6. Return a `RetryOutcome` with its `RetryResult` tuple. Never raise for per-row errors.

  Define the frozen dataclasses `RetryOutcome`/`RetryResult` here.
  `uv run pytest tests/test_notify_retry.py` must pass. (Depends on T024, T027)
- [x] T030 [US5] Re-export the public API in `src/invio/notify/__init__.py` per
  contracts/python-api.md (`__all__`): `CHANNEL_EMAIL`, `MAX_ATTEMPTS`, `STALE_PENDING_AFTER`,
  `DeliveryOutcome`, `RetryOutcome`, `RetryResult`, `SmtpMailer`, `deliver_digest`,
  `retry_failed`, `run_status_after_delivery`, `missing_smtp_settings`, `scrub_secret`,
  `NotificationPayload`, `DigestStats`. (Depends on T029)
- [x] T031 [US5] Create `src/invio/cli/commands/notify.py` with
  `app = typer.Typer(help="E-mail notifications.", no_args_is_help=True)` and command `retry`
  (contracts/cli-notify-retry.md):
  - **Settings**: `get_settings()` wrapped like `src/invio/cli/commands/db.py`.
    `ValidationError`/`ValueError` → `fail("Configuration error: …", 2)`.
    `require_secret("database_url")` → `MissingSettingError` → exit 2.
  - **SMTP check**: `missing_smtp_settings` non-empty →
    `fail("Configuration error: smtp not configured: missing <name>", 2)` before opening any
    session.
  - **Run**: `asyncio.run(retry_failed(session_factory(create_db_engine(url)), settings=settings))`.
  - **Output**:
    - per result: `f"{label:<9} #{id} {job_name} {recipient}"`, plus `"  " + error` when
      failed or given up, with labels `sent`, `failed` and `given up`;
    - then `retried N, sent S, failed F, given up G`;
    - or just `nothing to retry`.
  - **Exit codes**: `raise typer.Exit(1)` when `F + G > 0`. An unexpected exception →
    `fail(f"Error: {type(exc).__name__}: {scrub(str(getattr(exc, 'orig', None) or exc))}", 1)`
    with no traceback. `scrub` applies both `scrub_secret(text, settings)` and, like
    `cli/commands/db.py`, `redact(redact(text, raw_url), normalize_url(raw_url))` from
    `invio.db.session`, so neither the SMTP credentials nor the database URL/password can
    appear (constitution V).

  `uv run pytest tests/test_cli_notify.py` must pass. (Depends on T030, T028)

**Checkpoint**: Issue acceptance criterion 5 is met. All five stories are green.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [x] T032 [P] Update `.env.example` per contracts/settings.md: replace
  `INVIO_SMTP_STARTTLS=true` with `INVIO_SMTP_SECURITY=starttls` and add
  `INVIO_SMTP_TIMEOUT_SECONDS=30`, with a one-line comment listing `starttls | ssl | none`.
- [x] T033 [P] Update `README.md` (constitution: user-facing changes need docs):
  - in the settings section, document `INVIO_SMTP_SECURITY` (replacing `INVIO_SMTP_STARTTLS`;
    mention the "unknown setting ignored" warning for the old name) and
    `INVIO_SMTP_TIMEOUT_SECONDS`;
  - add a "Notifications" section: one mail per recipient (HTML + plain text), subject
    placeholders `{job_name}`/`{date}`, the `send_if_empty` behaviour, notification statuses,
    the 5-attempt limit, `invio notify retry` with its exit codes, and the stale-`pending`
    recovery after 1 h (with its possible duplicate mail);
  - update the package tree line `notify/` if needed.
- [x] T034 [P] Add a layering test in the new file `tests/test_notify_layering.py`,
  modelled on `tests/test_llm_layering.py`: no module under
  `invio.notify` imports `invio.cli`, `invio.graph`, `invio.scheduling` or `invio.services`.
- [x] T035 Run the full gates: `uv run ruff check`, `uv run ruff format --check`,
  `uv run mypy`, `uv run pytest` (coverage `fail_under = 95` must hold; add tests for any
  uncovered branch in `src/invio/notify/`). If `INVIO_TEST_DATABASE_URL` is available, also run
  `uv run pytest -m db` on MariaDB. (Depends on all previous tasks)
- [x] T036 Walk through `specs/011-gh-issue-20/quickstart.md`: confirm every row of the
  scenario → test map exists under the stated test name (rename tests or update the table),
  and run the manual smoke test (`python -m aiosmtpd -n -l 127.0.0.1:1025` +
  `invio notify retry` → `nothing to retry`). (Depends on T035)

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (T001–T002)**: T002 can run alongside T001.
- **Foundational (T003–T011)**: T004 needs T003. T007 needs T006. T008 needs T005 and T007.
  T010 needs T009. T011 needs T001 (aiosmtpd/trustme) and T004 (`smtp_security` in
  `Settings`). Blocks all stories.
- **US1 (T012–T017)**: needs Foundational. T015 needs T012 and T014. T017 needs T015 and T016.
- **US2 (T018–T019)**: needs T015 (`render.py` exists). It is independent of T016/T017, so it
  can run in parallel with the rest of US1.
- **US3 (T020–T024)**: needs T017. T023 needs T021. T024 needs T020 and T022.
- **US4 (T025–T026)**: needs T024 (same function; error paths in place).
- **US5 (T027–T031)**: needs T024 (shared send/mark helpers). T029 needs T027, T030 needs T029,
  and T031 needs T028 and T030.
- **Polish (T032–T036)**: T032–T034 can run any time after T004 / T031. T035 and T036 come
  last.

### Story dependencies

- **US1 → US3 → US4 / US5**: these all extend `deliver_digest` in
  `src/invio/notify/email.py`, so they run in sequence on that file.
- **US2**: touches only `render.py`, so it can run in parallel with US1's `email.py` work once
  T015 is done.
- **US4 and US5**: both build on US3, and can run in parallel (different code paths) if edits
  to `email.py` are coordinated.

### Parallel opportunities

- T001 ∥ T002.
- Foundational tests: T003 ∥ T005 ∥ T006 ∥ T009 (different files); T011 after T004
  (it builds `Settings(smtp_security=…)`), in parallel with T005–T010.
- US1: T012 ∥ T013 ∥ T014.
- US2: T018 ∥ T016 (and T019 ∥ T017).
- US3: T020 ∥ T021.
- US5: T027 ∥ T028; and T025/T026 (US4) ∥ T027/T028.
- Polish: T032 ∥ T033 ∥ T034.

### Parallel example: Foundational tests

```text
Task: "T003 tests/test_settings.py (smtp_security)"
Task: "T005 tests/test_db_notifications.py"
Task: "T006 tests/test_db_migrations.py (0004)"
Task: "T009 tests/test_notify_payload.py"
then (after T004): "T011 tests/smtp_helpers.py"
```

### Parallel example: User Story 1

```text
Task: "T012 tests/test_notify_render.py"
Task: "T013 tests/test_notify_email.py (US1 part)"
Task: "T014 templates digest.html.j2 / digest.txt.j2"
then: "T015 render.py" ∥ "T016 SmtpMailer"  →  "T017 deliver_digest success path"
```

### Parallel example: User Story 5

```text
Task: "T027 tests/test_notify_retry.py"
Task: "T028 tests/test_cli_notify.py"
then: "T029 retry_failed" → "T030 __init__ exports" → "T031 cli/commands/notify.py"
```

---

## Implementation Strategy

### MVP first (User Story 1)

1. Phase 1 and Phase 2.
2. Phase 3 (US1): mails with both parts arrive at the local server. Stop and validate the
   quickstart rows for US1.

US1 alone ships safe output, because raw HTML is escaped until US2 enables the allowlist.

### Incremental delivery

1. **+ US2**: sanitizer allowlist with raw HTML → acceptance criterion 2.
2. **+ US3**: failure tracking, never-raise behaviour, run status → acceptance criterion 4.
   This is the minimum shippable state for unattended runs.
3. **+ US4**: `send_if_empty` → acceptance criterion 3.
4. **+ US5**: `invio notify retry` → acceptance criterion 5.
5. **Polish**: docs, layering test, gates, quickstart walk-through, then the PR referencing
   #20 (justify the four new runtime dependencies in the PR description).

Each checkpoint leaves the suite green, so work can stop or be split into PRs at any of them.
