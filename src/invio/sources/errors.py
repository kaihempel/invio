"""Error types of the safe HTTP client and of the video transcript adapter.

Kept free of imports from ``invio.sources.http`` so the guard, robots and rate-limit modules
can raise them without an import cycle; ``invio.sources.http`` re-exports the HTTP ones.

:class:`TranscriptUnavailable` (a :class:`FetchError`) says why no transcript could be had;
:class:`TranscriptionDeferred` is not an error but a signal that the video is left for a later
run.
"""

import re
from enum import StrEnum
from typing import ClassVar, Final, Literal

from invio.sources.urls import redact_url, without_query

__all__ = [
    "BlockReason",
    "BlockedError",
    "FetchError",
    "RenderUnavailableError",
    "TooLargeError",
    "TranscriptUnavailable",
    "TranscriptionDeferred",
    "is_transient_fetch",
]


class BlockReason(StrEnum):
    """Why a request was refused before (or instead of) being sent."""

    UNSUPPORTED_SCHEME = "unsupported_scheme"
    NON_PUBLIC_ADDRESS = "non_public_address"
    BLOCKED_BY_ROBOTS = "blocked_by_robots"


class FetchError(Exception):
    """A fetch failed; ``reason`` is a short machine-readable code such as ``"timeout"``.

    ``url`` is always stored redacted (no ``user:password@``). The message leaves out the
    query and fragment as well (feed URLs often carry ``?token=``) and never contains request
    headers, so the error is safe to log and to show to the operator.
    """

    def __init__(self, reason: str, *, url: str, status: int | None = None) -> None:
        self.url = redact_url(url)
        self.status = status
        self.reason = reason
        super().__init__(self._format(reason, status))

    def _display_url(self) -> str:
        """The URL as shown in the message: without query and fragment."""
        return without_query(self.url)

    def _notes(self, status: int | None) -> list[str]:
        """Parenthesised notes appended to the message."""
        return [f"HTTP {status}"] if status is not None else []

    def _format(self, reason: str, status: int | None) -> str:
        message = f"{reason}: {self._display_url()}"
        for note in self._notes(status):
            message += f" ({note})"
        return message


class BlockedError(FetchError):
    """The request was refused by policy (scheme, non-public address or robots.txt)."""

    def __init__(self, reason: BlockReason, *, url: str) -> None:
        super().__init__(reason.value, url=url)
        self.reason: BlockReason = reason  # narrows the str set by FetchError


class TooLargeError(FetchError):
    """The response body is larger than the configured limit."""

    def __init__(self, *, url: str, limit: int) -> None:
        super().__init__("too_large", url=url)
        self.limit = limit


class RenderUnavailableError(FetchError):
    """``render: js`` was requested, but the optional browser support cannot be used.

    Either the ``render`` extra is not installed or Chromium is missing; the message says how
    to install both.
    """

    INSTALL_HINT = "install it with: uv sync --extra render && uv run playwright install chromium"

    def __init__(self, *, url: str = "") -> None:
        super().__init__("render_unavailable", url=url)
        self.args = (f"{self.args[0].rstrip(': ')} ({self.INSTALL_HINT})",)


_WATCH_URL: Final = re.compile(r"https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{1,64}")


class TranscriptUnavailable(FetchError):
    """No transcript could be had for a video; ``reason`` says why.

    Permanent reasons (``video_unavailable``, ``no_audio_format``, ``no_speech``,
    ``unsupported_url``, ``too_long``, ``whisper_not_installed``, ``transcription_disabled``)
    and transient ones (``timeout``, ``connection_failed``, ``invalid_response``, retryable
    ``http_status``) follow :func:`is_transient_fetch`. ``description`` is the video's
    description, kept as an attribute so the item can still be rated; it never appears in
    ``str(err)`` or ``repr``. The message shows the full canonical watch URL (the video id is
    the query) and nothing else from the library.
    """

    INSTALL_HINT: ClassVar[str] = "install it with: uv sync --extra whisper"

    def __init__(
        self,
        reason: str,
        *,
        url: str,
        status: int | None = None,
        description: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.description = description
        self.detail = detail
        super().__init__(reason, url=url, status=status)

    def _display_url(self) -> str:
        return self.url if _WATCH_URL.fullmatch(self.url) else without_query(self.url)

    def _notes(self, status: int | None) -> list[str]:
        notes = super()._notes(status)
        if self.reason == "whisper_not_installed":
            notes.append(self.INSTALL_HINT)
        if self.detail:
            notes.append(self.detail)
        return notes


class TranscriptionDeferred(Exception):
    """The video is left for a later run: the per-run cap or the run's time is used up.

    Not a :class:`FetchError`, so it can never fail an item; only the ``process_item`` boundary
    catches it and releases the item. ``str(err)`` is the reason code.
    """

    def __init__(self, reason: Literal["cap_reached", "not_enough_time"]) -> None:
        super().__init__(reason)
        self.reason = reason


_TRANSIENT_FETCH_REASONS = frozenset(
    {"timeout", "connection_failed", "dns_failed", "invalid_response", "render_failed"}
)
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429})


def is_transient_fetch(err: BaseException) -> bool:
    """Allow-list: only network trouble and retryable HTTP statuses are worth another try.

    Everything else (4xx other than 408/425/429, invalid URL, not HTML, malformed feed, missing
    selector, too many redirects, policy refusals, size limits, an unknown reason) is permanent.
    """
    if not isinstance(err, FetchError) or isinstance(
        err, BlockedError | TooLargeError | RenderUnavailableError
    ):
        return False
    if err.reason == "http_status":
        status = err.status
        return status is not None and (status in _TRANSIENT_HTTP_STATUSES or 500 <= status <= 599)
    return err.reason in _TRANSIENT_FETCH_REASONS
