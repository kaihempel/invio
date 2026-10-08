"""Health-check ping for ``invio run-due`` (#23): one best-effort GET, never an error.

The URL is a secret (it usually embeds a check id): it is never logged. The HTTP libraries log
the full URL (``httpx2`` at INFO, ``httpcore2`` the host at DEBUG), so a filter attached once to
their loggers drops their records while a ping is in flight; their levels and every other
client's logging stay as they are.
"""

import logging
from contextvars import ContextVar
from urllib.parse import urlsplit, urlunsplit

import httpx2

__all__ = ["ping"]

logger = logging.getLogger("invio.pipeline")

_TIMEOUT_SECONDS = 3.0
# The loggers the HTTP libraries write to. A logger's filter does not see the records of its
# child loggers, so every module logger is listed.
_HTTP_LOGGERS = (
    "httpx2",
    "httpx",
    *(
        f"{core}.{part}"
        for core in ("httpcore2", "httpcore")
        for part in ("connection", "http11", "http2", "proxy", "socks")
    ),
)

_pinging: ContextVar[bool] = ContextVar("invio_healthcheck_pinging", default=False)


class _DuringPing(logging.Filter):
    """Drops every record logged while the current context sends a ping."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _pinging.get()


def _install_filter() -> None:
    """Attach :class:`_DuringPing` to the HTTP loggers (idempotent)."""
    for name in _HTTP_LOGGERS:
        http_logger = logging.getLogger(name)
        if not any(isinstance(f, _DuringPing) for f in http_logger.filters):
            http_logger.addFilter(_DuringPing())


_install_filter()


def _fail_url(url: str) -> str:
    """``url`` with ``/fail`` appended to its path; query string and fragment are kept."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/fail"))


def _get(target: str, transport: httpx2.BaseTransport | None) -> httpx2.Response:
    """One GET with the HTTP libraries' own logging muted for this context only."""
    token = _pinging.set(True)
    try:
        with httpx2.Client(
            timeout=_TIMEOUT_SECONDS, follow_redirects=False, transport=transport
        ) as client:
            return client.get(target)
    finally:
        _pinging.reset(token)


def ping(url: str, *, failed: bool, transport: httpx2.BaseTransport | None = None) -> None:
    """GET ``url`` (``<url>/fail`` when ``failed``) with a 3 s timeout and no redirects.

    Never raises. A non-2xx answer is logged as ``healthcheck.failed`` with its status, a
    transport error with its class name only. ``transport`` exists for tests.
    """
    target = _fail_url(url) if failed else url
    try:
        response = _get(target, transport)
    except Exception as err:  # best effort: the message may embed the URL
        logger.warning("healthcheck.failed", extra={"error": type(err).__name__})
        return
    if response.is_success:
        logger.info("healthcheck.sent", extra={"failed": failed})
    else:
        logger.warning("healthcheck.failed", extra={"status": response.status_code})
