"""robots.txt handling of ``SafeHttpClient`` against loopback origins."""

import asyncio

import pytest

from invio.sources.http import (
    BlockedError,
    BlockReason,
    FetchError,
    FetchResult,
    HttpClientConfig,
    SafeHttpClient,
)
from invio.sources.robots import parse_robots
from tests.http_helpers import LOOPBACK, LoopbackServer, Route, second_server, server  # noqa: F401


def make_client(**overrides: object) -> SafeHttpClient:
    fields: dict[str, object] = {"respect_robots": True, "host_interval": 0.01} | overrides
    return SafeHttpClient(HttpClientConfig.model_validate(fields), allow_networks=LOOPBACK)


def robots(server: LoopbackServer, text: str) -> None:
    server.routes["/robots.txt"] = Route(headers={"Content-Type": "text/plain"}, body=text.encode())


def pages(server: LoopbackServer, *paths: str) -> None:
    for path in paths:
        server.routes[path] = Route(body=b"page")


async def test_disallowed_page_and_feed_are_blocked_and_never_requested(
    server: LoopbackServer,
) -> None:
    robots(server, "User-agent: *\nDisallow: /private/\nDisallow: /feed.xml\n")
    pages(server, "/private/page", "/feed.xml", "/public/page")

    async with make_client() as client:
        for path in ("/private/page", "/feed.xml"):
            with pytest.raises(BlockedError) as info:
                await client.get(f"{server.base_url}{path}")
            assert info.value.reason is BlockReason.BLOCKED_BY_ROBOTS
        result = await client.get(f"{server.base_url}/public/page")

    assert isinstance(result, FetchResult)
    assert server.paths() == ["/robots.txt", "/public/page"]


async def test_robots_is_fetched_once_for_sequential_and_concurrent_calls(
    server: LoopbackServer,
) -> None:
    robots(server, "User-agent: *\nDisallow: /private/\n")
    pages(server, "/a", "/b", "/c", "/d", "/e", "/f", "/g", "/h")

    async with make_client() as client:
        for path in ("/a", "/b", "/c"):
            await client.get(f"{server.base_url}{path}")
        await asyncio.gather(*(client.get(f"{server.base_url}/{p}") for p in "defgh"))

    assert server.paths().count("/robots.txt") == 1


@pytest.mark.parametrize("status", [404, 403, 410])
async def test_client_error_on_robots_allows_everything(
    server: LoopbackServer,
    status: int,
) -> None:
    server.routes["/robots.txt"] = Route(status=status)
    pages(server, "/private/page")

    async with make_client() as client:
        result = await client.get(f"{server.base_url}/private/page")

    assert isinstance(result, FetchResult)


async def test_server_error_on_robots_blocks_the_run_without_retry(
    server: LoopbackServer,
) -> None:
    server.routes["/robots.txt"] = Route(status=503)
    pages(server, "/a", "/b")

    async with make_client() as client:
        for path in ("/a", "/b"):
            with pytest.raises(BlockedError) as info:
                await client.get(f"{server.base_url}{path}")
            assert info.value.reason is BlockReason.BLOCKED_BY_ROBOTS

    assert server.paths() == ["/robots.txt"]


async def test_stalled_robots_blocks(server: LoopbackServer) -> None:
    server.routes["/robots.txt"] = Route(headers={"Content-Length": "10"}, stall=True)
    pages(server, "/a")

    async with make_client(read_timeout=0.2, total_timeout=0.5) as client:
        with pytest.raises(BlockedError) as info:
            await client.get(f"{server.base_url}/a")

    assert info.value.reason is BlockReason.BLOCKED_BY_ROBOTS


async def test_robots_redirect_to_private_address_blocks(
    server: LoopbackServer,
) -> None:
    server.routes["/robots.txt"] = Route(
        status=302, headers={"Location": "http://10.0.0.5/robots.txt"}
    )
    pages(server, "/a")

    async with make_client() as client:
        with pytest.raises(BlockedError) as info:
            await client.get(f"{server.base_url}/a")

    assert info.value.reason is BlockReason.BLOCKED_BY_ROBOTS
    assert server.paths() == ["/robots.txt"]


async def test_only_the_first_500_kb_of_robots_are_parsed(
    server: LoopbackServer,
) -> None:
    head = "User-agent: *\nDisallow: /private/\n"
    robots(server, head + "# filler\n" * 70_000)  # about 630 KB
    assert len(server.routes["/robots.txt"].body) > 600_000
    pages(server, "/private/page", "/ok")

    async with make_client() as client:
        with pytest.raises(BlockedError):
            await client.get(f"{server.base_url}/private/page")
        result = await client.get(f"{server.base_url}/ok")

    assert isinstance(result, FetchResult)


async def test_robots_larger_than_the_response_limit_is_truncated_not_an_error(
    server: LoopbackServer,
) -> None:
    robots(server, "User-agent: *\nDisallow: /x\n" + "#" * 5000)
    pages(server, "/ok")

    async with make_client(max_response_bytes=1024) as client:
        result = await client.get(f"{server.base_url}/ok")

    assert isinstance(result, FetchResult)


async def test_invio_group_overrides_wildcard_group(server: LoopbackServer) -> None:
    robots(server, "User-agent: *\nDisallow: /\n\nUser-agent: invio\nDisallow: /x/\n")
    pages(server, "/x/a", "/y")

    async with make_client() as client:
        with pytest.raises(BlockedError):
            await client.get(f"{server.base_url}/x/a")
        result = await client.get(f"{server.base_url}/y")

    assert isinstance(result, FetchResult)


async def test_robots_request_has_no_recursion_and_carries_the_user_agent(
    server: LoopbackServer,
) -> None:
    robots(server, "User-agent: *\nAllow: /\n")
    pages(server, "/a")

    async with make_client() as client:
        await client.get(f"{server.base_url}/a")

    first = server.requests[0]
    assert first.path == "/robots.txt"
    assert first.headers["user-agent"] == HttpClientConfig().user_agent
    assert server.paths() == ["/robots.txt", "/a"]


async def test_respect_robots_false_makes_no_robots_request(
    server: LoopbackServer,
) -> None:
    robots(server, "User-agent: *\nDisallow: /\n")
    pages(server, "/a")

    async with make_client(respect_robots=False) as client:
        await client.get(f"{server.base_url}/a")

    assert server.paths() == ["/a"]


async def test_redirect_target_on_another_origin_is_checked_against_its_robots(
    server: LoopbackServer,
    second_server: LoopbackServer,
) -> None:
    robots(second_server, "User-agent: *\nDisallow: /secret\n")
    pages(second_server, "/secret")
    pages(server, "/go")
    server.routes["/go"] = Route(
        status=302, headers={"Location": f"{second_server.base_url}/secret"}
    )

    async with make_client() as client:
        with pytest.raises(BlockedError) as info:
            await client.get(f"{server.base_url}/go")

    assert info.value.reason is BlockReason.BLOCKED_BY_ROBOTS
    assert "/secret" not in second_server.paths()
    assert "/robots.txt" in second_server.paths()


async def test_blocked_by_robots_is_logged(
    server: LoopbackServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.DEBUG, logger="invio.sources.http")
    robots(server, "User-agent: *\nDisallow: /\n")

    async with make_client() as client:
        with pytest.raises(FetchError):
            await client.get(f"{server.base_url}/a")

    records = [r for r in caplog.records if r.name == "invio.sources.http"]
    assert [r.getMessage() for r in records] == ["http_blocked"]
    assert records[0].__dict__["reason"] == "blocked_by_robots"


# --- RobotsCache unit tests ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("User-agent: *\nCrawl-delay: 5\n", 5.0),
        ("User-agent: invio\nCrawl-delay: 2\n\nUser-agent: *\nCrawl-delay: 9\n", 2.0),
        ("User-agent: *\nCrawl-delay: abc\n", None),
        ("User-agent: *\nDisallow: /x\n", None),
    ],
)
async def test_crawl_delay_is_parsed(body: str, expected: float | None) -> None:
    from invio.sources.netguard import Origin
    from invio.sources.robots import RobotsCache

    async def fetch(url: object) -> tuple[int, bytes]:
        return 200, body.encode()

    policy = await RobotsCache(fetch).policy(Origin("http", "h", 80))  # type: ignore[arg-type]

    assert policy.crawl_delay == expected


async def test_policy_modes_and_robots_url() -> None:
    from invio.sources.netguard import Origin
    from invio.sources.robots import RobotsCache

    seen: list[str] = []

    async def fetch(url: object) -> tuple[int, bytes]:
        seen.append(str(url))
        return 500, b""

    cache = RobotsCache(fetch)  # type: ignore[arg-type]
    origin = Origin("https", "example.org", 8443)

    assert cache.known(origin) is None
    policy = await cache.policy(origin)

    assert policy.mode == "disallow_all"
    assert policy.allows("https://example.org:8443/") is False
    assert cache.known(origin) is policy
    assert seen == ["https://example.org:8443/robots.txt"]


async def test_robots_redirect_on_the_same_origin_is_followed(server: LoopbackServer) -> None:
    server.routes["/robots.txt"] = Route(status=301, headers={"Location": "/real-robots.txt"})
    server.routes["/real-robots.txt"] = Route(body=b"User-agent: *\nDisallow: /private/\n")
    pages(server, "/private/page", "/ok")

    async with make_client() as client:
        with pytest.raises(BlockedError) as info:
            await client.get(f"{server.base_url}/private/page")
        result = await client.get(f"{server.base_url}/ok")

    assert info.value.reason is BlockReason.BLOCKED_BY_ROBOTS
    assert isinstance(result, FetchResult)
    assert server.paths() == ["/robots.txt", "/real-robots.txt", "/ok"]


async def test_concurrent_first_lookups_share_one_robots_fetch() -> None:
    from invio.sources.netguard import Origin
    from invio.sources.robots import RobotsCache

    release = asyncio.Event()
    calls: list[object] = []

    async def fetch(url: object) -> tuple[int, bytes]:
        calls.append(url)
        await release.wait()
        return 404, b""

    cache = RobotsCache(fetch)  # type: ignore[arg-type]
    origin = Origin("http", "h", 80)
    first = asyncio.create_task(cache.policy(origin))
    second = asyncio.create_task(cache.policy(origin))
    await asyncio.sleep(0)  # both tasks are inside policy(): one fetching, one on the lock
    release.set()

    assert await first is await second
    assert len(calls) == 1


@pytest.mark.parametrize("raw", ["abc", "nan", "inf", "-1", ""])
def test_unusable_crawl_delay_values_are_ignored(raw: str) -> None:
    from invio.sources.robots import parse_robots

    assert parse_robots(f"User-agent: *\nCrawl-delay: {raw}\n").crawl_delay is None


# --- RFC 9309 matching (the same on every supported Python version) --------------------------

WILDCARD_ROBOTS = """\
User-agent: *
Disallow: /*.pdf
Disallow: /private*
Disallow: /exact$
Disallow: /shop/
Allow: /shop/open
Allow: /tie
Disallow: /tie
Disallow: /q?session=
"""


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/docs/a.pdf", False),  # "*" matches any characters
        ("/docs/a.pdf?x=1", False),  # an unanchored pattern is a prefix
        ("/privatestuff", False),
        ("/exact", False),  # "$" anchors the end
        ("/exact/more", True),
        ("/shop/basket", False),
        ("/shop/open/now", True),  # the longest match (Allow) wins
        ("/tie", True),  # equal length: Allow wins
        ("/q?session=1", False),  # the query is part of the matched path
        ("/q", True),
        ("/robots.txt", True),
        ("/", True),
    ],
)
def test_rules_follow_rfc_9309(path: str, allowed: bool) -> None:
    from invio.sources.robots import parse_robots

    assert parse_robots(WILDCARD_ROBOTS).allows(f"https://h{path}") is allowed


def test_robots_txt_itself_is_always_allowed() -> None:
    from invio.sources.robots import parse_robots

    assert parse_robots("User-agent: *\nDisallow: /\n").allows("https://h/robots.txt")


def test_groups_for_invio_are_merged_and_matched_case_insensitively() -> None:
    from invio.sources.robots import parse_robots

    policy = parse_robots(
        "User-agent: Invio/1.0\nDisallow: /a\n\n"
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: other\nUser-agent: INVIO\nDisallow: /b\nCrawl-delay: 3\n"
    )

    assert not policy.allows("https://h/a")
    assert not policy.allows("https://h/b")
    assert policy.allows("https://h/c")
    assert policy.crawl_delay == 3.0


def test_percent_encoding_comments_bom_and_empty_disallow() -> None:
    from invio.sources.robots import parse_robots

    policy = parse_robots(
        "\ufeffUser-agent: *  # everyone\nDisallow: /a%7Eb # tilde\nDisallow:\nSitemap: /s.xml\n"
    )

    assert not policy.allows("https://h/a~b")
    assert not policy.allows("https://h/a%7eb")
    assert policy.allows("https://h/other")


def test_rules_before_any_user_agent_are_ignored() -> None:
    from invio.sources.robots import parse_robots

    assert parse_robots("Disallow: /\nUser-agent: *\nDisallow: /x\n").allows("https://h/y")


# --- retrying robots.txt after a transient failure -------------------------------------------


@pytest.mark.parametrize("reason", ["timeout", "connection_failed", "dns_failed"])
async def test_transient_failure_disallows_until_the_retry_window_ends(reason: str) -> None:
    from invio.sources.netguard import Origin
    from invio.sources.robots import ROBOTS_RETRY_AFTER, RobotsCache

    now = [100.0]
    answers: list[Exception | tuple[int, bytes]] = [
        FetchError(reason, url="http://h/robots.txt"),
        (404, b""),
    ]

    async def fetch(url: object) -> tuple[int, bytes]:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    cache = RobotsCache(fetch, clock=lambda: now[0])  # type: ignore[arg-type]
    origin = Origin("http", "h", 80)

    first = await cache.policy(origin)
    now[0] += ROBOTS_RETRY_AFTER - 1
    assert await cache.policy(origin) is first
    assert first.mode == "disallow_all"
    now[0] += 1
    assert (await cache.policy(origin)).mode == "allow_all"
    assert answers == []


@pytest.mark.parametrize(
    "error",
    [
        FetchError("invalid_response", url="http://h/robots.txt"),
        FetchError("too_many_redirects", url="http://h/robots.txt"),
        BlockedError(BlockReason.NON_PUBLIC_ADDRESS, url="http://h/robots.txt"),
    ],
)
async def test_permanent_failure_disallows_for_the_whole_run(error: FetchError) -> None:
    from invio.sources.netguard import Origin
    from invio.sources.robots import RobotsCache

    calls: list[object] = []

    async def fetch(url: object) -> tuple[int, bytes]:
        calls.append(url)
        raise error

    cache = RobotsCache(fetch, clock=lambda: 1e9 if calls else 0.0)  # type: ignore[arg-type]
    origin = Origin("http", "h", 80)

    policy = await cache.policy(origin)

    assert (policy.mode, policy.expires_at) == ("disallow_all", None)
    assert await cache.policy(origin) is policy
    assert len(calls) == 1


# --- Sitemap: lines ----------------------------------------------------------------------------


def test_parse_robots_collects_sitemap_lines_in_file_order() -> None:
    policy = parse_robots(
        "Sitemap: https://example.com/a.xml\n"
        "User-agent: *\n"
        "Disallow: /x\n"
        "SITEMAP: http://example.com/b.xml  # comment\n"
        "\n"
        "User-agent: otherbot\n"
        "sitemap:https://example.com/c.xml\n"
    )

    assert policy.sitemaps == (
        "https://example.com/a.xml",
        "http://example.com/b.xml",
        "https://example.com/c.xml",
    )


def test_parse_robots_keeps_only_absolute_http_sitemap_urls() -> None:
    policy = parse_robots(
        "Sitemap: /relative.xml\n"
        "Sitemap: ftp://example.com/s.xml\n"
        "Sitemap: javascript:alert(1)\n"
        "Sitemap: https:///nohost.xml\n"
        "Sitemap: http://[::1/unclosed.xml\n"
        "Sitemap:\n"
        "Sitemap: https://example.com/ok.xml\n"
    )

    assert policy.sitemaps == ("https://example.com/ok.xml",)


def test_parse_robots_without_sitemaps_has_none() -> None:
    assert parse_robots("User-agent: *\nDisallow:\n").sitemaps == ()


async def test_robots_sitemaps_returns_the_announced_urls_and_reuses_the_cache(
    server: LoopbackServer,
) -> None:
    robots(server, "User-agent: *\nSitemap: https://example.com/s.xml\n")
    pages(server, "/page")

    async with make_client() as client:
        found = await client.robots_sitemaps(f"{server.base_url}/page")
        await client.get(f"{server.base_url}/page")

    assert found == ("https://example.com/s.xml",)
    assert server.paths().count("/robots.txt") == 1


async def test_robots_sitemaps_works_without_respecting_robots(server: LoopbackServer) -> None:
    robots(server, "Sitemap: https://example.com/s.xml\n")

    async with make_client(respect_robots=False) as client:
        found = await client.robots_sitemaps(server.base_url)

    assert found == ("https://example.com/s.xml",)
    assert server.paths() == ["/robots.txt"]


@pytest.mark.parametrize("status", [404, 500])
async def test_robots_sitemaps_is_empty_without_a_usable_robots_txt(
    server: LoopbackServer, status: int
) -> None:
    server.routes["/robots.txt"] = Route(status=status)

    async with make_client() as client:
        assert await client.robots_sitemaps(server.base_url) == ()


async def test_robots_sitemaps_is_empty_for_a_refused_origin() -> None:
    async with SafeHttpClient(HttpClientConfig(host_interval=0.01)) as client:
        assert await client.robots_sitemaps("http://10.0.0.5/") == ()
        assert await client.robots_sitemaps("ftp://example.com/") == ()


def test_parse_robots_caps_the_collected_sitemaps() -> None:
    from invio.sources.robots import MAX_SITEMAPS, parse_robots

    text = "".join(f"Sitemap: https://h/s{i}.xml\n" for i in range(MAX_SITEMAPS + 20))

    policy = parse_robots(text)

    assert len(policy.sitemaps) == MAX_SITEMAPS
    assert policy.sitemaps[0] == "https://h/s0.xml"
