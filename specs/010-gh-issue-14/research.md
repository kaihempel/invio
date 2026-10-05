# Research: Per-Job Deduplication, Baseline Mode and Run Limits

All Technical Context items were known from the codebase; no NEEDS CLARIFICATION remained
after `/speckit-clarify`. The decisions below fix the open design points.

## R1 — Shape of the node: plain function, no LangGraph yet

- **Decision**: `deduplicate(session, *, job_id, run_id, candidates, limits) -> DedupResult`,
  synchronous, in `src/invio/graph/nodes/deduplicate.py`.
- **Rationale**: LangGraph is not a dependency yet; #21 builds `RunState` and the
  `StateGraph`. A plain function over a `Session` is testable now and trivially wrapped by a
  node adapter later (`state → deduplicate(...) → {"items": ...}`). Database access is
  synchronous everywhere in invio.
- **Alternatives**: Add LangGraph and a `RunState` now — rejected, duplicates #21 and adds a
  dependency the issue doesn't need. Async function — rejected, the DB layer is sync.

## R2 — Source identity for per-source baseline

- **Decision**: Input is `Mapping[str, Sequence[Candidate]]` keyed by a caller-chosen source
  key (insertion order = source order). `Candidate` stays unchanged.
- **Rationale**: `fetch_sources` fetches source by source, so grouping is free there.
  Changing the shared `Candidate` contract would touch every adapter (`rss.py`, `web.py`) and
  their tests for a field only this node needs.
- **Alternatives**: `Candidate.source: str | None` — rejected (shared contract churn); list of
  `(source, candidate)` tuples — equivalent but less readable.

## R3 — Lookup strategy

- **Decision**: `ItemRepository.find_many(job_id, url_hashes) -> dict[str, Item]`, one
  `SELECT … WHERE job_id = ? AND url_hash IN (…)` per chunk of 500 hashes.
- **Rationale**: Avoids N+1 queries; chunking keeps MariaDB/SQLite parameter counts safe. The
  unique `(job_id, url_hash)` index serves the query.
- **Alternatives**: `seen()`/`_find()` per candidate — rejected (N+1).

## R4 — Classification rules and attempt limit

- **Decision**: Module constant `MAX_ATTEMPTS: Final = 3`. For a known item (first match wins):
  1. both content hashes present and different → **changed** (eligible; reset)
  2. status `new` and `attempts == 0` → **waiting** (eligible)
  3. `attempts < MAX_ATTEMPTS` and (status `failed` or (status `new` and `attempts ≥ 1`)) →
     **retry** (eligible)
  4. otherwise → **drop**
  Unknown → **new** (eligible). Duplicates within the input: first occurrence (source order,
  then position) wins, later ones are ignored and not counted as dropped.
- **Rationale**: Mirrors FR-003…FR-006 and clarifications Q1/Q3. A content change is a fresh
  start, so it is checked before the attempt limit (spec edge case).
- **Alternatives**: Configurable `max_attempts` — rejected, spec fixes 3 (FR-007).

## R5 — "Successful run" lookup

- **Decision**: `RunRepository.has_successful_run(job_id) -> bool` —
  `EXISTS(runs WHERE job_id = ? AND status IN (succeeded, partial))`.
- **Rationale**: Clarification Q2. The current run is `running`, so it never counts.

## R6 — Ordering ("newest")

- **Decision**: Sort key `(published_at is None, -published_at, origin, source_index,
  position_or_id)`: dated before undated, newer first; ties: reported candidates before
  stored pending items, then source order and position; stored pending items by item id
  (the same order `list_pending` uses in SQL).
- **Rationale**: FR-014 and the tie-break edge case; fully deterministic (FR-015).
  `published_at` is aware UTC on both sides (`UTCDateTime`), so values compare directly.

## R7 — Order of the pipeline inside the node

- **Decision** (revised after PR review #53):
  1. flatten + in-input dedupe → 2. batch lookup (`find_many`) → 3. classify; reset changed
  versions and backfill missing fingerprints (one flush each) → 4. bulk-insert unknown
  candidates (`add_new`); rows another writer inserted meanwhile are dropped *before*
  baseline, so they never take a baseline slot → 5. baseline (only if no successful run; per
  source, over that source's **new** entries only; rejects get status `skipped_baseline`) →
  6. load the newest `max_items_per_run` stored pending items (waiting and retryable) that no
  source reported, plus their total counts (`list_pending` / `count_pending`; not subject to
  baseline) → 7. sort, take `max_items_per_run`; selected → `run_id`, `attempts + 1` →
  8. log + return.
- **Rationale**: Matches spec order (dedupe → baseline → run limit) and Q1 (cut new/changed
  items become waiting work, FR-012). Baseline only limits new items so known work is never
  discarded while a job has no successful run. Loading pending items in SQL with a limit keeps
  a large backlog from being loaded on every run.
- **Cut items that are known**: a cut retry/changed item keeps its stored state except that a
  changed item is still reset (new hash stored, status `new`, attempts 0) so it becomes
  waiting work; a cut retry item stays `failed`/`new` with its attempts and is retried by a
  later run from storage, whether or not a source reports it again.

## R8 — Concurrent runs of the same job

- **Decision**: Rely on `ItemRepository.add` (savepoint + unique constraint). If `add`
  returns `created=False` for a candidate classified as new, the item was inserted
  concurrently: count it as dropped and do not select it.
- **Rationale**: The scheduler lock (#23) normally prevents parallel runs; this keeps the node
  correct anyway without extra locking.

## R9 — Return type

- **Decision**: Frozen kw-only dataclasses `SelectedItem`, `DedupStats`, `DedupResult` defined
  in the node module; `SelectedItem` carries `id`, `url`, `url_hash`, `type`, `title`,
  `published_at`, `teaser`, `content_hash`, `attempts`, `reason`.
- **Rationale**: ORM instances must not leak beyond the unit of work into LangGraph state;
  downstream nodes (#15 keyword filter, #13 extraction) need title, teaser and URL.
- **Alternatives**: Return `list[Item]` — rejected (detached-instance risk); put records into
  `invio.domain` — rejected for now (only graph nodes use them; #21 may promote them).

## R10 — Logging

- **Decision**: `logger.info("deduplicated", extra={"event": "deduplicate", "found": …, "new":
  …, "changed": …, "retried": …, "waiting": …, "dropped": …, "baseline_skipped": …,
  "limit_cut": …, "selected": …, "baseline": bool})`. No URLs or titles.
- **Rationale**: FR-016 and principle V; `job`/`run_id` are added by `run_context`.

## R11 — Config surface of `baseline_items`

- **Decision**: `LimitsConfig.baseline_items: StrictInt = Field(default=10, ge=1)`, placed
  after `max_items_per_run`; regenerate `docs/job.schema.json`, add to
  `docs/job.example.yaml` and the wizard's `_LIMIT_QUESTIONS`.
- **Rationale**: Per-job like the other caps; optional with default keeps existing job files
  and `schema_version` valid. Wizard bounds derive from the model via `_LIMITS`.
