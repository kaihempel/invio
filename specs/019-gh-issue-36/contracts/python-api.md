# Python API Contract: Source Discovery (gh-issue-36)

Signatures are binding for implementation and tests; docstrings may elaborate.

## `invio.sources.discover` (new)

```python
MAX_ANNOUNCED_FEEDS: Final = 20
MAX_YOUTUBE_LINKS: Final = 20
MAX_ROBOTS_SITEMAPS: Final = 5
PROBE_PATHS: Final = ("feed", "rss", "atom.xml", "sitemap.xml")


def parse_target(raw: str) -> DiscoveryTarget:
    ...
    # ValueError("…") for anything that is not an http(s) URL with a host; no network.


async def discover(
    target: DiscoveryTarget,
    *,
    client: SafeHttpClient,
    youtube: YoutubeSource,
) -> DiscoveryReport:
    ...
    # Never raises FetchError: every failure becomes a Rejection (or page_problem).
    # Every HTTP request goes through ``client.get(..., conditional=False)``;
    # robots.txt sitemaps through ``client.robots_sitemaps``; YouTube through
    # ``youtube.describe``.


def is_comment_feed(final_url: str, *titles: str | None) -> bool: ...
```

Types `DiscoveryTarget`, `FindingOrigin`, `DiscoveredSource`, `Rejection`, `DiscoveryReport`:
see [data-model.md](../data-model.md). `Finding` stays private.

## `invio.sources.rss` (extended)

```python
@dataclass(frozen=True, slots=True)
class FeedSummary:
    title: str | None
    entry_count: int
    newest: datetime | None


def describe_feed(content: bytes, *, base_url: str) -> FeedSummary | None:
    ...
    # None unless feedparser recognises a feed version and a broken parse still has an entry
    # whose link resolves against base_url (same rule as RssFeedSource.fetch); an empty
    # well-formed feed is a FeedSummary(…, 0, None).
```

## `invio.sources.sitemap` (extended)

```python
@dataclass(frozen=True, slots=True)
class SitemapSummary:
    is_index: bool
    entry_count: int
    newest: datetime | None


def describe_sitemap(content: bytes, *, url: str, limit: int) -> SitemapSummary | None:
    ...
    # None when the body is not a urlset/sitemapindex (malformed XML included);
    # ``limit`` bounds gzip inflation (client.max_response_bytes). Children are not fetched.
```

## `invio.sources.robots` / `invio.sources.http` (extended)

```python
class RobotsPolicy:
    sitemaps: tuple[str, ...] = ()  # absolute http(s) URLs from ``Sitemap:`` lines, file order


class SafeHttpClient:
    async def robots_sitemaps(self, url: str) -> tuple[str, ...]:
        ...
        # Sitemap URLs of url's origin from the cached (or freshly fetched) robots.txt;
        # () when robots.txt is missing, unreadable or refused. Works with respect_robots=False.
```

## `invio.sources.youtube` (extended)

```python
@dataclass(frozen=True, slots=True)
class ListingSummary:
    title: str | None
    entry_count: int
    newest: datetime | None


class YoutubeSource:
    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        ...
        # moved from pipeline.deps._youtube_source; the pipeline uses it too

    async def describe(
        self, config: YoutubeChannelSource | YoutubePlaylistSource, /
    ) -> ListingSummary:
        ...
        # Same extractor/options/timeout as fetch, playlistend=5; raises FetchError like fetch.
```

## `invio.services.jobs` (extended)

```python
class SourceExistsError(Exception):
    name: str  # job name
    source_type: str


class JobService:
    def append_source(self, name: str, source: SourceConfig) -> JobRecord:
        ...
        # One unit of work; the job row is locked (SELECT … FOR UPDATE) for the read-modify-write.
        # Raises JobNotFoundError, StoredJobConfigError, SourceExistsError,
        # JobConfigError (config invalid after appending; nothing written).
```

## `invio.cli.commands.source` (new) — test seams

```python
app: typer.Typer  # registered as ``invio source``


def _make_service() -> JobService: ...
def _discovery_deps() -> AbstractAsyncContextManager[tuple[SafeHttpClient, YoutubeSource]]: ...
def _make_prompter() -> Prompter: ...
def _is_interactive() -> bool: ...
```

Tests monkeypatch these the same way `tests/test_cli_job*.py` patch `invio.cli.commands.job`.
