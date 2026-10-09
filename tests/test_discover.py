"""Tests for source discovery: target parsing and the discovery engine (no network)."""

from collections.abc import Iterator
from datetime import UTC, datetime

import httpx2
import pytest

from invio.config.job import (
    RssSource,
    SitemapSource,
    YoutubeChannelSource,
    YoutubePlaylistSource,
)
from invio.sources import discover as discover_module
from invio.sources.discover import (
    DiscoveredSource,
    DiscoveryReport,
    DiscoveryTarget,
    FindingOrigin,
    discover,
    is_comment_feed,
    parse_target,
)
from invio.sources.youtube import YoutubeSource
from tests.discover_helpers import RSS, TEXT, XML, Site, fixture, open_client
from tests.youtube_helpers import FakeExtractor

# --- parse_target / DiscoveryTarget ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "url", "root", "folder"),
    [
        ("example.com", "https://example.com/", "https://example.com/", None),
        (
            "example.com/x?next=http://a",
            "https://example.com/x?next=http://a",
            "https://example.com/",
            None,
        ),
        ("  example.com  ", "https://example.com/", "https://example.com/", None),
        ("localhost:8080", "https://localhost:8080/", "https://localhost:8080/", None),
        ("https://example.com", "https://example.com/", "https://example.com/", None),
        ("https://example.com/", "https://example.com/", "https://example.com/", None),
        (
            "https://example.com/blog/post-1",
            "https://example.com/blog/post-1",
            "https://example.com/",
            "https://example.com/blog/",
        ),
        (
            "https://example.com/blog/",
            "https://example.com/blog/",
            "https://example.com/",
            "https://example.com/blog/",
        ),
        ("https://example.com/about", "https://example.com/about", "https://example.com/", None),
        (
            "http://Example.COM:8080/a/b?x=1#frag",
            "http://example.com:8080/a/b?x=1",
            "http://example.com:8080/",
            "http://example.com:8080/a/",
        ),
        ("https://example.com:443/feed", "https://example.com/feed", "https://example.com/", None),
        (
            "https://[2001:db8::1]:8443/x/y",
            "https://[2001:db8::1]:8443/x/y",
            "https://[2001:db8::1]:8443/",
            "https://[2001:db8::1]:8443/x/",
        ),
    ],
)
def test_parse_target_normalises(raw: str, url: str, root: str, folder: str | None) -> None:
    assert parse_target(raw) == DiscoveryTarget(url=url, root=root, folder=folder)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "ftp://example.com/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:pw@example.com/",
        "https://user@example.com/",
        "https://",
        "https:///path",
        "https://exa mple.com/",
        "https://example.com:99999/",
        "https://example.com:0/",
        "https://example.com/\nfoo",
        "mailto:someone@example.com",
        "example",
        "example/blog",
    ],
)
def test_parse_target_rejects(raw: str) -> None:
    with pytest.raises(ValueError, match=r"\S"):
        parse_target(raw)


def test_rebased_applies_the_same_rules_to_the_final_url() -> None:
    target = parse_target("example.com")

    rebased = target.rebased("https://www.example.com/home/index.html")

    assert rebased == DiscoveryTarget(
        url="https://www.example.com/home/index.html",
        root="https://www.example.com/",
        folder="https://www.example.com/home/",
    )


def test_rebased_keeps_the_target_for_an_unusable_final_url() -> None:
    target = parse_target("example.com")

    assert target.rebased("not a url") == target
    assert target.rebased("ftp://example.com/") == target


# --- engine helpers ----------------------------------------------------------------------------


@pytest.fixture
def youtube() -> Iterator[YoutubeSource]:
    adapter = YoutubeSource(extract=FakeExtractor())
    yield adapter
    adapter.close()


async def run(
    site: Site, youtube: YoutubeSource, url: str = "https://example.com/", **config: object
) -> DiscoveryReport:
    async with open_client(site, **config) as client:
        return await discover(parse_target(url), client=client, youtube=youtube)


def urls(report: DiscoveryReport) -> list[str]:
    return [str(found.source.url) for found in report.sources]  # type: ignore[union-attr]


def reasons(report: DiscoveryReport) -> dict[str, str]:
    return {rejection.locator: rejection.reason for rejection in report.rejected}


def only(report: DiscoveryReport) -> DiscoveredSource:
    (found,) = report.sources
    return found


def feed_site(page: str = "page_rss_link.html") -> Site:
    site = Site()
    site.add("/", fixture(page))
    site.add("/blog/feed.xml", fixture("feed_rss.xml"), RSS)
    return site


# --- US1: announced feeds ------------------------------------------------------------------------


async def test_discover_announced_rss_link_is_offered(youtube: YoutubeSource) -> None:
    report = await run(feed_site(), youtube)

    found = only(report)
    assert isinstance(found.source, RssSource)
    assert str(found.source.url) == "https://example.com/blog/feed.xml"
    assert found.title == "Example Blog"
    assert found.entry_count == 3
    assert found.newest == datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    assert found.origin is FindingOrigin.ANNOUNCED
    assert found.is_comment_feed is False
    assert found.final_url == "https://example.com/blog/feed.xml"
    assert report.page_problem is None


async def test_discover_offers_rss_and_atom_links_as_rss_sources(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/", fixture("page_atom_and_rss.html"))
    site.add("/feeds/rss.xml", fixture("feed_rss.xml"), RSS)
    site.add("/feeds/atom.xml", fixture("feed_atom.xml"), "application/atom+xml")

    report = await run(site, youtube)

    assert urls(report) == [
        "https://example.com/feeds/rss.xml",
        "https://example.com/feeds/atom.xml",
    ]
    assert [found.title for found in report.sources] == ["Example Blog", "Example Atom"]
    assert {found.source.type for found in report.sources} == {"rss"}


async def test_discover_respects_the_base_href(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/", fixture("page_base_href.html"))
    site.add("/site/feed.xml", fixture("feed_rss.xml"), RSS)

    report = await run(site, youtube)

    assert urls(report) == ["https://example.com/site/feed.xml"]


async def test_discover_matches_link_attributes_case_insensitively(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.add(
        "/",
        '<html><head><LINK REL="Alternate Feed" TYPE="Application/RSS+XML; charset=utf-8" '
        'HREF="/f.xml"><link rel="stylesheet" type="application/rss+xml" href="/css">'
        '<link rel="alternate" type="text/html" href="/html"></head></html>',
    )
    site.add("/f.xml", fixture("feed_rss.xml"), RSS)

    report = await run(site, youtube)

    assert urls(report) == ["https://example.com/f.xml"]
    assert "/css" not in site.paths()
    assert "/html" not in site.paths()


async def test_discover_drops_unreachable_and_invalid_findings(youtube: YoutubeSource) -> None:
    site = Site()
    links = ["good", "gone", "broken-server", "slow", "html", "garbled", "private", "huge"]
    site.add(
        "/",
        "<html><head>"
        + "".join(
            f'<link rel="alternate" type="application/rss+xml" href="/{name}">' for name in links
        )
        + "</head></html>",
    )
    site.add("/good", fixture("feed_rss.xml"), RSS)
    site.add("/gone", status=404)
    site.add("/broken-server", status=500)
    site.add("/slow", error=httpx2.ReadTimeout("slow"))
    site.add("/html", fixture("not_a_feed.html"))
    site.add("/garbled", fixture("feed_broken.xml"), RSS)
    site.redirect("/private", "http://10.0.0.5/feed")
    site.add("/huge", b"x" * 10_000, RSS, headers={"Content-Length": "10000"})

    report = await run(site, youtube)

    assert urls(report) == ["https://example.com/good"]
    why = {
        loc.removeprefix("https://example.com"): reason for loc, reason in reasons(report).items()
    }
    assert why["/gone"] == "http_status"
    assert why["/broken-server"] == "http_status"
    assert why["/slow"] == "timeout"
    assert why["/html"] == "not_a_feed_or_sitemap"
    assert why["/garbled"] == "not_a_feed_or_sitemap"
    assert why["/private"] == "non_public_address"
    assert why["/huge"] == "too_large"
    status = {r.locator.removeprefix("https://example.com"): r.status for r in report.rejected}
    assert status["/gone"] == 404
    assert status["/broken-server"] == 500


async def test_discover_direct_feed_url_is_one_direct_candidate_without_html_parsing(
    youtube: YoutubeSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(html: str) -> object:
        raise AssertionError("a feed must not be parsed as HTML")

    monkeypatch.setattr(discover_module, "parse_html", forbidden)
    site = Site()
    site.add("/feed.xml", fixture("feed_rss.xml"), RSS)

    report = await run(site, youtube, "https://example.com/feed.xml")

    found = only(report)
    assert found.origin is FindingOrigin.DIRECT
    assert found.title == "Example Blog"
    assert report.page_problem is None
    assert site.paths().count("/feed.xml") == 1  # the start page is not fetched again


async def test_discover_lists_comment_feeds_last(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/", fixture("page_comments.html"))
    site.add("/comments/feed/", fixture("feed_rss.xml"), RSS)
    site.add("/feed/", fixture("feed_atom.xml"), RSS)

    report = await run(site, youtube)

    assert urls(report) == ["https://example.com/feed/", "https://example.com/comments/feed/"]
    assert [found.is_comment_feed for found in report.sources] == [False, True]


async def test_discover_reports_a_start_page_on_a_private_address(
    youtube: YoutubeSource,
) -> None:
    site = Site()

    report = await run(site, youtube, "http://10.0.0.5/")

    assert report.sources == ()
    assert report.page_problem is not None
    assert report.page_problem.reason == "non_public_address"
    assert site.requested == []


async def test_discover_reports_a_start_page_that_fails(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/", status=503)

    report = await run(site, youtube)

    assert report.page_problem is not None
    assert (report.page_problem.reason, report.page_problem.status) == ("http_status", 503)


async def test_discover_reports_a_start_page_that_is_neither_html_nor_a_feed(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.add("/", b"just words", TEXT)

    report = await run(site, youtube)

    assert report.page_problem is not None
    assert report.page_problem.reason == "not_a_feed_or_sitemap"


async def test_discover_checks_at_most_twenty_announced_feeds(youtube: YoutubeSource) -> None:
    site = Site()
    site.add(
        "/",
        "<html><head>"
        + "".join(
            f'<link rel="alternate" type="application/rss+xml" href="/f{i}">' for i in range(25)
        )
        + "</head></html>",
    )
    for i in range(25):
        site.add(f"/f{i}", fixture("feed_rss.xml"), RSS)

    report = await run(site, youtube)

    assert len(report.sources) == 20
    assert urls(report)[0] == "https://example.com/f0"
    assert "/f20" not in site.paths()
    assert report.skipped == ("5 more announced feeds (limit 20)",)


async def test_discover_lists_a_feed_announced_twice_once_and_fetches_it_once(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.add(
        "/",
        '<link rel="alternate" type="application/rss+xml" href="/f.xml">'
        '<link rel="alternate" type="application/rss+xml" href="/f.xml?utm_source=x#top">',
    )
    site.add("/f.xml", fixture("feed_rss.xml"), RSS)

    report = await run(site, youtube)

    assert urls(report) == ["https://example.com/f.xml"]
    assert site.paths().count("/f.xml") == 1


async def test_discover_lists_feeds_with_the_same_final_url_once(youtube: YoutubeSource) -> None:
    site = Site()
    site.add(
        "/",
        '<link rel="alternate" type="application/rss+xml" href="/old">'
        '<link rel="alternate" type="application/rss+xml" href="/new">',
    )
    site.redirect("/old", "/new")
    site.add("/new", fixture("feed_rss.xml"), RSS)

    found = only(await run(site, youtube))

    assert found.final_url == "https://example.com/new"
    assert found.position == 0  # the first announcement wins


async def test_discover_redacts_rejected_urls(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/", '<link rel="alternate" type="application/rss+xml" href="/f?token=secret">')
    site.add("/f", status=500)

    report = await run(site, youtube)

    assert all("secret" not in rejection.locator for rejection in report.rejected)
    assert any(rejection.reason == "http_status" for rejection in report.rejected)


@pytest.mark.parametrize(
    ("url", "titles", "expected"),
    [
        ("https://e.com/feed", (None,), False),
        ("https://e.com/comments/feed", (None,), True),
        ("https://e.com/comments/feed/", (None,), True),
        ("https://e.com/post/comments/", (None,), True),
        ("https://e.com/x", ("Example » Comments Feed",), True),
        ("https://e.com/x", (None, "Comments for Blog"), True),
        ("https://e.com/x", ("Blog", "News"), False),
        ("https://e.com/comments/feed.xml", (None,), False),
    ],
)
def test_is_comment_feed(url: str, titles: tuple[str | None, ...], expected: bool) -> None:
    assert is_comment_feed(url, *titles) is expected


async def test_discover_marks_a_feed_as_comments_by_the_link_title_alone(
    youtube: YoutubeSource,
) -> None:
    # Neither the path nor the feed's own title says "comments": only the announcing link does.
    site = Site()
    site.add(
        "/",
        '<link rel="alternate" type="application/rss+xml" title="Talk: COMMENTS" href="/c.xml">'
        '<link rel="alternate" type="application/rss+xml" title="Blog" href="/b.xml">',
    )
    site.add("/c.xml", fixture("feed_rss.xml"), RSS)
    site.add("/b.xml", fixture("feed_atom.xml"), RSS)

    report = await run(site, youtube)

    assert urls(report) == ["https://example.com/b.xml", "https://example.com/c.xml"]
    assert [found.is_comment_feed for found in report.sources] == [False, True]
    assert report.sources[1].title == "Example Blog"


async def test_discover_resolves_a_path_relative_feed_link_against_a_deep_page(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.add("/blog/post-1", '<link rel="alternate" type="application/rss+xml" href="feed.xml">')
    site.add("/blog/feed.xml", fixture("feed_rss.xml"), RSS)

    report = await run(site, youtube, "https://example.com/blog/post-1")

    assert urls(report) == ["https://example.com/blog/feed.xml"]
    assert report.sources[0].origin is FindingOrigin.ANNOUNCED


async def test_discover_resolves_feed_links_against_the_redirected_start_page(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.redirect("/", "https://www.example.com/home/")
    site.add(
        "/home/",
        '<link rel="alternate" type="application/rss+xml" href="feed.xml">',
        host="www.example.com",
    )
    site.add("/home/feed.xml", fixture("feed_rss.xml"), RSS, host="www.example.com")

    report = await run(site, youtube)

    assert urls(report) == ["https://www.example.com/home/feed.xml"]
    assert "/home/feed.xml" not in site.paths("example.com")


async def test_discover_classifies_an_announced_sitemap_by_its_content(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.add("/", '<link rel="alternate" type="application/rss+xml" href="/announced.xml">')
    site.add("/announced.xml", fixture("sitemap_urlset.xml"), RSS)

    found = only(await run(site, youtube))

    assert isinstance(found.source, SitemapSource)
    assert found.origin is FindingOrigin.ANNOUNCED
    assert found.title is None


# --- US2: probes and robots.txt ------------------------------------------------------------------


async def test_discover_probes_sitemap_xml_at_root(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/", fixture("page_plain.html"))
    site.add("/sitemap.xml", fixture("sitemap_urlset.xml"), XML)

    found = only(await run(site, youtube))

    assert isinstance(found.source, SitemapSource)
    assert str(found.source.url) == "https://example.com/sitemap.xml"
    assert found.origin is FindingOrigin.PROBED
    assert found.entry_count == 3
    assert found.newest == datetime(2026, 9, 30, tzinfo=UTC)
    assert found.sitemap_index is False
    assert found.title is None


async def test_discover_offers_a_sitemap_index(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/", fixture("page_plain.html"))
    site.add("/sitemap.xml", fixture("sitemap_index.xml"), XML)

    found = only(await run(site, youtube))

    assert found.sitemap_index is True
    assert found.entry_count == 2
    assert "/sitemap-posts.xml" not in site.paths()  # children are not fetched


async def test_discover_does_not_offer_failed_or_unreadable_probes(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/", fixture("page_plain.html"))
    site.add("/feed", status=404)
    site.add("/rss", status=500)
    site.add("/atom.xml", fixture("not_a_feed.html"))
    site.add("/sitemap.xml", b"<urlset><url>", XML)

    report = await run(site, youtube)

    assert report.sources == ()
    assert reasons(report) == {
        "https://example.com/feed": "http_status",
        "https://example.com/rss": "http_status",
        "https://example.com/atom.xml": "not_a_feed_or_sitemap",
        "https://example.com/sitemap.xml": "not_a_feed_or_sitemap",
    }


async def test_discover_lists_a_feed_found_by_two_routes_once_as_announced(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.add("/", '<link rel="alternate" type="application/rss+xml" href="/feed">')
    site.add("/feed", fixture("feed_rss.xml"), RSS)

    found = only(await run(site, youtube))

    assert found.origin is FindingOrigin.ANNOUNCED
    assert site.paths().count("/feed") == 1


async def test_discover_probes_root_before_folder_in_path_order(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/blog/post-1", fixture("page_plain.html"))

    await run(site, youtube, "https://example.com/blog/post-1")

    probes = [path for path in site.paths() if path not in ("/robots.txt", "/blog/post-1")]
    assert probes == [
        "/feed",
        "/rss",
        "/atom.xml",
        "/sitemap.xml",
        "/blog/feed",
        "/blog/rss",
        "/blog/atom.xml",
        "/blog/sitemap.xml",
    ]


async def test_discover_probes_each_path_once_for_a_root_page(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/", fixture("page_plain.html"))

    await run(site, youtube)

    assert sorted(site.paths()) == sorted(
        ["/", "/robots.txt", "/feed", "/rss", "/atom.xml", "/sitemap.xml"]
    )


async def test_discover_reads_sitemaps_from_robots_txt(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/", fixture("page_plain.html"))
    site.add("/robots.txt", fixture("robots_sitemaps.txt"), TEXT)
    site.add("/sitemap_index.xml", fixture("sitemap_index.xml"), XML)

    found = only(await run(site, youtube))

    assert found.origin is FindingOrigin.ROBOTS
    assert found.sitemap_index is True
    assert str(found.source.url) == "https://example.com/sitemap_index.xml"  # type: ignore[union-attr]


async def test_discover_checks_at_most_five_robots_sitemaps(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/", fixture("page_plain.html"))
    site.add("/robots.txt", fixture("robots_many_sitemaps.txt"), TEXT)
    for i in range(1, 8):
        site.add(f"/map-{i}.xml", fixture("sitemap_urlset.xml"), XML)

    report = await run(site, youtube)

    assert len(report.sources) == 5
    assert "/map-6.xml" not in site.paths()
    assert report.skipped == ("2 more robots.txt sitemaps (limit 5)",)


async def test_discover_classifies_a_sitemap_served_at_a_feed_path(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.add("/", fixture("page_plain.html"))
    site.add("/feed", fixture("sitemap_urlset.xml"), RSS)

    found = only(await run(site, youtube))

    assert found.source.type == "sitemap"


async def test_discover_still_probes_when_the_start_page_fails(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/", status=500)
    site.add("/sitemap.xml", fixture("sitemap_urlset.xml"), XML)

    report = await run(site, youtube)

    assert urls(report) == ["https://example.com/sitemap.xml"]
    assert report.page_problem is not None
    assert (report.page_problem.reason, report.page_problem.status) == ("http_status", 500)


async def test_discover_probes_the_final_url_of_a_redirected_start_page(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.redirect("/", "https://www.example.com/home/")
    site.add("/home/", fixture("page_plain.html"), host="www.example.com")
    site.add("/robots.txt", fixture("robots_sitemaps.txt"), TEXT, host="www.example.com")
    site.add("/sitemap.xml", fixture("sitemap_urlset.xml"), XML, host="www.example.com")

    report = await run(site, youtube)

    assert "https://www.example.com/sitemap.xml" in urls(report)
    assert sorted(site.paths("www.example.com")) == sorted(
        [
            "/home/",
            "/robots.txt",
            "/feed",
            "/rss",
            "/atom.xml",
            "/sitemap.xml",
            "/home/feed",
            "/home/rss",
            "/home/atom.xml",
            "/home/sitemap.xml",
        ]
    )
    assert not {"/feed", "/rss", "/atom.xml", "/sitemap.xml"} & set(site.paths("example.com"))


async def test_discover_orders_announced_before_probed_before_robots(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.add("/", '<link rel="alternate" type="application/rss+xml" href="/blog/feed.xml">')
    site.add("/blog/feed.xml", fixture("feed_rss.xml"), RSS)
    site.add("/sitemap.xml", fixture("sitemap_urlset.xml"), XML)
    site.add("/robots.txt", fixture("robots_sitemaps.txt"), TEXT)
    site.add("/sitemap_index.xml", fixture("sitemap_index.xml"), XML)

    report = await run(site, youtube)

    assert [found.origin for found in report.sources] == [
        FindingOrigin.ANNOUNCED,
        FindingOrigin.PROBED,
        FindingOrigin.ROBOTS,
    ]


async def test_discover_puts_the_direct_candidate_first(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/news.xml", fixture("feed_rss.xml"), RSS)
    site.add("/sitemap.xml", fixture("sitemap_urlset.xml"), XML)

    report = await run(site, youtube, "https://example.com/news.xml")

    assert [found.origin for found in report.sources] == [
        FindingOrigin.DIRECT,
        FindingOrigin.PROBED,
    ]


async def test_discover_offers_a_start_page_that_is_a_sitemap_as_direct(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.add("/map.xml", fixture("sitemap_urlset.xml"), XML)

    found = only(await run(site, youtube, "https://example.com/map.xml"))

    assert found.origin is FindingOrigin.DIRECT
    assert found.source.type == "sitemap"


async def test_discover_never_fetches_the_start_url_twice(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/feed", fixture("feed_rss.xml"), RSS)

    report = await run(site, youtube, "https://example.com/feed")

    assert site.paths().count("/feed") == 1
    assert [found.origin for found in report.sources] == [FindingOrigin.DIRECT]


async def test_discover_offers_a_redirected_probe_at_its_final_address(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.add("/", fixture("page_plain.html"))
    site.redirect("/feed", "/feed/")
    site.add("/feed/", fixture("feed_rss.xml"), RSS)

    found = only(await run(site, youtube))

    assert str(found.source.url) == "https://example.com/feed/"  # type: ignore[union-attr]
    assert found.final_url == "https://example.com/feed/"
    assert found.origin is FindingOrigin.PROBED


async def test_discover_lists_an_announced_feed_and_a_probe_redirecting_to_it_once(
    youtube: YoutubeSource,
) -> None:
    site = feed_site()
    site.redirect("/feed", "/blog/feed.xml")

    found = only(await run(site, youtube))

    assert found.origin is FindingOrigin.ANNOUNCED
    assert found.final_url == "https://example.com/blog/feed.xml"


async def test_discover_lists_a_robots_sitemap_that_is_also_probed_once(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.add("/", fixture("page_plain.html"))
    site.add("/robots.txt", "User-agent: *\nSitemap: https://EXAMPLE.com/sitemap.xml\n", TEXT)
    site.add("/sitemap.xml", fixture("sitemap_urlset.xml"), XML)

    found = only(await run(site, youtube))

    assert found.origin is FindingOrigin.PROBED
    assert site.paths().count("/sitemap.xml") == 1


async def test_discover_reports_a_start_page_redirecting_to_a_private_address_and_still_probes(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.redirect("/", "http://10.0.0.5/admin")
    site.add("/sitemap.xml", fixture("sitemap_urlset.xml"), XML)

    report = await run(site, youtube)

    assert report.page_problem is not None
    assert report.page_problem.reason == "non_public_address"
    assert urls(report) == ["https://example.com/sitemap.xml"]
    assert all("10.0.0.5" not in url for url in site.requested)


async def test_discover_robots_txt_may_forbid_probes(youtube: YoutubeSource) -> None:
    site = Site()
    site.add("/", fixture("page_plain.html"))
    site.add("/robots.txt", "User-agent: *\nDisallow: /sitemap.xml\n", TEXT)
    site.add("/sitemap.xml", fixture("sitemap_urlset.xml"), XML)

    report = await run(site, youtube)

    assert report.sources == ()
    assert reasons(report)["https://example.com/sitemap.xml"] == "blocked_by_robots"
    assert "/sitemap.xml" not in site.paths()


async def test_discover_sniffs_html_and_feeds_without_a_content_type(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.add(
        "/",
        b"<!doctype html><link rel=alternate type=application/rss+xml href=/f.xml>",
        None,
    )
    site.add("/f.xml", fixture("feed_rss.xml"), None)

    assert urls(await run(site, youtube)) == ["https://example.com/f.xml"]


# --- US4: YouTube links --------------------------------------------------------------------------

CHANNEL_URL = "https://www.youtube.com/@somecreator/videos"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PL123"
VIDEOS = {"entries": [{"id": "abc", "title": "T", "timestamp": 1_772_000_000}]}


class ScriptedExtractor:
    """A yt-dlp stand-in answering by listing URL; unknown listings fail like a missing page."""

    def __init__(self, replies: dict[str, object] | None = None) -> None:
        self.replies = replies or {}
        self.urls: list[str] = []

    def __call__(self, url: str, options: object) -> dict[str, object]:
        self.urls.append(url)
        reply = self.replies.get(url, {"title": "Some Creator"} | VIDEOS)
        if isinstance(reply, Exception):
            raise reply
        assert isinstance(reply, dict)
        return reply


@pytest.fixture
def scripted() -> Iterator[tuple[ScriptedExtractor, YoutubeSource]]:
    extract = ScriptedExtractor()
    adapter = YoutubeSource(extract=extract)
    yield extract, adapter
    adapter.close()


def links_page(*hrefs: str) -> str:
    return "<html><body>" + "".join(f'<a href="{href}">x</a>' for href in hrefs) + "</body></html>"


async def test_discover_offers_a_linked_channel_handle(
    scripted: tuple[ScriptedExtractor, YoutubeSource],
) -> None:
    extract, adapter = scripted
    site = Site()
    site.add("/", links_page("https://www.youtube.com/@somecreator/videos"))

    found = only(await run(site, adapter))

    assert isinstance(found.source, YoutubeChannelSource)
    assert found.source.channel_id == "@somecreator"
    assert found.title == "Some Creator"
    assert found.entry_count == 1
    assert found.newest == datetime.fromtimestamp(1_772_000_000, UTC)
    assert found.origin is FindingOrigin.LINKED
    assert extract.urls == [CHANNEL_URL]


async def test_discover_offers_channel_id_links_and_link_elements(
    scripted: tuple[ScriptedExtractor, YoutubeSource],
) -> None:
    _, adapter = scripted
    site = Site()
    site.add(
        "/",
        '<link rel="me" href="https://www.youtube.com/channel/UCabc123">' + links_page(),
    )

    found = only(await run(site, adapter))

    assert isinstance(found.source, YoutubeChannelSource)
    assert found.source.channel_id == "UCabc123"


@pytest.mark.parametrize(
    "href",
    [
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/watch?v=vid1&list=PL123",
        "http://www.youtube.com/playlist?list=PL123",
    ],
)
async def test_discover_offers_linked_playlists(
    scripted: tuple[ScriptedExtractor, YoutubeSource], href: str
) -> None:
    extract, adapter = scripted
    site = Site()
    site.add("/", links_page(href))

    found = only(await run(site, adapter))

    assert isinstance(found.source, YoutubePlaylistSource)
    assert found.source.playlist_id == "PL123"
    assert extract.urls == [PLAYLIST_URL]


async def test_discover_lists_one_channel_per_identity(
    scripted: tuple[ScriptedExtractor, YoutubeSource],
) -> None:
    extract, adapter = scripted
    site = Site()
    site.add(
        "/",
        links_page(
            "https://www.youtube.com/@somecreator",
            "https://www.youtube.com/@somecreator/videos",
            "https://www.youtube.com/@SomeCreator",
            "https://m.youtube.com/@somecreator/shorts",
        ),
    )

    report = await run(site, adapter)

    assert len(report.sources) == 1
    assert len(extract.urls) == 1


async def test_discover_ignores_youtube_links_that_are_no_channel_or_playlist(
    scripted: tuple[ScriptedExtractor, YoutubeSource],
) -> None:
    extract, adapter = scripted
    site = Site()
    site.add(
        "/",
        links_page(
            "https://www.youtube.com/watch?v=vid1",
            "https://www.youtube.com/results?search_query=x",
            "https://www.youtube.com/",
            "https://notyoutube.example/@somecreator",
            "https://youtube.com.evil.example/@somecreator",
            "https://www.youtube.com:8443/@somecreator",
            "mailto:me@example.com",
        ),
    )

    report = await run(site, adapter)

    assert report.sources == ()
    assert extract.urls == []


async def test_discover_does_not_offer_failing_or_empty_youtube_listings() -> None:
    extract = ScriptedExtractor(
        {
            "https://www.youtube.com/@broken/videos": RuntimeError("boom"),
            "https://www.youtube.com/@empty/videos": {"title": "Empty", "entries": []},
        }
    )
    adapter = YoutubeSource(extract=extract)
    site = Site()
    site.add("/", links_page("https://www.youtube.com/@broken", "https://www.youtube.com/@empty"))
    try:
        report = await run(site, adapter)
    finally:
        adapter.close()

    assert report.sources == ()
    assert sorted((r.locator, r.reason) for r in report.rejected if r.locator.startswith("@")) == [
        ("@broken", "invalid_response"),
        ("@empty", "empty_listing"),
    ]


@pytest.mark.parametrize(
    ("entered", "listing_url", "kind"),
    [
        ("https://www.youtube.com/@somecreator", CHANNEL_URL, YoutubeChannelSource),
        ("youtube.com/@somecreator/videos", CHANNEL_URL, YoutubeChannelSource),
        (PLAYLIST_URL, PLAYLIST_URL, YoutubePlaylistSource),
    ],
)
async def test_discover_direct_youtube_entry_makes_no_http_request(
    scripted: tuple[ScriptedExtractor, YoutubeSource],
    entered: str,
    listing_url: str,
    kind: type,
) -> None:
    extract, adapter = scripted
    site = Site()

    found = only(await run(site, adapter, entered))

    assert isinstance(found.source, kind)
    assert found.origin is FindingOrigin.DIRECT
    assert site.requested == []
    assert extract.urls == [listing_url]


async def test_discover_direct_youtube_entry_that_fails_is_reported() -> None:
    adapter = YoutubeSource(extract=ScriptedExtractor({CHANNEL_URL: RuntimeError("boom")}))
    site = Site()
    try:
        report = await run(site, adapter, "https://www.youtube.com/@somecreator")
    finally:
        adapter.close()

    assert report.sources == ()
    assert [r.reason for r in report.rejected] == ["invalid_response"]
    assert site.requested == []


async def test_discover_checks_at_most_twenty_youtube_links(
    scripted: tuple[ScriptedExtractor, YoutubeSource],
) -> None:
    extract, adapter = scripted
    site = Site()
    site.add("/", links_page(*(f"https://www.youtube.com/@creator{i}" for i in range(23))))

    report = await run(site, adapter)

    assert len(report.sources) == 20
    assert len(extract.urls) == 20
    assert report.skipped == ("3 more YouTube links (limit 20)",)


async def test_discover_orders_youtube_after_robots_sitemaps(
    scripted: tuple[ScriptedExtractor, YoutubeSource],
) -> None:
    _, adapter = scripted
    site = Site()
    site.add("/", links_page("https://www.youtube.com/@somecreator"))
    site.add("/robots.txt", fixture("robots_sitemaps.txt"), TEXT)
    site.add("/sitemap_index.xml", fixture("sitemap_urlset.xml"), XML)

    report = await run(site, adapter)

    assert [found.origin for found in report.sources] == [
        FindingOrigin.ROBOTS,
        FindingOrigin.LINKED,
    ]


# --- request budget (SC-005) --------------------------------------------------------------------


async def test_discover_stays_within_the_request_budget(youtube: YoutubeSource) -> None:
    # 30 s at the default 1 s per host: a page in a folder (8 probes), robots.txt and 3 feeds
    # may cost 1 + 1 + 8 + 3 = 13 requests, and none is repeated.
    site = Site()
    link = '<link rel="alternate" type="application/rss+xml" href="/{}.xml">'
    site.add("/blog/post-1", "".join(link.format(name) for name in "abc"))
    site.add("/robots.txt", "User-agent: *\nAllow: /\n", TEXT)
    for name in "abc":
        site.add(f"/{name}.xml", fixture("feed_rss.xml"), RSS)

    report = await run(site, youtube, "https://example.com/blog/post-1")

    assert len(report.sources) == 3
    assert len(site.requested) == 13
    assert len(set(site.requested)) == len(site.requested)


# --- robustness ----------------------------------------------------------------------------------


@pytest.mark.parametrize("raising", ["describe_feed", "describe_sitemap"])
async def test_discover_never_raises_for_a_body_the_parsers_choke_on(
    youtube: YoutubeSource, monkeypatch: pytest.MonkeyPatch, raising: str
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RecursionError

    site = feed_site()
    site.add("/sitemap.xml", fixture("sitemap_urlset.xml"), XML)
    monkeypatch.setattr(discover_module, raising, boom)

    report = await run(site, youtube)

    assert report.sources == ()
    assert "unreadable" in {r.reason for r in report.rejected}


async def test_discover_reports_a_start_page_the_parsers_choke_on(
    youtube: YoutubeSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RecursionError

    monkeypatch.setattr(discover_module, "describe_feed", boom)

    report = await run(feed_site(), youtube)

    assert report.page_problem is not None
    assert report.page_problem.reason == "unreadable"


async def test_discover_does_not_offer_a_truncated_feed_without_resolvable_links(
    youtube: YoutubeSource,
) -> None:
    site = Site()
    site.add("/", '<link rel="alternate" type="application/rss+xml" href="/f.xml">')
    site.add(
        "/f.xml",
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        b"<item><title>A</title><link>mailto:me@example.com</link></item>",
        RSS,
    )

    report = await run(site, youtube)

    assert report.sources == ()
