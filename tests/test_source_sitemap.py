"""Tests for the sitemap source adapter against sitemaps served by a loopback origin."""

import gzip
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from invio.config.job import SitemapSource
from invio.domain import Candidate
from invio.sources.base import Source
from invio.sources.http import FetchError, HttpClientConfig, SafeHttpClient
from invio.sources.sitemap import MAX_ENTRIES_PER_SITEMAP, SitemapUrlSource
from tests.http_helpers import LOOPBACK, LoopbackServer, Route, server  # noqa: F401

SITEMAPS = Path(__file__).parent / "fixtures" / "sitemaps"
NOW = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)
XML = {"Content-Type": "application/xml"}
NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


@pytest.fixture
async def client() -> AsyncIterator[SafeHttpClient]:
    config = HttpClientConfig(respect_robots=False, host_interval=0.01)
    async with SafeHttpClient(config, allow_networks=LOOPBACK) as instance:
        yield instance


def config_for(
    server: LoopbackServer, path: str = "/sitemap.xml", **fields: object
) -> SitemapSource:
    return SitemapSource.model_validate(
        {"type": "sitemap", "url": f"{server.base_url}{path}"} | fields
    )


def serve(server: LoopbackServer, body: bytes, path: str = "/sitemap.xml") -> None:
    server.routes[path] = Route(headers=dict(XML), body=body)


def urlset(*entries: str) -> bytes:
    return f'<urlset xmlns="{NS}">{"".join(entries)}</urlset>'.encode()


def url(loc: str, lastmod: str | None = None) -> str:
    mod = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
    return f"<url><loc>{loc}</loc>{mod}</url>"


def index(*locs: str) -> bytes:
    body = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return f'<sitemapindex xmlns="{NS}">{body}</sitemapindex>'.encode()


async def fetch(client: SafeHttpClient, config: SitemapSource) -> list[Candidate]:
    return await SitemapUrlSource(client, now=lambda: NOW).fetch(config)


def test_adapter_satisfies_source_protocol(client: SafeHttpClient) -> None:
    assert isinstance(SitemapUrlSource(client), Source)


# --- urlset ----------------------------------------------------------------------------------


async def test_urlset_fixture_yields_candidates(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    serve(server, (SITEMAPS / "urlset.xml").read_bytes())
    result = await fetch(client, config_for(server))
    assert [c.url for c in result] == [
        "https://e.com/news/new-post",
        "https://e.com/news/old-post",
        "https://e.com/about",
        "https://e.com/news/undated",
        f"{server.base_url}/news/relative",
    ]
    assert result[0].published_at == datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    assert result[0].title == result[0].url
    assert result[3].published_at is None
    assert result[4].published_at == NOW  # a future lastmod is clamped


async def test_url_pattern_filters(server: LoopbackServer, client: SafeHttpClient) -> None:
    serve(server, (SITEMAPS / "urlset.xml").read_bytes())
    result = await fetch(client, config_for(server, url_pattern=r"/news/"))
    assert result
    assert all("/news/" in c.url for c in result)


async def test_max_age_days_drops_old_but_keeps_undated(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    serve(server, (SITEMAPS / "urlset.xml").read_bytes())
    urls = [c.url for c in await fetch(client, config_for(server, max_age_days=30))]
    assert "https://e.com/news/old-post" not in urls
    assert "https://e.com/news/new-post" in urls
    assert "https://e.com/news/undated" in urls


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-03", datetime(2026, 3, 1, tzinfo=UTC)),
        ("2026", datetime(2026, 1, 1, tzinfo=UTC)),
        ("2026-03-01T00:00:00Z", datetime(2026, 3, 1, tzinfo=UTC)),
        ("2026-03-01T00:00:00", datetime(2026, 3, 1, tzinfo=UTC)),
        ("garbage", None),
    ],
)
async def test_lastmod_formats(
    server: LoopbackServer, client: SafeHttpClient, value: str, expected: datetime | None
) -> None:
    serve(server, urlset(url("https://e.com/a", value)))
    [candidate] = await fetch(client, config_for(server))
    assert candidate.published_at == expected


async def test_entries_per_sitemap_are_limited(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    entries = (url(f"https://e.com/{i}") for i in range(MAX_ENTRIES_PER_SITEMAP + 20))
    serve(server, urlset(*entries))
    assert len(await fetch(client, config_for(server))) == MAX_ENTRIES_PER_SITEMAP


async def test_gzip_body_is_decompressed(server: LoopbackServer, client: SafeHttpClient) -> None:
    serve(server, gzip.compress(urlset(url("https://e.com/a"))), "/sitemap.xml.gz")
    result = await fetch(client, config_for(server, "/sitemap.xml.gz"))
    assert [c.url for c in result] == ["https://e.com/a"]


async def test_gzip_bomb_is_rejected(server: LoopbackServer) -> None:
    config = HttpClientConfig(respect_robots=False, host_interval=0.01, max_response_bytes=10_000)
    bomb = gzip.compress(urlset(url("https://e.com/" + "a" * 200_000)))
    assert len(bomb) < 10_000
    serve(server, bomb)
    async with SafeHttpClient(config, allow_networks=LOOPBACK) as small:
        with pytest.raises(FetchError) as caught:
            await fetch(small, config_for(server))
    assert caught.value.reason == "too_large"


# --- sitemapindex ----------------------------------------------------------------------------


async def test_index_children_are_followed(server: LoopbackServer, client: SafeHttpClient) -> None:
    base = server.base_url
    text = (SITEMAPS / "index.xml").read_text().replace("{base}", base)
    serve(server, text.encode())
    serve(server, urlset(url("https://e.com/p1"), url("https://e.com/p2")), "/posts.xml")
    serve(server, index(f"{base}/deep.xml"), "/nested-index.xml")
    serve(server, urlset(url("https://e.com/deep")), "/deep.xml")
    # /missing.xml answers 404 and is skipped; the index inside the index is beyond the depth.
    result = await fetch(client, config_for(server))
    assert [c.url for c in result] == ["https://e.com/p1", "https://e.com/p2"]


async def test_index_cycle_terminates(server: LoopbackServer, client: SafeHttpClient) -> None:
    serve(server, index(f"{server.base_url}/sitemap.xml"))
    assert await fetch(client, config_for(server)) == []


# --- hostile and broken input ----------------------------------------------------------------

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://e.com/&lol2;</loc></url></urlset>"""

EXTERNAL_ENTITY = b"""<?xml version="1.0"?>
<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://e.com/&xxe;</loc></url></urlset>"""


@pytest.mark.parametrize("body", [BILLION_LAUGHS, EXTERNAL_ENTITY])
async def test_entity_attacks_are_rejected(
    server: LoopbackServer, client: SafeHttpClient, body: bytes
) -> None:
    serve(server, body)
    with pytest.raises(FetchError) as caught:
        await fetch(client, config_for(server))
    assert caught.value.reason == "malformed_sitemap"


@pytest.mark.parametrize("body", [b"not xml at all", b"<html><body/></html>", b""])
async def test_non_sitemap_is_malformed(
    server: LoopbackServer, client: SafeHttpClient, body: bytes
) -> None:
    serve(server, body)
    with pytest.raises(FetchError) as caught:
        await fetch(client, config_for(server))
    assert caught.value.reason == "malformed_sitemap"


# --- config ----------------------------------------------------------------------------------


def test_config_rejects_invalid_pattern() -> None:
    with pytest.raises(ValueError, match="invalid regular expression"):
        SitemapSource.model_validate(
            {"type": "sitemap", "url": "https://e.com/s.xml", "url_pattern": "("}
        )
