# Data Model: Job Management Service and Record Access Layer

**Feature**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md)

No schema change: the tables from #4 (`jobs`, `runs`, `items`, `digests`, `notifications`,
`llm_usage`, see [../002-gh-issue-4/data-model.md](../002-gh-issue-4/data-model.md)) are used
as they are. This feature adds in-memory records, errors and state rules on top.

## JobRecord (service output, `invio.services.jobs`)

Frozen, slotted dataclass; storage-independent (FR-005). Deliberately has no database id: the
name is the public identity; history writers resolve the id via `JobRepository.get_by_name`
inside their own unit of work (R4).

| Field | Type | Source / rule |
|---|---|---|
| `name` | `str` | `jobs.name`; 1–200 chars, no leading/trailing whitespace (FR-014) |
| `enabled` | `bool` | `jobs.enabled` |
| `config` | `JobConfig` | `jobs.config` re-validated with `validate_job` (FR-007a) |
| `next_run_at` | `datetime \| None` | aware UTC; `None` while disabled |
| `created_at` | `datetime` | aware UTC |
| `updated_at` | `datetime` | aware UTC |

## Stored job row (`jobs`, existing)

| Column | Written by this feature |
|---|---|
| `name` | create/import; never changed by update (FR-003a) |
| `enabled` | `True` on create/import; `set_enabled` |
| `config` | `JobConfig.model_dump(mode="json")` on create/update/import (FR-003) |
| `next_run_at` | see state rules |
| `locked_until` | untouched (out of scope) |
| `created_at` / `updated_at` | model defaults / `onupdate` |

## Job lifecycle and `next_run_at`

```text
               create / import
   (none) ─────────────────────────▶ ENABLED  (next_run_at = next_run(schedule, now))
                                       │  ▲
              set_enabled(False)       │  │ set_enabled(True)
              next_run_at = None       ▼  │ next_run_at = next_run(schedule, now)
                                     DISABLED
   ENABLED  ── update ──▶ ENABLED   (config replaced, next_run_at recalculated)
   DISABLED ── update ──▶ DISABLED  (config replaced, next_run_at stays None)
   ENABLED/DISABLED ── delete ──▶ (none)   (history removed by FK cascade)
   set_enabled to current state ──▶ no-op (no write, no log)
```

`next_run(schedule, after)` is the injectable calculation; until #6 lands it returns `after`.

## Errors (`invio.services.jobs`)

| Error | Base | When | Message |
|---|---|---|---|
| `JobExistsError` | `Exception` | create/import (no replace) with an existing name, incl. race | `job 'x' already exists` |
| `JobNotFoundError` | `LookupError` | get/update/set_enabled/delete/export of unknown name | `job 'x' not found` |
| `JobNameError` | `ValueError` | invalid name (FR-014) | names the rule broken |
| `StoredJobConfigError` | `JobConfigError` | stored config fails validation on read | `invalid stored job 'x':` + error lines |
| `JobConfigError` (existing, #3) | `Exception` | invalid config on write or invalid file on import | field-level lines |

Each error exposes `name` (where applicable) as an attribute.

## Repository row operations (`invio.db.repositories`)

| Repository | Table | Notable rules |
|---|---|---|
| `JobRepository` | `jobs` | `list(enabled_only)` ordered by name |
| `RunRepository` | `runs` | `start` → status `running`; `finish` sets status, `finished_at`, stats, error; `list_for_job` newest first (`started_at DESC, id DESC`) |
| `ItemRepository` | `items` | `add` from a `Candidate` is idempotent per `(job_id, url_hash)` → `(item, created)`; `list_for_job(status=…)` ordered by id |
| `DigestRepository` | `digests` | `list_for_job` newest first |
| `NotificationRepository` | `notifications` | `mark` sets status, `sent_at` (for `sent`), error |
| `UsageRepository` | `llm_usage` | `totals_for_run` / `totals_for_job` → `UsageTotals(input_tokens, output_tokens, cost_usd)`; zero totals when empty, `cost_usd` is the sum of known costs |

`UsageTotals` is a frozen dataclass in `invio.db.repositories` (stdlib types only:
`int`, `Decimal`).

## Unit of work

`session_scope(factory)` → one `Session`; commit on success, rollback on exception, always
closed. Every `JobService` method opens exactly one scope (FR-019).
