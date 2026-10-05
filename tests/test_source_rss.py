"""Tests for the RSS/Atom source adapter against feeds served by a loopback origin."""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from invio.config.job import RssSource
from invio.domain import Candidate, url_hash
from invio.sources.base import Source
from invio.sources.http import FetchError, HttpClientConfig, SafeHttpClient
from invio.sources.rss import TEASER_MAX_CHARS, RssFeedSource, _entry_date
from tests.http_helpers import LOOPBACK, LoopbackServer, Route, server  # noqa: F401

FEEDS = Path(__file__).parent / "fixtures" / "feeds"
NOW = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)
RSS_TYPE = {"Content-Type": "application/rss+xml"}


@pytest.fixture
async def client() -> AsyncIterator[SafeHttpClient]:
    config = HttpClientConfig(respect_robots=False, host_interval=0.01)
    async with SafeHttpClient(config, allow_networks=LOOPBACK) as instance:
        yield instance


def feed_config(server: LoopbackServer, path: str = "/feed", **fields: object) -> RssSource:
    return RssSource.model_validate({"type": "rss", "url": f"{server.base_url}{path}"} | fields)


def serve(server: LoopbackServer, body: bytes, path: str = "/feed", **route: object) -> None:
    server.routes[path] = Route(headers=dict(RSS_TYPE), body=body, **route)  # type: ignore[arg-type]


def rss(*items: str) -> bytes:
    """A minimal RSS 2.0 document with the given ``<item>`` bodies."""
    body = "".join(f"<item>{item}</item>" for item in items)
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>T</title>'
        f"<link>https://e.com/</link><description>D</description>{body}</channel></rss>"
    ).encode()


async def fetch(
    client: SafeHttpClient,
    config: RssSource,
    now: Callable[[], datetime] = lambda: NOW,
) -> list[Candidate]:
    return await RssFeedSource(client, now=now).fetch(config)


# --- acceptance: RSS 2.0 and Atom fixtures ---------------------------------------------------


async def test_rss2_fixture_yields_candidates(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    serve(server, (FEEDS / "rss2.xml").read_bytes())

    candidates = await fetch(client, feed_config(server))

    assert candidates == [
        Candidate(
            url="https://blog.example.com/posts/1",
            url_hash=url_hash("https://blog.example.com/posts/1"),
            title="First post",
            published_at=datetime(2026, 3, 2, 9, 30, tzinfo=UTC),
            type="article",
            teaser="Hello & welcome! Second paragraph.",
            content_hash=None,
        ),
        Candidate(
            url="https://blog.example.com/posts/2",
            url_hash=url_hash("https://blog.example.com/posts/2"),
            title="Second post",
            published_at=datetime(2026, 3, 1, 8, 0, tzinfo=UTC),
            type="article",
            teaser=None,
            content_hash=None,
        ),
        Candidate(
            url="https://blog.example.com/posts/3",
            url_hash=url_hash("https://blog.example.com/posts/3"),
            title="https://blog.example.com/posts/3",  # no title: falls back to the URL
            published_at=None,
            type="article",
            teaser=None,
            content_hash=None,
        ),
    ]


async def test_atom_fixture_yields_candidates(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    server.routes["/feed"] = Route(
        headers={"Content-Type": "application/atom+xml"}, body=(FEEDS / "atom.xml").read_bytes()
    )

    candidates = await fetch(client, feed_config(server))

    assert [(c.url, c.title, c.published_at, c.teaser) for c in candidates] == [
        (
            "https://news.example.org/a/1",
            "Atom entry one",  # type="html": markup removed
            datetime(2026, 3, 2, 7, 0, tzinfo=UTC),  # published wins over updated
            "Plain <summary> text",  # type="text": kept literally
        ),
        (
            "https://news.example.org/a/2",
            "Atom entry two",
            datetime(2026, 2, 20, tzinfo=UTC),  # no published: updated
            None,
        ),
    ]
    assert all(c.type == "article" and c.content_hash is None for c in candidates)
    assert all(c.published_at is not None and c.published_at.tzinfo is UTC for c in candidates)


async def test_xml_declaration_encoding_wins_over_http_charset(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    server.routes["/feed"] = Route(
        headers={"Content-Type": "text/xml; charset=utf-8"},
        body=(FEEDS / "latin1.xml").read_bytes(),
    )

    [candidate] = await fetch(client, feed_config(server))

    assert candidate.title == "Grüße aus Köln"
    assert candidate.url == "https://example.com/köln"


async def test_adapter_satisfies_source_protocol(client: SafeHttpClient) -> None:
    source: Source[RssSource] = RssFeedSource(client)

    assert isinstance(source, RssFeedSource)


# --- acceptance: utm_* variants share one hash -----------------------------------------------


async def test_utm_variants_are_one_candidate(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    serve(
        server,
        rss(
            "<title>A</title><link>https://e.com/a?utm_source=rss</link>",
            "<title>B</title><link>https://E.com/a?utm_medium=mail#x</link>",
            "<title>C</title><link>https://e.com/a</link>",
            "<title>D</title><link>https://e.com/b?id=1&amp;utm_term=y</link>",
        ),
    )

    candidates = await fetch(client, feed_config(server))

    assert [(c.title, c.url) for c in candidates] == [
        ("A", "https://e.com/a"),  # the first occurrence wins
        ("D", "https://e.com/b?id=1"),
    ]
    assert candidates[0].url_hash == url_hash("https://e.com/a")


# --- acceptance: max_age_days ----------------------------------------------------------------


async def test_entries_older_than_max_age_days_are_dropped(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    serve(
        server,
        rss(
            "<link>https://e.com/new</link><pubDate>Tue, 03 Mar 2026 08:00:00 GMT</pubDate>",
            "<link>https://e.com/edge</link><pubDate>Fri, 27 Feb 2026 12:00:00 GMT</pubDate>",
            "<link>https://e.com/old</link><pubDate>Fri, 27 Feb 2026 11:59:59 GMT</pubDate>",
            "<link>https://e.com/undated</link>",
        ),
    )

    candidates = await fetch(client, feed_config(server, max_age_days=4))

    assert [c.url for c in candidates] == [
        "https://e.com/new",
        "https://e.com/edge",  # exactly max_age_days old: kept
        "https://e.com/undated",  # no date: kept
    ]


async def test_no_max_age_keeps_old_entries(server: LoopbackServer, client: SafeHttpClient) -> None:
    serve(
        server,
        rss("<link>https://e.com/old</link><pubDate>Mon, 01 Jan 2001 00:00:00 GMT</pubDate>"),
    )

    candidates = await fetch(client, feed_config(server))

    assert [c.url for c in candidates] == ["https://e.com/old"]


async def test_default_clock_is_used_without_now(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    serve(
        server,
        rss(
            "<link>https://e.com/old</link><pubDate>Mon, 01 Jan 2001 00:00:00 GMT</pubDate>",
            "<link>https://e.com/future</link><pubDate>Fri, 01 Jan 2100 00:00:00 GMT</pubDate>",
        ),
    )

    candidates = await RssFeedSource(client).fetch(feed_config(server, max_age_days=1))

    assert [c.url for c in candidates] == ["https://e.com/future"]


# --- acceptance: malformed feeds -------------------------------------------------------------


def _truncated() -> bytes:
    data = (FEEDS / "rss2.xml").read_bytes()
    return data[: data.index(b"<item>") + 20]  # inside the first item, before its link


@pytest.mark.parametrize(
    "body",
    [
        pytest.param((FEEDS / "page.html").read_bytes(), id="html-page"),
        pytest.param(_truncated(), id="truncated-xml"),
        pytest.param(bytes(range(256)) * 8, id="binary-garbage"),
        pytest.param(b"", id="empty-body"),
        pytest.param(b'{"items": [{"url": "https://e.com/a"}]}', id="json"),
    ],
)
async def test_malformed_feed_raises_fetch_error(
    server: LoopbackServer, client: SafeHttpClient, body: bytes
) -> None:
    serve(server, body)
    config = feed_config(server, "/feed?token=secret")
    server.routes["/feed?token=secret"] = server.routes["/feed"]

    with pytest.raises(FetchError) as info:
        await fetch(client, config)

    assert info.value.reason == "malformed_feed"
    assert info.value.status is None
    assert "secret" not in str(info.value)  # the query stays out of the message


async def test_truncated_feed_keeps_complete_entries(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    data = (FEEDS / "rss2.xml").read_bytes()
    serve(server, data[: data.index(b"</item>") + len(b"</item>") + 10])

    candidates = await fetch(client, feed_config(server))

    assert [c.url for c in candidates] == ["https://blog.example.com/posts/1"]


async def test_body_that_names_a_local_file_is_not_opened(
    server: LoopbackServer, client: SafeHttpClient, tmp_path: Path
) -> None:
    local = tmp_path / "feed.xml"
    local.write_bytes((FEEDS / "rss2.xml").read_bytes())
    serve(server, str(local).encode())

    with pytest.raises(FetchError, match="malformed_feed"):
        await fetch(client, feed_config(server))


# --- other outcomes --------------------------------------------------------------------------


async def test_empty_valid_feed_returns_no_candidates(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    serve(server, (FEEDS / "empty.xml").read_bytes())

    assert await fetch(client, feed_config(server)) == []


async def test_not_modified_returns_no_candidates(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    serve(server, (FEEDS / "rss2.xml").read_bytes(), conditional=True)
    server.routes["/feed"].headers["ETag"] = '"v1"'
    source = RssFeedSource(client, now=lambda: NOW)

    first = await source.fetch(feed_config(server))
    second = await source.fetch(feed_config(server))

    assert len(first) == 3
    assert second == []
    assert server.requests[1].headers["if-none-match"] == '"v1"'


@pytest.mark.parametrize("status", [404, 500])
async def test_http_error_propagates(
    server: LoopbackServer, client: SafeHttpClient, status: int
) -> None:
    server.routes["/feed"] = Route(status=status)

    with pytest.raises(FetchError) as info:
        await fetch(client, feed_config(server))

    assert (info.value.reason, info.value.status) == ("http_status", status)


async def test_relative_links_resolve_against_the_final_feed_url(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    server.routes["/old"] = Route(status=301, headers={"Location": "/blog/feed.xml"})
    serve(
        server,
        rss("<link>/posts/1</link>", "<link>posts/2?utm_source=x</link>"),
        path="/blog/feed.xml",
    )

    candidates = await fetch(client, feed_config(server, "/old"))

    assert [c.url for c in candidates] == [
        f"{server.base_url}/posts/1",
        f"{server.base_url}/blog/posts/2",
    ]


@pytest.mark.parametrize(
    "link",
    [
        "",
        "   ",
        "ftp://e.com/file",
        "javascript:alert(1)",
        "mailto:a@e.com",
        "http://[::1/x",
        "http://e.com:99999/x",
        "https://",
        "http://:80/x",
        "http://e.com:0/x",
    ],
)
async def test_entries_without_usable_link_are_skipped(
    server: LoopbackServer, client: SafeHttpClient, link: str
) -> None:
    serve(server, rss(f"<title>bad</title><link>{link}</link>", "<link>https://e.com/ok</link>"))

    candidates = await fetch(client, feed_config(server))

    assert [c.url for c in candidates] == ["https://e.com/ok"]


async def test_entry_without_link_element_is_skipped(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    serve(server, rss("<title>No link</title>", "<title>Ok</title><link>https://e.com/ok</link>"))

    candidates = await fetch(client, feed_config(server))

    assert [c.title for c in candidates] == ["Ok"]


@pytest.mark.parametrize(
    "date",
    ["not a date", "Mon, 30 Feb 2026 10:00:00 GMT", "", "Mon, 01 Jan 0000 00:00:00 GMT"],
)
async def test_unparsable_date_becomes_none(
    server: LoopbackServer, client: SafeHttpClient, date: str
) -> None:
    serve(server, rss(f"<link>https://e.com/a</link><pubDate>{date}</pubDate>"))

    [candidate] = await fetch(client, feed_config(server, max_age_days=1))

    assert candidate.published_at is None


async def test_long_teaser_is_cut_at_a_word_boundary(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    words = " ".join(f"word{i}" for i in range(200))
    serve(server, rss(f"<link>https://e.com/a</link><description>{words}</description>"))

    [candidate] = await fetch(client, feed_config(server))

    assert candidate.teaser is not None
    assert len(candidate.teaser) <= TEASER_MAX_CHARS
    assert candidate.teaser.endswith("…")
    assert words.startswith(candidate.teaser.removesuffix("…"))
    assert candidate.teaser.removesuffix("…").split(" ")[-1] in words.split(" ")


async def test_long_teaser_without_spaces_is_cut_hard(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    serve(server, rss(f"<link>https://e.com/a</link><description>{'x' * 900}</description>"))

    [candidate] = await fetch(client, feed_config(server))

    assert candidate.teaser == "x" * (TEASER_MAX_CHARS - 1) + "…"


async def test_markup_only_summary_gives_no_teaser(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    serve(
        server,
        rss("<link>https://e.com/a</link><description>&lt;p&gt; &lt;/p&gt;</description>"),
    )

    [candidate] = await fetch(client, feed_config(server))

    assert candidate.teaser is None


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({}, None),
        ({"published_parsed": None, "updated_parsed": None}, None),
        ({"published_parsed": (0, 1, 1, 0, 0, 0, 0, 1, 0)}, None),  # year 0: ValueError
        ({"published_parsed": "garbage"}, None),  # TypeError
        (
            {"published_parsed": "garbage", "updated_parsed": (2026, 3, 1, 8, 0, 0, 6, 60, 0)},
            datetime(2026, 3, 1, 8, 0, tzinfo=UTC),
        ),
    ],
)
def test_entry_date_is_defensive(entry: dict[str, object], expected: datetime | None) -> None:
    assert _entry_date(entry) == expected
