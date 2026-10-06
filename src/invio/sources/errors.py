"""Error types of the safe HTTP client.

Kept free of imports from ``invio.sources.http`` so the guard, robots and rate-limit modules
can raise them without an import cycle; ``invio.sources.http`` re-exports all of them.
"""

from enum import StrEnum

from invio.sources.urls import redact_url, without_query

__all__ = [
    "BlockReason",
    "BlockedError",
    "FetchError",
    "RenderUnavailableError",
    "TooLargeError",
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
        message = f"{reason}: {without_query(self.url)}"
        if status is not None:
            message += f" (HTTP {status})"
        super().__init__(message)


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
