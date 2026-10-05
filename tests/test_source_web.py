"""Tests for the web page source against pages served by a loopback origin (static fetch)."""

import codecs
import hashlib
from collections.abc import AsyncIterator

import pytest

from invio.config.job import WebSource
from invio.domain import Candidate, url_hash
from invio.sources.base import Source
from invio.sources.http import (
    BlockedError,
    BlockReason,
    FetchError,
    HttpClientConfig,
    SafeHttpClient,
    TooLargeError,
)
from invio.sources.text import TEASER_MAX_CHARS
from invio.sources.urls import canonical_url
from invio.sources.web import WebPageSource
from tests.http_helpers import (  # noqa: F401
    HTML,
    LOOPBACK,
    LoopbackServer,
    Route,
    server,
    web_source,
)


@pytest.fixture
async def client() -> AsyncIterator[SafeHttpClient]:
    config = HttpClientConfig(respect_robots=False, host_interval=0.01)
    async with SafeHttpClient(config, allow_networks=LOOPBACK) as instance:
        yield instance


def web_config(server: LoopbackServer, path: str = "/page", **fields: object) -> WebSource:
    return web_source(f"{server.base_url}{path}", **fields)


def serve(server: LoopbackServer, body: str | bytes, path: str = "/page", **route: object) -> None:
    data = body.encode() if isinstance(body, str) else body
    headers = dict(route.pop("headers", HTML))  # type: ignore[call-overload]
    server.routes[path] = Route(headers=headers, body=data, **route)  # type: ignore[arg-type]


def page(*paragraphs: str, title: str = "A page", head: str = "") -> str:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return f"<html><head><title>{title}</title>{head}</head><body>{body}</body></html>"


async def fetch_fresh(config: WebSource) -> list[Candidate]:
    """Fetch with a new client, as separate runs do (no validators, so no 304)."""
    run_config = HttpClientConfig(respect_robots=False, host_interval=0.01)
    async with SafeHttpClient(run_config, allow_networks=LOOPBACK) as fresh:
        return await WebPageSource(fresh).fetch(config)


# --- acceptance criteria 1 and 2 -------------------------------------------------------------


async def test_unchanged_page_has_the_same_hash_in_ten_runs(server: LoopbackServer) -> None:
    serve(server, page("Alpha", "Beta"))

    hashes = set()
    for _ in range(10):
        [candidate] = await fetch_fresh(web_config(server))
        hashes.add(candidate.content_hash)

    assert len(hashes) == 1
    assert hashes != {None}


async def test_changed_paragraph_changes_the_hash(server: LoopbackServer) -> None:
    serve(server, page("Alpha", "Beta"))
    [before] = await fetch_fresh(web_config(server))

    serve(server, page("Alpha", "Gamma"))
    [after] = await fetch_fresh(web_config(server))

    assert before.content_hash != after.content_hash


async def test_markup_script_style_comment_and_whitespace_changes_keep_the_hash(
    server: LoopbackServer,
) -> None:
    serve(server, page("Alpha", "Beta"))
    [before] = await fetch_fresh(web_config(server))
    noisy = (
        "<!DOCTYPE html>\n<html lang=en>\n<head>\n  <title>Another title</title>\n"
        "  <style>p { color: red }</style><script>var t = Date.now()</script>\n</head>\n"
        '<body class="x">\n  <!-- built at 12:00 -->\n\n   <p class="a">  Alpha </p>\n'
        "   <div>\n<p><b>Be</b>ta</p></div>\n<script>track()</script>\n</body></html>"
    )
    serve(server, noisy)

    [after] = await fetch_fresh(web_config(server))

    assert after.content_hash == before.content_hash


# --- candidate fields ------------------------------------------------------------------------


async def test_page_candidate_fields(server: LoopbackServer) -> None:
    serve(server, page("Hello", "world", title="  My \n page "))
    config = web_config(server)

    [candidate] = await fetch_fresh(config)

    url = canonical_url(str(config.url))
    assert candidate == Candidate(
        url=url,
        url_hash=url_hash(url),
        title="My page",
        published_at=None,
        type="article",
        teaser="Hello world",
        content_hash=hashlib.sha256(b"Hello world").hexdigest(),
    )


async def test_candidate_url_is_the_configured_url_even_after_a_redirect(
    server: LoopbackServer,
) -> None:
    server.routes["/old"] = Route(status=301, headers={"Location": "/new"})
    serve(server, page("Moved"), path="/new")
    config = web_config(server, "/old")

    [candidate] = await fetch_fresh(config)

    assert candidate.url == canonical_url(str(config.url))
    assert candidate.url.endswith("/old")
    assert candidate.teaser == "Moved"


async def test_configured_url_is_canonicalized(server: LoopbackServer) -> None:
    serve(server, page("x"), path="/page?a=1&utm_source=feed")
    config = web_config(server, "/page?a=1&utm_source=feed#top")

    [candidate] = await fetch_fresh(config)

    assert candidate.url == f"{server.base_url}/page?a=1"
    assert candidate.url_hash == url_hash(candidate.url)


async def test_title_falls_back_to_the_url(server: LoopbackServer) -> None:
    serve(server, "<html><body><p>No title here</p></body></html>")

    [candidate] = await fetch_fresh(web_config(server))

    assert candidate.title == candidate.url


async def test_long_text_gives_a_cut_teaser_that_is_not_part_of_the_hash(
    server: LoopbackServer,
) -> None:
    words = " ".join(f"word{i}" for i in range(200))
    serve(server, page(words))

    [candidate] = await fetch_fresh(web_config(server))

    assert candidate.teaser is not None
    assert len(candidate.teaser) <= TEASER_MAX_CHARS
    assert candidate.teaser.endswith("…")
    assert candidate.content_hash == hashlib.sha256(words.encode()).hexdigest()


async def test_empty_body_gives_a_candidate_with_the_empty_hash_and_no_teaser(
    server: LoopbackServer,
) -> None:
    serve(server, "<html><head><title>Empty</title></head><body>  \n </body></html>")

    [candidate] = await fetch_fresh(web_config(server))

    assert candidate.content_hash == hashlib.sha256(b"").hexdigest()
    assert candidate.teaser is None
    assert candidate.title == "Empty"


async def test_document_without_body_uses_the_whole_document(server: LoopbackServer) -> None:
    serve(server, "<html><head><title>T</title></head></html>")

    [candidate] = await fetch_fresh(web_config(server))

    assert candidate.content_hash == hashlib.sha256(b"").hexdigest()


async def test_page_is_decoded_with_its_declared_charset(server: LoopbackServer) -> None:
    serve(
        server,
        page("Grüße aus Köln").encode("iso-8859-1"),
        headers={"Content-Type": "text/html; charset=iso-8859-1"},
    )

    [candidate] = await fetch_fresh(web_config(server))

    assert candidate.teaser == "Grüße aus Köln"
    assert candidate.content_hash == hashlib.sha256("Grüße aus Köln".encode()).hexdigest()


async def test_html_without_content_type_is_accepted(server: LoopbackServer) -> None:
    serve(server, "<!doctype html>" + page("Sniffed"), headers={})

    [candidate] = await fetch_fresh(web_config(server))

    assert candidate.teaser == "Sniffed"


# --- other outcomes --------------------------------------------------------------------------


async def test_not_modified_gives_no_candidates(server: LoopbackServer) -> None:
    server.routes["/page"] = Route(
        headers=HTML | {"ETag": '"v1"'}, body=page("Alpha").encode(), conditional=True
    )
    config = HttpClientConfig(respect_robots=False, host_interval=0.01)
    async with SafeHttpClient(config, allow_networks=LOOPBACK) as client:
        source = WebPageSource(client)
        first = await source.fetch(web_config(server))
        second = await source.fetch(web_config(server))

    assert len(first) == 1
    assert second == []


@pytest.mark.parametrize(
    ("content_type", "body"),
    [("application/pdf", b"%PDF-1.7 ..."), ("application/json", b'{"a": 1}')],
)
async def test_non_html_response_is_an_error(
    server: LoopbackServer, content_type: str, body: bytes
) -> None:
    serve(server, body, headers={"Content-Type": content_type})

    with pytest.raises(FetchError) as info:
        await fetch_fresh(web_config(server))

    assert info.value.reason == "not_html"
    assert info.value.url == f"{server.base_url}/page"


async def test_http_error_status_is_the_clients_error(server: LoopbackServer) -> None:
    with pytest.raises(FetchError) as info:
        await fetch_fresh(web_config(server, "/missing"))

    assert info.value.reason == "http_status"
    assert info.value.status == 404


async def test_robots_refusal_is_the_clients_blocked_error(server: LoopbackServer) -> None:
    server.routes["/robots.txt"] = Route(
        headers={"Content-Type": "text/plain"}, body=b"User-agent: *\nDisallow: /page\n"
    )
    serve(server, page("Alpha"))
    config = HttpClientConfig(respect_robots=True, host_interval=0.01)

    async with SafeHttpClient(config, allow_networks=LOOPBACK) as robots_client:
        with pytest.raises(BlockedError) as info:
            await WebPageSource(robots_client).fetch(web_config(server))

    assert info.value.reason == BlockReason.BLOCKED_BY_ROBOTS
    assert "/page" not in server.paths()


async def test_private_target_is_the_clients_blocked_error(server: LoopbackServer) -> None:
    serve(server, page("Alpha"))

    async with SafeHttpClient(HttpClientConfig(respect_robots=False)) as guarded:
        with pytest.raises(BlockedError) as info:
            await WebPageSource(guarded).fetch(web_config(server))

    assert info.value.reason == BlockReason.NON_PUBLIC_ADDRESS
    assert server.paths() == []


async def test_oversized_page_is_the_clients_too_large_error(server: LoopbackServer) -> None:
    serve(server, page("x" * 5000))
    config = HttpClientConfig(respect_robots=False, host_interval=0.01, max_response_bytes=1024)

    async with SafeHttpClient(config, allow_networks=LOOPBACK) as small:
        with pytest.raises(TooLargeError) as info:
            await WebPageSource(small).fetch(web_config(server))

    assert info.value.reason == "too_large"


async def test_a_page_that_never_finishes_is_the_clients_timeout(server: LoopbackServer) -> None:
    serve(server, page("Alpha"), stall=True)
    config = HttpClientConfig(
        respect_robots=False, host_interval=0.01, read_timeout=0.3, total_timeout=0.5
    )

    async with SafeHttpClient(config, allow_networks=LOOPBACK) as impatient:
        with pytest.raises(FetchError) as info:
            await WebPageSource(impatient).fetch(web_config(server))

    assert info.value.reason == "timeout"


async def test_a_failed_fetch_does_not_affect_the_next_source(
    server: LoopbackServer, client: SafeHttpClient
) -> None:
    serve(server, b"%PDF-1.7", path="/doc", headers={"Content-Type": "application/pdf"})
    serve(server, page("Alpha"))
    source = WebPageSource(client)

    with pytest.raises(FetchError):
        await source.fetch(web_config(server, "/doc"))
    [candidate] = await source.fetch(web_config(server))

    assert candidate.teaser == "Alpha"


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (page("Grüße aus Köln").encode("utf-8"), "text/html; charset=utf-8"),
        (page("Grüße aus Köln").encode("cp1252"), "text/html; charset=windows-1252"),
        (
            page("Grüße aus Köln", head='<meta charset="iso-8859-15">').encode("iso-8859-15"),
            "text/html",
        ),
        (codecs.BOM_UTF16_LE + page("Grüße aus Köln").encode("utf-16-le"), "text/html"),
    ],
    ids=["utf-8", "header-cp1252", "meta-latin9", "utf-16-bom"],
)
async def test_same_text_in_different_encodings_has_the_same_hash(
    server: LoopbackServer, body: bytes, content_type: str
) -> None:
    serve(server, body, headers={"Content-Type": content_type})

    [candidate] = await fetch_fresh(web_config(server))

    assert candidate.content_hash == hashlib.sha256("Grüße aus Köln".encode()).hexdigest()


def test_web_page_source_satisfies_the_source_contract() -> None:
    assert isinstance(WebPageSource(SafeHttpClient()), Source)


# --- selector (user story 2) -----------------------------------------------------------------


def region_page(
    news: str = "Release 1", sidebar: str = "Trending", footer: str = "2026", script: str = "a()"
) -> str:
    return (
        "<html><head><title>Site</title></head><body>"
        f"<header>Header {sidebar}</header><aside>{sidebar}</aside>"
        f'<main><div class="posts"><p>{news}</p></div></main>'
        f"<footer>{footer}</footer><script>{script}</script></body></html>"
    )


async def selected_hash(server: LoopbackServer, html: str, selector: str = ".posts") -> str | None:
    serve(server, html)
    [candidate] = await fetch_fresh(web_config(server, selector=selector))
    return candidate.content_hash


async def test_selector_limits_the_hash_to_the_region(server: LoopbackServer) -> None:
    serve(server, region_page())
    [candidate] = await fetch_fresh(web_config(server, selector=".posts"))

    assert candidate.content_hash == hashlib.sha256(b"Release 1").hexdigest()
    assert candidate.teaser == "Release 1"


async def test_change_inside_the_region_changes_the_hash(server: LoopbackServer) -> None:
    before = await selected_hash(server, region_page(news="Release 1"))
    after = await selected_hash(server, region_page(news="Release 2"))

    assert before != after


@pytest.mark.parametrize(
    "outside",
    [
        {"sidebar": "Other trending"},
        {"footer": "2027"},
        {"script": "b()"},
        {"sidebar": "x", "footer": "y", "script": "z"},
    ],
)
async def test_change_outside_the_region_keeps_the_hash(
    server: LoopbackServer, outside: dict[str, str]
) -> None:
    before = await selected_hash(server, region_page())
    after = await selected_hash(server, region_page(**outside))

    assert before == after


async def test_selector_with_several_matches_uses_all_in_document_order(
    server: LoopbackServer,
) -> None:
    def two(first: str, second: str) -> str:
        return (
            "<body><section class=a>x</section>"
            f"<div class=m>{first}</div><p>between</p><div class=m>{second}</div></body>"
        )

    serve(server, two("one", "two"))
    [candidate] = await fetch_fresh(web_config(server, selector=".m"))
    swapped = await selected_hash(server, two("two", "one"), ".m")

    assert candidate.content_hash == hashlib.sha256(b"one two").hexdigest()
    assert swapped == hashlib.sha256(b"two one").hexdigest()
    assert swapped != candidate.content_hash


async def test_selector_without_match_is_an_error_and_does_not_fall_back_to_the_body(
    server: LoopbackServer,
) -> None:
    serve(server, region_page())

    with pytest.raises(FetchError) as info:
        await fetch_fresh(web_config(server, selector=".nope"))

    assert info.value.reason == "selector_not_found"
    assert info.value.url == f"{server.base_url}/page"


async def test_selector_matching_only_empty_elements_gives_the_empty_hash(
    server: LoopbackServer,
) -> None:
    serve(server, region_page().replace("<p>Release 1</p>", "<p> </p><script>x()</script>"))

    [candidate] = await fetch_fresh(web_config(server, selector=".posts"))

    assert candidate.content_hash == hashlib.sha256(b"").hexdigest()
    assert candidate.teaser is None


async def test_title_still_comes_from_the_document_title(server: LoopbackServer) -> None:
    serve(server, region_page())

    [candidate] = await fetch_fresh(web_config(server, selector=".posts"))

    assert candidate.title == "Site"


# --- links mode (user story 3) ---------------------------------------------------------------


def anchors(*hrefs: str) -> str:
    return "".join(f'<a href="{href}">Link {i}</a>' for i, href in enumerate(hrefs))


def index(body: str, head: str = "") -> str:
    return f"<html><head><title>Index</title>{head}</head><body>{body}</body></html>"


async def links(server: LoopbackServer, html: str, **fields: object) -> list[Candidate]:
    serve(server, html)
    return await fetch_fresh(web_config(server, mode="links", **fields))


def urls(candidates: list[Candidate]) -> list[str]:
    return [candidate.url for candidate in candidates]


async def test_url_pattern_keeps_only_matching_urls_on_any_host_in_page_order(
    server: LoopbackServer,
) -> None:
    html = index(
        anchors(
            "https://other.example/news/2026/b",
            "/about",
            "/news/2026/a",
            "https://other.example/blog",
            "https://third.example/news/2027/c",
        )
    )

    result = await links(server, html, url_pattern=r"/news/\d{4}/")

    assert urls(result) == [
        "https://other.example/news/2026/b",
        f"{server.base_url}/news/2026/a",
        "https://third.example/news/2027/c",
    ]
    assert all("/news/" in url for url in urls(result))


async def test_url_pattern_has_search_semantics_and_anchors_work(server: LoopbackServer) -> None:
    html = index(anchors("https://a.example/x/post", "https://b.example/post/x"))

    anywhere = await links(server, html, url_pattern="post")
    anchored = await links(server, html, url_pattern=r"^https://b\.example/")

    assert len(anywhere) == 2
    assert urls(anchored) == ["https://b.example/post/x"]


async def test_without_a_pattern_only_same_host_links_are_kept(server: LoopbackServer) -> None:
    port = server.origin_port
    html = index(
        anchors(
            "/a",
            f"http://127.0.0.1:{port}/b",
            "http://localhost/c",  # a different host name for the same machine
            "https://elsewhere.example/d",
            "relative/e",
        )
    )

    result = await links(server, html)

    assert urls(result) == [
        f"{server.base_url}/a",
        f"{server.base_url}/b",
        f"{server.base_url}/relative/e",
    ]


async def test_relative_links_resolve_against_the_final_url(server: LoopbackServer) -> None:
    server.routes["/old"] = Route(status=302, headers={"Location": "/dir/new"})
    serve(server, index(anchors("item", "../up", "/root")), path="/dir/new")

    serve_old = web_config(server, "/old", mode="links")
    result = await fetch_fresh(serve_old)

    assert urls(result) == [
        f"{server.base_url}/dir/item",
        f"{server.base_url}/up",
        f"{server.base_url}/root",
    ]


async def test_base_href_is_used_to_resolve_links(server: LoopbackServer) -> None:
    html = index(anchors("item", "/abs"), head='<base href="/docs/"><base href="/ignored/">')

    result = await links(server, html)

    assert urls(result) == [f"{server.base_url}/docs/item", f"{server.base_url}/abs"]


async def test_invalid_base_href_is_ignored(server: LoopbackServer) -> None:
    html = index(anchors("item"), head='<base href="mailto:x@example.org">')

    result = await links(server, html)

    assert urls(result) == [f"{server.base_url}/item"]


async def test_duplicates_after_canonicalization_appear_once_and_the_first_wins(
    server: LoopbackServer,
) -> None:
    html = index(
        '<a href="/post?utm_source=a">First</a>'
        '<a href="/post#comments">Second</a>'
        f'<a href="{server.base_url}/post?fbclid=1">Third</a>'
        '<a href="/post?id=2">Other</a>'
    )

    result = await links(server, html)

    assert [(c.url, c.title) for c in result] == [
        (f"{server.base_url}/post", "First"),
        (f"{server.base_url}/post?id=2", "Other"),
    ]


async def test_the_page_itself_is_dropped(server: LoopbackServer) -> None:
    server.routes["/old"] = Route(status=302, headers={"Location": "/new"})
    serve(
        server,
        index(anchors("/old", "/new", "/new?utm_campaign=x", "#top", "", "/other")),
        path="/new",
    )

    result = await fetch_fresh(web_config(server, "/old", mode="links"))

    assert urls(result) == [f"{server.base_url}/other"]


@pytest.mark.parametrize(
    "href",
    [
        "mailto:me@example.org",
        "javascript:void(0)",
        "tel:+4912345",
        "ftp://files.example.org/a",
        "#section",
        "http://user@",
        "http://:80/x",
        "http://[::1/x",
    ],
)
async def test_unusable_links_are_ignored(server: LoopbackServer, href: str) -> None:
    html = index(anchors(href, "/good"))

    result = await links(server, html, url_pattern=".")

    assert urls(result) == [f"{server.base_url}/good"]


async def test_anchor_without_href_is_ignored(server: LoopbackServer) -> None:
    result = await links(server, index('<a name="x">no href</a><a href="/y">y</a>'))

    assert urls(result) == [f"{server.base_url}/y"]


async def test_selector_limits_links_to_the_region(server: LoopbackServer) -> None:
    html = index(
        '<nav><a href="/nav">Nav</a></nav>'
        '<main><a href="/in1">One</a><p><a href="/in2">Two</a></p></main>'
        '<footer><a href="/foot">Foot</a></footer>'
    )

    result = await links(server, html, selector="main")

    assert urls(result) == [f"{server.base_url}/in1", f"{server.base_url}/in2"]


async def test_selector_may_match_the_anchors_themselves(server: LoopbackServer) -> None:
    html = index(
        '<a class="post" href="/p1">P1</a><a href="/skip">S</a><a class="post" href="/p2">'
    )

    result = await links(server, html, selector="a.post")

    assert urls(result) == [f"{server.base_url}/p1", f"{server.base_url}/p2"]


async def test_selector_without_match_is_an_error_in_links_mode(server: LoopbackServer) -> None:
    with pytest.raises(FetchError) as info:
        await links(server, index(anchors("/a")), selector=".nope")

    assert info.value.reason == "selector_not_found"


async def test_link_candidate_fields(server: LoopbackServer) -> None:
    html = index(
        '<a href="/a">  Hello \n <b>big</b>   world </a><a href="/b"><img src="x.png"></a>'
    )

    first, second = await links(server, html)

    url = f"{server.base_url}/a"
    assert first == Candidate(
        url=url,
        url_hash=url_hash(url),
        title="Hello big world",
        published_at=None,
        type="article",
        teaser=None,
        content_hash=None,
    )
    assert second.title == second.url  # no link text: the URL
    assert second.url_hash == url_hash(second.url)


async def test_page_without_qualifying_links_gives_no_candidates(server: LoopbackServer) -> None:
    html = index(anchors("https://elsewhere.example/a", "mailto:x@example.org"))

    assert await links(server, html) == []
    assert await links(server, "<html><body>No links</body></html>") == []


def test_host_comparison_is_case_insensitive_and_exact() -> None:
    from invio.sources.web import _links, _parse

    tree = _parse(
        index(
            anchors(
                "https://EXAMPLE.org/a", "https://www.example.org/b", "https://example.org:8443/c"
            )
        )
    )

    result = _links(
        tree,
        [tree.body] if tree.body else [],
        final_url="https://Example.ORG/page",
        page_urls=frozenset({"https://example.org/page"}),
        url_pattern=None,
    )

    assert urls(result) == ["https://example.org/a", "https://example.org:8443/c"]


async def test_links_mode_fetches_only_the_index_page(server: LoopbackServer) -> None:
    serve(server, "<html><body>article</body></html>", path="/a")

    found = await links(server, index(anchors("/a", "/b")))

    assert urls(found) == [f"{server.base_url}/a", f"{server.base_url}/b"]
    assert server.paths() == ["/page"]  # the articles themselves are never fetched


async def test_links_mode_returns_every_match_without_a_cap(server: LoopbackServer) -> None:
    hrefs = [f"/news/{i}" for i in range(1500)]

    found = await links(server, index(anchors(*hrefs)), url_pattern=r"/news/\d+$")

    assert urls(found) == [f"{server.base_url}{href}" for href in hrefs]


async def test_links_mode_not_modified_gives_no_candidates(server: LoopbackServer) -> None:
    server.routes["/page"] = Route(
        headers=HTML | {"ETag": '"v1"'}, body=index(anchors("/a")).encode(), conditional=True
    )
    config = HttpClientConfig(respect_robots=False, host_interval=0.01)
    async with SafeHttpClient(config, allow_networks=LOOPBACK) as client:
        source = WebPageSource(client)
        first = await source.fetch(web_config(server, mode="links"))
        second = await source.fetch(web_config(server, mode="links"))

    assert urls(first) == [f"{server.base_url}/a"]
    assert second == []


async def test_non_html_response_is_an_error_in_links_mode(server: LoopbackServer) -> None:
    serve(server, b'{"a": "/x"}', headers={"Content-Type": "application/json"})

    with pytest.raises(FetchError) as info:
        await fetch_fresh(web_config(server, mode="links"))

    assert info.value.reason == "not_html"


async def test_link_title_ignores_scripts_and_styles_inside_the_anchor(
    server: LoopbackServer,
) -> None:
    html = index('<a href="/a">Read <script>evil()</script><style>a{}</style>more</a>')

    [candidate] = await links(server, html)

    assert candidate.title == "Read more"


async def test_link_title_is_nfc(server: LoopbackServer) -> None:
    html = index('<a href="/a">Cafe\u0301 news</a>')  # "e" + combining acute accent

    [candidate] = await links(server, html)

    assert candidate.title == "Caf\u00e9 news"


async def test_single_page_app_routes_are_separate_links(server: LoopbackServer) -> None:
    html = index(anchors("#/post/1", "#!/post/2", "/page#/post/1", "#section", "/other#top"))

    result = await links(server, html)

    base = f"{server.base_url}/page"
    assert urls(result) == [f"{base}#/post/1", f"{base}#!/post/2", f"{server.base_url}/other"]


async def test_a_route_fragment_is_matched_by_url_pattern(server: LoopbackServer) -> None:
    html = index(anchors("#/post/1", "#/about"))

    result = await links(server, html, url_pattern="#/post/")

    assert urls(result) == [f"{server.base_url}/page#/post/1"]
