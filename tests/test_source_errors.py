"""Transcript error types: message formats, secret-safety and the transient/permanent split."""

import pytest

from invio.sources.errors import (
    FetchError,
    TranscriptionDeferred,
    TranscriptUnavailable,
    is_transient_fetch,
)

WATCH = "https://www.youtube.com/watch?v=abc_123-XYZ"


def test_whisper_not_installed_message_carries_the_install_hint() -> None:
    err = TranscriptUnavailable("whisper_not_installed", url=WATCH)

    assert str(err) == f"whisper_not_installed: {WATCH} (install it with: uv sync --extra whisper)"
    assert TranscriptUnavailable.INSTALL_HINT == "install it with: uv sync --extra whisper"


def test_too_long_message_appends_the_detail() -> None:
    err = TranscriptUnavailable("too_long", url=WATCH, detail="95 min > limit 90 min")

    assert str(err) == f"too_long: {WATCH} (95 min > limit 90 min)"


def test_plain_reason_and_status_follow_the_fetch_error_format() -> None:
    assert str(TranscriptUnavailable("no_speech", url=WATCH)) == f"no_speech: {WATCH}"
    err = TranscriptUnavailable("http_status", url=WATCH, status=429)
    assert str(err) == f"http_status: {WATCH} (HTTP 429)"
    assert err.status == 429
    assert err.reason == "http_status"
    assert isinstance(err, FetchError)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc&list=PL1&token=secret",
        "https://example.org/watch?v=abc",
        "https://www.youtube.com/watch?v=" + "a" * 65,
        "not a url?token=secret",
    ],
)
def test_message_keeps_the_query_only_for_a_canonical_watch_url(url: str) -> None:
    message = str(TranscriptUnavailable("video_unavailable", url=url))

    assert "token=secret" not in message
    assert "pw@" not in message
    assert "?v=" not in message


def test_userinfo_is_removed_from_the_canonical_url() -> None:
    err = TranscriptUnavailable("no_speech", url="https://user:pw@www.youtube.com/watch?v=abc")

    assert str(err) == "no_speech: https://www.youtube.com/watch?v=abc"


def test_description_is_never_part_of_the_message_or_repr() -> None:
    err = TranscriptUnavailable("video_unavailable", url=WATCH, description="SECRET TEXT")

    assert err.description == "SECRET TEXT"
    assert "SECRET TEXT" not in str(err)
    assert "SECRET TEXT" not in repr(err)


def test_other_fetch_errors_keep_their_message_format() -> None:
    assert str(FetchError("timeout", url="https://a.example/x?token=1")) == (
        "timeout: https://a.example/x"
    )


@pytest.mark.parametrize(
    "reason",
    [
        "whisper_not_installed",
        "too_long",
        "video_unavailable",
        "no_audio_format",
        "no_speech",
        "unsupported_url",
        "transcription_disabled",
    ],
)
def test_permanent_reasons_are_not_transient(reason: str) -> None:
    assert not is_transient_fetch(TranscriptUnavailable(reason, url=WATCH))


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("timeout", None),
        ("connection_failed", None),
        ("invalid_response", None),
        ("http_status", 429),
        ("http_status", 503),
    ],
)
def test_network_reasons_are_transient(reason: str, status: int | None) -> None:
    assert is_transient_fetch(TranscriptUnavailable(reason, url=WATCH, status=status))


def test_deferral_is_not_a_fetch_error_and_prints_only_its_reason() -> None:
    err = TranscriptionDeferred("cap_reached")

    assert not isinstance(err, FetchError)
    assert not is_transient_fetch(err)
    assert str(err) == "cap_reached"
    assert err.reason == "cap_reached"
