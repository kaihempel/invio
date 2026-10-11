# Configuration Reference

**Last Updated:** 2026-10-11

invio is configured in three places:

| Where | What | Reference |
|---|---|---|
| Job file (YAML, one per job, stored in the database) | schedule, recipients, sources, search, LLM roles, limits, archive | [Job file](#job-file) |
| Environment or `.env` (`INVIO_*`) | database, API keys, SMTP, HTTP client, run and transcription limits | [Environment settings](#environment-settings-invio_) |
| Model registry (`src/invio/llm/models.d/*.yaml`) | which model ids exist per provider, prices, context windows | [Model registry](#model-registry) |

Source of truth: `src/invio/config/job.py` (job models), `src/invio/config/settings.py`
(settings), `src/invio/llm/models.d/` (registry). The JSON Schema
[docs/job.schema.json](job.schema.json) is generated from the job models, and
[docs/job.example.yaml](job.example.yaml) is a complete, valid job file.

Regenerate the schema after changing a job model (a test fails when it is stale):

```bash
uv run python -m invio.config.job    # rewrites docs/job.schema.json
```

Check a job file: `invio job import <file>` validates it and saves the job; invalid files are
rejected with the messages shown in [Job file errors](#job-file-errors).

---

## Job file

A job file is a UTF-8 YAML mapping. Unknown keys are errors at every level. Only the fields marked
"required: yes" must be present. Comments are not kept when a job is saved, and every field
(defaults included) is written on save.

| Field | Type | Required | Default | Constraints | Example |
|---|---|---|---|---|---|
| `schema_version` | integer | no | `1` | `>= 1` and `<= 1` (the only supported version) | `schema_version: 1` |
| `language` | string | no | `en` | lower-case ISO 639-1 code (`^[a-z]{2}$`, must be a known code) | `language: de` |
| `schedule` | object | yes | | see [`schedule`](#schedule) | |
| `notification` | object | yes | | see [`notification`](#notification) | |
| `sources` | list | yes | | at least one entry; see [Sources](#sources) | |
| `search` | object | yes | | see [`search`](#search) | |
| `llm` | object | yes | | see [`llm`](#llm) | |
| `limits` | object | no | defaults of [`limits`](#limits) | see [`limits`](#limits) | |
| `archive` | object | no | `enabled: false` | see [`archive`](#archive) | |

Minimal skeleton (every other field is optional or shown in the sections below):

```yaml
schedule:
  frequency: daily
  time: 07:30
  timezone: Europe/Berlin
notification:
  to: [research@example.com]
  subject: "invio: digest {date}"
sources:
  - type: rss
    url: https://example.com/feed.xml
search:
  semantic_description: New tools and papers about LLM agent frameworks.
llm:
  provider: openai
  models:
    fast: gpt-4.1-mini-2025-04-14
    smart: gpt-4.1-2025-04-14
```

### `schedule`

When the job runs. The fields depend on `frequency`.

| Field | Type | Required | Default | Constraints | Example |
|---|---|---|---|---|---|
| `frequency` | enum | yes | | `daily`, `weekly` or `monthly` | `frequency: weekly` |
| `time` | string | yes | | `HH:MM`, 24 hours (`00:00` to `23:59`); read as a string even unquoted | `time: 07:30` |
| `weekday` | enum | if `weekly` | `null` | `monday` ... `sunday`; any capitalisation is accepted, the schema lists lower case. Forbidden unless `weekly` | `weekday: monday` |
| `day_of_month` | integer | if `monthly` | `null` | `1` to `31`. Forbidden unless `monthly`. Months that are too short run on their last day | `day_of_month: 15` |
| `timezone` | string | yes | | exact IANA name, case-sensitive (`Europe/Berlin`, not `europe/berlin`) | `timezone: Europe/Berlin` |

```yaml
schedule:
  frequency: monthly
  day_of_month: 31        # runs on the 28th/29th/30th in shorter months
  time: 06:00
  timezone: UTC
```

Errors: `schedule: weekday is required when frequency is 'weekly'`,
`schedule: day_of_month is only allowed when frequency is 'monthly'`.

### `notification`

Who gets the digest and how it is titled. Delivery is described in the
[README (Notifications)](../README.md#notifications).

| Field | Type | Required | Default | Constraints | Example |
|---|---|---|---|---|---|
| `to` | list of e-mail addresses | yes | | at least one valid address; each distinct address gets its own mail | `to: [a@example.com, b@example.com]` |
| `subject` | string | yes | | non-empty. `{job_name}` and `{date}` (run date in the job's time zone, `YYYY-MM-DD`) are replaced; other braces stay | `subject: "invio: {job_name} {date}"` |
| `send_if_empty` | boolean | no | `false` | `true` sends a mail saying that nothing new was found, for empty digests | `send_if_empty: true` |

### `sources`

A list of source entries. Each entry is selected by its `type` (a discriminator). All types share
these fields:

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `type` | enum | yes | | `rss`, `web`, `sitemap`, `youtube_channel` or `youtube_playlist` |
| `name` | string | no | `null` | Free-text label for the source, non-empty if set. The run pipeline does not read it |
| `enabled` | boolean | no | `true` | `false` keeps the entry in the file but skips it in runs |

RSS, sitemap and web fetches go through the HTTP client configured in the [Environment settings](#environment-settings-invio_)
(SSRF guard, robots.txt, rate limit, size and time limits); YouTube access uses yt-dlp with the `INVIO_YOUTUBE_*` settings; see [docs/security.md](security.md)
and the README section "Fetching sources safely".

#### `rss`

An RSS or Atom feed. Entries older than `max_age_days` are ignored; undated entries are kept.

| Field | Type | Required | Default | Constraints | Example |
|---|---|---|---|---|---|
| `url` | URL | yes | | absolute `http` or `https` URL of the feed | `url: https://example.com/feed.xml` |
| `max_age_days` | integer | no | `null` | `>= 1` | `max_age_days: 30` |

```yaml
- type: rss
  url: https://example.com/feed.xml
  name: Example blog
  max_age_days: 30
```

#### `web`

A web page tracked for changes (`mode: page`) or an index page whose article links are collected
(`mode: links`).

| Field | Type | Required | Default | Constraints | Example |
|---|---|---|---|---|---|
| `url` | URL | yes | | absolute `http` or `https` URL | `url: https://example.org/news` |
| `selector` | CSS selector | no | `null` | non-empty, must parse as CSS. Limits the tracked text (`page`) or the collected links (`links`) to the matching elements; without it the whole `<body>` counts | `selector: "main"` |
| `mode` | enum | no | `page` | `page` or `links` | `mode: links` |
| `url_pattern` | regular expression | no | `null` | non-empty, valid regex, only with `mode: links`. Searched in the absolute link URL, any host. Without it only links on the page's own host are kept | `url_pattern: "/news/\\d{4}/"` |
| `render` | enum | no | `static` | `static` (plain HTTP) or `js` (headless browser first) | `render: js` |
| `wait_for` | CSS selector | no | `null` | non-empty, plain CSS only, only with `render: js`. The browser waits for this element | `wait_for: "article"` |

`render: js` needs the optional browser extra and Chromium:
`uv sync --extra render` and `uv run playwright install chromium`.

```yaml
- type: web
  url: https://example.org/news
  mode: links
  selector: "main"
  url_pattern: "/news/"
  render: js
  wait_for: "main article"
```

Errors: `url_pattern: only allowed with mode: links`, `wait_for: only allowed with render: js`,
`invalid CSS selector`, `invalid regular expression: ...`.

#### `sitemap`

A sitemap (`urlset`, or a `sitemapindex` one level deep). Its URLs are candidates for the job.
Entries whose `lastmod` is older than `max_age_days` are ignored.

| Field | Type | Required | Default | Constraints | Example |
|---|---|---|---|---|---|
| `url` | URL | yes | | absolute `http` or `https` URL of the sitemap | `url: https://example.net/sitemap.xml` |
| `url_pattern` | regular expression | no | `null` | non-empty, valid regex; searched in the absolute URL | `url_pattern: "/blog/"` |
| `max_age_days` | integer | no | `null` | `>= 1` | `max_age_days: 14` |

```yaml
- type: sitemap
  url: https://example.net/sitemap.xml
  url_pattern: "/blog/"
  max_age_days: 14
```

#### `youtube_channel`

A YouTube channel; its latest videos are collected.

| Field | Type | Required | Default | Constraints | Example |
|---|---|---|---|---|---|
| `channel_id` | string | yes | | a channel id (`UC...`), an `@handle`, or an https `youtube.com` channel URL (`/channel/<id>` or `/@<handle>`, optionally followed by one tab such as `/videos`). No credentials or port in URLs | `channel_id: "@somechannel"` |
| `max_items` | integer | no | `20` | `1` to `200`. Caps the listing (yt-dlp `playlistend`) before the result is sorted newest first | `max_items: 10` |
| `max_age_days` | integer | no | `null` | `>= 1`. Drops videos older than this, only when yt-dlp supplies a date; undated videos are kept | `max_age_days: 14` |

```yaml
- type: youtube_channel
  channel_id: "https://www.youtube.com/@somechannel/videos"
  max_items: 10
  max_age_days: 14
```

#### `youtube_playlist`

A YouTube playlist; its first `max_items` entries are collected.

| Field | Type | Required | Default | Constraints | Example |
|---|---|---|---|---|---|
| `playlist_id` | string | yes | | a playlist id, or an https `youtube.com` URL with exactly one `list=<id>` parameter | `playlist_id: PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| `max_items` | integer | no | `20` | `1` to `200`. Applied to the listing (yt-dlp `playlistend`) before sorting newest first. A playlist listed oldest first therefore yields its oldest `max_items` entries | `max_items: 50` |
| `max_age_days` | integer | no | `null` | `>= 1`, same rule as `youtube_channel` | `max_age_days: 90` |

Video transcripts and YouTube access settings are in the README sections "YouTube sources" and
"Video transcripts", and the `INVIO_YOUTUBE_*` and `INVIO_WHISPER_*` settings below.

### `search`

What the job looks for.

| Field | Type | Required | Default | Constraints | Example |
|---|---|---|---|---|---|
| `keywords` | object | no | empty lists | `any`, `all`, `exclude`, see below | |
| `semantic_description` | string | yes | | non-empty. Sent to the model to judge relevance | `semantic_description: "New agent frameworks."` |
| `min_relevance` | number | no | `0.6` | `0` to `1` (inclusive) | `min_relevance: 0.6` |

`keywords` is a pre-filter that runs before any LLM call. Matching is case-insensitive (Unicode
case folding) and matches whole words (a phrase matches with any whitespace between its words):

| Field | Type | Default | Rule |
|---|---|---|---|
| `any` | list of strings | `[]` | at least one term must occur, when the list is non-empty |
| `all` | list of strings | `[]` | every term must occur |
| `exclude` | list of strings | `[]` | any hit rejects the item |

Terms are stripped; each term is non-empty. With `any` and `all` both empty, every item passes
unless excluded. Details: the docstring of `src/invio/graph/nodes/keyword_filter.py`.

```yaml
search:
  keywords:
    any: [LangGraph, agent]
    all: []
    exclude: [crypto]
  semantic_description: >-
    New tools, papers and production experience reports about LLM agent frameworks.
  min_relevance: 0.6
```

### `llm`

Which provider and models the job uses. A job has two model roles:

- `fast`: cheap, one call per item (relevance and summary).
- `smart`: merging and the digest.

| Field | Type | Required | Default | Constraints | Example |
|---|---|---|---|---|---|
| `provider` | enum | yes | | `mistral`, `openai`, `anthropic`, `google` or `ollama`. The default provider of both roles | `provider: openai` |
| `models` | object | yes | | `fast` and `smart` are both required; each is a model id (shorthand) or a mapping (long form), see below | |
| `fallback_provider` | enum | no | `null` | one of the providers above, different from `provider` when a role uses `provider`. Used when the primary provider is down or rate-limited | `fallback_provider: anthropic` |
| `fallback_models` | object | if `fallback_provider` | `null` | `fast` and `smart`: model ids of `fallback_provider`. Requires `fallback_provider` | |

Model ids must be registered for their provider in the [model registry](#model-registry). A job
file can name an id that is not registered; the job then fails at the start of its run with
`model '<id>' is not registered for LLM provider '<provider>'`. `job import` does not check ids.

Shorthand, both roles on `llm.provider`:

```yaml
llm:
  provider: openai
  models:
    fast: gpt-4.1-mini-2025-04-14
    smart: gpt-4.1-2025-04-14
  fallback_provider: anthropic
  fallback_models:
    fast: claude-haiku-4-5-20251001
    smart: claude-sonnet-4-6
```

Long form, per role (`models.<role>` as a mapping):

| Field | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `provider` | enum | no | `llm.provider` | the role's own provider |
| `model` | string | yes | | non-empty model id of that provider |
| `fallback` | object | no | job-level fallback for the role | `provider` (enum) and `model` (string); `provider` must differ from the role's provider |

```yaml
llm:
  provider: openai
  models:
    fast: gpt-4.1-mini-2025-04-14
    smart:
      provider: anthropic
      model: claude-sonnet-4-6
      fallback: {provider: openai, model: gpt-4.1-2025-04-14}
```

Fallback resolution per role: the role's own `fallback` if set, else `fallback_provider` with
`fallback_models.<role>`, else none. `fallback_provider` alone (without `fallback_models`)
validates but configures no fallback.

Credentials: a run binds every provider it uses, including the fallback providers, before any
item is processed. Each needs its key (`INVIO_<PROVIDER>_API_KEY`, or `INVIO_OLLAMA_BASE_URL` for
`ollama`). A missing key fails the whole run with `LLMAuthError`. Check each provider with
`uv run invio llm test <provider>`.

Errors: `fallback_provider must differ from provider`, `fallback_models requires fallback_provider`,
`the fallback provider of role 'smart' must differ from its provider`.

### `limits`

Per-run caps. Every field is optional and has a default.

| Field | Type | Default | Constraints | Meaning |
|---|---|---|---|---|
| `max_items_per_source` | integer | `20` | `>= 1` | Items kept from each source per run (the newest ones) |
| `max_items_per_run` | integer | `100` | `>= 1` | Items processed per run, over all sources; `invio job run --max-items` can only lower it |
| `baseline_items` | integer | `10` | `>= 1` | Per source, on the first run only (no earlier succeeded or partial run): the newest N new items are kept, the older ones are skipped |
| `max_items_in_notification` | integer | `20` | `>= 1` | Validated and stored, but no code in the run path reads it yet: it has no effect today |
| `max_llm_tokens_per_run` | integer | `200000` | `>= 1` | Token budget of the run's LLM calls. When it is exceeded the run ends `partial` (README, "Runs, statistics and the token budget") |

```yaml
limits:
  max_items_per_source: 20
  max_items_per_run: 100
  baseline_items: 10
  max_items_in_notification: 20
  max_llm_tokens_per_run: 200000
```

### `archive`

Optional static HTML copy of each non-empty digest. The directory is set by
`INVIO_ARCHIVE_DIR`; see the README section "Digest archive".

| Field | Type | Default | Constraints | Meaning |
|---|---|---|---|---|
| `enabled` | boolean | `false` | | Write a page per non-empty digest |
| `base_url` | string | `null` | `http` or `https` URL with a host; no credentials, query, fragment or whitespace; trailing `/` is removed | Only used for the link in the mail |

```yaml
archive:
  enabled: true
  base_url: https://digests.example.org/invio
```

### Job file errors

Problems are reported one per line, with the field path. Field errors of a section are shown
before cross-field errors of the same section:

```text
invalid job file job.yaml:
  schedule.timezone: unknown timezone 'Europe/Atlantis'
  notification.to[0]: value is not a valid email address: An email address must have an @-sign.
  sources[2].url: URL scheme should be 'http' or 'https'
  llm.frequncy: Extra inputs are not permitted
```

---

## Environment settings (`INVIO_*`)

Read from the process environment, then from `.env` in the working directory (real environment
variables win). Set `INVIO_ENV_FILE` to read the file from another path (for example under
systemd, where the working directory differs); a missing file is then an error. Names are
case-insensitive in the environment. Secrets are never shown in `repr`, `str` or logs.

Provider keys and SMTP credentials are optional at startup. A command fails only when it needs
the value (`Configuration error: ... is not set`, exit code `2`).

A template with every variable is in [.env.example](../.env.example). A variable name that
matches no setting (usually a typo) is logged as a warning.

### Database

| Variable | Default | Meaning |
|---|---|---|
| `INVIO_DATABASE_URL` | unset | SQLAlchemy URL. Production: `mysql+pymysql://user:pass@host:3306/invio?charset=utf8mb4`. Local development: `sqlite:///invio.db` (relative to the working directory). Required by every command that touches jobs or runs. Secret (may contain a password) |
| `INVIO_TEST_DATABASE_URL` | unset | Database for `pytest -m db` and `-m e2e`. Unset or any `sqlite` URL means in-memory SQLite; a MariaDB URL must point at an empty test database. Secret |

### LLM providers

| Variable | Default | Meaning |
|---|---|---|
| `INVIO_MISTRAL_API_KEY` | unset | Mistral API key. Secret |
| `INVIO_OPENAI_API_KEY` | unset | OpenAI API key. Secret |
| `INVIO_ANTHROPIC_API_KEY` | unset | Anthropic API key. Secret |
| `INVIO_GOOGLE_API_KEY` | unset | Google (Gemini) API key. Secret |
| `INVIO_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL (no key) |
| `INVIO_LLM_TIMEOUT_SECONDS` | `60` | Timeout of one LLM request, in seconds. Must be `> 0` |

### Run orchestration

| Variable | Default | Meaning |
|---|---|---|
| `INVIO_MAX_PARALLEL_ITEMS` | `4` | Items (and sources) processed at once in one run. Must be `>= 1`. `invio run-due --parallel` multiplies this with the number of jobs |
| `INVIO_RUN_LOCK_SECONDS` | `7200` | How long a run holds its job lock. A lock past this time is taken over, so a crashed run never blocks a job. Must be `>= 60`. There is no unlock command and no heartbeat: a run longer than this lets a second run start |

### E-mail (SMTP)

| Variable | Default | Meaning |
|---|---|---|
| `INVIO_SMTP_HOST` | unset | SMTP server host. Required to send mail (`notify retry` reports a missing host with exit `2`) |
| `INVIO_SMTP_PORT` | `587` | Port. Not derived from the security mode (use `465` for `ssl`) |
| `INVIO_SMTP_USER` | unset | Login name. Login happens only when user and password are both set |
| `INVIO_SMTP_PASSWORD` | unset | Login password. Secret |
| `INVIO_SMTP_FROM` | unset | Sender address, for example `Invio <invio@example.com>` |
| `INVIO_SMTP_SECURITY` | `starttls` | `starttls` (fails when the server does not offer it), `ssl` or `none`. Replaces the old `INVIO_SMTP_STARTTLS` |
| `INVIO_SMTP_TIMEOUT_SECONDS` | `30` | Timeout of each SMTP operation, in seconds. Must be `> 0` |

### Operations

| Variable | Default | Meaning |
|---|---|---|
| `INVIO_LOG_LEVEL` | `INFO` | Standard logging level name (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). Logs are JSON lines on stderr |
| `INVIO_HEALTHCHECK_URL` | unset | Pinged after every `invio run-due`; `<url>/fail` on a non-zero exit. Absolute `http(s)` URL. Secret (embeds a check id) |
| `INVIO_ARCHIVE_DIR` | `/var/lib/invio/archive` | Directory of the static digest archive (used by jobs with `archive.enabled: true`) |
| `INVIO_ENV_FILE` | unset (`.env` in the working directory) | Path of the `.env` file. Not a setting; read directly. A path that does not exist is an error |

### YouTube

| Variable | Default | Meaning |
|---|---|---|
| `INVIO_YOUTUBE_COOKIES_FILE` | unset | Cookies file for yt-dlp; only read. Empty means unset. Path |
| `INVIO_YOUTUBE_PROXY` | unset | Proxy URL for YouTube requests; may embed credentials. Empty means unset. Secret |
| `INVIO_YOUTUBE_TIMEOUT_SECONDS` | `60` | Timeout of YouTube requests, in seconds. Must be `> 0` |

### Video transcripts

Subtitles are used first. Local Whisper transcription is an optional fallback that needs
`uv sync --extra whisper`.

| Variable | Default | Meaning |
|---|---|---|
| `INVIO_WHISPER_MODEL_SIZE` | `small` | Whisper model: `tiny`, `base`, `small`, `medium` or `large-v3` |
| `INVIO_WHISPER_MAX_DURATION_MINUTES` | `90` | Longer videos are never transcribed. Must be `>= 1` |
| `INVIO_WHISPER_MAX_PER_RUN` | `3` | Local transcriptions per run; the rest wait for the next run. `0` turns the fallback off. Must be `>= 0` |

### HTTP client (sources)

Applies to every source fetch. Constraints are shared with the HTTP client code, so both validate
alike.

| Variable | Default | Meaning |
|---|---|---|
| `INVIO_HTTP_CONTACT` | `admin@example.invalid` | Contact sent in the `User-Agent` header so site owners can reach you. ASCII, no control characters. Set your own address |
| `INVIO_HTTP_MAX_RESPONSE_BYTES` | `10485760` (10 MiB) | Largest response body accepted. Must be `> 0` |
| `INVIO_HTTP_MAX_REDIRECTS` | `5` | Redirects followed per request. Must be `>= 0` |
| `INVIO_HTTP_CONNECT_TIMEOUT_SECONDS` | `10` | Connect timeout, seconds. Must be `> 0` |
| `INVIO_HTTP_READ_TIMEOUT_SECONDS` | `30` | Read timeout, seconds. Must be `> 0` |
| `INVIO_HTTP_TOTAL_TIMEOUT_SECONDS` | `60` | Deadline for one request including redirects. Must be `>=` the read timeout |
| `INVIO_HTTP_HOST_INTERVAL_SECONDS` | `1` | Minimum interval between requests to the same host, seconds. Must be `> 0` |
| `INVIO_HTTP_RESPECT_ROBOTS` | `true` | Obey robots.txt before fetching a page |

---

## Model registry

Prices and context windows of the models a job may name live in
[src/invio/llm/models.d/](../src/invio/llm/models.d/README.md): one `<provider>.yaml` per provider.
The file format, the strict checks and the thinking fields are described in that README.

- The registry is read from the installed package. There is no setting for another directory.
- A model id must be unique across all files.
- Prices (USD per 1,000,000 tokens) feed the cost figures of `invio usage` and of run reports.
- A job's `llm.models` and `fallback_models` ids must appear here for their provider (see
  [`llm`](#llm)).
- The registry is loaded once per process, so a changed file takes effect in the next process
  (the next `invio` command or timer run).

Model ids registered at the time of writing:

| Provider | Registered model ids (`fast`, `smart` ordering in the file) |
|---|---|
| `anthropic` | `claude-haiku-4-5-20251001`, `claude-sonnet-4-6` |
| `google` | `gemini-3.5-flash-lite`, `gemini-3.8-flash` |
| `mistral` | `mistral-small-2603`, `mistral-medium-2604` |
| `openai` | `gpt-4.1-mini-2025-04-14`, `gpt-4.1-2025-04-14` |
| `ollama` | `llama3.2:3b`, `qwen2.5:14b` (free, local) |

Run `uv run invio llm test <provider> --model <id>` after adding an id; the command sends one tiny
request to that model.

---

## Related

- [docs/job.example.yaml](job.example.yaml): a complete job file.
- [docs/job.schema.json](job.schema.json): JSON Schema for editor validation.
- [README](../README.md): quick start, architecture and developer reference.
- [docs/deployment.md](deployment.md): systemd installation and environment file.
- [docs/operations.md](operations.md), [docs/troubleshooting.md](troubleshooting.md),
  [docs/backup.md](backup.md), [docs/security.md](security.md).
