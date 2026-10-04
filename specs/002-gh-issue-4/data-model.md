# Data Model: Persistent Research Data Store

**Feature**: [spec.md](./spec.md) · **Research**: [research.md](./research.md)

All tables: InnoDB, `utf8mb4` / `utf8mb4_unicode_ci` on MariaDB/MySQL (R3). Timestamps are
`UTCDateTime` (aware UTC in Python, naive UTC in the DB; naive input rejected — R6). Primary
keys `id` are BigInteger (Integer on SQLite) autoincrement (R4). Constraint names follow the
naming convention in R8 (`ix_<table>_<col1>_<col2>`, `uq_<table>_<cols>`, `ck_<table>_<name>`).

## Relationships

```text
jobs ─┬─< runs ─────────────┐ (SET NULL)
      │                     ├──> items.run_id, digests.run_id,
      │                     │    notifications.run_id, llm_usage.run_id
      ├─< items             │
      ├─< digests ──────────┼──> notifications.digest_id (SET NULL)
      ├─< notifications     │
      └─< llm_usage         │
(every child.job_id → jobs.id ON DELETE CASCADE, NOT NULL)
```

## Enumerations (`src/invio/domain.py`, `StrEnum`, stdlib only)

| Enum | Values | Default |
|---|---|---|
| `ItemStatus` | `new`, `extracted`, `skipped_keyword`, `skipped_irrelevant`, `relevant`, `summarized`, `failed` | `new` |
| `RunStatus` | `running`, `succeeded`, `partial`, `failed` | `running` |
| `NotificationStatus` | `pending`, `sent`, `failed`, `skipped` | `pending` |
| `ItemType` (existing `Literal`) | `article`, `video` | — |

DB representation: native `ENUM` on MariaDB, `VARCHAR` + named `CHECK` on SQLite (R5).
No transition rules are enforced in the DB in this issue; the pipeline owns transitions.
Intended item lifecycle (informational, for later tracks):
`new → extracted → (skipped_keyword | skipped_irrelevant | relevant) → summarized`, any step
→ `failed` (with `attempts` incremented and `last_error` set).

## `jobs`

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BigInteger PK | no | auto | |
| `name` | `String(200)` | no | — | `uq_jobs_name`; matches job file name (FR-007a) |
| `enabled` | `Boolean` | no | `true` | |
| `config` | `JSON` | yes | — | snapshot of last-loaded `JobConfig` (audit only, never authoritative) |
| `next_run_at` | `UTCDateTime` | yes | — | |
| `locked_until` | `UTCDateTime` | yes | — | scheduler lock |
| `created_at` | `UTCDateTime` | no | now | |
| `updated_at` | `UTCDateTime` | no | now | `onupdate=now` |

Indexes: `ix_jobs_enabled_next_run_at (enabled, next_run_at)` (FR-007).

## `runs`

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | PK | no | auto | |
| `job_id` | FK → jobs CASCADE | no | — | |
| `status` | `Enum(RunStatus)` | no | `running` | |
| `started_at` | `UTCDateTime` | no | now | |
| `finished_at` | `UTCDateTime` | yes | — | |
| `stats` | `JSON` | yes | — | counts per stage etc. |
| `error` | `Text` | yes | — | |

Indexes: `ix_runs_job_id_started_at (job_id, started_at)`.

## `items`

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | PK | no | auto | |
| `job_id` | FK → jobs CASCADE | no | — | |
| `run_id` | FK → runs SET NULL | yes | — | run that discovered it |
| `url` | `Text` | no | — | full URL, untruncated (FR-005) |
| `url_hash` | `CHAR(64)` | no | — | SHA-256 hex of `url` as stored, UTF-8 (`domain.url_hash`, FR-004) |
| `type` | `Enum(ItemType)` | no | — | |
| `title` | `String(1000)` | no | — | utf8mb4 |
| `published_at` | `UTCDateTime` | yes | — | |
| `teaser` | `Text` | yes | — | |
| `content_hash` | `CHAR(64)` | yes | — | |
| `raw_content` | `Text` / `MEDIUMTEXT` | yes | — | up to 16 MB (UTF-8 bytes) on MariaDB; needs `max_allowed_packet` ≥ 32M |
| `summary` | `Text` | yes | — | utf8mb4 |
| `status` | `Enum(ItemStatus)` | no | `new` | server default too |
| `attempts` | `Integer` | no | `0` | `ck_items_attempts_non_negative` (`>= 0`) |
| `last_error` | `Text` | yes | — | |
| `relevance` | `Numeric(3,2)` | yes | — | `ck_items_relevance_range` (`0 ≤ x ≤ 1`) |
| `created_at` / `updated_at` | `UTCDateTime` | no | now | |

Constraints/indexes: `uq_items_job_id_url_hash (job_id, url_hash)` (also serves the `job_id`
FK); `ix_items_run_id`.

Field mapping to `domain.Candidate`: `url`, `url_hash`, `title`, `published_at`, `type`,
`teaser`, `content_hash`. To `domain.ProcessedItem`: `id`, `url`, `type`, `title`,
`raw_content`, `relevance` (float ↔ Decimal), `summary`, `error ↔ last_error`. Conversion
helpers are out of scope (later repository issue).

## `digests`

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | PK | no | auto | |
| `job_id` | FK → jobs CASCADE | no | — | indexed `ix_digests_job_id` |
| `run_id` | FK → runs SET NULL | yes | — | `ix_digests_run_id` |
| `title` | `String(500)` | no | — | |
| `body` | `Text` / `MEDIUMTEXT` | no | — | rendered digest |
| `item_ids` | `JSON` | no | `[]` | ids of included items |
| `created_at` | `UTCDateTime` | no | now | |

## `notifications`

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | PK | no | auto | |
| `job_id` | FK → jobs CASCADE | no | — | `ix_notifications_job_id` |
| `run_id` | FK → runs SET NULL | yes | — | `ix_notifications_run_id` |
| `digest_id` | FK → digests SET NULL | yes | — | `ix_notifications_digest_id` |
| `channel` | `String(32)` | no | — | e.g. `email` |
| `recipient` | `String(320)` | no | — | max e-mail length |
| `status` | `Enum(NotificationStatus)` | no | `pending` | |
| `sent_at` | `UTCDateTime` | yes | — | |
| `error` | `Text` | yes | — | |
| `payload` | `JSON` | yes | — | e.g. subject, message id |
| `created_at` | `UTCDateTime` | no | now | |

## `llm_usage`

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | PK | no | auto | |
| `job_id` | FK → jobs CASCADE | no | — | `ix_llm_usage_job_id` |
| `run_id` | FK → runs SET NULL | yes | — | `ix_llm_usage_run_id` |
| `provider` | `String(32)` | no | — | `mistral`/`openai`/… (free string; provider list lives in config) |
| `model` | `String(200)` | no | — | |
| `purpose` | `String(64)` | yes | — | e.g. `relevance`, `summary` |
| `input_tokens` | `Integer` | no | `0` | `ck_llm_usage_input_tokens_non_negative` |
| `output_tokens` | `Integer` | no | `0` | `ck_llm_usage_output_tokens_non_negative` |
| `cost_usd` | `Numeric(12,6)` | yes | — | estimated |
| `created_at` | `UTCDateTime` | no | now | |

## Validation rules → requirements

| Rule | Enforced by | Requirement |
|---|---|---|
| One item per (job, url_hash) | unique constraint | FR-004, SC-003 |
| Status vocabularies | ENUM / CHECK + strict mode | FR-006, FR-006a |
| `0 ≤ relevance ≤ 1`, 2 decimals | `Numeric(3,2)` + CHECK | FR-006 |
| `attempts`, tokens ≥ 0 | CHECK | FR-006 |
| Unique job name | unique constraint | FR-007a |
| Job deletion removes children | FK `ON DELETE CASCADE` | FR-008, SC-004 |
| Run/digest deletion keeps children | FK `ON DELETE SET NULL` | FR-008a |
| UTC instants | `UTCDateTime` | FR-016 |
| Full Unicode | utf8mb4 table + connection charset | FR-003, SC-005 |
