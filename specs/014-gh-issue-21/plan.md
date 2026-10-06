# Implementation Plan: Assembled Research Workflow with Parallel Item Processing and Retries

**Branch**: `gh-issue-21` | **Date**: 2026-10-06 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/014-gh-issue-21/spec.md`

## Summary

This plan wires the existing stages (#11–#20) into one LangGraph `StateGraph` per job run:

`load_job → fetch_sources → deduplicate → keyword_prefilter → Send × N process_item → join → synthesize_digest → persist → notify → finalize`

`process_item` is a subgraph:

`extract_text | video_path (pass-through until #29) → score_relevance → (relevant?) summarize_item`

The subgraph is bounded by a per-run semaphore (default 4, `INVIO_MAX_PARALLEL_ITEMS`).

**Retries.** Transient failures (rate limit, provider unavailable, fetch errors) are retried
with exponential backoff **around each external call**: source fetch, item page fetch and
provider request. They are not retried by re-running a node, because the existing nodes catch
these errors themselves and re-running a node would repeat side effects (research R4).

**Error boundaries.** Each item has its own error boundary, so a bad item becomes `failed` and
the run continues. Credential and configuration errors are run-fatal. Every stage is guarded:
an exception routes to `finalize`, and a `try/finally` in `run_job` covers cancellation.

**finalize** closes the run through #19's `finalize_run`/`record_failed_run` and applies the
synthesis and delivery status rules. It then sets `next_run_at` and releases the job lock in one
atomic, ownership-checked `UPDATE`.

**Lock.** `run_job` claims the lock itself, with #23's SQL, and refuses with `JobBusyError`.

**Dry run.** A dry run rolls back the whole work session and keeps only a run record with
`stats.dry_run = true`. It sends no mail and leaves `next_run_at` alone.

**Status rule.** The rule gains source outcomes: some sources failed → `partial`; all sources
failed → `failed`.

**Layering.** The composition root is a new `invio.pipeline` package, because `invio.graph`
must not import `notify` or `scheduling`.

**No migration.** The new runtime dependency is `langgraph`.

## Technical Context

**Language/Version**: Python 3.12+

**Primary Dependencies**:
- **new**: `langgraph` (>= 1.0, < 2) for `StateGraph`, `Send` and conditional edges, with no
  checkpointer (R1);
- existing: SQLAlchemy 2.x, Pydantic v2, `invio.llm`, `invio.sources` (`SafeHttpClient`,
  `RssFeedSource`, `WebPageSource`, `extract_text`), `invio.notify.email.deliver_digest`, and
  `invio.scheduling.next_run.compute_next_run`.

**Storage**: the existing `jobs` (`locked_until`, `next_run_at`), `runs`, `items`, `digests`,
`llm_usage` and `notifications` tables. **No schema change**. The new `stats` keys are additive
under version 1.

**Testing**: pytest + pytest-asyncio, with SQLite in memory (`db` fixtures) and the optional
MariaDB run for the concurrent-claim test. A purpose-routed `FakeProvider` helper, fake
fetch/notify ports, the fixture feeds and articles, a fixed clock and a no-wait sleep. No
network.

**Target Platform**: Linux server, unattended cron/systemd (via #23) and CLI (via #22).

**Project Type**: CLI tool / pipeline library (`src/invio`).

**Performance Goals**: items are processed in parallel, up to the limit (default 4). The
overhead per run is a graph compile of about a dozen nodes (milliseconds). The retry waits are
bounded (≤ 3 attempts, ≤ 30 s each).

**Constraints**:
- SQLite has a single writer, so there is one work session per run and the lock, run-start and
  finalize writes run in short separate transactions (#19 R1).
- No secrets or document text in errors or logs.
- `invio.graph` must not import `notify`, `scheduling` or `cli`.

**Scale/Scope**: ≤ `max_items_per_run` (default 100) items per run, a few hundred LLM calls,
and a few sources per job.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Check | Status |
|---|---|---|
| I. Strict contracts | Stored job config re-validated into `JobConfig` in `load_job`; state uses typed dataclasses/`TypedDict`; new settings validated (`ge=1`, `ge=60`); `RunDeps`/`RetrySettings` reject invalid values; stats layout documented (contracts/run-stats-delta.md) | PASS |
| II. CLI-first | `run_job` is the library entry point; its CLI command `invio job run [--dry-run]` is scoped to #22 (depends on #21), `run-due` to #23. No stdout output here | DEVIATION (recorded in Complexity Tracking) |
| III. Test-covered | Every acceptance criterion and scenario maps to quickstart scenarios 1–31, incl. rejection paths (busy lock, invalid config, non-retryable errors); FakeProvider + fixtures, no network, deterministic (injected clock/sleep, purpose-routed fake under fan-out) | PASS |
| IV. Quality gates | Strict mypy: no untyped `configurable` dict (R1), deps typed via `RunDeps`; `Any` only for `runs.stats` (existing justification); `uv.lock` updated, CI `--locked` | PASS |
| V. Secrets / observability | `RunError` keeps error class only; `runs.error` via #19's sanitizer; source keys instead of URLs; whole run inside `run_context(job, run_id)` (FR-014); new structured events listed in contracts/run-job.md | PASS |
| Layering | `cli → pipeline → graph → (db, llm, sources)`; `pipeline` also imports `notify`, `scheduling`; `graph` gets them as ports; `invio.retry` is a dependency-free leaf; new AST tests | PASS |
| New dependency | `langgraph`: named by issue #21 and README; justification goes in the PR description | PASS (justified) |
| Simplicity | No checkpointer, no migration, no heartbeat; retry helper ≈ 30 lines instead of `tenacity`; unsupported source types skipped rather than stubbed | PASS |
| Docs | README "Running a job" section (run_job, dry run, status rules, settings) in the same PR | PASS (planned) |

**Post-design re-check (after Phase 1)**: PASS, with one recorded constitution deviation. The
design adds no table and no upward import. Principle II (CLI reachability) is deferred to #22
and recorded in Complexity Tracking. The only deviation from the issue text, retries at the
call boundary instead of node-level `RetryPolicy`, is justified in research R4 and keeps the
issue's policy parameters and error kinds.

## Project Structure

### Documentation (this feature)

```text
specs/014-gh-issue-21/
├── spec.md
├── plan.md              # this file
├── research.md          # R1–R13
├── data-model.md
├── quickstart.md        # validation scenarios 1–31
├── contracts/
│   ├── run-job.md       # public entry point, status rule, retry policy, settings, logging
│   ├── graph.md         # topology, node contracts, reducers, concurrency guarantee
│   └── run-stats-delta.md  # new stats keys, repository additions, notify fix
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks (not created here)
```

### Source Code (repository root)

```text
src/invio/
├── retry.py                      # NEW  RetrySettings, retrying(), is_transient_llm/fetch predicates
├── config/settings.py            # +max_parallel_items, +run_lock_seconds
├── db/repositories.py            # +JobRepository.get/claim/release, +ItemRepository.set_extracted
├── sources/http.py               # SafeHttpClient.get(..., conditional=True): False sends no validators (item pages, R6)
├── llm/retry.py                  # NEW  RetryingProvider (LLMProvider decorator)
├── graph/
│   ├── state.py                  # NEW  RunState, ItemState, ItemTask, ItemResult, RunError
│   ├── ports.py                  # NEW  RunDeps, SourceFetcher, PageFetcher, Notifier, ProviderBinding
│   ├── build.py                  # NEW  RunScope, build_graph(deps, *, job_id, run_id, token, dry_run) -> (graph, scope), guarded(), routing, process_item subgraph
│   ├── stages.py                 # NEW  stage node functions (load_job … finalize) over RunDeps
│   └── nodes/
│       ├── extract.py            # NEW  extract_text node (page fetch + sources.extract), video_path
│       └── persist.py            # RunResult→RunDraft, StageCounts.sources*, dry_run, store_empty_digest, ALL_SOURCES_FAILED_ERROR, ValidationError field paths in _sanitized_error
├── pipeline/
│   ├── __init__.py               # NEW
│   ├── deps.py                   # NEW  default_deps(settings): real adapters, lifecycle
│   └── run.py                    # NEW  run_job (claim, run start, run_context around ainvoke), RunResult, JobBusyError, JobDisabledError, safety net
└── notify/email.py               # _items_found reads "found" (integration fix)

tests/
├── pipeline_helpers.py           # NEW  purpose-routed fake provider, fake ports, job factory, snapshots
├── test_retry.py                 # NEW  backoff, jitter bound, retry_after, predicates
├── test_llm_retry.py             # NEW  RetryingProvider
├── test_db_job_lock.py           # NEW  claim/release incl. MariaDB concurrency (-m db)
├── test_graph_state.py           # NEW  reducers, RunError hygiene
├── test_graph_build.py           # NEW  topology, routing on fatal, subgraph routing
├── test_graph_extract.py         # NEW  extract/video path, too_short/too_large
├── test_pipeline_run.py          # NEW  scenarios 1–4, 19–20, 27–28
├── test_pipeline_failures.py     # NEW  scenarios 5–7, 13–16, 21–22, 26
├── test_pipeline_retries.py      # NEW  scenarios 8–11
├── test_pipeline_concurrency.py  # NEW  scenarios 12, 17–18
├── test_pipeline_sources.py      # NEW  scenarios 23–25
├── test_pipeline_layering.py     # NEW  scenario 29
├── test_persist.py               # rename RunResult→RunDraft; source status rule; dry_run stats
└── test_notify_email.py          # "found" key
```

**Structure Decision**: This is a single project. The new orchestration package
`src/invio/pipeline/` is the composition root above `graph`, as the constitution's layering
requires (research R2). `graph/` holds the LangGraph state, ports, builder and stage functions,
next to the existing nodes. The cross-cutting retry helper is a leaf module at
`src/invio/retry.py`, so that `llm`, `sources`-facing code and `graph` can share it.

## Complexity Tracking

| Deviation | Why needed | Simpler alternative rejected because |
|---|---|---|
| New runtime dependency `langgraph` | Required by issue #21, README and #29 integration point | Plain asyncio contradicts the issue; justified in PR description |
| New package `invio.pipeline` | `graph` may not import `notify`/`scheduling` (existing test) | Putting `run_job` in `graph/build.py` breaks layering; relaxing the test removes a deliberate rule |
| Call-level retries instead of node-level `RetryPolicy` | Nodes catch transient errors themselves; node re-runs duplicate side effects | Node-level policy would never fire (R4) |
| Principle II: `run_job` and dry run not yet reachable through the CLI | The issue split puts `invio job run [--dry-run]` in #22 and `invio run-due` in #23, both depending on #21; the library entry point must exist first | A thin `invio job run` here would duplicate #22's command, progress output and exit-code contract, and would be rewritten there. Temporary: the deviation ends when #22 merges |
