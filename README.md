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
must be > 0) bounds every single LLM request; `INVIO_MAX_PARALLEL_ITEMS` and
`INVIO_RUN_LOCK_SECONDS` configure job runs (see "Running a job"):

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

### YouTube sources

`youtube_channel` and `youtube_playlist` list the latest videos with
[yt-dlp](https://github.com/yt-dlp/yt-dlp) used as a library, in metadata-only mode: the flat
listing never downloads media and never fetches a transcript (that is #29). Each video becomes a
`video` item with the canonical `https://www.youtube.com/watch?v=<id>` URL.

```yaml
sources:
  - type: youtube_channel
    channel_id: "@somechannel"   # a channel id (UC...), an @handle or a youtube.com channel URL
    max_age_days: 14             # optional, >= 1; default: no age limit
    max_items: 20                # optional, 1..200; default 20
  - type: youtube_playlist
    playlist_id: PLxxxxxxxxxxxx  # a playlist id or a youtube.com URL with ?list=
```

* Only `https` URLs on `youtube.com`, `www.`, `m.` and `music.youtube.com` are accepted. invio
  builds the URL it gives to yt-dlp from the validated id or handle, and restricts yt-dlp to its
  YouTube extractors. This source does not go through the safe HTTP client (yt-dlp has its own
  network stack), so the SSRF guard, robots.txt and per-host rate limit do not apply to it.
* Order: videos with a date newest first, then videos without one in listing order, cut to
  `max_items`.
* `max_age_days` only applies when yt-dlp supplies a date. Flat listings often carry no upload
  date; such videos are kept (`max_age_days` cannot judge them) and no per-video request is made
  to find out. A listing without any dates is therefore just its first `max_items` entries.
* `max_items` also caps the listing itself (yt-dlp's `playlistend`), *before* the newest-first
  sort. A channel's `/videos` tab is listed newest first, so that is the latest videos. A
  playlist is listed in its own order: one kept oldest first returns its first (oldest)
  `max_items` entries, not the latest ones. Raise `max_items` for such playlists.
* A channel URL with a tab (`/shorts`, `/streams`, ...) is accepted, but the `/videos` tab is
  listed.
* Entries that are private, deleted, members-only, premium-only or need a login (by title
  placeholder or yt-dlp's `availability`), or that lack an id or title, are skipped.
* `INVIO_YOUTUBE_TIMEOUT_SECONDS` (default 60) is the limit for one listing. It is best effort:
  a timed-out listing fails the source at once, but its worker thread runs on until yt-dlp's own
  per-request socket timeout (at most 20 s) ends it; at most four such threads exist. The
  process waits for such a thread when it exits, so shutdown can be delayed by that long.
  `INVIO_YOUTUBE_PROXY` (a secret; never logged) routes the requests through a proxy.
  `INVIO_YOUTUBE_COOKIES_FILE` points to a cookies file in Netscape format, which helps against
  "confirm you're not a bot" blocks on server IPs. The file is only read: yt-dlp gets a private
  copy for each listing (it writes cookies back on exit), so the file may be read-only and
  refreshed cookies are not kept. A configured file that is not a readable file fails the source
  with `cookies_unavailable`. An empty value for either variable counts as unset.
* Failures (timeout, connection error, HTTP 429/403/404, block page) fail only that source and
  carry a short reason code, never yt-dlp's message. 429 and network errors are retried like
  other transient source errors.
* You are responsible for complying with YouTube's terms of use for your usage.

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
- `archive` is optional: `enabled` (default `false`) keeps a static HTML copy of each non-empty
  digest, `base_url` (`http`/`https`, no query or fragment, trailing `/` removed) only feeds
  the link in the mail. See [Digest archive](#digest-archive).
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
file or stored config, missing or unusable setting (database URL, model registry), or an
interactive command without a terminal.

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

### OpenAI

Set `INVIO_OPENAI_API_KEY` (see `.env.example`) and name `provider: openai` in a job's `llm`
section. The shipped, dated model snapshots (no aliases, non-reasoning models only, so
`temperature` is accepted) are listed in `src/invio/llm/models.d/openai.yaml`. The provider uses
the official `openai` SDK on the Responses API; every call is one stateless request
(`store: false`). Retries, typed errors, messages and the `llm.retry` log line are the same as
for Mistral (shared code in `invio.llm.http_retry`), with one difference: a 429 with the code
`insufficient_quota` (billing exhausted) raises `LLMRateLimitError` without retrying. Refused,
empty or cut-off (incomplete) answers raise `LLMUnavailableError`. Structured requests send a
strict JSON schema (closed objects, every property required). OpenAI rejects output limits
below 16 tokens, so smaller `max_tokens` values are sent as 16.

### Anthropic

Set `INVIO_ANTHROPIC_API_KEY` (see `.env.example`) and name `provider: anthropic` in a job's
`llm` section. The shipped models are listed in `src/invio/llm/models.d/anthropic.yaml`
(`claude-haiku-4-5-20251001` for `fast`, `claude-sonnet-4-6` for `smart`). Only models that
accept both a forced tool choice and a `temperature` are listed, because structured output
forces one tool call and the pipeline always sends a temperature; newer Claude models reject
one of the two. Both listed models are legacy, so check the retirement dates in the YAML header.
The provider uses the official `anthropic` SDK on the Messages API; every call is one stateless
request. Structured requests declare the schema as a single tool and force it, with the model's
registered `max_output_tokens` (every `anthropic` registry entry must define it, or building
the provider fails with `LLMConfigError`). Retries, typed errors, messages and the `llm.retry`
log line are the same as for Mistral and OpenAI (shared code in `invio.llm.http_retry`);
overloaded (529) answers are retried like other 5xx ones, and exhausted credit (402, or the
legacy 400 about the credit balance) raises `LLMQuotaError` without retrying. Refused, empty or
cut-off (`max_tokens`) answers raise `LLMUnavailableError`. Only the `x-api-key` credential is
sent, to `https://api.anthropic.com`: `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` are
ignored.

### Google

Set `INVIO_GOOGLE_API_KEY` (see `.env.example`) and name `provider: google` in a job's `llm`
section. The shipped models are listed in `src/invio/llm/models.d/google.yaml`
(`gemini-3.5-flash-lite` for `fast`, `gemini-3.8-flash` for `smart`); the header of the file
notes that `gemini-3.8-flash` gets more expensive on 2027-01-01. The provider uses the official
`google-genai` SDK on the Gemini Developer API (`generateContent`); every call is one stateless
request. Gemini 3 models always think, so every `google` registry entry must define
`thinking_level` (sent with every request) and `thinking_allowance_tokens` (added to the
`max_tokens` of a free-text call, because thought tokens count against the output limit), or
building the provider fails with `LLMConfigError`. Thinking tokens are billed and reported as
output tokens. With `keep_default_temperature: true` (both shipped models) the request sends no
temperature, as Google recommends for Gemini 3, so answers are not deterministic. Structured
requests send a converted JSON schema (`$defs` inlined, unsupported value constraints dropped; the
Pydantic model still validates the answer, with one repair attempt). A schema using a keyword
whose loss would widen it (`not`, `if`/`then`/`else`, a multi-branch `allOf`, ...) raises
`LLMConfigError` before any request. A blocked prompt or an answer the
service stopped for a policy reason (safety, recitation, ...) raises `LLMInvalidOutputError`
naming the reason, without retry or repair; cut-off (`MAX_TOKENS`) and empty answers raise
`LLMUnavailableError`. Retries, typed errors and the `llm.retry` log line are the shared ones
(`invio.llm.http_retry`); the `RetryInfo` delay of a 429 is the wait hint, and an exhausted daily
quota raises `LLMQuotaError` without retrying. Only the Gemini Developer API is used, with the
key as the `x-goog-api-key` header: `GOOGLE_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_GEMINI_BASE_URL`
and `GOOGLE_GENAI_USE_VERTEXAI` are ignored.

### Common provider notes

The HTTP client is created per event loop. Call `await provider.aclose()` before the loop ends
to close its connections (the CLI does this); the provider stays usable afterwards.

Check a provider setup with one tiny request:

```
invio llm test mistral [--model MODEL]
invio llm test openai [--model MODEL]
invio llm test anthropic [--model MODEL]
invio llm test google [--model MODEL]
```

It prints `ok provider=... model=... input_tokens=... output_tokens=... duration_ms=...` and
exits 0; exit 1 means the provider call failed (`Error: <ErrorType>: ...`), exit 2 a
configuration problem (unknown provider, missing key, model not in the registry). The default
model is the provider's cheapest registered one.

The test suite never touches the network. Optional live tests (plain and structured call)
use the real API of each provider whose key is set (`INVIO_MISTRAL_API_KEY`,
`INVIO_OPENAI_API_KEY`, `INVIO_ANTHROPIC_API_KEY`, `INVIO_GOOGLE_API_KEY`): `uv run pytest -m live`. Without a `-m`
expression that names `live`, they are skipped. Every provider must pass the shared contract suite
`tests/test_llm_provider_contract.py`; a new provider adds one harness there.

Models and prices live in `src/invio/llm/models.d/<provider>.yaml` (USD per 1M tokens):

```yaml
schema_version: 1
provider: <provider>   # must equal the file name
models:
  <model-id>:
    input_price_per_mtok: 0.5
    output_price_per_mtok: 1.5
    context_window: 128000
    max_output_tokens: 8192   # optional; required for anthropic models
    thinking_level: low       # optional (minimal|low|medium|high); required for google models
    thinking_allowance_tokens: 2048   # optional; required for google models
    keep_default_temperature: true    # optional, default false; send no temperature
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

### Digest synthesis

`invio.graph.nodes.synthesize.synthesize_digest(entries, ctx)` turns the run's summarized items
into the Markdown digest with one `smart` call per run (`entries_from_items` builds the entries
from stored items). The model writes an intro and `##` theme sections in the job's `language`;
invio appends "More items" for entries the answer did not link, and "Worth a closer look" with
the top 3 by relevance. Only the input URLs survive: every other URL, image and raw HTML link in
the answer is removed (an unknown link keeps its text). No items means an empty digest and no
LLM call; the notifier's `send_if_empty` decides whether anything is sent. If the model request
fails or its answer is unusable, a plain fallback digest listing all items is built and
`run_status_after_synthesis` turns a `succeeded` run into `partial`; credential and
configuration errors propagate. Headings exist in English and German; other languages get
English headings (the model still writes in the job's language).
### Runs, statistics and the token budget

A run is saved all-or-nothing by `invio.graph.nodes.persist.finalize_run`: the item updates of
the LLM nodes, their `llm_usage` rows, the digest and the run's final status and statistics are
committed in one transaction. If the save fails, everything is rolled back, the usage rows are
written again from the run's `BudgetTracker` ledger, and the run is `failed` with
`finished_at` and a sanitized `error` (error class and a fixed phrase, never SQL, document text
or provider messages). `record_failed_run` does the same for a stage that raised before the
save. If that recovery transaction fails too, the error is re-raised and the run stays
`running`.

Run status (`decide_status`): `failed` when every attempted item failed and the budget was not
exceeded; `partial` when the budget stopped the run or any item failed; otherwise `succeeded`
(also with no items). `runs.stats` holds the stage counts, tokens, estimated cost and budget
figures, see
[`specs/013-gh-issue-19/contracts/run-stats.md`](specs/013-gh-issue-19/contracts/run-stats.md).
Version 1 gained additive keys with #22 (readers ignore unknown keys and tolerate missing
ones): `llm_calls_unpriced` (calls without a price), `processed` (items that reached an outcome),
`errors` (the per-run error list, at most 100 entries, written by `finalize`) and
`errors_omitted` (only when entries were left out).

`limits.max_llm_tokens_per_run` caps the tokens (input plus output) of the per-item LLM calls.
Before each relevance or summary call the tracker is checked; once `used` is above the limit no
further per-item call starts, the batch returns what is finished, and items without a final
state are released (attempt undone, `run_id` cleared, status `new`; an untouched `failed` retry
stays `failed`), so the next run picks them up. The digest call passes `per_item=False`, so it
always runs and is counted. Limits and known behaviour:

- The check happens before a call, so the last call and the digest call can push `tokens` above
  `budget_limit`; `budget_exceeded` is only set when a check failed, so it can be `false` while
  `tokens > budget_limit`. `over_budget` in `runs.stats` shows that case (`tokens > budget_limit`).
- Released items are rated again by the next run, so their relevance tokens are spent twice.
- A killed process leaves the run `running` and only the `llm.call` log lines as usage record.
- The orchestrator (`run_job`, see "Running a job") commits the run start and `mark_taken`
  before the LLM stages, commits no `llm_usage` row before `finalize_run` (the recovery replays
  the whole ledger), and calls `record_failed_run` when a stage raises.
- A digest may only name items the run summarized; any other id fails the save.
- A run `failed` because every attempted item failed gets `runs.error`
  `"all attempted items failed"`. A commit that fails after the database applied it is detected
  by the recovery (the run is no longer `running`): nothing is replayed and the stored status is
  kept.

### Running a job

`invio.pipeline.run.run_job(job_id, dry_run=False)` runs one job end to end. It returns a
`RunResult` (`job_id`, `run_id`, final `status`, `dry_run`, `digest`, `stats`, `errors`,
`notifications_sent`, `notifications_failed`, `error`, `started_at`, `finished_at`) for every
run that started, whatever its status. `run_job_by_name(name, ...)` resolves a job name first
(`JobNotFoundError` for an unknown name). Both take `max_items` and an `observer`, see below.
It raises, before any run row is created or lock is changed, `JobNotFoundError` (unknown id),
`JobDisabledError` and `JobBusyError` (another run holds an unexpired lock; `locked_until` says
until when), `JobNotDueError` (only with `due_by`, see `run-due`) and `ValueError` for
`concurrency < 1` or `max_items < 1`. `invio run-due` runs every due job, see below.

```python
import asyncio

from invio.pipeline.run import run_job

result = asyncio.run(run_job(1, dry_run=True))
print(result.status, result.digest)
```

Stages, as one LangGraph graph per run: `load_job` (validate the stored config, bind the
provider) -> `fetch_sources` -> `deduplicate` -> `keyword_prefilter` -> one `process_item` per
item (page extraction, relevance, summary) -> `join` -> `synthesize_digest` -> `persist` ->
`notify` -> `finalize`. `finalize` is the only way out: it sets `next_run_at` and releases the
job lock whatever happened, also after a stage exception or a cancellation.

Final status, in order: a stage raised -> `failed` (`runs.error` is `"<Class>: run failed"`, the
LLM facts for an `LLMError`, or the failing field paths for an invalid job config); every source
with an adapter failed -> `failed` (`"all sources failed"`); every attempted item failed ->
`failed` (`"all attempted items failed"`); any source or item failed, or the token budget was
exceeded -> `partial`; otherwise `succeeded`. A fallback digest (the digest call failed) and a
failed or skipped recipient lower `succeeded` to `partial`. Credential and configuration errors
(`LLMAuthError`, `LLMConfigError`, a missing API key) fail the whole run instead of every item.

Retries: a source fetch, an item page fetch and each provider request are retried on transient
errors (`LLMRateLimitError`, `LLMUnavailableError`, `FetchError` except `BlockedError`,
`TooLargeError`, `RenderUnavailableError`) with exponential backoff: at most 3 attempts, 1 s
initial wait, factor 2, at most 30 s, plus up to 10 % jitter; `retry_after` of a rate limit is
used when larger. Everything else is attempted once. Retries wrap the single call (not the
node), so a node never repeats side effects.

Settings: `INVIO_MAX_PARALLEL_ITEMS` (default `4`, at least `1`) bounds the items processed at
once (and the sources fetched at once); `INVIO_RUN_LOCK_SECONDS` (default `7200`, at least `60`)
is how long a run holds its job lock.

Dry run: the run produces the digest in `RunResult.digest` only. Nothing is sent, `next_run_at`
stays as it is, and no item, digest or `llm_usage` row is kept: the work is rolled back and
only the run row remains, with `stats["dry_run"] = true` and the token figures of the calls
that were made.

#### `invio job run`

```
invio job run <name> [--dry-run] [--max-items N] [--verbose|-v]
```

Runs the job once, now. Progress (one line per stage, or a live panel on a terminal) and notes go
to **stderr**; the digest (`# <title>` plus the Markdown body, verbatim except that control characters and ANSI escapes are stripped; dry runs and `--verbose`
only), the statistics table and the `Tokens: in .. · out .. · total .. · cost ..` line go to
**stdout**, so `invio job run demo --dry-run > digest.md` gives a clean document. Without
`--dry-run` the run is real: it saves its results, mails the digest and reports the
`Notifications sent` / `Notifications failed` counts.

- Exit codes: `0` succeeded, `1` failed or could not start (unknown, disabled or busy job,
  invalid stored config, missing or invalid settings, database error, `--max-items < 1`, Ctrl-C), `2`
  partial (also Click usage errors). Unlike `invio job show`, an invalid job config exits 1 here.
  Every refusal is a single stderr line and creates no run row.
- `--max-items N` caps the items processed in this run at `min(N, limits.max_items_per_run)`;
  it can only lower the cap, a note is printed when `N` is above the job limit, and the stored
  config is never changed. In baseline mode (a job's first run) it still caps the items overall.
- `--verbose` prints one stderr line per item (`item: relevant|irrelevant|failed  <title>`, plus
  the sanitized message for a failure) and prints the digest of real runs too.
- Cost: `$0.0103` when every call has a price, `≥ $0.0050 (2 calls without price)` when some
  have none, `unknown` when none has, `$0.00` without LLM calls.
- Titles and URLs come from the network: control characters and ANSI escapes are stripped, URLs
  are shown without query string and fragment.

#### Run history

`invio run list [--job NAME] [--limit N]` lists runs newest first (id, job, start, duration,
status, found/new/relevant; dry runs are marked `(dry)`). `invio run show <id>` prints one run:
the statistics, the token usage and **every stored error** with its stage, sanitized message and
the item's id, title and URL (or the source position for a source error); a failed delivery
appears as a `notify` entry with its counts. The list is written by
`finalize` into `runs.stats["errors"]`, so it survives later retries and dry runs. A run from
before this feature has no such list: `show` says "item errors are not available for this run".

#### Usage and cost: `invio usage`

```
invio usage [--job NAME] [--since YYYY-MM-DD] [--by provider|model|job|day] [--json]
```

Sums the `llm_usage` rows (one per LLM call) into one row per group (`--by`, default `model`:
`provider/model`; `day` is the UTC day) with calls, input and output tokens and cost, plus a
`Total` row; `No usage.` when nothing matches. `--job` keeps one job's rows (an unknown job
exits 1), `--since` the rows from that day 00:00 UTC on. The cost is computed from the current
model registry (`models.d/*.yaml`) on the summed tokens per group and model, not from the stored
`cost_usd`; each group's cost is rounded to 6 decimals, so totals under different `--by` can
differ by a few millionths of a dollar. A model the registry does not price (for its provider)
is never counted as zero: its tokens count, the cost shows as `≥ $X` (or `unknown` when nothing in the group is priced), and a
warning naming each such model goes to stderr. `--json` prints one JSON document on stdout
(`by`, `job`, `since`, `groups`, `total`, `unpriced_models`; costs are exact decimal strings or
`null`, with `cost_complete` and `unpriced_models` per group). Exit codes: `0` success, `1`
unknown job or database error, `2` invalid option, a missing or unusable `INVIO_DATABASE_URL` or
an unreadable model registry.

Known limitations: the lock has no heartbeat, so a run longer than `INVIO_RUN_LOCK_SECONDS` can
be overtaken by another one; a source type without an adapter would be
skipped with a `source.unsupported` log line (every type has one now); video items use the text of their page until #29.

### Scheduled runs: `invio run-due`

```text
invio run-due [--limit N] [--parallel N]
```

Runs every enabled job whose `next_run_at` has passed, oldest first, and prints one line per due
job plus a summary (`nothing due` when there is none). Meant for a systemd timer.

- `--limit N` starts at most N runnable jobs. Jobs locked by another run are reported as `busy`
  and do not count; jobs left over by the limit are counted in the summary (`deferred`) and run
  on the next invocation.
- `--parallel N` runs up to N jobs at the same time (default 1: strictly sequential). It
  multiplies with `INVIO_MAX_PARALLEL_ITEMS` (items per run), so N jobs can use N times that many
  database connections. On SQLite it is lowered to 1.
- Exit codes: `0` no run failed (including nothing due, busy, skipped and partial runs), `1` a
  run failed, a job raised an unexpected error or the invocation aborted (database error,
  Ctrl-C), `2` invalid option or configuration. Invalid options run nothing.

Overlapping invocations are safe: each job is claimed by one atomic UPDATE that also checks the
job is still due, so of two invocations (or a manual `job run`) exactly one runs it; the other
reports `busy` or `skipped  no longer due`. A lock past its expiry (`INVIO_RUN_LOCK_SECONDS`,
default 7200) is taken over, so a crashed run never blocks a job. A job that missed several slots
runs once and then waits for its next future slot.

**Retry after a failure.** After a failed real run (also a failed `invio job run`) the next run
is `min(regular slot, finish + delay)`, with a delay of 1 h that doubles with every consecutive
failure up to 24 h; a succeeded or partial run resets it. The output marks it `(retry)`. Dry
runs never change the schedule or the streak. A failed job whose schedule cannot be read gets
`finish + delay` alone, so it is not rerun on every invocation.

**Health check.** With `INVIO_HEALTHCHECK_URL` set (treated as a secret and never logged) every
invocation sends exactly one `GET` after the runs: `<url>` on exit 0, `<url>/fail` on exit 1 and
on exit 2 for a configuration error found after the settings were read (e.g. a missing
`INVIO_DATABASE_URL`). Usage errors and settings that cannot be read at all (an invalid value,
including an invalid `INVIO_HEALTHCHECK_URL`) exit 2 without a signal: there is no readable URL. Timeout 3 s, no redirects, no
retry; a failing ping is logged (`healthcheck.failed`) and never changes the exit code.

Example systemd units (not shipped):

```ini
# /etc/systemd/system/invio-run-due.service
[Service]
Type=oneshot
EnvironmentFile=/etc/invio/invio.env
ExecStart=/usr/local/bin/invio run-due

# /etc/systemd/system/invio-run-due.timer
[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
```

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

### Digest archive

With `archive.enabled: true` in the job file, every non-empty digest is also written as a
self-contained HTML page below `INVIO_ARCHIVE_DIR` (default `/var/lib/invio/archive`):

```text
<archive_dir>/index.html                       all jobs
<archive_dir>/<job-slug>/index.html            pages of one job, newest first
<archive_dir>/<job-slug>/<YYYY-MM-DD-HHMM>.html   one digest (UTC minute of the run start)
```

For local runs set `INVIO_ARCHIVE_DIR` to a writable path; a failed archive write is only logged (`archive.failed`) and never fails the run or the mail.

The job name is reduced to a safe slug (`Weekly AI News` becomes `weekly-ai-news`); two names
with the same slug get `-<hash8>` for the second. Pages have no scripts, images or external
files and use the same sanitizer as the mail. Files are written atomically (mode `0644`,
directories `0755`), a page is never overwritten (a second digest in the same minute becomes
`...-2.html`), and no archive failure can fail the run or stop the mail: it is logged as
`archive.failed` and the mail goes out without a link. Hard links must be supported by the
file system. With `archive.base_url` set, the mail footer links to
`<base_url>/<job-slug>/<page>.html`, also when `invio notify retry` re-sends it (as long as the
page exists). Serving the directory is up to a web server, see
[docs/deployment.md](docs/deployment.md#digest-archive).

## Deployment

invio runs unattended on a Debian 12 or 13 server with systemd: a timer starts `invio run-due`
every 15 minutes and another retries failed notifications hourly, both as the locked-down `invio`
account with logs in the journal. The Ansible role in `deploy/ansible` provisions a host in one
run (database, account, env file, uv and Python, code, migration, timers); the same steps by
hand, the update procedure and the troubleshooting notes are in
[docs/deployment.md](docs/deployment.md). The units and the example env file are in `deploy/`.

## Layout

```
src/invio/
  cli/          Typer app (main.py) and auto-discovered commands/ (db.py, job.py, notify.py,
                run.py, usage.py); wizard.py, prompts.py (prompt seam), source_check.py,
                editor.py, progress.py (live/plain run progress), run_output.py and
                usage_output.py (pure formatters)
  config/       settings, job.py (job file models + YAML load/save)
  domain.py     shared records, status enums, url_hash (stdlib only)
  db/           models, engine/session helpers, repositories.py, migrations/ (Alembic)
  services/     jobs.py (JobService: job CRUD, YAML import/export), runs.py (run history),
                usage.py (usage and cost report)
  graph/        LangGraph research graph: state, ports, stages, build; nodes/ (LLM stages)
  pipeline/     run orchestration (run_job) and the production wiring of the graph's ports
  textsafe.py   strips control characters / ANSI from untrusted text (leaf)
  retry.py      call-level retry with exponential backoff (dependency-free leaf)
  sources/      source adapters (rss.py, web.py; browser.py is the only Playwright user, loaded
                lazily); http.py (SafeHttpClient) with netguard, robots, ratelimit;
                extract.py (article text); text.py, urls.py
  llm/          base, registry, factory, fake, models.d/ (LLM layer)
  notify/       e-mail notifier: payload, render (Markdown, sanitizer, MIME), email (SMTP, retry)
  scheduling/   next-run calculation (next_run.py)
alembic.ini     developer entry point for `uv run alembic ...`
deploy/         systemd units (systemd/), env example (env/), checker scripts (scripts/) and the
                Ansible role `invio` with Molecule tests (ansible/)
tests/
```
