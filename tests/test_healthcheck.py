"""``invio.pipeline.healthcheck.ping``: one best-effort GET per ``run-due`` (#23)."""

import logging

import httpx2
import pytest

from invio.pipeline.healthcheck import ping

SECRET = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
URL = f"https://hc.example.com/ping/{SECRET}"


class Recorder:
    """A transport that records every request and answers with ``status`` (or raises)."""

    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.status = status
        self.error = error
        self.transport = httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx2.Response(self.status, headers={"location": "https://elsewhere.example/"})

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def test_success_is_one_get_to_the_exact_url() -> None:
    recorder = Recorder()

    ping(URL, failed=False, transport=recorder.transport)

    assert recorder.urls == [URL]
    assert [r.method for r in recorder.requests] == ["GET"]


@pytest.mark.parametrize("url", [URL, URL + "/"])
def test_failure_appends_fail_once(url: str) -> None:
    recorder = Recorder()

    ping(url, failed=True, transport=recorder.transport)

    assert recorder.urls == [URL + "/fail"]


def test_fail_keeps_the_query_string() -> None:
    recorder = Recorder()

    ping(URL + "?create=1", failed=True, transport=recorder.transport)

    assert recorder.urls == [URL + "/fail?create=1"]


@pytest.mark.parametrize("status", [200, 204])
def test_a_2xx_answer_is_logged_as_sent(caplog: pytest.LogCaptureFixture, status: int) -> None:
    with caplog.at_level(logging.INFO, logger="invio.pipeline"):
        ping(URL, failed=True, transport=Recorder(status=status).transport)

    (record,) = [r for r in caplog.records if r.getMessage().startswith("healthcheck.")]
    assert (record.getMessage(), record.failed) == ("healthcheck.sent", True)  # type: ignore[attr-defined]


def test_a_server_error_is_logged_with_its_status_and_never_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = Recorder(status=500)

    with caplog.at_level(logging.INFO, logger="invio.pipeline"):
        ping(URL, failed=False, transport=recorder.transport)

    (record,) = [r for r in caplog.records if r.getMessage() == "healthcheck.failed"]
    assert record.status == 500  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "error",
    [
        httpx2.ConnectError("cannot connect to " + URL),
        httpx2.TimeoutException("timed out " + URL),
        RuntimeError("boom " + URL),
    ],
)
def test_a_transport_error_is_logged_with_its_class_only(
    caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    recorder = Recorder(error=error)

    with caplog.at_level(logging.DEBUG):
        ping(URL, failed=True, transport=recorder.transport)

    (record,) = [r for r in caplog.records if r.getMessage() == "healthcheck.failed"]
    assert record.error == type(error).__name__  # type: ignore[attr-defined]
    assert record.exc_info is None


@pytest.mark.parametrize("failed", [False, True])
def test_the_url_never_reaches_the_logs(caplog: pytest.LogCaptureFixture, failed: bool) -> None:
    # No configure_logging(): the HTTP library's own INFO line must not carry the URL either.
    for recorder in (Recorder(), Recorder(status=503), Recorder(error=httpx2.ConnectError("x"))):
        with caplog.at_level(logging.DEBUG):
            ping(URL, failed=failed, transport=recorder.transport)

    assert SECRET not in caplog.text
    assert "hc.example.com" not in caplog.text


def test_a_redirect_is_not_followed_and_counts_as_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = Recorder(status=302)

    with caplog.at_level(logging.INFO, logger="invio.pipeline"):
        ping(URL, failed=False, transport=recorder.transport)

    assert len(recorder.requests) == 1
    (record,) = [r for r in caplog.records if r.getMessage() == "healthcheck.failed"]
    assert record.status == 302  # type: ignore[attr-defined]


def test_the_client_uses_a_three_second_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    real_client = httpx2.Client

    def spy(*args: object, **kwargs: object) -> httpx2.Client:
        seen.update(kwargs)
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx2, "Client", spy)

    ping(URL, failed=False, transport=Recorder().transport)

    assert (seen["timeout"], seen["follow_redirects"]) == (3.0, False)


def test_the_http_loggers_keep_their_levels_and_log_outside_a_ping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    http_logger = logging.getLogger("httpx2")
    level = http_logger.level

    with caplog.at_level(logging.DEBUG):
        ping(URL, failed=False, transport=Recorder().transport)
        http_logger.info("another client's request")

    assert http_logger.level == level
    assert "another client's request" in caplog.text
    assert SECRET not in caplog.text
