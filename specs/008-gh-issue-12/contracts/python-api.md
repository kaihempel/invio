# Contract: Python API for the web page source

## `invio.sources.web`

```python
from invio.sources.web import WebPageSource

async with SafeHttpClient() as client, WebPageSource(client) as source:
    candidates = await source.fetch(web_config)  # list[Candidate]
```

```python
class WebPageSource:  # satisfies Source[WebSource]
    def __init__(self, client: SafeHttpClient, *, renderer: PageRenderer | None = None) -> None: ...
    async def fetch(self, config: WebSource, /) -> list[Candidate]: ...
    async def aclose(self) -> None: ...  # closes the browser if one was started
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc: object) -> None: ...
```

- `renderer` exists for tests. When it is `None`, the first `render: js` fetch creates the
  Playwright renderer, importing `invio.sources.browser` at that moment and not before.
- Importing `invio.sources.web` never imports `playwright` (FR-017).
- `fetch` behaviour and errors: see data-model.md, "Fetch outcomes". It is safe to call
  concurrently.

## `invio.sources.text` (shared with the RSS source)

```python
TEASER_MAX_CHARS: Final = 500


def collapse(text: str) -> str: ...  # whitespace runs -> one space, trimmed
def html_to_text(html: str) -> str: ...  # comments/script/style/noscript/template dropped
def teaser(text: str) -> str | None: ...  # collapsed, <= 500 chars incl. "…", None if empty
```

`invio.sources.rss` keeps exporting `TEASER_MAX_CHARS` (re-export) so existing imports keep
working.

## `invio.sources.web`: renderer types (no playwright)

```python
class RenderedPage(NamedTuple):
    url: str  # final URL of the main document
    html: str


class PageRenderer(Protocol):
    async def render(self, url: str, *, wait_for: str | None) -> RenderedPage: ...
    async def aclose(self) -> None: ...
```

`WebPageSource` owns its renderer, injected or lazily started, and closes it in `aclose()`.

## `invio.sources.browser` (internal, needs the `render` extra)

```python
RENDER_TIMEOUT_SECONDS: Final = 30.0  # read when a render starts


class PlaywrightRenderer:  # the only module that imports playwright
    @classmethod
    async def start(cls, client: SafeHttpClient) -> Self: ...  # raises RenderUnavailableError
    async def render(self, url: str, *, wait_for: str | None) -> RenderedPage: ...
    async def aclose(self) -> None: ...
```

## `invio.sources.http.SafeHttpClient`: two new public methods

```python
async def check_target(self, url: str) -> None:
    """Scheme + resolved-address guard only (uses allow_networks/resolver); raises BlockedError
    or FetchError("invalid_url"/"dns_failed"/"timeout"). Nothing is sent and nothing is logged
    (a page makes many requests; the caller decides what to report)."""


@asynccontextmanager
async def admission(self, url: str) -> AsyncIterator[None]:
    """For fetches made outside the client (browser navigation): guard, robots.txt check and
    the per-origin rate-limit slot, held for the whole block (other fetches of the origin wait;
    slots are not re-entrant). A refusal is logged once, like get(); errors raised inside the
    block are the caller's to report."""
```

## `invio.sources.http.SafeHttpClient.user_agent` (new read-only property)

```python
@property
def user_agent(self) -> str: ...  # the configured User-Agent, also used by the browser context
```

## `invio.sources.errors.RenderUnavailableError` (new)

```python
class RenderUnavailableError(FetchError):
    """reason == "render_unavailable"; the message ends with the install hint
    ("uv sync --extra render" and "uv run playwright install chromium")."""

    def __init__(self, *, url: str) -> None: ...
```

Re-exported from `invio.sources.http` like the other error types.

## New `FetchError` reasons

`not_html`, `selector_not_found`, `render_unavailable`, `render_failed`, plus the existing
`timeout`, `http_status`, `too_large`, `blocked_by_robots`, `non_public_address`,
`unsupported_scheme` and `dns_failed`.
