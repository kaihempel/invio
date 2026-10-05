# Data Model: Per-Job Deduplication, Baseline Mode and Run Limits

No schema change. Existing tables from #4 are used as-is; new types are in-memory records.

## Existing stored entities (used fields)

### `items` (`invio.db.models.Item`)

| Field | Use in this feature |
|-------|---------------------|
| `job_id`, `url_hash` | identity; unique per job (FR-002) |
| `content_hash` | change detection for web pages (FR-004); `None` for feed entries; a stored `None` is backfilled from a reported fingerprint |
| `status` (`ItemStatus`) | `new`, `failed`, `skipped_baseline` (new, migration 0003), … (classification input/output) |
| `attempts` (≥ 0) | retry limit (`MAX_ATTEMPTS = 3`); `+1` when selected (FR-013) |
| `last_error` | cleared on reset; no longer used for baseline skips |
| `run_id` | set to the current run when selected |
| `published_at` | "newest" ordering (aware UTC, nullable) |

### `runs` (`invio.db.models.Run`)

| Field | Use |
|-------|-----|
| `job_id`, `status` | "successful run" = any run with status `succeeded` or `partial` |

## Configuration

### `LimitsConfig` (`invio.config.job`) — addition

| Field | Type | Default | Rule |
|-------|------|---------|------|
| `baseline_items` | `StrictInt` | `10` | `ge=1`; per source, first runs only |
| `max_items_per_run` | `StrictInt` | `100` | existing; cap after baseline |

## In-memory records (`invio.graph.nodes.deduplicate`)

### Input: `candidates: Mapping[str, Sequence[Candidate]]`

Source key → that source's candidates in source order. Mapping order = source order.

### `SelectedItem` (frozen, kw-only)

`id: int`, `url: str`, `url_hash: str`, `type: ItemType`, `title: str`,
`published_at: datetime | None`, `teaser: str | None`, `content_hash: str | None`,
`attempts: int` (after increment), `reason: SelectReason`
where `SelectReason = Literal["new", "changed", "retry", "waiting"]`.

Values are always taken from the stored item after persistence. Changed items already carry
the candidate's values (via `reset_versions`); retry/waiting items keep their stored
title/teaser.

### `DedupStats` (frozen, kw-only; all `int` ≥ 0)

`found` (unique reported candidates), `new`, `changed`, `retried`, `retried` and `waiting` (eligible
retry/waiting items, reported or from storage), `dropped`, `baseline_skipped`, `limit_cut`,
`selected`; plus `baseline: bool` (baseline mode active).

Invariant: `new + changed + retried + waiting + dropped = found + stored_only`, where
`stored_only` is the number of stored pending items no source reported (they are part of
`waiting`/`retried` but not of `found`);
`selected + limit_cut + baseline_skipped = new + changed + retried + waiting`.

### `DedupResult` (frozen, kw-only)

`items: list[SelectedItem]` (newest first), `stats: DedupStats`.

## Classification (per unique reported candidate)

| Stored item | Condition | Class |
|-------------|-----------|-------|
| none | — | `new` |
| exists | both `content_hash` set and different | `changed` |
| exists | `run_id` is the current run | `drop` (already taken by this run: repeated calls are idempotent) |
| exists | `status = new`, `attempts = 0` | `waiting` |
| exists | `attempts < 3` and (`status = failed` or (`status = new` and `attempts ≥ 1`)) | `retry` |
| exists | anything else | `drop` |

Rows are checked top to bottom; the first match wins. Stored items no source reported are
classified by the last four rows (`ItemRepository.list_pending` / `count_pending` mirror them
in SQL, newest first, limited to `max_items_per_run`).

Baseline mode ranks only `new` entries per source; changed, retry and waiting items are
never rejected by it.

## Item state transitions caused by this step

```text
(unknown) ──selected────────────────▶ new, attempts=1, run_id=run
(unknown) ──cut by run limit────────▶ new, attempts=0, run_id=NULL        (waiting)
(unknown) ──baseline reject─────────▶ skipped_baseline, attempts=0
(known)   ──changed version─────────▶ new, attempts=0, last_error=NULL, content_hash=new
                                       then selected (attempts=1, run_id=run) or left waiting
waiting   ──selected────────────────▶ new, attempts=1, run_id=run
failed / interrupted (attempts<3) ──selected──▶ status unchanged, attempts+1, run_id=run
dropped   ──────────────────────────▶ unchanged
```

An item taken by an earlier run (different `run_id`) that downstream nodes never finished
counts as interrupted and is retried.

Later status changes (`extracted`, `summarized`, `failed`, …) belong to downstream nodes.
A retried `failed` item keeps status `failed` while in the run; the downstream outcome
overwrites it.
