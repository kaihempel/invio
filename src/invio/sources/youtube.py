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

import logging
import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
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
from invio.sources import ytdlp
from invio.sources.errors import FetchError
from invio.sources.freshness import age_cutoff, clamp_or_expire
from invio.sources.text import collapse
from invio.sources.ytdlp import Extractor

__all__ = ["Extractor", "ListingSummary", "YoutubeSource"]

# The default extractor; tests and the network guard in tests/conftest.py patch it here.
_yt_dlp_extract = ytdlp.extract_info

_log = logging.getLogger("invio.sources.youtube")

_VIDEO_ID: Final = ytdlp.VIDEO_ID
_UPLOAD_DATE: Final = re.compile(r"[0-9]{8}")
_MAX_WORKERS: Final = ytdlp.MAX_WORKERS
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
        return ytdlp.watch_url(self.id)


@dataclass(frozen=True, slots=True)
class ListingSummary:
    """What :meth:`YoutubeSource.describe` learned about a channel or playlist."""

    title: str | None
    entry_count: int
    newest: datetime | None


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
        # resolved here, not at import time, so a patched ``_yt_dlp_extract`` takes effect
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
        url = _Locator.from_config(config).listing_url()
        info = await self._extract_info(url, config.max_items)
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
        url = _Locator.from_config(config).listing_url()
        info = await self._extract_info(url, _DESCRIBE_ITEMS)
        videos = _videos(_entries(info, url))
        dates = [video.published for video in videos if video.published is not None]
        return ListingSummary(_listing_title(info), len(videos), max(dates, default=None))

    async def _extract_info(self, url: str, max_items: int) -> object:
        """Run the extractor for the listing ``url`` on the worker pool under the hard timeout."""
        if self._closed:
            raise RuntimeError("YoutubeSource is closed")
        ytdlp.check_cookies(self._cookies_file, url)
        options = self._options(max_items)
        try:
            return await ytdlp.run_in_pool(
                self._executor, partial(self._extract, url, options), self._timeout
            )
        except FetchError:
            raise
        except TimeoutError:
            _log.warning("source.youtube_failed", extra={"error": "TimeoutError"})
            raise FetchError("timeout", url=url) from None
        except Exception as exc:
            _log.warning("source.youtube_failed", extra={"error": type(exc).__name__})
            reason, status = ytdlp.classify_safely(exc)
            raise FetchError(reason, url=url, status=status) from None

    def _options(self, max_items: int) -> dict[str, object]:
        options = ytdlp.base_options(
            timeout=self._timeout, cookies_file=self._cookies_file, proxy=self._proxy
        )
        options.update(
            {"extract_flat": "in_playlist", "skip_download": True, "playlistend": max_items}
        )
        return options


def _listing_title(info: object) -> str | None:
    """The listing's own title, else its ``channel`` or ``uploader``; whitespace collapsed."""
    if not isinstance(info, Mapping):
        return None
    for key in ("title", "channel", "uploader"):
        value = info.get(key)
        if isinstance(value, str) and (text := collapse(value)):
            return text
    return None


def _newest_first(videos: list[_Video]) -> list[_Video]:
    """Dated videos newest first (stable for equal dates), then undated ones in listing order."""
    dated = [(video.published, video) for video in videos if video.published is not None]
    dated.sort(key=lambda pair: pair[0], reverse=True)
    return [video for _, video in dated] + [video for video in videos if video.published is None]


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
