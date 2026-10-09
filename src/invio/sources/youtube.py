"""YouTube source adapter: a channel or playlist in, one video :class:`Candidate` per video out.

This adapter deliberately does not use :class:`~invio.sources.http.SafeHttpClient`: listing a
channel needs the extractor logic of ``yt-dlp`` (consent pages, continuation tokens, bot checks),
and ``yt-dlp`` brings its own network stack. The exposure is kept small instead:

* the user's locator is parsed by :func:`invio.config.job.parse_youtube_channel` /
  :func:`~invio.config.job.parse_youtube_playlist` and the URL handed to ``yt-dlp`` is built here
  from the validated token, never taken from the job file;
* ``allowed_extractors=["youtube.*"]`` keeps ``yt-dlp`` away from its generic extractor;
* the extraction is metadata only (``extract_flat``, ``skip_download``): no media is downloaded.

``yt-dlp`` is blocking, so it runs on a small thread pool owned by the adapter under a hard
timeout. The timeout is best effort: a worker thread cannot be killed, so a timed-out
extraction keeps its thread until ``yt-dlp`` gives up on its own (every request is bounded by
``socket_timeout``). The pool is bounded, so hung extractions never take more than
``_MAX_WORKERS`` threads and never starve the event loop's shared default executor; fetches
queued behind them simply time out.

``yt-dlp`` writes the cookie jar back to ``cookiefile`` when it closes. The default extractor
therefore hands it a private copy (``0600``, in a fresh temporary directory, removed
afterwards): the configured file is only ever read, may be read-only, and concurrent fetches
cannot corrupt it.

Every failure becomes a :class:`FetchError` carrying only a reason code and the canonical
listing URL; the library's message can contain the proxy URL (with credentials) or the cookie
file path and is never copied into an error or a log line.
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cache, partial
from pathlib import Path
from typing import Final, Literal, Self

from invio.config.job import (
    YoutubeChannelSource,
    YoutubePlaylistSource,
    parse_youtube_channel,
    parse_youtube_playlist,
)
from invio.config.settings import Settings
from invio.domain import Candidate, url_hash
from invio.sources.errors import FetchError
from invio.sources.freshness import age_cutoff, clamp_or_expire

__all__ = ["Extractor", "ListingSummary", "YoutubeSource"]

type Extractor = Callable[[str, Mapping[str, object]], Mapping[str, object]]
"""``(listing_url, yt_dlp_options) -> info mapping``; blocking, runs in a worker thread."""

_log = logging.getLogger("invio.sources.youtube")

_VIDEO_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_UPLOAD_DATE: Final = re.compile(r"[0-9]{8}")
_SOCKET_TIMEOUT_CAP: Final = 20.0
_CHAIN_DEPTH: Final = 8
_MAX_WORKERS: Final = 4
# Flat listings keep private/deleted videos as placeholder entries with these titles.
_PLACEHOLDER_TITLES: Final = frozenset({"[Private video]", "[Deleted video]"})
_HIDDEN_AVAILABILITY: Final = frozenset(
    {"private", "needs_auth", "subscriber_only", "premium_only"}
)

_DESCRIBE_ITEMS: Final = 5

_Kind = Literal["channel_id", "handle", "playlist_id"]


@dataclass(frozen=True, slots=True)
class _Locator:
    kind: _Kind
    token: str

    @classmethod
    def from_config(cls, config: YoutubeChannelSource | YoutubePlaylistSource) -> Self:
        if isinstance(config, YoutubeChannelSource):
            channel_kind, token = parse_youtube_channel(config.channel_id)
            return cls(channel_kind, token)
        return cls("playlist_id", parse_youtube_playlist(config.playlist_id))

    def listing_url(self) -> str:
        if self.kind == "channel_id":
            return f"https://www.youtube.com/channel/{self.token}/videos"
        if self.kind == "handle":
            return f"https://www.youtube.com/@{self.token}/videos"
        return f"https://www.youtube.com/playlist?list={self.token}"


@dataclass(frozen=True, slots=True)
class _Video:
    id: str
    title: str
    published: datetime | None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"


@dataclass(frozen=True, slots=True)
class ListingSummary:
    """What :meth:`YoutubeSource.describe` learned about a channel or playlist."""

    title: str | None
    entry_count: int
    newest: datetime | None


class _NullLogger:
    """Swallows yt-dlp's own output (it may echo the proxy URL or cookie path)."""

    def debug(self, msg: object) -> None:
        pass

    def info(self, msg: object) -> None:
        pass

    def warning(self, msg: object) -> None:
        pass

    def error(self, msg: object) -> None:
        pass


class YoutubeSource:
    """Source adapter for :class:`YoutubeChannelSource` and :class:`YoutubePlaylistSource`.

    ``proxy`` is the plain proxy URL (the caller unwraps the secret). ``extract`` replaces the
    ``yt-dlp`` call and ``now`` returns the current aware time; both exist for tests. Call
    :meth:`close` when done to shut the adapter's worker pool down.
    """

    def __init__(
        self,
        *,
        cookies_file: Path | None = None,
        proxy: str | None = None,
        timeout: float = 60.0,
        extract: Extractor | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._cookies_file = cookies_file
        self._proxy = proxy
        self._timeout = timeout
        self._extract: Extractor = extract if extract is not None else _yt_dlp_extract
        self._now = now if now is not None else lambda: datetime.now(UTC)
        # threads are started lazily, on the first fetch
        self._executor = ThreadPoolExecutor(_MAX_WORKERS, thread_name_prefix="invio-youtube")
        self._closed = False

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """The adapter configured from ``settings`` (blank cookies/proxy are already ``None``)."""
        proxy = settings.youtube_proxy
        return cls(
            cookies_file=settings.youtube_cookies_file,
            proxy=proxy.get_secret_value() if proxy is not None else None,
            timeout=settings.youtube_timeout_seconds,
        )

    def close(self) -> None:
        """Shut the worker pool down without waiting for (possibly hung) extractions."""
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def fetch(
        self, config: YoutubeChannelSource | YoutubePlaylistSource, /
    ) -> list[Candidate]:
        """List the channel or playlist: dated videos newest first, then undated ones.

        Raises :class:`FetchError` (``url`` is the canonical listing URL) for a timeout, a
        network or HTTP-level failure, an unusable result or an unreadable cookies file.
        Raises :class:`RuntimeError` after :meth:`close`.
        """
        locator = _Locator.from_config(config)
        url = locator.listing_url()
        info = await self._extract_info(locator, config.max_items)
        videos = _videos(_entries(info, url))
        now = self._now()
        cutoff = age_cutoff(now, config.max_age_days)
        kept: list[_Video] = []
        for video in videos:
            keep, published = clamp_or_expire(video.published, now, cutoff)
            if keep:
                kept.append(replace(video, published=published))
        return [
            Candidate(
                url=video.url,
                url_hash=url_hash(video.url),
                title=video.title,
                published_at=video.published,
                type="video",
                teaser=None,
                content_hash=None,
            )
            for video in _newest_first(kept)[: config.max_items]
        ]

    async def describe(
        self, config: YoutubeChannelSource | YoutubePlaylistSource, /
    ) -> ListingSummary:
        """Title, size and newest date of the first few listed videos (for source discovery).

        Same extraction, limits and errors as :meth:`fetch`, but only ``_DESCRIBE_ITEMS`` entries
        are listed. Raises :class:`RuntimeError` after :meth:`close`.
        """
        locator = _Locator.from_config(config)
        url = locator.listing_url()
        info = await self._extract_info(locator, _DESCRIBE_ITEMS)
        videos = _videos(_entries(info, url))
        dates = [video.published for video in videos if video.published is not None]
        return ListingSummary(_listing_title(info), len(videos), max(dates, default=None))

    async def _extract_info(self, locator: _Locator, max_items: int) -> object:
        """Run the extractor for ``locator`` on the worker pool under the hard timeout."""
        if self._closed:
            raise RuntimeError("YoutubeSource is closed")
        url = locator.listing_url()
        self._check_cookies(url)
        options = self._options(max_items)
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self._executor, partial(self._extract, url, options)),
                self._timeout,
            )
        except FetchError:
            raise
        except TimeoutError:
            _log.warning("source.youtube_failed", extra={"error": "TimeoutError"})
            raise FetchError("timeout", url=url) from None
        except Exception as exc:
            _log.warning("source.youtube_failed", extra={"error": type(exc).__name__})
            reason, status = _classify_safely(exc)
            raise FetchError(reason, url=url, status=status) from None

    def _check_cookies(self, url: str) -> None:
        path = self._cookies_file
        if path is not None and not (path.is_file() and os.access(path, os.R_OK)):
            raise FetchError("cookies_unavailable", url=url)

    def _options(self, max_items: int) -> dict[str, object]:
        options: dict[str, object] = {
            "extract_flat": "in_playlist",
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "ignoreconfig": True,
            "cachedir": False,
            "socket_timeout": min(self._timeout, _SOCKET_TIMEOUT_CAP),
            "retries": 0,
            "extractor_retries": 0,
            "allowed_extractors": ["youtube.*"],
            "playlistend": max_items,
            "logger": _NullLogger(),
        }
        if self._cookies_file is not None:
            options["cookiefile"] = str(self._cookies_file)
        if self._proxy:
            options["proxy"] = self._proxy
        return options


def _listing_title(info: object) -> str | None:
    """The listing's own title, else its ``channel`` or ``uploader``; whitespace collapsed."""
    if not isinstance(info, Mapping):
        return None
    for key in ("title", "channel", "uploader"):
        value = info.get(key)
        if isinstance(value, str) and (text := " ".join(value.split())):
            return text
    return None


def _newest_first(videos: list[_Video]) -> list[_Video]:
    """Dated videos newest first (stable for equal dates), then undated ones in listing order."""
    dated = [(video.published, video) for video in videos if video.published is not None]
    dated.sort(key=lambda pair: pair[0], reverse=True)
    return [video for _, video in dated] + [video for video in videos if video.published is None]


def _yt_dlp_extract(url: str, options: Mapping[str, object]) -> Mapping[str, object]:
    """The default extractor: the only place that touches ``yt-dlp`` (imported lazily).

    A ``cookiefile`` is replaced by a private copy, because ``YoutubeDL`` writes the cookie jar
    back to that file on close.
    """
    import yt_dlp

    params = dict(options)
    with ExitStack() as stack:
        cookies = params.get("cookiefile")
        if isinstance(cookies, str):
            try:
                copy = stack.enter_context(_private_cookie_copy(Path(cookies)))
            except OSError:
                raise FetchError("cookies_unavailable", url=url) from None
            params["cookiefile"] = str(copy)
        with yt_dlp.YoutubeDL(params) as ydl:
            info: object = ydl.extract_info(url, download=False)
    if not isinstance(info, Mapping):
        raise TypeError("yt-dlp returned no info mapping")
    return info


@contextmanager
def _private_cookie_copy(path: Path) -> Iterator[Path]:
    """Copy ``path`` into a new ``0700`` temporary directory as a ``0600`` file; remove both."""
    with tempfile.TemporaryDirectory(prefix="invio-yt-", ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "cookies.txt"
        with path.open("rb") as src:
            fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst)
        yield copy


def _entries(info: object, url: str) -> list[object]:
    if not isinstance(info, Mapping):
        raise FetchError("invalid_response", url=url)
    entries = info.get("entries")
    if not isinstance(entries, list):
        raise FetchError("invalid_response", url=url)
    return entries


def _videos(entries: list[object]) -> list[_Video]:
    """Convert entries defensively: unusable ones are skipped, the first of a duplicate id wins."""
    videos: dict[str, _Video] = {}
    skipped = 0
    for entry in entries:
        video = _video(entry)
        if video is None or video.id in videos:
            skipped += 1
            continue
        videos[video.id] = video
    if skipped:
        _log.debug("source.youtube_entries_skipped", extra={"count": skipped})
    return list(videos.values())


def _video(entry: object) -> _Video | None:
    if not isinstance(entry, Mapping):
        return None
    video_id, title = entry.get("id"), entry.get("title")
    if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
        return None
    if not isinstance(title, str):
        return None
    title = " ".join(title.split())
    if not title or title in _PLACEHOLDER_TITLES:
        return None
    availability = entry.get("availability")
    # ``isinstance``: an unhashable value must not reach the frozenset lookup
    if isinstance(availability, str) and availability in _HIDDEN_AVAILABILITY:
        return None
    return _Video(video_id, title, _published_at(entry))


def _published_at(entry: Mapping[object, object]) -> datetime | None:
    """``timestamp`` (epoch seconds), else ``upload_date`` (``YYYYMMDD``) at midnight UTC."""
    stamp = entry.get("timestamp")
    if isinstance(stamp, int | float) and not isinstance(stamp, bool):
        try:
            return datetime.fromtimestamp(stamp, UTC)
        except (ValueError, OverflowError, OSError):
            pass
    day = entry.get("upload_date")
    if isinstance(day, str) and _UPLOAD_DATE.fullmatch(day):
        try:
            return datetime(int(day[:4]), int(day[4:6]), int(day[6:]), tzinfo=UTC)
        except ValueError:
            return None
    return None


# --- error mapping ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _NetworkErrors:
    http: type[BaseException]
    transport: tuple[type[BaseException], ...]


@cache
def _network_errors() -> _NetworkErrors:
    """yt-dlp's network exception classes, imported once (lazily, like ``yt-dlp`` itself)."""
    from yt_dlp.networking.exceptions import HTTPError, ProxyError, TransportError

    return _NetworkErrors(http=HTTPError, transport=(TransportError, ProxyError))


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """``exc`` and what it wraps explicitly: ``__cause__``, yt-dlp's ``cause`` and ``exc_info``.

    ``__context__`` is not followed: an exception that merely happened while another one was
    being handled (inside yt-dlp's cleanup, say) says nothing about why the listing failed.
    """
    seen: set[int] = set()
    pending: list[tuple[BaseException, int]] = [(exc, 0)]
    while pending:
        node, depth = pending.pop(0)
        if id(node) in seen or depth > _CHAIN_DEPTH:
            continue
        seen.add(id(node))
        yield node
        inner: list[object] = [node.__cause__, getattr(node, "cause", None)]
        exc_info = getattr(node, "exc_info", None)
        if isinstance(exc_info, tuple) and len(exc_info) > 1:
            inner.append(exc_info[1])
        pending.extend((e, depth + 1) for e in inner if isinstance(e, BaseException))


def _http_status(node: BaseException) -> int | None:
    """Status of a yt-dlp ``HTTPError`` (``.status``) or a urllib ``HTTPError`` (``.code``)."""
    from urllib.error import HTTPError as UrllibHTTPError

    if isinstance(node, _network_errors().http):
        status: object = getattr(node, "status", None)
    elif isinstance(node, UrllibHTTPError):
        status = node.code
    else:
        return None
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _classify_safely(exc: BaseException) -> tuple[str, int | None]:
    """:func:`_classify`, falling back to ``invalid_response`` if classifying itself fails."""
    try:
        return _classify(exc)
    except Exception as failure:  # e.g. an exception whose ``__str__`` raises
        _log.debug("source.youtube_classify_failed", extra={"error": type(failure).__name__})
        return "invalid_response", None


def _classify(exc: BaseException) -> tuple[str, int | None]:
    """Map a yt-dlp failure to ``(reason, status)`` without using its message text for output."""
    from urllib.error import URLError

    nodes = list(_chain(exc))
    if any(isinstance(n, TimeoutError) for n in nodes):
        return "timeout", None
    for node in nodes:
        status = _http_status(node)
        if status is not None:
            return "http_status", status
    network = (*_network_errors().transport, URLError, ConnectionError)
    if any(isinstance(n, network) for n in nodes):
        return "connection_failed", None
    text = " ".join(str(n) for n in nodes).lower()
    if _has_code(text, "429") or "not a bot" in text:
        return "http_status", 429
    if _has_code(text, "403"):
        return "http_status", 403
    if (
        _has_code(text, "404")
        or "does not exist" in text
        or "is private" in text
        or "video unavailable" in text
        or "playlist is unavailable" in text
    ):
        return "http_status", 404
    return "invalid_response", None


def _has_code(text: str, code: str) -> bool:
    """``code`` as a standalone number (not part of an id such as ``UC4291``)."""
    return re.search(rf"(?<![\w.-]){code}(?![\w.-])", text) is not None
