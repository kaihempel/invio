# Data Model: End-to-End Integration Test Suite (gh-issue-26)

This feature adds no product data. The "entities" are test fixtures and the observed product
records the scenarios assert on. Every fixture is test-only code under `tests/`.

## Test fixtures

### E2EArticle

One article that the feed fixture publishes.

| Field | Type | Rule |
|---|---|---|
| `slug` | str | unique within a feed; URL path `/articles/<slug>.html` |
| `title` | str | unique; the fake provider routes by it |
| `published_at` | datetime (UTC) | relative to the scenario clock; never older than the job's `max_age_days` |
| `relevant` | bool | `True` → score 0.9, `False` → score 0.1 (below `min_relevance` 0.6) |
| `fail_at` | `None` \| `"relevance"` | Scenario D: the provider raises for this title at that stage |

### E2EFeed

Wraps a `LoopbackServer` (`tests/http_helpers.py`).

- `articles: list[E2EArticle]`. The initial set is four articles: three relevant (A1–A3) and
  one off-topic (X1).
- `add(article)` appends the article and re-renders the `/feed.xml` route (RSS 2.0, newest
  first) and the article's page route.
- Static routes: `/robots.txt` (allow-all).
- `feed_url` is `http://127.0.0.1:<port>/feed.xml`.
- `requests` is the server's request log (used to assert fetch behaviour on reruns).

### MailSink

The existing `SmtpServer` from `tests/smtp_helpers.py` (no AUTH).

- `messages: list[EmailMessage]`; `handler.received` holds the recipients per mail.
- Helper `mails_to(recipient) -> list[EmailMessage]`.

### Provider stand-ins

| Stand-in | Used by | Behaviour |
|---|---|---|
| `RoutedFakeProvider` (`tests/pipeline_helpers.py`), registered as `ollama` | scenarios A–D | relevance by title (`relevant` / `fail_at`), fixed summary, fixed digest text; `titles_for(purpose)` records calls |
| `FakeOllamaServer` (new, `tests/e2e_helpers.py`) | separate-process smoke test | `POST /api/chat`, reply chosen by the `format` schema (`score` → relevance, `headline` → summary, none → digest); relevance and summary answer by the prompt's `<title>` like the in-process provider, so X1 is filtered |

### E2EEnv (scenario environment)

One per test; nothing is shared between tests (FR-005).

| Field | Content |
|---|---|
| `archive_dir` | `<tmp>/archive` |
| `job_name` | `e2e-<scenario>-<short uuid>` (unique, so MariaDB leftovers cannot collide) |
| `job_yaml` | `<tmp>/<job_name>.yaml`, rendered from the template below |
| `feed`, `mail`, `provider` | the fixtures above |
| `clock` | `FakeClock`; `advance(timedelta)` |
| `assert_engine` | a separate SQLAlchemy engine for reading rows |

Operations:

| Operation | Effect |
|---|---|
| `cli(*args) -> Result` | invokes `invio.cli.main.app` (contract: [contracts/e2e-harness.md](contracts/e2e-harness.md)) |
| `make_due()` | `next_run_at = clock() - 1 min` and `locked_until = NULL` for the job |
| `snapshot() -> Observed` | reads everything listed under "Observed outcomes" |

### Job YAML template

Uses the job contract from `src/invio/config/job.py`, no new keys:

- `schedule`: daily, 07:00, `UTC`
- `notification.to`: `[reader-1@example.test, reader-2@example.test]` (two recipients, so "one
  mail per recipient" is meaningful)
- `sources`: `[{type: rss, url: <feed_url>}]`
- `search.semantic_description`: a fixed topic
- `llm`: `provider: ollama`, `models: {fast: llama3.2:3b, smart: qwen2.5:14b}`
- `archive`: `enabled: true`, `base_url: https://archive.example.test/digests`

## Observed outcomes (product records the scenarios read)

Only externally visible state is asserted: DB rows, mails, files, CLI exit code and stdout.

| Observed | Source | Asserted in |
|---|---|---|
| runs of the job: `status`, `errors` | `runs` table | A, B, C, D |
| items: count, `status`, per title | `items` table | A, B, C, D |
| digests: count, item ids | `digests` table | A, B, C, D |
| notifications: count per recipient, status | `notifications` table | A, B, C, D |
| received mails: recipients, subject, text/HTML body, archive link in footer | `MailSink` | A, B, C, D, smoke |
| archive: `<job-slug>/*.html` pages, `<job-slug>/index.html`, `index.html` | `archive_dir` | A, B, C, D, smoke |
| no external resources in archive HTML: no `<script src=`, no `<link … href="http…">`, no `url(http…)` / `@import` in CSS (links to article URLs in the content are allowed) | archive files | A |
| job schedule state: `locked_until IS NULL`, `next_run_at == compute_next_run(schedule, clock())` (exact; compared as UTC) | `jobs` table | A, B, D |
| provider calls per purpose and title | `RoutedFakeProvider.titles_for` | B, C, D |
| CLI exit code and `run-due` stdout (`ran …`, `nothing due`, summary line) | `Result` | all |

## State transitions exercised

```text
job created (next_run_at in the future)
  └─ make_due() → due
       └─ run-due → locked → run: running → succeeded | partial
                         → lock released, next_run_at = compute_next_run(schedule, clock)
                         → not due (B4: "nothing due")
items: new → (relevance) relevant/irrelevant | failed
          → (summarize) summarized → in digest → notified
       already stored (same url_hash) → not inserted again (B, C)
```
