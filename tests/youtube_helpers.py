"""Shared fakes and builders for the YouTube source tests (no network)."""

import stat
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from invio.config.job import YoutubeChannelSource, YoutubePlaylistSource
from invio.sources.youtube import YoutubeSource

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
PROXY = "http://user:secret@proxy.invalid:8080"
LISTING_URL = "https://www.youtube.com/channel/UC123/videos"
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
