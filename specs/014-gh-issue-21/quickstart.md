# Quickstart: Validating the Assembled Research Workflow (gh-issue-21)

Feature: [spec.md](spec.md) · Contracts: [run-job](contracts/run-job.md),
[graph](contracts/graph.md), [stats delta](contracts/run-stats-delta.md)

## Prerequisites

```bash
uv sync                    # installs langgraph (new runtime dependency)
uv run invio --version
```

No network, LLM key or SMTP server is needed. All scenarios use SQLite in memory, the scripted
`FakeProvider` (routed by purpose, `tests/pipeline_helpers.py`), local fixtures from
`tests/fixtures/{feeds,articles}`, fake source and page fetch ports, a recording notifier, a
fixed clock and a no-wait `sleep`.

## Gates (same as CI)

```bash
uv run ruff check && uv run ruff format --check
uv run mypy src
uv run pytest tests/test_pipeline_*.py tests/test_graph_*.py tests/test_retry.py -q
uv run pytest -q             # full suite, incl. renamed RunDraft in test_persist.py
```

## Scenarios

Each scenario is one or more tests in `tests/test_pipeline_*.py`. "Expect" lists what the test
asserts.

| # | Scenario (spec) | Setup | Expect |
|---|---|---|---|
| 1 | End-to-end digest (US1, AC 1, SC-001) | 1 RSS fixture source with 3 entries, article fixtures for each, fake scripted relevant + summary + digest | `RunResult.status == succeeded`; 1 `digests` row covering 3 item ids; items `summarized`; stats `found=3 … summarized=3 sources=1 sources_failed=0`; notifier called once with the digest id; `next_run_at` = fixed-clock schedule value; lock cleared |
| 2 | Stage order (US1-2) | as 1, with a recording wrapper on the ports | calls ordered load → fetch → dedup → prefilter → items → synthesize → persist → notify → finalize; each item processed once |
| 3 | No new items (US1-3) | second run of scenario 1 | no LLM requests; `succeeded`; zero counts; `next_run_at` advanced |
| 4 | Irrelevant item (US1-4) | one item rated 0.1 | item `skipped_irrelevant`, no summary request, not in digest |
| 5 | One failing item (US2-1, AC 2, SC-002) | 3 items; page fetch of item 2 raises `TooLargeError` | items 1 and 3 summarized and in digest; item 2 `failed` with `last_error`; status `partial`; one `RunError(stage="extract_text", item_id=2)` |
| 6 | All items fail (US2-2) | every relevance call → `LLMInvalidRequestError` | status `failed`, `runs.error == "all attempted items failed"`; lock released |
| 7 | Error hygiene (US2-3, SC-007) | provider error carrying a secret-looking message, URL with `?token=` | no `RunError`, `runs.error`, `last_error` or log line contains the message, token or document text |
| 8 | Transient retry succeeds (US3-1, SC-005) | relevance: `LLMRateLimitError(retry_after=2)` ×2 then reply | item relevant; `succeeded`; recorded sleeps `[2.0, 2.0]` (retry_after > backoff) |
| 9 | Source fetch retried (US3-2) | source raises `FetchError("timeout")` once | candidates included; `sources_failed == 0` |
| 10 | Retries exhausted (US3-3) | `LLMUnavailableError` × 3 on one item | item `failed`, others processed, `partial`; sleeps `[1.0, 2.0]` (+jitter bound) |
| 11 | Not retried (US3-4) | `LLMAuthError`; `LLMInvalidRequestError`; `BlockedError` | each attempted exactly once; auth → run `failed` (run-fatal) |
| 12 | Concurrency bound (US4, AC 4, SC-004) | 10 items, `concurrency=4`; counting gate in fake `fetch_page` | max in flight == 4, never > 4; all 10 processed. Repeat with 1 (max 1) and with default (4) |
| 13 | `fetch_sources` raises (US5-1, AC 3, SC-003) | fake `fetch_source` raises `RuntimeError` | run `failed`, `runs.error == "RuntimeError: run failed"`; `next_run_at` recomputed; `locked_until` NULL; `run_job` returns |
| 14 | Save fails (US5-2) | inject failure in `persist_run` (as in #19 tests) | run `failed` via `record_failed_run`; usage rows replayed once; lock released; next run set |
| 15 | Delivery fails (US5-3) | notifier reports `failed=1` | status `partial` (from `succeeded`); finalize ran |
| 16 | Cancellation (US5-4) | cancel the `run_job` task while items are in flight | `CancelledError` re-raised; run `failed`; lock released |
| 17 | Busy and expired lock (US5-5, FR-016) | `locked_until = now + 1h`; then `now − 1s` | busy: `JobBusyError`, no run row, items untouched, lock unchanged; expired: run proceeds and releases |
| 18 | Concurrent claim (FR-016) | two `run_job` calls on the same job via `asyncio.gather` | exactly one runs; the other raises `JobBusyError` (and on MariaDB with threads, `-m db`) |
| 19 | Dry run (US6, AC 5, SC-006) | as 1 with `dry_run=True`; snapshot items/digests/usage/notifications/`next_run_at` before | no notifier call; `RunResult.digest` set; no new digests/usage/notifications rows; items identical to snapshot (incl. not inserted); `next_run_at` byte-equal; run row `stats["dry_run"] is True`; lock released |
| 20 | Dry run then real run (US6-4) | scenario 19, then a normal run | the normal run processes the same 3 items (attempts 1, not 2) |
| 21 | One source down (SC-008) | 2 sources, one raises `FetchError` × 3 | other source's items in digest; `partial`; `RunError(stage="fetch_sources", source="1:rss")` |
| 22 | All sources down (SC-008) | every source raises | `failed`, `runs.error == "all sources failed"`; finalize ran |
| 23 | Unsupported source type | job with an RSS and a `youtube_channel` source | YouTube skipped (`source.unsupported` logged), not counted; `sources == 1` |
| 24 | Video item (FR-004) | candidate `type="video"` with a page fixture | goes through `video_path` → `extract_text`; summarized from page text; no special status |
| 25 | Too-short page (edge case) | page with < `min_chars` text | item rated from title + teaser; not failed |
| 26 | Budget stop mid fan-out (edge case) | `max_llm_tokens_per_run` small | `partial`; `budget_stopped` items released (attempts undone); digest of summarized ones |
| 27 | Invalid stored config / missing / disabled job (edge cases) | broken `jobs.config`; unknown id; disabled job | config: run `failed` with `runs.error == "ValidationError: invalid fields search.min_relevance, llm.provider"` (paths only, no values), no LLM calls; missing API key: `failed`, `"MissingSettingError: run failed"`, no LLM calls; unknown → `JobNotFoundError`; disabled → `JobDisabledError`; no run, no lock for the last two |
| 28 | Run context (FR-014) | capture log records of scenario 1 | every record from inside the run carries `job` and `run_id` (= `runs.id`) |
| 29 | Layering | AST tests | `invio.graph` imports neither `invio.notify`, `invio.scheduling` nor `invio.pipeline`; only `invio.pipeline`/`invio.cli` import `invio.pipeline` |
| 30 | Page-mode web source (research R6) | `web` source `mode: page` through a real `SafeHttpClient` + `MockTransport`; server sends `ETag` and answers `304` to `If-None-Match` | item (same URL as the source) extracted and summarized; the item-page request carried no `If-None-Match` |
| 31 | Empty digest with `send_if_empty` (US1-3) | run without new items; `notification.send_if_empty` true, then false | true: one digest with empty `item_ids`, notifier called once; false: no digest, notifier not called |

## Manual smoke test (optional, real database and provider)

```bash
export INVIO_DATABASE_URL=sqlite:///./invio.db INVIO_MISTRAL_API_KEY=…
uv run invio db upgrade
uv run invio job create --from-file docs/job.example.yaml --name demo
uv run python -c "import asyncio; from invio.pipeline.run import run_job; \
print(asyncio.run(run_job(1, dry_run=True)))"
```

Expect a `RunResult(status=…, dry_run=True, digest=DigestDraft(…))`, and no change to
`invio job list`'s "next run" column. The CLI command (`invio job run`) follows in #22.
