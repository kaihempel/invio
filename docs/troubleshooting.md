# Troubleshooting

**Last Updated:** 2026-10-11

Symptom, cause and fix for the failures an unattended installation shows most. Commands and
journal lines are the ones the code prints; the procedures they refer to are in
[operations.md](operations.md). Deployment-level problems (missing env file, units that fail to
load, database unreachable, `No such command 'run-due'`) are in
[deployment.md, Troubleshooting](deployment.md#troubleshooting).

Where to look first:

```bash
invio-svc run list --limit 10                    # status of the last runs (see operations.md)
invio-svc run show 42                            # stage counts and every stored error of run 42
journalctl -u invio-run-due -o cat --since -24h | grep -E '"level": "(WARNING|ERROR)"'
```

In `run show`, a source failure appears with its source position, an item failure with the item's
title and URL, and a notification failure as a `notify` entry.

## YouTube blocks

### Sources fail with HTTP 429 or 403

- **Symptom.** The source fails and the run is `partial` (or `failed` when every source failed).
  `run show` names the error as a short reason code, never yt-dlp's text, for example
  `http_status: https://www.youtube.com/@example (HTTP 429)`. The log has `source.youtube_failed`
  with the exception class only.
- **Cause.** YouTube answers a server IP with "confirm you're not a bot" (classified as HTTP 429)
  or refuses the request (403). The block is per IP address, so it hits every job on that host.
- **Fix.**
  - Provide a cookies file exported from a logged-in browser (Netscape format) with
    `INVIO_YOUTUBE_COOKIES_FILE=/var/lib/invio/youtube-cookies.txt`. It is only read; invio gives
    yt-dlp a private copy. A file that is missing or not readable fails the source with
    `cookies_unavailable`, so check that the `invio` account can read it (a copy under
    `/var/lib/invio`, owned by `invio`, is the simplest place).
  - Route the requests through a proxy with `INVIO_YOUTUBE_PROXY` (treated as a secret; it may
    embed credentials).
  - Reduce the request count: fewer channels per job, a longer schedule, or `limits.max_items_per_source`.
  - A 429 or network error is retried inside the run (see [Rate limits](#rate-limits)); a block that
    lasts across runs will fail every run until the cause is removed.
- **Check.** Once the fix is in, `invio job run <name> --dry-run -v` shows whether the sources
  now return items (it still calls the LLM for items it finds).

### Video transcripts are missing or rated from the description

The item still gets a rating. The log line `transcript.unavailable` names the reason, and the
item is not failed for permanent reasons. Reasons, with the usual fix:

| `reason` | Kind | Meaning | Fix |
|---|---|---|---|
| `video_unavailable` | permanent | private, deleted or region-locked (HTTP 404 or 410, or a 403 that yt-dlp reports as unavailable) | none; the video is gone |
| `no_audio_format` | permanent | yt-dlp listed no audio format; often a missing JavaScript runtime | install a JavaScript runtime such as Deno for yt-dlp (README, "Video transcripts") |
| `whisper_not_installed` | permanent | a video without subtitles and the `whisper` extra is not installed; the log hint is `install it with: uv sync --extra whisper` | install the extra, or set `invio_whisper_enabled: true` in the Ansible role ([deployment.md](deployment.md#local-transcription-optional)) |
| `transcription_disabled` | permanent | `INVIO_WHISPER_MAX_PER_RUN=0` | intended; raise the value to transcribe |
| `too_long` | permanent | longer than `INVIO_WHISPER_MAX_DURATION_MINUTES` (default 90) | raise the limit if the host can take it |
| `live_stream` | permanent | live or upcoming stream | none |
| `no_speech` | permanent | transcription produced no text | none |
| `unsupported_url` | permanent | not a video URL the adapter handles | none |
| `cookies_unavailable` | configuration | the cookies file is not readable | see above |
| `timeout`, `connection_failed`, `invalid_response` | transient | network trouble | retried; a later run tries again |

`transcript.deferred` with `cap_reached` or `not_enough_time` is not an error: the video is left
for the next run because the per-run transcription cap or the run's time is used up.

## SMTP errors

### `notify retry` exits 2 with a configuration error

- **Symptom.**
  - `Configuration error: smtp not configured: missing INVIO_SMTP_HOST` (or `INVIO_SMTP_FROM`).
  - `Configuration error: ...` for an invalid database URL.
- **Cause.** The setting is missing or empty in `/etc/invio/invio.env`. Only `INVIO_SMTP_HOST` and
  `INVIO_SMTP_FROM` are required to send; `INVIO_SMTP_USER` and `INVIO_SMTP_PASSWORD` are used
  for a login only when both are set.
- **Fix.** Set the missing variable and check it with `systemctl start invio-notify-retry.service`
  (exit code and journal). No restart is needed.

### A digest mail fails during a run

- **Symptom.** The run is `partial` (a delivery problem lowers `succeeded` to `partial`), `run show`
  has a `notify` entry, the statistics show `Notifications failed`, and the log has
  `notification failed` with `error` set to the exception class and a scrubbed message, for example
  `SMTPAuthenticationError: ...`, `SMTPConnectError: ...` or `TimeoutError: ...`. The mail is
  stored as `failed` and retried hourly.
- **Cause and fix.**

  | Message (class) | Cause | Fix |
  |---|---|---|
  | `SMTPAuthenticationError` | wrong `INVIO_SMTP_USER` or `INVIO_SMTP_PASSWORD`, or the provider needs an app password | correct the credentials, then `invio-svc notify retry` |
  | `SMTPConnectError`, `ConnectionRefusedError`, `gaierror` | wrong `INVIO_SMTP_HOST`/`INVIO_SMTP_PORT`, or the host cannot reach it | check the name and port; from the host: `nc -vz smtp.example.com 587` |
  | `SMTPServerDisconnected`, TLS errors | `INVIO_SMTP_SECURITY` does not match the port | `starttls` with 587, `ssl` with 465, `none` only for a trusted local relay |
  | `TimeoutError` | server slow or blocked; limit is `INVIO_SMTP_TIMEOUT_SECONDS` (default 30) | check the network; raise the timeout only if the server is slow |
  | `digest no longer available` | the stored digest was deleted | the mail cannot be rebuilt; mark the row handled (see operations) |

- **Check.** `invio-svc notify retry` prints one line per retried row, and exits 1 while a
  notification still fails. Rows that fail five times become `skipped` ("given up").
- **Duplicate mails.** A `notification sent but not recorded` log line means the mail went out
  but the database write failed; the row stays `pending` and a later retry can send it again.
  Check the mailbox before re-sending such a row.

## Rate limits

### LLM provider returns 429 or quota errors

- **Symptom.** `llm.retry` lines in the journal; `llm.fallback` when a fallback is configured;
  otherwise the item or the run fails with `LLMRateLimitError: ...` or `LLMQuotaError: ...` in
  `run show`.
- **Cause.** The provider limits requests per minute (`LLMRateLimitError`) or the account's quota
  or billing is exhausted (`LLMQuotaError`).
- **What the code does.** A rate-limited request is retried at most 3 times with exponential
  backoff (1 s initial, factor 2, at most 30 s, plus up to 10 % jitter). A wait the provider asks
  for (`retry_after`) is used when it is longer. `LLMQuotaError` is not retried, because waiting
  does not help. After the retries the role's fallback is tried (README, "Per-role providers and
  fallback"). There is no circuit breaker: a long outage costs time on every call.
- **Fix.**
  - Lower the parallelism: `INVIO_MAX_PARALLEL_ITEMS` (default 4) bounds the items processed at
    once in one run, and `invio run-due --parallel N` runs N jobs at the same time, multiplying it.
  - Spread the schedules of heavy jobs (`schedule` in the job YAML).
  - For `LLMQuotaError`, raise the provider's spending limit or check the billing page. Waiting
    will not clear it; a fallback provider will.
  - Confirm the key still works: `invio-svc llm test mistral` (see [operations.md](operations.md#adding-or-rotating-a-provider-key)).

### Source sites return 429

- **Symptom.** `http_status: <url> (HTTP 429)` in `run show`, for a web or feed source.
- **What the code does.** Requests to one site are spaced by `INVIO_HTTP_HOST_INTERVAL_SECONDS`
  (default `1.0`) and never run in parallel for the same host. A 429 is transient and retried
  with the same backoff as above. A `Crawl-delay` in robots.txt can increase the spacing.
- **Fix.** Raise `INVIO_HTTP_HOST_INTERVAL_SECONDS` (for example `5`) for a busy site, or move the
  job to a longer schedule. Do not disable robots.txt to get around a limit; `INVIO_HTTP_RESPECT_ROBOTS`
  is a deliberate choice (see [security.md](security.md)).

## Budget exceeded

- **Symptom.**
  - The run status is `partial`.
  - `run show` has a `Skipped (budget)` row.
  - The log has a warning `budget.exceeded` with the message `token budget exceeded: <used> used, limit <limit>`.
  - The digest contains only the items summarized before the limit.
- **Cause.** The per-item LLM calls (relevance and summary) of one run used up
  `limits.max_llm_tokens_per_run` (default `200000`, input plus output). The check runs before each
  call, so the last call can overshoot. The digest call always runs and is counted.
- **Facts that matter.**
  - Items that were not finished are released: the next run rates them again, and their relevance
    tokens are spent twice.
  - `budget_exceeded` in the run statistics is set only when a check failed. `over_budget` shows the
    case where the tokens are above the limit without that flag.
- **Fix.**
  - Find the spend first: `invio-svc usage --job daily-digest --since 2026-10-01 --by day`.
  - Lower the work per run: `limits.max_items_per_run`, `limits.max_items_per_source`, or a higher
    `search.min_relevance` (fewer items reach the summary step).
  - Raise `limits.max_llm_tokens_per_run` if the spend is expected, then `invio-svc job edit daily-digest`.
  - Use a cheaper model for the `fast` role (`llm.models.fast`), which scores relevance for every
    item. The `smart` role is used for the summaries and the digest.

## Empty digests

- **Symptom.** A run is `succeeded` but no mail arrives and no archive page is written. The
  statistics show `Found`, `New`, `Relevant` or `Summarized` at 0.
- **Cause.** Nothing reached the digest. Read `run show` from the top down; the first stage that
  drops to zero is the cause:

  | Statistic | Usually means | Where to change it |
  |---|---|---|
  | `Found` 0, `Sources failed` > 0 | the sources failed (see [YouTube blocks](#youtube-blocks) and the source's HTTP errors) | the source URL or access |
  | `Found` 0, nothing failed | the source has no entries, or every entry is older than `max_age_days` | `max_age_days` on the feed, sitemap or channel (drops entries older than the limit) |
  | `New` 0 | every item was seen in an earlier run (deduplication) | nothing to do; the next new item appears in a later run |
  | `After keyword filter` 0 | `search.keywords` reject every item: any `exclude` hit rejects, every `all` term must occur, one `any` term is needed | loosen the keywords |
  | Items `skipped_baseline` on a first run | the baseline takes only `limits.baseline_items` per source (default 10) | nothing to do; later runs take the rest, or raise `limits.baseline_items` |
  | `Relevant` 0 with `Processed` > 0 | the relevance score is below `search.min_relevance` (default `0.6`) | refine `search.semantic_description`, or lower `min_relevance` |
  | `Skipped (budget)` > 0 | the token budget stopped the run (see [Budget exceeded](#budget-exceeded)) | the budget |

- **Quiet by design.** With `notification.send_if_empty: false` (the default) an empty digest is
  neither stored nor sent. The log line `empty digest not sent` (INFO) appears when a stored digest
  has no items. Set `notification.send_if_empty: true` if you want a mail that says nothing was
  found.
- **Diagnose.** Dry run prints the digest and one line per item (`relevant`, `irrelevant`, `failed`)
  without storing items or sending mail:

  ```bash
  invio-svc job run daily-digest --dry-run -v
  ```

  It calls the LLM and spends tokens; the next real run is not affected.

## Other symptoms

- **A job is reported `busy` or `is running`, or runs twice.** Lock behaviour and its expiry
  (`INVIO_RUN_LOCK_SECONDS`): see [operations.md, Stuck job locks](operations.md#stuck-job-locks)
  and [deployment.md, Troubleshooting](deployment.md#troubleshooting).
- **`run show` reports `running` for a run that is no longer active.** The process was killed; the
  lock is released when it expires. See [operations.md](operations.md#stuck-job-locks).
- **A job file is rejected.** `invio job edit <name>` reports the invalid field and keeps the job
  unchanged on abort; `invio job run` exits 1 for an invalid stored config. The field reference is
  in [configuration.md](configuration.md).
- **A key in the env file has no effect.** The log line `unknown setting ignored` (with the `key`
  field) means the name is not a setting, usually a typo. Compare it with
  `deploy/env/invio.env.example`.

## Related pages

- [operations.md](operations.md): the commands used above, lock and notification procedures.
- [deployment.md](deployment.md): installation, units, and the Troubleshooting section for the
  host.
- [configuration.md](configuration.md): every setting named here.
- [security.md](security.md): the source guards and secret handling.
