"""Tests for the YouTube source adapter; yt-dlp is replaced by a fake extractor (no network)."""

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from invio.config.job import YoutubePlaylistSource
from invio.domain import Candidate, url_hash
from invio.sources.base import Source
from invio.sources.errors import FetchError, is_transient_fetch
from tests.youtube_helpers import (
    LISTING_URL,
    NOW,
    PROXY,
    FakeExtractor,
    channel,
    entry,
    listing,
    playlist,
    source,
    watch,
)

# --- US1: channels -------------------------------------------------------------------------


def test_adapter_satisfies_source_protocol() -> None:
    adapter = source(FakeExtractor())

    assert isinstance(adapter, Source)


@pytest.mark.parametrize(
    ("locator", "url"),
    [
        ("UC123", "https://www.youtube.com/channel/UC123/videos"),
        ("@some.channel", "https://www.youtube.com/@some.channel/videos"),
        ("https://www.youtube.com/@handle", "https://www.youtube.com/@handle/videos"),
        ("https://m.youtube.com/@handle/videos", "https://www.youtube.com/@handle/videos"),
        ("https://youtube.com/channel/UCx", "https://www.youtube.com/channel/UCx/videos"),
        ("https://www.youtube.com/@handle/shorts", "https://www.youtube.com/@handle/videos"),
        ("https://youtube.com/channel/UCx/shorts", "https://www.youtube.com/channel/UCx/videos"),
    ],
)
async def test_channel_locators_call_extractor_with_canonical_url(locator: str, url: str) -> None:
    extract = FakeExtractor()

    await source(extract).fetch(channel(locator))

    assert [call[0] for call in extract.calls] == [url]


async def test_entries_become_video_candidates() -> None:
    extract = FakeExtractor(listing(entry("a1", "  Two\n words ", timestamp=1_772_000_000)))

    result = await source(extract).fetch(channel())

    assert result == [
        Candidate(
            url=watch("a1"),
            url_hash=url_hash(watch("a1")),
            title="Two words",
            published_at=datetime.fromtimestamp(1_772_000_000, UTC),
            type="video",
            teaser=None,
            content_hash=None,
        )
    ]


async def test_upload_date_is_midnight_utc() -> None:
    extract = FakeExtractor(listing(entry("a", upload_date="20260301")))

    (candidate,) = await source(extract).fetch(channel())

    assert candidate.published_at == datetime(2026, 3, 1, tzinfo=UTC)


async def test_timestamp_wins_over_upload_date() -> None:
    stamp = int(datetime(2026, 3, 5, 8, 30, tzinfo=UTC).timestamp())
    extract = FakeExtractor(listing(entry("a", timestamp=stamp, upload_date="20260301")))

    (candidate,) = await source(extract).fetch(channel())

    assert candidate.published_at == datetime(2026, 3, 5, 8, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"upload_date": "2026-03-01"},
        {"upload_date": "20261301"},
        {"upload_date": 20260301},
        {"upload_date": None},
        {"timestamp": True},
        {"timestamp": "1772000000"},
        {"timestamp": float("nan")},
        {"timestamp": 10**30},
    ],
)
async def test_unusable_dates_leave_the_video_undated(extra: dict[str, object]) -> None:
    extract = FakeExtractor(listing(entry("a", **extra)))

    (candidate,) = await source(extract).fetch(channel())

    assert candidate.published_at is None


async def test_empty_channel_yields_nothing() -> None:
    assert await source(FakeExtractor(listing())).fetch(channel()) == []


async def test_extractor_options_are_metadata_only() -> None:
    extract = FakeExtractor()

    await source(extract, timeout=60.0).fetch(channel(max_items=7))

    (_, options) = extract.calls[0]
    assert options["extract_flat"] == "in_playlist"
    assert options["skip_download"] is True
    assert options["ignoreconfig"] is True
    assert options["allowed_extractors"] == ["youtube.*"]
    assert options["playlistend"] == 7
    assert options["retries"] == 0
    assert options["extractor_retries"] == 0
    assert options["cachedir"] is False
    assert options["socket_timeout"] == 20


async def test_socket_timeout_is_capped_by_the_timeout() -> None:
    extract = FakeExtractor()

    await source(extract, timeout=5.0).fetch(channel())

    assert extract.calls[0][1]["socket_timeout"] == 5.0


async def test_options_are_known_to_yt_dlp() -> None:
    import yt_dlp

    extract = FakeExtractor()
    await source(extract, cookies_file=None, proxy=PROXY).fetch(channel())
    (_, options) = extract.calls[0]
    doc = yt_dlp.YoutubeDL.__doc__ or ""
    # one ``name:  description`` line per option (indented unless the docstring is dedented)
    documented = set(re.findall(r"^[ \t]*([a-z_]+):[ \t]", doc, re.MULTILINE))
    # plus the comma-separated list of downloader parameters (``retries`` lives there)
    downloader = re.search(r"used by\s+the downloader[^:]*:\s*([a-z_,\s]+)\.", doc)
    assert downloader is not None
    documented |= {name.strip() for name in downloader.group(1).split(",")}

    assert {"proxy", "logger", "cookiefile", "extract_flat"} <= documented  # the regex works
    # ``ignoreconfig`` is read by yt-dlp's CLI option parser, not by ``YoutubeDL``: harmless here
    # and kept as a guard for the day the options are routed through the parser.
    assert sorted(set(options) - documented - {"ignoreconfig"}) == []
    with yt_dlp.YoutubeDL(dict(options)):  # constructing must neither raise nor touch the network
        pass


async def test_allowed_extractors_cover_the_listing_urls_only() -> None:
    from yt_dlp.extractor import gen_extractor_classes

    extract = FakeExtractor()
    await source(extract).fetch(channel())
    (pattern,) = extract.calls[0][1]["allowed_extractors"]  # type: ignore[misc]
    # yt-dlp matches each ``allowed_extractors`` regex in full against the lower-case IE_NAME
    allowed = [ie for ie in gen_extractor_classes() if re.fullmatch(pattern, ie.IE_NAME.lower())]
    suitable = {
        url: [ie.ie_key() for ie in allowed if ie.suitable(url)]
        for url in (
            "https://www.youtube.com/@x/videos",
            "https://www.youtube.com/channel/UC1/videos",
            "https://www.youtube.com/playlist?list=PL1",
            "https://example.com/video",
        )
    }

    assert suitable["https://www.youtube.com/@x/videos"] == ["YoutubeTab"]
    assert suitable["https://www.youtube.com/channel/UC1/videos"] == ["YoutubeTab"]
    assert suitable["https://www.youtube.com/playlist?list=PL1"] == ["YoutubeTab"]
    assert suitable["https://example.com/video"] == []


# --- US2: playlists ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "locator",
    [
        "PL123",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/watch?v=x&list=PL123",
    ],
)
async def test_playlist_locators_call_extractor_with_canonical_url(locator: str) -> None:
    extract = FakeExtractor(listing(entry("v1")))

    result = await source(extract).fetch(playlist(locator))

    assert extract.calls[0][0] == "https://www.youtube.com/playlist?list=PL123"
    assert [c.type for c in result] == ["video"]


async def test_playlist_listing_is_capped_by_max_items() -> None:
    extract = FakeExtractor()

    await source(extract).fetch(playlist(max_items=5))

    assert extract.calls[0][1]["playlistend"] == 5


def test_adapter_satisfies_playlist_protocol() -> None:
    adapter: Source[YoutubePlaylistSource] = source(FakeExtractor())

    assert isinstance(adapter, Source)


async def test_unusable_entries_are_skipped() -> None:
    extract = FakeExtractor(
        listing(
            entry("ok1", "One"),
            {"title": "no id"},
            entry("noTitle", None),
            entry("blank", "   "),
            entry("bad id!", "x"),
            entry("x" * 65, "too long"),
            entry(123, "int id"),  # type: ignore[arg-type]
            None,
            "string entry",
            entry("ok2", "Two"),
        )
    )

    result = await source(extract).fetch(playlist())

    assert [c.url for c in result] == [watch("ok1"), watch("ok2")]


@pytest.mark.parametrize("availability", ["public", "unlisted", None, 5])
async def test_listed_availabilities_are_kept(availability: object) -> None:
    extract = FakeExtractor(listing(entry("ok", "Fine", availability=availability)))

    result = await source(extract).fetch(playlist())

    assert [c.url for c in result] == [watch("ok")]


async def test_private_deleted_and_restricted_placeholders_are_skipped() -> None:
    extract = FakeExtractor(
        listing(
            entry("good1", "First"),
            entry("priv1", "[Private video]"),
            entry("del1", " [Deleted video] "),
            entry("priv2", "Members", availability="private"),
            entry("auth1", "Login", availability="needs_auth"),
            entry("subs1", "Subs", availability="subscriber_only"),
            entry("prem1", "Premium", availability="premium_only"),
            entry("good2", "Second"),
        )
    )

    result = await source(extract).fetch(playlist())

    assert [c.url for c in result] == [watch("good1"), watch("good2")]


async def test_duplicate_ids_yield_one_candidate_first_wins() -> None:
    extract = FakeExtractor(listing(entry("d", "First"), entry("d", "Second")))

    result = await source(extract).fetch(playlist())

    assert [c.title for c in result] == ["First"]


@pytest.mark.parametrize("bad", [None, [], "x", 5, {"entries": "nope"}, {"entries": {"a": 1}}, {}])
async def test_unusable_result_shape_is_invalid_response(bad: object) -> None:
    extract = FakeExtractor(bad)
    extract.result = bad

    with pytest.raises(FetchError) as info:
        await source(extract).fetch(playlist())

    assert info.value.reason == "invalid_response"


# --- US3: limits ---------------------------------------------------------------------------


def days_ago(days: int) -> int:
    return int(NOW.timestamp()) - days * 86400


async def test_max_age_days_drops_old_videos() -> None:
    extract = FakeExtractor(
        listing(entry("new", timestamp=days_ago(2)), entry("old", timestamp=days_ago(30)))
    )

    result = await source(extract).fetch(channel(max_age_days=7))

    assert [c.url for c in result] == [watch("new")]


async def test_future_date_is_clamped_to_now() -> None:
    extract = FakeExtractor(listing(entry("f", timestamp=days_ago(-5))))

    (candidate,) = await source(extract).fetch(channel())

    assert candidate.published_at == NOW


async def test_default_limit_is_twenty_newest_first() -> None:
    entries = [entry(f"v{i}", timestamp=days_ago(i + 1)) for i in range(50)]
    extract = FakeExtractor(listing(*reversed(entries)))

    result = await source(extract).fetch(channel())

    assert [c.url for c in result] == [watch(f"v{i}") for i in range(20)]


async def test_max_items_limits_the_result() -> None:
    entries = [entry(f"v{i}", timestamp=days_ago(i + 1)) for i in range(10)]

    result = await source(FakeExtractor(listing(*entries))).fetch(channel(max_items=5))

    assert len(result) == 5


async def test_undated_videos_follow_dated_ones_in_listing_order_and_count() -> None:
    extract = FakeExtractor(
        listing(
            entry("u1"),
            entry("d_old", timestamp=days_ago(9)),
            entry("u2"),
            entry("d_new", timestamp=days_ago(1)),
        )
    )

    result = await source(extract).fetch(channel(max_items=3))

    assert [c.url for c in result] == [watch("d_new"), watch("d_old"), watch("u1")]


async def test_undated_videos_survive_the_age_filter() -> None:
    extract = FakeExtractor(listing(entry("u"), entry("old", timestamp=days_ago(40))))

    result = await source(extract).fetch(channel(max_age_days=7))

    assert [c.url for c in result] == [watch("u")]


async def test_undated_listing_with_age_filter_keeps_the_first_items_in_order() -> None:
    extract = FakeExtractor(listing(*(entry(f"u{i}") for i in range(30))))

    result = await source(extract).fetch(channel(max_age_days=7))

    assert [c.url for c in result] == [watch(f"u{i}") for i in range(20)]
    assert all(c.published_at is None for c in result)


async def test_without_max_age_days_old_videos_are_kept() -> None:
    extract = FakeExtractor(listing(entry("ancient", timestamp=days_ago(3650))))
    config = channel()

    result = await source(extract).fetch(config)

    assert config.max_age_days is None
    assert [c.url for c in result] == [watch("ancient")]


async def test_one_extractor_call_per_fetch() -> None:
    extract = FakeExtractor(listing(*(entry(f"v{i}") for i in range(30))))

    await source(extract).fetch(channel())

    assert len(extract.calls) == 1


# --- US4: cookies and proxy ----------------------------------------------------------------


async def test_cookies_and_proxy_reach_the_extractor(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    extract = FakeExtractor()

    await source(extract, cookies_file=cookies, proxy=PROXY).fetch(channel())

    options = extract.calls[0][1]
    assert options["cookiefile"] == str(cookies)
    assert options["proxy"] == PROXY


@pytest.mark.parametrize("proxy", [None, ""])
async def test_without_cookies_and_proxy_the_keys_are_absent(proxy: str | None) -> None:
    extract = FakeExtractor()

    await source(extract, cookies_file=None, proxy=proxy).fetch(channel())

    options = extract.calls[0][1]
    assert "cookiefile" not in options
    assert "proxy" not in options


@pytest.mark.parametrize("kind", ["missing", "directory"])
async def test_unusable_cookies_file_fails_before_the_extractor(
    tmp_path: Path, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "cookies-secret-name.txt"
    if kind == "directory":
        path.mkdir()
    extract = FakeExtractor()

    with caplog.at_level(logging.DEBUG), pytest.raises(FetchError) as info:
        await source(extract, cookies_file=path).fetch(channel())

    assert (info.value.reason, info.value.url) == ("cookies_unavailable", LISTING_URL)
    assert not is_transient_fetch(info.value)
    assert "cookies-secret-name" not in str(info.value)
    assert "cookies-secret-name" not in caplog.text
    assert extract.calls == []


@pytest.mark.parametrize("path", [Path(""), Path(".")])
async def test_blank_cookies_path_is_not_a_readable_file(path: Path) -> None:
    extract = FakeExtractor()

    with pytest.raises(FetchError) as info:
        await source(extract, cookies_file=path).fetch(channel())

    assert info.value.reason == "cookies_unavailable"
    assert extract.calls == []


async def test_unreadable_cookies_file_fails(tmp_path: Path) -> None:
    import os

    path = tmp_path / "c.txt"
    path.write_text("x")
    path.chmod(0)
    extract = FakeExtractor()
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running with privileges that ignore file modes")
        with pytest.raises(FetchError) as info:
            await source(extract, cookies_file=path).fetch(channel())
    finally:
        path.chmod(0o600)

    assert info.value.reason == "cookies_unavailable"
    assert extract.calls == []
