# Contract delta: `runs.stats` (still version 1) and repositories

This builds on [#19 run-stats](../../013-gh-issue-19/contracts/run-stats.md). The keys are
additive, so `version` stays `1`. Consumers already must ignore unknown keys.

## New keys

| Key | Type | Present | Meaning |
|---|---|---|---|
| `sources` | int | normal and dry-run layouts | enabled sources with an adapter |
| `sources_failed` | int | normal and dry-run layouts | of those, still failing after retries |
| `dry_run` | bool | dry runs only (`true`) | the run's results were rolled back |

Recovery layout (#19): unchanged. Stage and source counts are omitted.

## Invariant exceptions

In a dry run, the token and cost keys come from the budget ledger. No `llm_usage` rows exist
for the run, so the #19 invariant "equals `UsageRepository.totals_for_run`" does **not** apply.
Tests check `dry_run` runs against the ledger instead.

## Integration fix

`invio.notify.email._items_found` reads `stats["items_found"]`, but the #19 layout writes
`found`. This issue makes notify read `found`, and still accepts `items_found`, so the
"items found" line of the mail is filled for pipeline runs.

## Repository additions (`invio.db.repositories`)

```python
class JobRepository:
    def get(self, job_id: int) -> Job | None: ...
    def claim(self, job_id: int, *, now: datetime, until: datetime) -> bool:
        """One UPDATE … WHERE id AND (locked_until IS NULL OR locked_until < now); rowcount == 1."""

    def release(
        self, job_id: int, *, until: datetime, next_run_at: datetime | None | _Keep = KEEP
    ) -> bool:
        """One UPDATE … SET locked_until = NULL[, next_run_at] WHERE id AND locked_until = until.
        False (and no change) when the lock is no longer this run's."""


class ItemRepository:
    def set_extracted(self, item: Item, text: str) -> None:
        """raw_content = text, status = extracted; one flush."""
```

## HTTP client addition (`invio.sources.http`)

```python
class SafeHttpClient:
    async def get(
        self, url: str, *, headers: Mapping[str, str] | None = None, conditional: bool = True
    ) -> FetchResult | NotModified:
        """conditional=False: send no remembered validators (If-None-Match / If-Modified-Since)
        and do not store the response's validators; never returns NotModified."""
```

It is used by `fetch_page` for item pages: a web page-mode item has the same URL as its source
(research R6).

## Error text for an invalid stored config (`persist._sanitized_error`)

`pydantic.ValidationError` → `"ValidationError: invalid fields <loc>[, <loc>…]"`. Each `<loc>`
is the dotted `loc` of one error (`sources.0.url`). There are at most 5, then `", …"`. Input
values and pydantic messages are never included.

`claim` and `release` execute in their caller's session and are committed by the caller, each
in its own short transaction (SQLite has a single writer, #19 R1).
