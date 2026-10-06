# invio

CLI-based AI research system.

## Development quickstart

Requirements: [uv](https://docs.astral.sh/uv/) (Python 3.12 is installed by uv if missing).

```bash
uv sync                      # create .venv and install runtime + dev dependencies
uv run invio --version       # smoke test
cp .env.example .env         # local configuration (never commit .env)
uv run pre-commit install    # optional: run checks on every commit
```

### Checks (same as CI)

```bash
uv run ruff check
uv run ruff format --check   # `uv run ruff format` to fix
uv run mypy src
uv run pytest                # `-m browser` for the headless-Chromium tests only
```

The `browser` tests need Chromium (`uv run playwright install chromium`); they are skipped when
it cannot be launched. CI installs it.

### Adding a CLI command

Create `src/invio/cli/commands/<name>.py` exposing a module-level `typer.Typer` named `app`:

```python
import typer

app = typer.Typer(help="Say hello.")


@app.command()
def greet(name: str = "world") -> None:
    typer.echo(f"Hello {name}")
```

It is registered automatically as the command group `invio <name>` (underscores become
dashes, e.g. `run_now.py` -> `invio run-now`), so the example above runs as
`invio <name> greet --name you`. No other file needs to change. Modules whose name starts
with `_` are ignored, so use them for shared helpers.

### Configuration & logging

Settings come from `INVIO_*` environment variables or a `.env` file in the working directory
(see `.env.example`; real environment variables win over `.env`). Set `INVIO_ENV_FILE` to load
the file from elsewhere, e.g. under cron/systemd where the working directory differs; a missing
file is then an error. Secrets are `SecretStr` and never appear in `repr`/logs. Provider keys
are optional until the provider is actually used. `INVIO_LLM_TIMEOUT_SECONDS` (default `60`,
must be > 0) bounds every single LLM request:

```python
from invio.config.settings import get_settings

settings = get_settings()  # cached instance
api_key = settings.require_secret("openai_api_key")  # raises "INVIO_OPENAI_API_KEY is not set"
```

E-mail settings: `INVIO_SMTP_HOST`, `INVIO_SMTP_PORT` (default `587`), `INVIO_SMTP_USER` /
`INVIO_SMTP_PASSWORD` (login only when both are set), `INVIO_SMTP_FROM`,
`INVIO_SMTP_SECURITY` (`starttls` (default), `ssl` or `none`; replaces `INVIO_SMTP_STARTTLS`,
and a leftover old name is reported by the "unknown setting ignored" warning) and
`INVIO_SMTP_TIMEOUT_SECONDS` (default `30`, must be > 0, per SMTP operation). `starttls` fails
when the server does not offer it; the port is not derived from the mode (use `465` for `ssl`).

Every `invio` sub-command configures logging on startup: one JSON object per line on stderr,
level from `INVIO_LOG_LEVEL`. Unknown `INVIO_*` keys (usually typos) are logged as warnings, and
configuration errors end the command with exit code 2. Wrap a job run in `run_context` to tag
every line with `job` and `run_id` (a nested context without `job` keeps the outer one). `extra`
keys that clash with built-in fields such as `job` or `level` are emitted as `extra_<key>`:

```python
import logging

from invio.log import configure_logging, run_context

configure_logging()  # done by the CLI already; call it yourself in scripts
with run_context(job="digest") as run_id:
    logging.getLogger(__name__).info("started", extra={"sources": 3})
```

### Fetching sources safely

Source fetchers get web content only through `invio.sources.http.SafeHttpClient`; importing
`httpx2` or `urllib.request` in `src/invio/sources/` outside the client modules fails `ruff check`.

```python
from invio.sources.http import HttpClientConfig, SafeHttpClient

async with SafeHttpClient(HttpClientConfig.from_settings()) as client:
    result = await client.get("https://example.org/feed.xml")  # FetchResult or NotModified
```

- **Blocked targets**: only `http` and `https`. Every hop (the first URL and each redirect) is
  resolved first and refused (`BlockedError`) if any address is not public: loopback, private,
  link-local (cloud metadata), carrier-grade NAT, multicast and IPv6 forms that embed such an
  address. The connection goes only to the validated addresses (the next one is tried if
  one cannot be reached), so DNS rebinding does not help. `user:password@` in a URL is dropped,
  never sent and never logged; query strings stay out of error messages and logs. Cookies are
  never stored.
- **robots.txt**: fetched once per site and run, for feeds as well as pages. A disallowed path
  raises `BlockedError` with `reason == "blocked_by_robots"` and is never requested. A 4xx on
  robots.txt allows everything; a 5xx blocks the site for the rest of the run, and a timeout,
  connection or DNS failure blocks it for 5 minutes before robots.txt is fetched again. Rules
  follow RFC 9309 (`*` and `$` wildcards, longest match wins) on every Python version. Set
  `INVIO_HTTP_RESPECT_ROBOTS=false` to switch the check off.
- **Rate limiting**: per site at most one request at a time, starts spaced by
  `INVIO_HTTP_HOST_INTERVAL_SECONDS`. A `Crawl-delay` in robots.txt can raise that spacing, by at
  most 30 s; other sites are not slowed down.
- **Limits**: responses over `INVIO_HTTP_MAX_RESPONSE_BYTES` (decoded size) raise
  `TooLargeError`; only one layer of gzip or deflate is accepted. At most
  `INVIO_HTTP_MAX_REDIRECTS` redirects; `INVIO_HTTP_TOTAL_TIMEOUT_SECONDS` bounds sending and
  reading within one `get()` from its first request (DNS checks, rate-limit waits and the first
  robots.txt fetch of a site come on top). Requests identify as
  `invio/<version> (+https://github.com/kaihempel/invio; contact: <INVIO_HTTP_CONTACT>)`, so
  **set `INVIO_HTTP_CONTACT`** to an address site owners can reach.
- **Conditional requests**: `ETag`/`Last-Modified` are remembered per URL for the client's
  lifetime (at most 10,000 URLs); a repeated `get` returns `NotModified` on 304. A 304 to a
  request without validators is an `http_status` error.

| Setting | Default |
|---------|---------|
| `INVIO_HTTP_CONTACT` | `admin@example.invalid` |
| `INVIO_HTTP_MAX_RESPONSE_BYTES` | `10485760` |
| `INVIO_HTTP_MAX_REDIRECTS` | `5` |
| `INVIO_HTTP_CONNECT_TIMEOUT_SECONDS` | `10` |
| `INVIO_HTTP_READ_TIMEOUT_SECONDS` | `30` |
| `INVIO_HTTP_TOTAL_TIMEOUT_SECONDS` | `60` (must be >= the read timeout) |
| `INVIO_HTTP_HOST_INTERVAL_SECONDS` | `1` |
| `INVIO_HTTP_RESPECT_ROBOTS` | `true` |

### Web page sources

A `web` source tracks a page that has no feed (`invio.sources.web.WebPageSource`). It fetches the
page through the safe client above, takes the readable text of the page (script, style, noscript,
template and comments dropped, whitespace collapsed, Unicode NFC) and reports it as one candidate
whose `content_hash` is the SHA-256 of that text. The same text always gives the same hash, so
the pipeline can tell whether the page changed.

```yaml
sources:
  # Page mode (default): one item for the page, new when the text of the region changes.
  - type: web
    url: https://example.org/news
    selector: "main .post-list"   # optional CSS selector; default is the whole <body>

  # Links mode: one item per article linked from an index page.
  - type: web
    url: https://example.org/blog
    mode: links
    selector: "main"
    url_pattern: "/blog/\\d{4}/"   # regex searched in the absolute URL, any host
```

| Key | Meaning |
|-----|---------|
| `selector` | Only the matching elements count (all matches, in page order). A selector that matches nothing fails the fetch with `selector_not_found`; there is no fallback to the whole page. |
| `mode` | `page` (default) or `links`. Without `url_pattern`, links mode keeps only links on the page's own host (`www.` is a different host). Fragments are dropped from link URLs, except single-page-app routes (`#/post/1`, `#!/post/1`), which count as separate articles. |
| `url_pattern` | Python regular expression for `mode: links`. |
| `render` | `static` (default) or `js`, see below. |
| `wait_for` | Plain CSS selector to wait for with `render: js` (no Playwright-only selector syntax; a selector the browser cannot parse fails with `invalid_wait_for`). |

Invalid selectors and patterns, and `url_pattern` without `mode: links` or `wait_for` without
`render: js`, are rejected when the job file is loaded. Other failures: `not_html` (a PDF or JSON
response), and the client's errors (blocked, robots.txt, size, timeout, HTTP status).

The page is decoded like a browser does (BOM, then the `Content-Type` charset, then `<meta
charset>`, with the WHATWG labels, so `gb2312` is read as GBK and `latin1` as windows-1252).
The `content_hash` is computed from the text of the parsed page; an upgrade of the HTML parser
(selectolax) that parses a page differently can change the hash of an unchanged page once.

**JavaScript pages (`render: js`)** are loaded in headless Chromium, which is an optional
dependency that is imported only for such sources:

```bash
uv sync --extra render                 # Playwright
uv run playwright install chromium     # the browser (development: `uv sync` already has Playwright)
```

Without them a `render: js` fetch fails with `render_unavailable`; other sources are not
affected. A render waits until the network has been quiet for 0.5 s (and `wait_for` matches),
for at most 30 s (`timeout`). The page URL goes through the client's address guard, robots.txt
check and rate limit, and every request the page makes (including WebSockets) is checked by the
same address guard and aborted if it fails; the page still renders without it. Redirects are
followed by invio so each hop is checked (once a redirect leaves the requested origin, no later
hop gets the page's credentials or cookies), and the page keeps seeing the URL it asked for. A
main-frame navigation that is refused fails the render instead of returning the browser's error
page. Service workers, downloads and proxies are off and every render starts with empty cookies
and storage. At most four renders run at a time.

Limits of `render: js`: the browser resolves a host name again when it connects, so a DNS answer
that changes between the check and the connection (DNS rebinding) is not caught, unlike with the
static client, which pins the connection to the checked address. Every response, the main
document included, is capped at `INVIO_HTTP_MAX_RESPONSE_BYTES` (an oversized document fails
with `too_large`, an oversized sub-resource is dropped); Playwright buffers a body before it can
be measured, and a compressed body is decompressed first, so the cap is not a memory bound
against a hostile server. CORS preflight requests (`OPTIONS`) are sent by the browser without
passing the guard; they carry no body and the page never sees their answer. robots.txt is
checked for the page URL only. Use it for sites you trust to some degree.

### Article text extraction

`invio.sources.extract.extract_text(html, url)` turns the HTML of an article page that was
already fetched into its main text for summaries, without navigation, cookie banners, sidebars
or footers. It never touches the network.

```python
from invio.sources.extract import ExtractionError, extract_text

article = extract_text(html, url)  # max_chars=200_000, min_chars=200, max_input_chars=4_000_000
article.title, article.published_at, article.language, article.truncated
print(article.text)  # one paragraph per line, whitespace collapsed, NFC
```

The text comes from [trafilatura](https://trafilatura.readthedocs.io/) in precision mode. When
it finds nothing (or fails on hostile input), the plain text of `<body>` is used instead, with
the text rules of web page sources. The title is trafilatura's, else `og:title`, else `<title>`;
`published_at` is the publish day at midnight UTC (trafilatura dates have no time); `language`
is the primary subtag (`de` for `de-DE`) from trafilatura, else `<html lang>`. Text longer than
`max_chars` is cut at a word boundary with `truncated=True`; text shorter than `min_chars`
raises `ExtractionError` with `reason == "too_short"` and the redacted URL. HTML longer than
`max_input_chars` raises `reason == "too_large"` before parsing. Extraction is synchronous and
CPU-bound: call it via `asyncio.to_thread` from async code.

## Job files

A job file is a YAML description of one research job: schedule, notification, sources, search,
LLM and limits. See [docs/job.example.yaml](docs/job.example.yaml) for a complete example and
[docs/job.schema.json](docs/job.schema.json) for the JSON Schema (for editor validation).

```python
from invio.config.job import JobConfigError, dump_yaml, load_yaml, write_yaml

try:
    job = load_yaml("docs/job.example.yaml")
except JobConfigError as exc:
    print(exc)  # one line per problem
write_yaml(job, "job.yaml")  # or dump_yaml(job) for a string
```

- Unknown keys are errors at every level. Scalars follow YAML 1.2 style: `time: 17:30`, `on`
  and `2026-10-04` stay strings.
- Comments are not preserved on save, and every field (defaults included) is written.
- `language` is optional: a lower-case ISO 639-1 code (default `en`) that sets the language of
  item summaries, e.g. `language: de`. An unknown code is an error:
  `language: unknown ISO 639-1 language code 'xx'`.
- `write_yaml` replaces the file atomically, keeps an existing file's mode and follows
  symlinks (the link stays, its target is updated).
- `weekday` accepts any capitalisation, but the JSON Schema lists the lowercase values only.
- Cross-field errors (e.g. `schedule: weekday is required ...`) may appear only after the
  field errors of the same section are fixed.

```text
invalid job file job.yaml:
  schedule.timezone: unknown timezone 'Europe/Atlantis'
  notification.to[0]: value is not a valid email address: An email address must have an @-sign.
  sources[2].url: URL scheme should be 'http' or 'https'
  llm.frequncy: Extra inputs are not permitted
```

Regenerate the schema after changing the models (a test fails when it is stale):

```bash
uv run python -m invio.config.job
```

## Database

Invio stores jobs, runs, items, digests, notifications and LLM usage in MariaDB (production) or
SQLite (development and tests) through SQLAlchemy 2.x. Configure the engine in
`INVIO_DATABASE_URL`:

```bash
INVIO_DATABASE_URL=mysql+pymysql://user:pass@host:3306/invio?charset=utf8mb4
INVIO_DATABASE_URL=sqlite:////absolute/path/invio.sqlite   # development only
```

- MariaDB needs `utf8mb4` (emoji). Invio forces `charset=utf8mb4`, strict SQL mode and a UTC
  session on every connection, runs transactions at `READ COMMITTED`, and creates all tables as
  InnoDB / `utf8mb4_unicode_ci`.
- Set the server's `max_allowed_packet` to at least 32M if you store raw content up to 16 MB.
- Timestamps are aware UTC in Python (naive datetimes are rejected) and naive UTC in the database.
- The schema is versioned with Alembic; the migrations ship inside the package.

```bash
uv run invio db upgrade       # apply migrations; prints "database at revision 0003"
```

DDL is not transactional on MariaDB: if an upgrade fails midway, earlier steps stay applied and
may need manual cleanup before retrying. Job names compare exactly on every backend (binary
collation on MariaDB since revision `0002`), so `Digest` and `digest` are two different jobs.

Exit codes: `0` success (also when already up to date), `2` `INVIO_DATABASE_URL` missing or not a
valid URL, or its driver is not installed, `1` any other failure (unreachable server, migration error, unknown revision). The
password never appears in output.

Developers can use Alembic directly (reads `INVIO_DATABASE_URL`):

```bash
uv run alembic downgrade base
uv run alembic revision --autogenerate -m "describe the change"
```

Tests marked `db` (`uv run pytest -m db`) run on in-memory SQLite by default. To run them on
MariaDB, point `INVIO_TEST_DATABASE_URL` at an empty test database (the schema is managed by the
tests, so never use a database you care about):

```bash
INVIO_TEST_DATABASE_URL="mysql+pymysql://root@127.0.0.1:3306/invio_test?charset=utf8mb4" \
  uv run pytest -m db
```

Run them serially against MariaDB (no `pytest -n`): the migration tests downgrade and re-upgrade
the shared test database.

### Job service

`invio.services.jobs.JobService` manages jobs; use it instead of touching SQLAlchemy directly
(records and errors never expose SQLAlchemy types).

```python
from invio.services.jobs import JobService

service = JobService.from_settings()  # needs INVIO_DATABASE_URL
service.import_yaml("docs/job.example.yaml")  # stored as "job.example"; replace=True to overwrite
record = service.create("ai-news", {...})  # mapping or JobConfig; JobConfigError if invalid
service.list(enabled_only=True)  # ordered by name
service.set_enabled("ai-news", False)  # keeps history, clears next_run_at
print(service.export_yaml("ai-news"))  # YAML text; pass a path to write a file
service.delete("ai-news")  # removes the job and its history
```

Errors: `JobNameError`, `JobExistsError`, `JobNotFoundError`, `JobConfigError` (invalid input) and
`StoredJobConfigError` (a stored config no longer validates; `list` skips such jobs and logs a
warning). Disabling a job with a broken stored config still pauses it, then raises
`StoredJobConfigError`. `next_run_at` is computed by `invio.scheduling.next_run.compute_next_run`:

- the configured local time in the job's time zone, stored as UTC and always strictly in the future
- monthly day 29-31 falls back to the last day of shorter months
- a time skipped by a DST jump runs shifted by the gap (02:30 becomes 03:30, or the next local
  day if the gap ends at midnight); a repeated time runs at its first occurrence
- missed runs are not replayed

Record repositories for runs, items, digests, notifications and LLM usage live in
`invio.db.repositories`; they flush but never commit (use `session_scope`).

### Managing jobs

`invio job` manages jobs through `JobService`; every command needs `INVIO_DATABASE_URL`.

| Command | What it does |
|---|---|
| `create [--from-file PATH] [--name NAME]` | Interactive wizard, or create from a job YAML without prompts |
| `list` | Table of all jobs: enabled, frequency, next run, last run status |
| `show NAME` | Status header plus the full YAML |
| `edit NAME` | Edit the YAML in `$VISUAL` / `$EDITOR` (falls back to `vi`, or `notepad` on Windows) |
| `enable NAME` / `disable NAME` | Schedule or pause a job (repeating is a no-op) |
| `delete NAME [--yes/-y]` | Delete a job and its run history (asks first unless `--yes`) |
| `export NAME [-o PATH]` | Write the YAML to stdout or atomically to a file |
| `import FILE [--name NAME] [--replace]` | Store a job file under `--name` or the file stem |

The wizard asks for schedule, recipients, sources, keywords, description, LLM and limits, rejects
invalid answers inline and shows the YAML before saving. Source URLs are checked for reachability
(and, for RSS, for a feed document); a failed check is only a warning you can override. A model
that is not registered for the chosen provider is accepted after a warning: runs fail until
`models.d/<provider>.yaml` lists it. `create --from-file`, `import` and `delete --yes` never
prompt, so they work in scripts without a terminal; the wizard and an unconfirmed `delete` exit 2
without one. `edit` validates the saved text and offers to re-open the editor with your edits;
the job name is never read from the file. Ctrl+C prints `aborted; nothing saved`.

Exit codes: `0` success (including a no-op enable/disable and an unchanged edit); `1` not found,
already exists, invalid name, aborted, declined preview or delete, aborted edit; `2` invalid job
file or stored config, missing setting, or an interactive command without a terminal.

## LLM layer

`invio.llm` is the provider-neutral LLM contract used by pipeline nodes. A provider offers
`complete(system, user, *, model, temperature, max_tokens)` and
`complete_structured(system, user, schema, *, model, temperature)`; both return the result plus
a `Usage` (`input_tokens`, `output_tokens`, `requests`). `resolve` maps a job's `llm` section
and a role (`fast` or `smart`) to a provider and a model id, checked against the model registry:

```python
from invio.llm.factory import resolve

provider, model = resolve(job.llm, "fast")  # LLMConfigError if the model is not registered
text, usage = await provider.complete("system", "user", model=model, temperature=0, max_tokens=500)
score, usage = await provider.complete_structured(
    "system", "user", Score, model=model, temperature=0
)
```

Structured answers are validated against the Pydantic schema; an invalid answer gets exactly one
repair request, then `LLMInvalidOutputError` (`usage.requests == 2` marks a repair). Failures are
typed: `LLMRateLimitError`, `LLMAuthError` (a missing key names the `INVIO_<PROVIDER>_API_KEY`
variable), `LLMUnavailableError` (also a request timeout) and `LLMInvalidOutputError`;
configuration problems raise `LLMConfigError`. Every call logs one `llm.call` line (provider,
model, tokens, `cost_usd`, `duration_ms`, `repaired`) and never logs prompts, answers or keys.

### Mistral

Set `INVIO_MISTRAL_API_KEY` (see `.env.example`) and name `provider: mistral` in a job's `llm`
section. The shipped, version-pinned models are listed in `src/invio/llm/models.d/mistral.yaml`
(no `-latest` aliases). The provider uses the official `mistralai` SDK; all retrying is done by
invio: 429, 5xx and connection failures are retried up to 3 times with exponential backoff
(1, 2, 4 s, +-25 % jitter), a `Retry-After` header is honoured up to 60 s (a longer one fails
at once), and timeouts, authentication failures, other 4xx answers and requests that cannot be
sent (bad URL, unsupported protocol) are not retried. Every failure, including an undecodable
or redirect-looping response, maps to the typed errors above; `LLMRateLimitError.retry_after`
carries the wait the provider asked for, and the new `LLMInvalidRequestError` (with `status`)
means the provider rejected the request itself. Each retry logs one `llm.retry` line.
Structured requests send the schema in strict mode (`additionalProperties: false` on every
object).

The HTTP client is created per event loop. Call `await provider.aclose()` before the loop ends
to close its connections (the CLI does this); the provider stays usable afterwards.

Check a provider setup with one tiny request:

```
invio llm test mistral [--model MODEL]
```

It prints `ok provider=... model=... input_tokens=... output_tokens=... duration_ms=...` and
exits 0; exit 1 means the provider call failed (`Error: <ErrorType>: ...`), exit 2 a
configuration problem (unknown provider, missing key, model not in the registry). The default
model is the provider's cheapest registered one.

The test suite never touches the network. Optional live tests (plain and structured call)
use the real API when `INVIO_MISTRAL_API_KEY` is set: `uv run pytest -m live`. Without a `-m`
expression that names `live`, they are skipped.

Models and prices live in `src/invio/llm/models.d/<provider>.yaml` (USD per 1M tokens):

```yaml
schema_version: 1
provider: <provider>   # must equal the file name
models:
  <model-id>:
    input_price_per_mtok: 0.5
    output_price_per_mtok: 1.5
    context_window: 128000
```

`default_registry().cost(model, usage)` returns the cost as a `Decimal` with 6 decimals (or
`None` for unknown models). Tests use `invio.llm.fake.FakeProvider`, a scripted provider that
records requests (replies, `FakeDelay`, or `LLMError` instances).

To add a provider, create one module `src/invio/llm/<name>.py` whose class is decorated with
`@register_provider("<name>")` and has a `from_settings(settings)` classmethod (use
`require_api_key` and `settings.llm_timeout_seconds`; wrap each request in `with_timeout` and
use `structured_with_repair`), plus one `models.d/<name>.yaml` file. Modules are discovered
automatically; no shared file changes. A provider beyond the five in the job schema also needs
the `LLMProvider` enum in `invio.config.job` extended.

## Notifications

`invio.notify.deliver_digest(session_factory, digest_id, settings=...)` e-mails a stored digest
to the job's `notification.to` recipients. Each distinct address gets its own mail
(`multipart/alternative`: plain text and HTML) and its own row in `notifications`. In the
`notification.subject` template `{job_name}` and `{date}` (the run date in the job's time zone,
`YYYY-MM-DD`) are replaced; other braces stay and line breaks become spaces. The HTML part is
the digest Markdown rendered and sanitized against an allowlist (no scripts, images, styles or
unsafe links). An empty digest sends nothing unless `notification.send_if_empty` is `true`,
in which case the mail says that no new items were found.

Delivery never raises. Each row ends `sent`, `failed` (with a scrubbed error text, never the
SMTP credentials) or `skipped` after the fifth failed attempt. `run_status_after_delivery`
turns a planned `succeeded` run into `partial` when any notification failed.

```bash
uv run invio notify retry
```

re-sends every `failed` notification, and every `pending` one whose last attempt is more than
an hour old (a crashed process), from the stored digest and payload, so the mail matches the
first attempt. Output is one line per notification (`sent`, `failed` or `given up`) and the
summary `retried N, sent S, failed F, given up G`, or `nothing to retry`. Exit codes: `0` nothing
failed, `1` a notification failed or was given up, `2` configuration error (database URL, SMTP
host or sender). A crashed first attempt may have reached the recipient before the row was
marked, so recovering a stale `pending` row can send a duplicate mail.

## Layout

```
src/invio/
  cli/          Typer app (main.py) and auto-discovered commands/ (db.py, job.py, notify.py);
                wizard.py, prompts.py (prompt seam), source_check.py, editor.py
  config/       settings, job.py (job file models + YAML load/save)
  domain.py     shared records, status enums, url_hash (stdlib only)
  db/           models, engine/session helpers, repositories.py, migrations/ (Alembic)
  services/     jobs.py (JobService: job CRUD, YAML import/export)
  graph/        LangGraph pipelines
  sources/      source adapters (rss.py, web.py; browser.py is the only Playwright user, loaded
                lazily); http.py (SafeHttpClient) with netguard, robots, ratelimit;
                extract.py (article text); text.py, urls.py
  llm/          base, registry, factory, fake, models.d/ (LLM layer)
  notify/       e-mail notifier: payload, render (Markdown, sanitizer, MIME), email (SMTP, retry)
  scheduling/   next-run calculation (next_run.py)
alembic.ini     developer entry point for `uv run alembic ...`
tests/
```
