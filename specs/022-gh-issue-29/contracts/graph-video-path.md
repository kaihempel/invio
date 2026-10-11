# Contract: `video_path` in the `process_item` subgraph

**Feature**: [../spec.md](../spec.md) · amends `specs/014-gh-issue-21/contracts/graph.md` ·
research R8.

## Topology (changed edge)

```text
START ─► route_kind ─┬─ article ─► extract_text ─┐
                     └─ video ───► video_path ───┴─► score_relevance ─┬─ relevant ─► summarize_item ─► END
                                                                      └─ otherwise ───────────────────► END
```

`video_path` now goes straight to `score_relevance` (it was `→ extract_text`). It is wrapped by
`_recording("extract_text", …)`: it records `extract_text` as the item's stage, stops when the
run is failing, and checks the lock, like the other item nodes. `RunStage` is unchanged.

The #21 node contract is kept: in `ItemState`, out `ItemUpdate` (`{"stage": "extract_text"}`),
with `raw_content` set or left unchanged.

## Port

```python
class TranscriptFetcher(Protocol):
    async def __call__(
        self,
        url: str,
        /,
        *,
        language: str,
        may_transcribe: Callable[[], bool],
        deadline: datetime | None,
    ) -> Transcript: ...


RunDeps.fetch_transcript: TranscriptFetcher
RunDeps.max_transcriptions_per_run: int = 3  # >= 0
RunScope.transcriptions: set[int]  # item ids holding a slot
RunScope.deferred: set[int]  # item ids released by a deferral (persist must not release again)
```

## Node behaviour

| Outcome of `fetch_transcript` | Item effect | Next |
|---|---|---|
| `Transcript` | `set_extracted(item, text)` → `raw_content`, status `extracted` | `score_relevance` |
| permanent `TranscriptUnavailable` | `raw_content` unchanged (`None`); `teaser` ← description (collapsed, ≤ 2 000 chars) if it was empty; not failed | `score_relevance` (rated from title + description) |
| transient `TranscriptUnavailable` / other `FetchError` | retried per `deps.retry`; then propagates → boundary `mark_failed` | END (failed; next run retries while `attempts < max_attempts`) |
| `TranscriptionDeferred` (`cap_reached` or `not_enough_time`) | propagates → boundary `ItemRepository.release([item])` (status `new`, attempts − 1, `run_id` cleared); for `not_enough_time` the item id is also removed from `scope.transcriptions`; no `ItemResult` | END (not processed, not failed) |

The boundary releases a deferred item exactly once: it adds the id to `scope.deferred`, and the
`persist` stage passes that set to `RunDraft.deferred`, which `unprocessed()` excludes. A run in
which every item was deferred is not failed (no outcome, nothing attempted). With
`INVIO_WHISPER_MAX_PER_RUN=0` the production adapter never asks `may_transcribe`; it raises the
permanent `transcription_disabled` instead (the item is rated from title and description).

`language` passed to the port is `scope.config.language` (the job's language); `deadline` is
`scope.token` (the run's lock expiry, which `ensure_lock_held` enforces).

`may_transcribe` (closure over `scope`):

```text
item_id in scope.transcriptions                          → True   (a retry of the same item)
len(scope.transcriptions) < deps.max_transcriptions_per_run → add item_id, True
otherwise                                                → False
```

## Logging (`invio.graph`)

| Event | Level | Fields |
|---|---|---|
| `extract.transcript` | info | `item_id`, `method`, `language`, `chars` |
| `extract.transcript_unavailable` | info | `item_id`, `reason` |
| `item.transcription_deferred` | info | `item_id`, `reason` |

## Production wiring (`invio.pipeline.deps.default_deps`)

`YoutubeTranscripts.from_settings(settings, clock=utcnow)` is created with the other adapters
and closed on exit (the same clock as `RunDeps.clock`, so the deadline check and
`ensure_lock_held` agree); `fetch_transcript = transcripts.get_transcript`;
`max_transcriptions_per_run = settings.whisper_max_per_run`.
