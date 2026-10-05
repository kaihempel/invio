# Quickstart: validating the web page source (gh-issue-12)

Contracts: [job-file.md](contracts/job-file.md), [python-api.md](contracts/python-api.md).
Mapping and error table: [data-model.md](data-model.md).

## Prerequisites

```bash
uv sync --locked                          # runtime + dev (dev includes playwright)
uv run playwright install chromium        # only needed for the browser scenarios (6, 7)
```

## 1. Quality gates (Constitution IV)

```bash
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest --cov
```

Expected: all green, coverage ≥ 95 %. Without Chromium, the `browser` tests are reported as
skipped and coverage may fall short locally. CI installs Chromium.

## 2. Configuration contract (FR-001–FR-003)

```bash
uv run pytest tests/test_job_sources.py tests/test_job_schema.py tests/test_job_yaml.py -q
uv run python -c "from invio.config.job import load_yaml; load_yaml('docs/job.example.yaml')"  # no error
```

Expected: valid combinations load, and every row of the job-file contract's error table
exits 2 with the documented message. The committed `docs/job.schema.json` is current, and old
`web` entries without the new keys still load.

## 3. Change detection (Stories 1–2, acceptance criteria 1–3)

```bash
uv run pytest tests/test_source_web.py -q -k "page or selector or hash"
uv run pytest tests/test_web_text.py -q                  # incl. Hypothesis properties
```

Expected: the same page fetched twice gives the same `content_hash`. One changed paragraph
in the region changes it. Changes outside the selector, and changes only to scripts, styles,
comments, markup or whitespace, do not. A selector with no match raises `selector_not_found`.
The page result has a title and a teaser of at most 500 characters.

## 4. Link discovery (Story 3, acceptance criterion 4)

```bash
uv run pytest tests/test_source_web.py -q -k links
```

Expected: only URLs matching `url_pattern` are returned (any host). Without a pattern, only
same-host links. Relative links and `<base>` are resolved, duplicates and the page itself are
dropped, and `mailto:`/`javascript:`/`#` links are ignored.

## 5. Playwright is not loaded unless needed (acceptance criterion 5)

```bash
uv run pytest tests/test_source_web_render.py -q -k "lazy or unavailable"
```

Expected: importing `invio.sources.web` and fetching a static page leaves `playwright` out of
`sys.modules` (checked in a subprocess). With `playwright` blocked, a `render: js` fetch fails
with `render_unavailable` and a hint on how to install it.

## 6. JS rendering and the browser guard (Story 4, FR-018–FR-019)

```bash
uv run pytest -m browser -q
```

Expected: script-inserted text shows up in the hash, and `wait_for` is honoured. A page that
never settles fails with `timeout` within about 30 s. Sub-requests and redirects to a private
address outside the test allow-list are aborted, and the page still renders. A page URL
blocked by robots.txt is never opened.

## 7. Manual end-to-end check (optional, needs internet)

```bash
uv run python - <<'PY'
import asyncio
from invio.config.job import WebSource
from invio.sources.http import SafeHttpClient
from invio.sources.web import WebPageSource

async def main() -> None:
    cfg = WebSource(type="web", url="https://example.org/", selector="body")
    async with SafeHttpClient() as client, WebPageSource(client) as source:
        for c in await source.fetch(cfg):
            print(c.title, c.content_hash)

asyncio.run(main())
PY
```

Expected: one line with the page title and a 64-character hash. Running it again prints the
same hash.
