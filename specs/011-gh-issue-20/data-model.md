# Data Model: E-mail Notifier (gh-issue-20)

Builds on [specs/002-gh-issue-4/data-model.md](../002-gh-issue-4/data-model.md). Only changes
and the new typed payload are listed.

## `notifications` (changed, migration `0003_notification_attempts`)

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `id` | PK | no | auto | unchanged |
| `job_id` | FK → jobs (CASCADE) | no | — | unchanged |
| `run_id` | FK → runs (SET NULL) | yes | — | unchanged |
| `digest_id` | FK → digests (SET NULL) | yes | — | unchanged; `NULL` at retry → "digest no longer available" |
| `channel` | `String(32)` | no | — | always `"email"` for this feature |
| `recipient` | `String(320)` | no | — | one address as configured; the job's list is de-duplicated by exact string, keeping the first occurrence |
| `status` | enum `NotificationStatus` | no | `pending` | `pending`, `sent`, `failed`, `skipped` |
| `sent_at` | UTC datetime | yes | — | set only on `sent` |
| `error` | `Text` | yes | — | scrubbed, ≤ 2,000 chars + `… [truncated]`; cleared on `sent` |
| `payload` | JSON | yes | — | `NotificationPayload` v1 (below) |
| `created_at` | UTC datetime | no | now | unchanged |
| **`attempts`** | `Integer` | no | `0` (server default) | **new**: number of send attempts started |
| **`last_attempt_at`** | UTC datetime | yes | — | **new**: start time of the latest attempt |

**Migration `0003`**:

- `add_column` × 2 with `server_default="0"` for `attempts`, so existing rows get 0.
- Downgrade drops both columns.
- No index is needed: retry scans by `status` over a small table. The existing primary key
  keeps the ordering.

### State transitions

```text
              add()                begin_attempt()/send ok
  (none) ──────────────► pending ───────────────────────► sent        (terminal)
                           │  send error, attempts < 5
                           ├───────────────────────────► failed
                           │  send error, attempts ≥ 5
                           └───────────────────────────► skipped     (terminal)

  failed ──claim_for_retry()──► pending ──► sent | failed | skipped   (same rules)
  pending (COALESCE(last_attempt_at, created_at) < now−1h)
         ──claim_for_retry()──► pending ──► …                         (crash recovery)
         └─ if attempts ≥ 5 before claim: ──► skipped ("interrupted: attempt limit reached")
```

**Rules**:

- `attempts` is incremented exactly once per started send, *before* the send, and committed.
- The limit is `MAX_ATTEMPTS = 5`. Reaching it with a failure yields `skipped`, and the last
  error is kept.
- A row whose digest is gone counts the attempt and fails with `digest no longer available`, so
  it reaches `skipped` after 5 retries.
- `skipped` and `sent` are never selected for retry.

## `NotificationPayload` (new, `invio.notify.payload`)

A strict Pydantic model (`extra="forbid"`, frozen). It is serialized with `model_dump(mode="json")`
into `notifications.payload` and validated on read. An invalid payload makes the notification fail
with `invalid notification payload: <reason>`; it is never sent with guessed data.

| Field | Type | Rules |
|-------|------|-------|
| `schema_version` | `Literal[1]` | required |
| `job_name` | `str` | min length 1; job name at first delivery |
| `subject` | `str` | min length 1; fully rendered subject (placeholders replaced, no line breaks) |
| `digest_date` | `date` | ISO `YYYY-MM-DD`, in job schedule time zone |
| `is_empty` | `bool` | digest had zero items (sent only because `send_if_empty=true`) |
| `stats` | `DigestStats` | see below |

### `DigestStats`

| Field | Type | Rules |
|-------|------|-------|
| `items_found` | `int \| None` | ≥ 0; from `runs.stats["items_found"]` when present |
| `items_included` | `int` | ≥ 0; `len(digest.item_ids)` |
| `duration_seconds` | `float \| None` | ≥ 0; `None` when the run row is missing |

## In-memory types (`invio.notify`)

- **`DeliveryOutcome`** (frozen dataclass): `sent: int`, `failed: int` (ended `failed` or
  `skipped`), `skipped_empty: bool`, `notification_ids: tuple[int, ...]`,
  `error: str | None = None`. `error` is set (scrubbed) only when delivery failed before any
  row could be created, i.e. the recipients were never determined.
- **`RetryOutcome`** (frozen dataclass): `retried: int`, `sent: int`, `failed: int`,
  `given_up: int`, plus `results: tuple[RetryResult, ...]`, where each `RetryResult` holds
  `notification_id`, `job_name`, `recipient`, `status` and `error`.
- **`RenderedMail`** (frozen dataclass): `subject: str`, `text: str`, `html: str`. Built once
  per digest and reused for every recipient. Only the `To` header differs per message.

## Settings (changed, `invio.config.settings.Settings`)

| Setting | Env var | Type | Default | Rules |
|---------|---------|------|---------|-------|
| `smtp_security` | `INVIO_SMTP_SECURITY` | `Literal["starttls","ssl","none"]` | `"starttls"` | any other value → validation error naming the field and allowed values |
| `smtp_timeout_seconds` | `INVIO_SMTP_TIMEOUT_SECONDS` | `float` | `30.0` | `> 0`, finite |
| ~~`smtp_starttls`~~ | ~~`INVIO_SMTP_STARTTLS`~~ | — | — | **removed**; if still set it is reported by the existing "unknown setting ignored" warning |

Unchanged: `smtp_host`, `smtp_port` (587), `smtp_user`, `smtp_password` (`SecretStr`),
`smtp_from`.

## Read-only inputs

- **`Digest`**: `id`, `job_id`, `run_id`, `title`, `body` (Markdown), `item_ids`, `created_at`.
- **`Run`**: `started_at`, `stats`.
- **`Job`**: `name`, `config` → `JobConfig`, whose `.notification` gives `to`, `subject` and
  `send_if_empty`, and whose `.schedule.timezone` gives the time zone.
