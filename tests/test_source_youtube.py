"""Tests for the YouTube source adapter; yt-dlp is replaced by a fake extractor (no network)."""

import asyncio
import logging
import re
import stat
import threading
import urllib.error
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from yt_dlp.networking.exceptions import HTTPError as YtHTTPError
from yt_dlp.networking.exceptions import TransportError
from yt_dlp.utils import DownloadError, ExtractorError

from invio.config.job import YoutubeChannelSource, YoutubePlaylistSource
from invio.domain import Candidate, url_hash
from invio.sources import youtube as youtube_module
from invio.sources.base import Source
from invio.sources.errors import FetchError, is_transient_fetch
from invio.sources.youtube import YoutubeSource

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
PROXY = "http://user:secret@proxy.invalid:8080"
RAW_MESSAGE = "ERROR: [youtube:tab] sensitive-library-text"


class FakeExtractor:
    """Records ``(url, options)`` calls and returns ``result`` or raises ``exc``."""

    def __init__(
        self,
        result: object = None,
        *,
        exc: BaseException | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        self.result: object = {"entries": []} if result is None else result
        self.exc = exc
        self.gate = gate
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.threads: list[str] = []

    def __call__(self, url: str, options: Mapping[str, object]) -> Mapping[str, object]:
        self.calls.append((url, options))
        self.threads.append(threading.current_thread().name)
        if self.gate is not None:
            self.gate.wait(10)
        if self.exc is not None:
            raise self.exc
        return self.result  # type: ignore[return-value]


@dataclass
class FakeYoutubeDL:
    """Stands in for ``yt_dlp.YoutubeDL``; ``__exit__`` rewrites the cookie file like yt-dlp."""

    result: Mapping[str, object] = field(default_factory=lambda: {"entries": []})
    params: Mapping[str, object] | None = None
    url: str | None = None
    download: bool | None = None
    closed: bool = False
    cookie_copy_mode: int | None = None
    cookie_copy_text: str | None = None

    def __call__(self, params: Mapping[str, object]) -> "FakeYoutubeDL":
        self.params = params
        return self

    def __enter__(self) -> "FakeYoutubeDL":
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True
        cookiefile = (self.params or {}).get("cookiefile")
        if isinstance(cookiefile, str):
            Path(cookiefile).write_text("# rewritten by yt-dlp\n")  # what save_cookies() does

    def extract_info(self, url: str, *, download: bool) -> Mapping[str, object]:
        self.url, self.download = url, download
        cookiefile = (self.params or {}).get("cookiefile")
        if isinstance(cookiefile, str):
            path = Path(cookiefile)
            self.cookie_copy_mode = stat.S_IMODE(path.stat().st_mode)
            self.cookie_copy_text = path.read_text()
        return self.result


@pytest.fixture
def fake_ydl(monkeypatch: pytest.MonkeyPatch) -> FakeYoutubeDL:
    """Replace ``yt_dlp.YoutubeDL`` so the real default extractor runs without the network."""
    import yt_dlp

    fake = FakeYoutubeDL()
    monkeypatch.setattr(yt_dlp, "YoutubeDL", fake)
    return fake


@pytest.fixture(autouse=True)
def _no_real_yt_dlp(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Network guard: the default extractor fails unless the test replaces ``YoutubeDL``."""
    if "fake_ydl" in request.fixturenames:
        return

    def forbidden(url: str, options: Mapping[str, object]) -> Mapping[str, object]:
        raise AssertionError("the real yt-dlp extractor must not run in tests")

    monkeypatch.setattr(youtube_module, "_yt_dlp_extract", forbidden)


def entry(vid: str = "abc", title: str | None = "T", **extra: object) -> dict[str, object]:
    data: dict[str, object] = {"id": vid, **extra}
    if title is not None:
        data["title"] = title
    return data


def listing(*entries: object) -> dict[str, object]:
    return {"entries": list(entries)}


def channel(value: str = "UC123", **fields: object) -> YoutubeChannelSource:
    return YoutubeChannelSource.model_validate(
        {"type": "youtube_channel", "channel_id": value} | fields
    )


def playlist(value: str = "PL123", **fields: object) -> YoutubePlaylistSource:
    return YoutubePlaylistSource.model_validate(
        {"type": "youtube_playlist", "playlist_id": value} | fields
    )


def source(extract: FakeExtractor, **kwargs: object) -> YoutubeSource:
    return YoutubeSource(extract=extract, now=lambda: NOW, **kwargs)  # type: ignore[arg-type]


def watch(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


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


async def test_default_extractor_passes_url_and_options_to_yt_dlp(
    fake_ydl: FakeYoutubeDL,
) -> None:
    fake_ydl.result = listing(entry("zz", "Real path"))

    result = await YoutubeSource(now=lambda: NOW).fetch(channel())

    assert [c.url for c in result] == [watch("zz")]
    assert fake_ydl.url == "https://www.youtube.com/channel/UC123/videos"
    assert fake_ydl.download is False
    assert fake_ydl.closed is True
    assert fake_ydl.params is not None
    assert "cookiefile" not in fake_ydl.params


async def test_default_extractor_gives_yt_dlp_a_private_copy_of_the_cookies(
    fake_ydl: FakeYoutubeDL, tmp_path: Path
) -> None:
    cookies = tmp_path / "cookies.txt"
    original = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tv\n"
    cookies.write_bytes(original)
    cookies.chmod(0o400)  # read-only: yt-dlp's write-back in __exit__ must not need it writable
    try:
        result = await YoutubeSource(cookies_file=cookies, now=lambda: NOW).fetch(channel())
    finally:
        cookies.chmod(0o600)

    assert result == []
    assert fake_ydl.params is not None
    copy = Path(str(fake_ydl.params["cookiefile"]))
    assert copy != cookies
    assert fake_ydl.cookie_copy_text == original.decode()
    assert fake_ydl.cookie_copy_mode == 0o600
    assert cookies.read_bytes() == original  # byte-identical despite the write-back
    assert not copy.parent.exists()


def test_cookie_copies_are_private_separate_and_removed(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    seen: list[Path] = []

    with (
        youtube_module._private_cookie_copy(cookies) as first,
        youtube_module._private_cookie_copy(cookies) as second,
    ):
        seen += [first, second]
        assert first != second
        assert first.read_text() == second.read_text() == "# Netscape HTTP Cookie File\n"
        assert stat.S_IMODE(first.stat().st_mode) == 0o600
        assert stat.S_IMODE(first.parent.stat().st_mode) == 0o700
        first.write_text("rewritten")

    assert not any(path.parent.exists() for path in seen)
    assert cookies.read_text() == "# Netscape HTTP Cookie File\n"


def test_cookie_copy_is_removed_when_the_body_fails(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("x")
    seen: list[Path] = []

    with pytest.raises(RuntimeError), youtube_module._private_cookie_copy(cookies) as copy:
        seen.append(copy)
        raise RuntimeError

    assert not seen[0].parent.exists()


async def test_cookies_vanishing_before_the_copy_is_cookies_unavailable(
    fake_ydl: FakeYoutubeDL, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookies = tmp_path / "cookies-secret-name.txt"
    cookies.write_text("x")
    adapter = YoutubeSource(cookies_file=cookies, now=lambda: NOW)
    monkeypatch.setattr(adapter, "_check_cookies", lambda url: cookies.unlink())

    with pytest.raises(FetchError) as info:
        await adapter.fetch(channel())

    assert (info.value.reason, info.value.url) == ("cookies_unavailable", LISTING_URL)
    assert "cookies-secret-name" not in str(info.value)
    assert fake_ydl.params is None  # yt-dlp was never constructed


async def test_default_extractor_never_logs_yt_dlp_output() -> None:
    extract = FakeExtractor()

    await source(extract).fetch(channel())

    logger = extract.calls[0][1]["logger"]
    for method in ("debug", "info", "warning", "error"):
        assert getattr(logger, method)(f"proxy {PROXY}") is None


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


# --- US4: failures -------------------------------------------------------------------------

LISTING_URL = "https://www.youtube.com/channel/UC123/videos"


def http_error(status: int) -> YtHTTPError:
    class Response:
        def __init__(self) -> None:
            self.status = status
            self.url = "https://secret.invalid/"
            self.reason = "x"

        def close(self) -> None: ...

    return YtHTTPError(Response())  # type: ignore[arg-type]


def wrapped(inner: BaseException) -> DownloadError:
    return DownloadError(RAW_MESSAGE, (type(inner), inner, None))


FAILURES = [
    pytest.param(
        DownloadError(f"{RAW_MESSAGE} HTTP Error 429: Too Many Requests"),
        "http_status",
        429,
        id="429-text",
    ),
    pytest.param(
        DownloadError(f"{RAW_MESSAGE} Sign in to confirm you're not a bot"),
        "http_status",
        429,
        id="bot",
    ),
    pytest.param(
        DownloadError(f"{RAW_MESSAGE} HTTP Error 403: Forbidden"), "http_status", 403, id="403-text"
    ),
    pytest.param(
        DownloadError(f"{RAW_MESSAGE} HTTP Error 404: Not Found"), "http_status", 404, id="404-text"
    ),
    pytest.param(
        DownloadError(f"{RAW_MESSAGE} The playlist does not exist."), "http_status", 404, id="gone"
    ),
    pytest.param(
        DownloadError(f"{RAW_MESSAGE} This playlist is private"), "http_status", 404, id="private"
    ),
    pytest.param(
        DownloadError(f"{RAW_MESSAGE} Video unavailable"), "http_status", 404, id="unavailable"
    ),
    pytest.param(wrapped(http_error(503)), "http_status", 503, id="wrapped-http-503"),
    pytest.param(wrapped(http_error(429)), "http_status", 429, id="wrapped-http-429"),
    pytest.param(
        ExtractorError(RAW_MESSAGE, cause=urllib.error.HTTPError("u", 404, "m", {}, None)),  # type: ignore[arg-type]
        "http_status",
        404,
        id="extractor-cause-http",
    ),
    pytest.param(wrapped(TransportError(RAW_MESSAGE)), "connection_failed", None, id="transport"),
    pytest.param(wrapped(urllib.error.URLError("dns")), "connection_failed", None, id="urlerror"),
    pytest.param(OSError(RAW_MESSAGE), "connection_failed", None, id="oserror"),
    pytest.param(ConnectionResetError(RAW_MESSAGE), "connection_failed", None, id="reset"),
    pytest.param(TimeoutError(RAW_MESSAGE), "timeout", None, id="timeout-error"),
    pytest.param(wrapped(TimeoutError()), "timeout", None, id="wrapped-timeout"),
    pytest.param(DownloadError(RAW_MESSAGE), "invalid_response", None, id="other-download"),
    pytest.param(RuntimeError(RAW_MESSAGE), "invalid_response", None, id="runtime"),
    pytest.param(
        DownloadError(f"{RAW_MESSAGE} channel UC4291, id x-403 and 404.5 failed"),
        "invalid_response",
        None,
        id="codes-inside-ids",
    ),
]


def with_context(exc: BaseException, context: BaseException) -> BaseException:
    """``exc`` as if raised while handling ``context`` (implicit chaining, no ``from``)."""
    exc.__context__ = context
    return exc


class UnprintableError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("no text")


FAILURES += [
    pytest.param(
        with_context(RuntimeError(RAW_MESSAGE), OSError("during handling")),
        "invalid_response",
        None,
        id="context-type-not-followed",
    ),
    pytest.param(
        with_context(RuntimeError(RAW_MESSAGE), DownloadError("HTTP Error 429")),
        "invalid_response",
        None,
        id="context-text-not-read",
    ),
    pytest.param(UnprintableError(), "invalid_response", None, id="classification-fails"),
]


@pytest.mark.parametrize(("exc", "reason", "status"), FAILURES)
async def test_failures_map_to_fetch_errors_without_leaking(
    exc: Exception, reason: str, status: int | None, caplog: pytest.LogCaptureFixture
) -> None:
    extract = FakeExtractor(exc=exc)

    with caplog.at_level(logging.DEBUG), pytest.raises(FetchError) as info:
        await source(extract, proxy=PROXY).fetch(channel())

    err = info.value
    assert (err.reason, err.status, err.url) == (reason, status, LISTING_URL)
    assert err.__cause__ is None
    assert err.__suppress_context__ is True
    for secret in ("sensitive-library-text", "secret", "proxy.invalid", "user"):
        assert secret not in str(err)
        assert secret not in caplog.text


async def test_failure_is_logged_with_the_exception_class_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    extract = FakeExtractor(exc=RuntimeError(RAW_MESSAGE))

    with caplog.at_level(logging.WARNING), pytest.raises(FetchError):
        await source(extract).fetch(channel())

    (record,) = [r for r in caplog.records if r.getMessage() == "source.youtube_failed"]
    assert record.error == "RuntimeError"  # type: ignore[attr-defined]


def test_transient_classification_of_mapped_errors() -> None:
    assert is_transient_fetch(FetchError("http_status", url=LISTING_URL, status=429))
    assert not is_transient_fetch(FetchError("http_status", url=LISTING_URL, status=404))


async def test_a_listing_that_outlives_the_timeout_is_a_transient_timeout() -> None:
    gate = threading.Event()
    extract = FakeExtractor(listing(), gate=gate)
    try:
        with pytest.raises(FetchError) as info:
            await source(extract, timeout=0.05).fetch(channel())
    finally:
        gate.set()

    assert info.value.reason == "timeout"
    assert info.value.url == LISTING_URL
    assert is_transient_fetch(info.value)


async def test_cancellation_ends_the_fetch_promptly() -> None:
    gate = threading.Event()
    extract = FakeExtractor(listing(), gate=gate)
    task = asyncio.ensure_future(source(extract).fetch(channel()))

    async def started() -> None:
        while not extract.calls:
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(started(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    finally:
        gate.set()


async def test_extractions_run_on_the_adapters_own_bounded_pool() -> None:
    gate = threading.Event()
    extract = FakeExtractor(listing(), gate=gate)
    adapter = source(extract, timeout=0.2)
    fetches = youtube_module._MAX_WORKERS + 2
    try:
        results = await asyncio.gather(
            *(adapter.fetch(channel()) for _ in range(fetches)), return_exceptions=True
        )
    finally:
        gate.set()
        adapter.close()

    # hung extractions occupy at most _MAX_WORKERS threads of the adapter's own pool; the
    # queued fetches run into the timeout instead of starting further threads
    assert len(extract.threads) == youtube_module._MAX_WORKERS
    assert all(name.startswith("invio-youtube") for name in extract.threads)
    assert [getattr(r, "reason", r) for r in results] == ["timeout"] * fetches


async def test_close_is_idempotent_and_a_closed_adapter_fails_cleanly() -> None:
    adapter = source(FakeExtractor())
    adapter.close()
    adapter.close()

    with pytest.raises(RuntimeError):
        await adapter.fetch(channel())


async def test_fetch_error_from_extractor_is_passed_through() -> None:
    err = FetchError("too_large", url="https://example.com/")
    extract = FakeExtractor(exc=err)

    with pytest.raises(FetchError) as info:
        await source(extract).fetch(channel())

    assert info.value is err


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
