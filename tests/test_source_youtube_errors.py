"""Failure mapping, timeouts and pool behaviour of the YouTube source adapter."""

import asyncio
import logging
import threading
import urllib.error

import pytest
from yt_dlp.networking.exceptions import HTTPError as YtHTTPError
from yt_dlp.networking.exceptions import TransportError
from yt_dlp.utils import DownloadError, ExtractorError

from invio.sources import youtube as youtube_module
from invio.sources.errors import FetchError, is_transient_fetch
from tests.youtube_helpers import (
    LISTING_URL,
    PROXY,
    RAW_MESSAGE,
    FakeExtractor,
    channel,
    listing,
    source,
)

# --- US4: failures -------------------------------------------------------------------------


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
    pytest.param(OSError(RAW_MESSAGE), "invalid_response", None, id="oserror"),
    pytest.param(
        wrapped(RuntimeError("bad private key")), "invalid_response", None, id="private-key"
    ),
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
