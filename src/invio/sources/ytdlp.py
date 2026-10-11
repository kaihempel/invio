"""Private yt-dlp plumbing shared by the YouTube source and the transcript adapter.

Both adapters apply the same secret-safety rules, so the code lives once, here:

* yt-dlp's own output is swallowed (:class:`NullLogger`): it can echo the proxy URL or the
  cookie file path;
* yt-dlp writes the cookie jar back to ``cookiefile`` when it closes, so it only ever gets a
  private copy (:func:`private_cookie_copy`: ``0600``, in a fresh ``0700`` temporary directory);
* a failure is mapped to a reason code and an HTTP status (:func:`classify_safely`); the
  library's message text is never copied into an error or a log line. :func:`failure_text` is
  for internal classification only and must not be logged or raised.

``yt_dlp`` is imported lazily, only inside the functions that need it.
"""

import logging
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs, urlsplit

from invio.sources.errors import FetchError

__all__ = [
    "MAX_WORKERS",
    "VIDEO_ID",
    "Extractor",
    "NullLogger",
    "base_options",
    "classify_safely",
    "download",
    "extract_info",
    "failure_text",
    "parse_video_url",
    "private_cookie_copy",
    "watch_url",
]

type Extractor = Callable[[str, Mapping[str, object]], Mapping[str, object]]
"""``(url, yt_dlp_options) -> info mapping``; blocking, runs in a worker thread."""

_log = logging.getLogger("invio.sources.ytdlp")

MAX_WORKERS: Final = 4
"""Worker threads of an adapter's metadata pool: hung calls never take more."""

VIDEO_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SOCKET_TIMEOUT_CAP: Final = 20.0
_CHAIN_DEPTH: Final = 8
_YOUTUBE_HOSTS: Final = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})


def watch_url(video_id: str) -> str:
    """The canonical watch URL for a validated ``video_id``."""
    return f"https://www.youtube.com/watch?v={video_id}"


def parse_video_url(url: str) -> str | None:
    """The video id of a YouTube watch, shorts or ``youtu.be`` URL; ``None`` for anything else."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    candidate: str | None = None
    if host in _YOUTUBE_HOSTS:
        if parts.path == "/watch":
            values = parse_qs(parts.query).get("v", [])
            candidate = values[0] if values else None
        elif parts.path.startswith("/shorts/"):
            candidate = parts.path.removeprefix("/shorts/").strip("/")
    elif host == "youtu.be":
        candidate = parts.path.strip("/")
    if candidate is not None and VIDEO_ID.fullmatch(candidate):
        return candidate
    return None


class NullLogger:
    """Swallows yt-dlp's own output (it may echo the proxy URL or cookie path)."""

    def debug(self, msg: object) -> None:
        pass

    def info(self, msg: object) -> None:
        pass

    def warning(self, msg: object) -> None:
        pass

    def error(self, msg: object) -> None:
        pass


def base_options(
    *, timeout: float, cookies_file: Path | None, proxy: str | None
) -> dict[str, object]:
    """The yt-dlp options every adapter shares: quiet, no config, no cache, no retries."""
    options: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreconfig": True,
        "cachedir": False,
        "socket_timeout": min(timeout, _SOCKET_TIMEOUT_CAP),
        "retries": 0,
        "extractor_retries": 0,
        "allowed_extractors": ["youtube.*"],
        "logger": NullLogger(),
    }
    if cookies_file is not None:
        options["cookiefile"] = str(cookies_file)
    if proxy:
        options["proxy"] = proxy
    return options


@contextmanager
def private_cookie_copy(path: Path) -> Iterator[Path]:
    """Copy ``path`` into a new ``0700`` temporary directory as a ``0600`` file; remove both."""
    with tempfile.TemporaryDirectory(prefix="invio-yt-", ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "cookies.txt"
        with path.open("rb") as src:
            fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst)
        yield copy


def _private_params(stack: ExitStack, url: str, options: Mapping[str, object]) -> dict[str, object]:
    """``options`` with a ``cookiefile`` replaced by a private copy (removed with ``stack``)."""
    params = dict(options)
    cookies = params.get("cookiefile")
    if isinstance(cookies, str):
        try:
            copy = stack.enter_context(private_cookie_copy(Path(cookies)))
        except OSError:
            raise FetchError("cookies_unavailable", url=url) from None
        params["cookiefile"] = str(copy)
    return params


def extract_info(url: str, options: Mapping[str, object]) -> Mapping[str, object]:
    """The default extractor: the only place that runs ``YoutubeDL.extract_info`` (no download).

    A ``cookiefile`` is replaced by a private copy, because ``YoutubeDL`` writes the cookie jar
    back to that file on close.
    """
    import yt_dlp

    with ExitStack() as stack:
        params = _private_params(stack, url, options)
        with yt_dlp.YoutubeDL(params) as ydl:
            info: object = ydl.extract_info(url, download=False)
    if not isinstance(info, Mapping):
        raise TypeError("yt-dlp returned no info mapping")
    return info


def download(url: str, options: Mapping[str, object]) -> None:
    """Run ``YoutubeDL.download`` for ``url`` (subtitles or audio, as ``options`` say).

    The same private cookie copy rule as :func:`extract_info` applies.
    """
    import yt_dlp

    with ExitStack() as stack:
        params = _private_params(stack, url, options)
        with yt_dlp.YoutubeDL(params) as ydl:
            ydl.download([url])


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
    being handled (inside yt-dlp's cleanup, say) says nothing about why the request failed.
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


def failure_text(exc: BaseException) -> str:
    """Lower-cased messages of ``exc`` and what it wraps, for internal marker matching only.

    The text can contain the proxy URL or the cookie path: never log it, never raise it. Returns
    ``""`` if rendering a message fails.
    """
    try:
        return " ".join(str(node) for node in _chain(exc)).lower()
    except Exception:  # e.g. an exception whose ``__str__`` raises
        return ""


def classify_safely(exc: BaseException) -> tuple[str, int | None]:
    """:func:`_classify`, falling back to ``invalid_response`` if classifying itself fails."""
    try:
        return _classify(exc)
    except Exception as failure:  # e.g. an exception whose ``__str__`` raises
        _log.debug("source.ytdlp_classify_failed", extra={"error": type(failure).__name__})
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
