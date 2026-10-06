# Contract: Token Budget and Run Persistence

Internal Python API consumed by the pipeline graph (#21), the digest node (#18) and tests.

## `invio.graph.budget`

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class UsageEntry:
    provider: str
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None
    created_at: datetime

class BudgetExceeded(Exception):
    used: int
    limit: int

class BudgetTracker:
    def __init__(self, limit: int) -> None          # ValueError if limit < 1
    limit: int
    used: int                                       # input + output tokens recorded so far
    exceeded: bool                                  # latched by the first failing check()
    ledger: tuple[UsageEntry, ...]
    input_tokens: int
    output_tokens: int
    calls: int
    cost_usd: Decimal                               # sum of known costs, 6 places
    cost_complete: bool                             # False if any entry has cost_usd None
    def check(self) -> None                         # raises BudgetExceeded if used > limit
    def record(self, entry: UsageEntry) -> None     # never raises
```

## `invio.graph.nodes.llm_calls` (changed)

- `CallContext` gains `budget: BudgetTracker`; `ScoringContext` and `SummaryContext` gain the
  same required field.
- `call_structured(ctx, schema, *, model, purpose, system, user, per_item=True)`:
  1. if `per_item`: `ctx.budget.check()` (may raise `BudgetExceeded`; no provider call made);
  2. provider call;
  3. on success and on `LLMInvalidOutputError`: write the usage row **and**
     `ctx.budget.record(...)` with the same values.
- `BudgetExceeded` is not in `PER_ITEM_ERRORS`; it never marks an item failed.

## Batch nodes (changed)

- `score_items(items, ctx)` and `summarize_items(items, ctx)` stop at the first
  `BudgetExceeded`, log `budget.exceeded` once (`used`, `limit`) and return the outcomes
  completed so far (input order). The item in progress is left unchanged.

## `invio.db.repositories.ItemRepository` (new method)

```python
def release(self, items: Iterable[Item]) -> None
```
Per item: `attempts = max(attempts - 1, 0)`, `run_id = None`, `status = new` unless
`status == failed`. One flush.

## `invio.db.repositories.UsageRepository.add` (changed)

Gains keyword `created_at: datetime | None = None` so the recovery step keeps call times.

## `invio.graph.nodes.persist`

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class StageCounts:
    found: int
    new: int
    after_keyword_filter: int

@dataclass(frozen=True, slots=True, kw_only=True)
class DigestDraft:
    title: str
    body: str
    item_ids: list[int]

@dataclass(frozen=True, slots=True, kw_only=True)
class RunResult:
    job_id: int
    run_id: int
    counts: StageCounts
    taken: Sequence[Item]
    relevance: Sequence[RelevanceOutcome]
    summaries: Sequence[SummaryOutcome]
    digest: DigestDraft | None
    budget: BudgetTracker

def unprocessed(result: RunResult) -> list[Item]
def build_stats(result: RunResult) -> dict[str, Any]          # contracts/run-stats.md
def decide_status(result: RunResult) -> RunStatus                # FR-010 rules, no error path
def persist_run(session: Session, result: RunResult) -> Run   # flush-only
def finalize_run(factory: sessionmaker[Session], session: Session, result: RunResult) -> RunStatus
def record_failed_run(
    factory: sessionmaker[Session], *, job_id: int, run_id: int,
    budget: BudgetTracker, error: BaseException,
) -> RunStatus
```

Preconditions for callers (#21): the run row and the deduplication take (`mark_taken`) are
committed before the LLM stages; any exception raised by a stage before the save is handled
with `record_failed_run` (FR-011).

Behaviour:

- `persist_run`: when `budget.exceeded`, releases `unprocessed(result)`; adds the digest if
  `digest` is not `None` and has at least one item id (ids must be in `taken` and summarized
  by the run, else `ValueError`; repeated ids are a `ValueError` too; an unknown run is a
  `LookupError`); finishes the run with `decide_status(result)` and `build_stats(result)`, and
  with `runs.error = ALL_FAILED_ERROR` when that status is `failed`. Flush only.
- `finalize_run`: `persist_run` + `session.commit()`; logs `run.persisted` (`status`, `job_id`,
  `db_run_id` + stats fields). On any exception:
  `session.rollback()` (a failing rollback is logged as `run.rollback_failed`, recovery goes on),
  log `run.persist_failed` (class, `job_id`, `db_run_id`), return `record_failed_run(...)`
  (`RunStatus.FAILED`, or the stored status if the commit was applied before it failed). Never re-raises the error of the save itself; an error of the recovery
  transaction (`record_failed_run`) *is* re-raised; `KeyboardInterrupt`/`SystemExit` are
  re-raised after recovery. Status and stats are computed before any write.
- `record_failed_run`: in a new `session_scope`: if the run is no longer `running` (a commit
  applied before it failed), log `run.already_finished` and return its status without any
  write; otherwise re-insert every ledger entry as a usage row of
  the run, set the run `failed` with `finished_at`, stats from the ledger only (stage counts
  omitted, `budget_exceeded` from the tracker) and a sanitized error: `failure_message(err)` for an `LLMError`, otherwise
  `"<ErrorClass>: run failed"` (research R8). If this also
  fails, log `run.record_failed_error` with the error class, `job_id` and `db_run_id` and
  re-raise (the CLI exits non-zero). Returns `RunStatus.FAILED`.

Further preconditions for callers (#21): do not commit `llm_usage` rows before `finalize_run`
(`record_failed_run` replays the whole ledger and assumes the work session's rows were rolled
back), and commit the run start and `mark_taken` before the LLM stages.

`budget_exceeded` is set only by a failed `check()`: the last per-item call and the digest call
can push `tokens` above `budget_limit` while it stays `false` (`over_budget` is `true` then).
`budget.exceeded` is logged once (`stage`, `used`, `limit`, `job_id`, `db_run_id`), by the stage
that first hits the limit; both stages share the loop `llm_calls.run_until_exceeded`.

Log events carry the database ids as `job_id` and `db_run_id`: `run_id` is a reserved key of
the JSON formatter (the `invio.log` run context id).
