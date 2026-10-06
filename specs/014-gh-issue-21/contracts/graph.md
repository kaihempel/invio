# Contract: Research Graph (`invio.graph.build`, `invio.graph.state`)

```python
def build_graph(
    deps: RunDeps, *, job_id: int, run_id: int, token: datetime, dry_run: bool
) -> tuple[CompiledStateGraph[RunState], RunScope]: ...
```

`RunScope` is the mutable per-run holder shared by the node closures and `run_job`'s safety
net (see data-model.md).

The graph is compiled without a checkpointer, and all nodes are `async`. It is invoked once per
run by `run_job` with
`{"job_id": …, "run_id": …, "dry_run": …, "items": [], "errors": []}` and
`config={"max_concurrency": deps.concurrency}`, inside
`run_context(job=<name>, run_id=str(run_id))`. Context variables set inside a node do not
reach later nodes.

## Topology

```text
START
  └─► load_job ─?─► fetch_sources ─?─► deduplicate ─?─► keyword_prefilter
                                                             │
                              ┌── Send("process_item", ItemTask) × N (N ≥ 1)
                              │   or ─► join (N = 0)
                              ▼
                        process_item  (subgraph, semaphore-bounded)
                              │
                              ▼
                            join ─?─► synthesize_digest ─?─► persist ─?─► notify ─► finalize ─► END

 ─?─  = conditional edge: state["fatal"] is set ─► finalize, else next stage
```

`process_item` subgraph:

```text
START ─► route_kind ─┬─ article ─────────────► extract_text ─► score_relevance ─┬─ relevant ─► summarize_item ─► END
                     └─ video ─► video_path ─┘                                   └─ otherwise ───────────────────► END
```

`video_path` is a pass-through placeholder until #29. #29 replaces the node and keeps its
contract: in `ItemState`, out `ItemState`, with `raw_content` set or left unchanged.

## Node contracts

| Node | Reads | Writes (state) | Side effects | Guarded |
|---|---|---|---|---|
| `load_job` | `job_id`, `run_id` (set by `run_job`) | `config` | validates `jobs.config`; binds the provider (`deps.provider_for`) on the scope. The run row and the run context already exist, created by `run_job` | yes |
| `fetch_sources` | `config` | `candidates`, `sources_total`, `sources_failed`, `errors` | network via `deps.fetch_source` (retried); unsupported types skipped | yes |
| `deduplicate` | `candidates`, `config.limits` | `counts.found/new`, `taken` | opens the work session; `deduplicate(...)`; **commit** in normal runs only (R11) | yes |
| `keyword_prefilter` | `taken`, `config.search.keywords` | `selected`, `counts.after_keyword_filter` | `keyword_filter` per item (flush) | yes |
| `process_item` | `ItemTask` | `items += [ItemResult]`, `errors += [...]` | item writes (flush); error boundary R6 | own boundary |
| `join` | `items` | — | barrier for the fan-out (no logic) | no |
| `synthesize_digest` | `items`, `config` | `digest`, `digest_md` | one `smart` call (`per_item=False`) | yes |
| `persist` | all | `status`, `digest_id` | `finalize_run(RunDraft)`, or the dry-run rollback and finish (R11). An empty digest is stored only when `notification.send_if_empty` is set (normal runs) | yes |
| `notify` | `digest_id`, `dry_run` | `status` (delivery rule) | `deps.notify(digest_id)`; skipped in a dry run or without a digest | yes |
| `finalize` | `fatal`, `status`, `config` | `status` | `record_failed_run` on fatal; `release(lock, next_run_at)`; logs `run.finalized` | never fails silently: an error propagates to the `run_job` safety net |

## State reducers

- `items` and `errors`: `operator.add`. The merge order of parallel branches is unspecified.
  Consumers must not depend on it, and `RunResult.errors` is sorted by stage order, then by
  `item_id`.
- Every other key: last write wins. Only one node writes each key.

## Concurrency guarantee

At any moment, the number of `process_item` executions between entering and leaving their
semaphore is ≤ `deps.concurrency`. Source fetches in `fetch_sources` share the same bound.
