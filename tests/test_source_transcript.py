"""``YoutubeTranscripts``: subtitle selection, URL and error handling (offline)."""

import asyncio
import importlib
import importlib.util
import logging
import re
import sys
import threading
import types
import urllib.error
from collections.abc import Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from invio.config.settings import Settings
from invio.sources import transcript as transcript_module
from invio.sources.errors import (
    FetchError,
    TranscriptionDeferred,
    TranscriptUnavailable,
    is_transient_fetch,
)
from invio.sources.transcript import (
    AudioTooLong,
    SubtitleTrack,
    Transcript,
    TranscriptBackends,
    YoutubeTranscripts,
    _metadata,
    _model,
    _ordered_tracks,
    _whisper_available,
    _whisper_transcribe,
    _yt_dlp_audio,
    _yt_dlp_subtitle,
)
from tests.conftest import FakeClock
from tests.transcript_helpers import (
    NOW,
    PROXY,
    RAW_MESSAGE,
    URL,
    VIDEO_ID,
    FakeAudio,
    FakeExtract,
    FakeSubtitles,
    FakeTranscriber,
    fetch,
    formats,
    info,
    make,
    vtt,
)

# --- track order (FR-003) ---------------------------------------------------------------------


def order(
    job: str,
    *,
    subtitles: Mapping[str, object] | None = None,
    automatic: Mapping[str, object] | None = None,
    language: object = None,
) -> list[tuple[str, bool]]:
    meta = _metadata(info(subtitles=subtitles, automatic=automatic, language=language), VIDEO_ID)
    return [(t.key, t.automatic) for t in _ordered_tracks(meta.tracks, job)]


def test_order_prefers_job_language_then_english_then_others() -> None:
    result = order(
        "de",
        subtitles={"fr": formats(), "en": formats(), "de": formats()},
        automatic={"de-orig": formats(), "en-orig": formats()},
    )

    assert result == [
        ("de", False),
        ("de-orig", True),
        ("en", False),
        ("en-orig", True),
    ]  # capped at four tracks; "fr" would be the fifth


def test_manual_beats_automatic_within_a_language() -> None:
    assert order("de", subtitles={"de": formats()}, automatic={"de": formats()}) == [
        ("de", False),
        ("de", True),
    ]


def test_original_job_language_automatic_beats_manual_english() -> None:
    result = order(
        "de", subtitles={"en": formats()}, automatic={"de-orig": formats()}, language="de"
    )

    assert result == [("de-orig", True), ("en", False)]


def test_a_translation_into_the_job_language_is_the_last_subtitle_tier() -> None:
    result = order(
        "de",
        subtitles={"en": formats(), "es": formats()},
        automatic={"de": formats(), "fr-orig": formats(), "en-orig": formats()},
        language="fr",
    )

    assert result == [
        ("en", False),
        ("en-orig", True),
        ("es", False),
        ("fr-orig", True),
    ]  # capped at four: the translation would be the fifth


def test_translations_follow_every_original_and_manual_track() -> None:
    result = order(
        "de", subtitles={"es": formats()}, automatic={"de": formats(), "fr-orig": formats()}
    )

    assert result == [("es", False), ("fr-orig", True), ("de", True)]


def test_other_manual_before_other_original_automatic() -> None:
    result = order(
        "de",
        subtitles={"es": formats()},
        automatic={"fr": formats(), "fr-orig": formats()},
        language="fr",
    )

    assert result == [("es", False), ("fr-orig", True), ("fr", True)]


def test_primary_subtag_matching() -> None:
    result = order("de", subtitles={"de-DE": formats()}, automatic={"de-orig": formats()})

    assert result == [("de-DE", False), ("de-orig", True)]


def test_job_language_with_region_matches_the_primary_subtag() -> None:
    assert order("de-AT", subtitles={"de": formats()}) == [("de", False)]


def test_original_asr_comes_before_translation_in_the_same_tier() -> None:
    result = order(
        "de",
        automatic={"de": formats(), "de-orig": formats()},
        language="en",
    )

    assert result == [("de-orig", True), ("de", True)]


def test_machine_translations_are_not_tried_as_other_automatic() -> None:
    result = order(
        "de",
        automatic={"fr": formats(), "ja": formats(), "es-orig": formats()},
        language="es",
    )

    assert result == [("es-orig", True)]


def test_only_translations_into_the_job_language_are_ever_used() -> None:
    result = order(
        "de",
        automatic={"ja": formats(), "de": formats(), "en": formats()},
        language="ja",
    )

    assert result == [("ja", True), ("de", True)]  # no English machine translation


def test_a_track_offered_twice_is_tried_once() -> None:
    # job language English: the job-language and English tiers both list the same tracks
    result = order("en", subtitles={"en": formats()}, automatic={"en": formats()})

    assert result == [("en", False), ("en", True)]


@pytest.mark.parametrize(
    ("text", "language"), [("", "en"), ("   ", "en"), ("hello", ""), ("hello", "  ")]
)
def test_a_transcript_needs_text_and_a_language(text: str, language: str) -> None:
    with pytest.raises(ValueError, match="needs"):
        Transcript(text, language, "subtitles")


def test_at_most_four_tracks_are_tried() -> None:
    manual = {key: formats() for key in ("a", "b", "c", "d", "e", "f")}

    assert len(order("de", subtitles=manual)) == 4


def test_tracks_without_a_vtt_format_are_ignored() -> None:
    result = order(
        "de",
        subtitles={"de": [{"ext": "json3", "url": "u"}], "en": formats(), "fr": "junk"},
        automatic={"es": [], "it": [42, None]},
    )

    assert result == [("en", False)]


def test_metadata_is_built_defensively() -> None:
    meta = _metadata(
        {
            "duration": "long",
            "subtitles": ["not", "a", "mapping"],
            "automatic_captions": None,
            "description": 7,
            "language": 5,
        },
        VIDEO_ID,
    )

    assert meta.duration_seconds is None
    assert meta.tracks == []
    assert meta.description is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (300, 300),
        (300.2, 301),
        (0, 0),
        (-5, None),
        (True, None),
        (float("nan"), None),
        (None, None),
    ],
)
def test_metadata_duration(raw: object, expected: int | None) -> None:
    assert _metadata(info(duration=raw), VIDEO_ID).duration_seconds == expected


def test_metadata_description_is_kept_when_text() -> None:
    assert _metadata(info(description="About X"), VIDEO_ID).description == "About X"
    assert _metadata(info(description="   "), VIDEO_ID).description is None


def test_track_flags() -> None:
    meta = _metadata(
        info(
            subtitles={"en-US": formats()},
            automatic={"de-orig": formats(), "de": formats(), "en": formats()},
            language="de-DE",
        ),
        VIDEO_ID,
    )

    by_key = {(t.key, t.automatic): t for t in meta.tracks}
    assert by_key[("en-US", False)] == SubtitleTrack("en-US", "en", False, False)
    assert by_key[("de-orig", True)].original
    assert by_key[("de", True)].original  # equals the video's own language
    assert not by_key[("en", True)].original


# --- subtitle path ----------------------------------------------------------------------------


async def test_first_non_empty_track_wins_and_no_audio_is_used() -> None:
    subtitles = FakeSubtitles({"de": vtt("Hallo", "Welt")})
    audio, transcriber = FakeAudio(), FakeTranscriber()
    adapter = make(
        extract=FakeExtract(info(subtitles={"de": formats()}, automatic={"en": formats()})),
        subtitles=subtitles,
        audio=audio,
        transcriber=transcriber,
    )

    result = await fetch(adapter)

    assert (result.text, result.language, result.method) == ("Hallo Welt", "de", "subtitles")
    assert subtitles.calls == [(URL, "de", False)]
    assert audio.calls == []
    assert transcriber.calls == []


async def test_the_fallback_is_not_consulted_when_subtitles_exist() -> None:
    probes: list[str] = []

    def guard() -> bool:
        probes.append("whisper_available")
        return True

    adapter = make(
        extract=FakeExtract(info(automatic={"en-orig": formats()})),
        subtitles=FakeSubtitles({"en-orig": vtt("hi")}),
        whisper=guard,
    )
    may: list[str] = []

    def may_transcribe() -> bool:
        may.append("called")
        return True

    result = await fetch(adapter, may_transcribe=may_transcribe)

    assert result.method == "subtitles"
    assert probes == [] and may == []


async def test_job_de_manual_en_and_automatic_de_uses_automatic_de() -> None:
    subtitles = FakeSubtitles({"de": vtt("Guten Tag"), "en": vtt("Good day")})
    adapter = make(
        extract=FakeExtract(
            info(subtitles={"en": formats()}, automatic={"de": formats()}, language="de")
        ),
        subtitles=subtitles,
    )

    result = await fetch(adapter, language="de")

    assert (result.text, result.language) == ("Guten Tag", "de")
    assert subtitles.calls == [(URL, "de", True)]


async def test_only_spanish_available_returns_spanish() -> None:
    adapter = make(
        extract=FakeExtract(info(subtitles={"es": formats()})),
        subtitles=FakeSubtitles({"es": vtt("hola")}),
    )

    result = await fetch(adapter, language="de")

    assert (result.text, result.language, result.method) == ("hola", "es", "subtitles")


async def test_a_track_that_converts_to_nothing_falls_through_to_the_next() -> None:
    subtitles = FakeSubtitles(
        {"de": "WEBVTT\n\n00:00.000 --> 00:01.000\n<c></c>\n", "en": vtt("ok")}
    )
    adapter = make(
        extract=FakeExtract(info(subtitles={"de": formats(), "en": formats()})),
        subtitles=subtitles,
    )

    result = await fetch(adapter, language="de")

    assert (result.text, result.language) == ("ok", "en")
    assert subtitles.keys == ["de", "en"]


async def test_subtitle_seam_receives_options_and_a_temp_dir_that_is_removed() -> None:
    subtitles = FakeSubtitles({"de": vtt("x")})
    adapter = make(
        extract=FakeExtract(info(subtitles={"de": formats()})),
        subtitles=subtitles,
        proxy=PROXY,
        timeout=12.0,
    )

    await fetch(adapter)

    (options,) = subtitles.options
    assert options["proxy"] == PROXY
    assert options["socket_timeout"] == 12.0
    assert options["allowed_extractors"] == ["youtube.*"]
    (tmp,) = subtitles.tmp_dirs
    assert tmp.name.startswith("invio-sub-")
    assert not tmp.exists()
    assert subtitles.threads[0].startswith("invio-transcript")


async def test_metadata_options_are_the_shared_base_options(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    extract = FakeExtract(info(subtitles={"de": formats()}))
    adapter = make(
        extract=extract,
        subtitles=FakeSubtitles({"de": vtt("x")}),
        proxy=PROXY,
        cookies_file=cookies,
    )

    await fetch(adapter)

    ((url, options),) = extract.calls
    assert url == URL
    assert options["cookiefile"] == str(cookies)
    assert options["proxy"] == PROXY
    assert options["retries"] == 0
    assert extract.threads[0].startswith("invio-transcript")


# --- URLs -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        URL,
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}?si=abc",
        f"https://m.youtube.com/watch?v={VIDEO_ID}&t=5s",
        f"http://youtube.com/watch?v={VIDEO_ID}&list=PL1",
    ],
)
async def test_video_urls_are_canonicalised_for_the_seams(url: str) -> None:
    extract, subtitles = (
        FakeExtract(info(subtitles={"de": formats()})),
        FakeSubtitles({"de": vtt("x")}),
    )
    adapter = make(extract=extract, subtitles=subtitles)

    await fetch(adapter, url)

    assert extract.calls[0][0] == URL
    assert subtitles.calls[0][0] == URL


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "https://example.org/watch?v=abc",
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch?v=",
        "https://www.youtube.com/watch?v=bad%20id",
        "https://www.youtube.com/watch?v=" + "a" * 65,
        "ftp://www.youtube.com/watch?v=abc",
        "https://www.youtube.com/channel/UC123",
        "https://youtu.be/",
        "https://www.youtube.com/shorts/",
        "https://[broken/watch?v=abc",
    ],
)
async def test_unsupported_urls_raise_without_touching_any_seam(url: str) -> None:
    extract = FakeExtract()
    adapter = make(extract=extract)

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter, url)

    assert caught.value.reason == "unsupported_url"
    assert not is_transient_fetch(caught.value)
    assert extract.calls == []


# --- errors -----------------------------------------------------------------------------------


async def test_a_rate_limit_is_transient_and_never_falls_back() -> None:
    audio = FakeAudio()
    adapter = make(
        extract=FakeExtract(exc=RuntimeError("HTTP Error 429: " + RAW_MESSAGE)), audio=audio
    )

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "http_status" and caught.value.status == 429
    assert is_transient_fetch(caught.value)
    assert audio.calls == []
    assert RAW_MESSAGE not in str(caught.value)


async def test_a_transient_subtitle_error_does_not_trigger_the_fallback() -> None:
    audio, transcriber = FakeAudio(), FakeTranscriber()
    adapter = make(
        extract=FakeExtract(info(subtitles={"de": formats()})),
        subtitles=FakeSubtitles(exc=RuntimeError("HTTP Error 503")),
        audio=audio,
        transcriber=transcriber,
    )

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert is_transient_fetch(caught.value)
    assert audio.calls == [] and transcriber.calls == []


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("ERROR: Video unavailable"),
        RuntimeError("ERROR: This video is private"),
        urllib.error.HTTPError(URL, 404, "Not Found", {}, None),  # type: ignore[arg-type]
        urllib.error.HTTPError(URL, 410, "Gone", {}, None),  # type: ignore[arg-type]
        RuntimeError("Sign in to confirm your age. This video may be inappropriate"),
        RuntimeError("This video is age-restricted"),
        RuntimeError("The uploader has not made this video available in your country"),
        RuntimeError("Join this channel to get access to members-only content"),
    ],
)
async def test_unavailable_videos_are_permanent(exc: BaseException) -> None:
    adapter = make(extract=FakeExtract(exc=exc))

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "video_unavailable"
    assert not is_transient_fetch(caught.value)


def _forbidden(message: str | None = None) -> BaseException:
    http = urllib.error.HTTPError(URL, 403, "Forbidden", {}, None)  # type: ignore[arg-type]
    if message is None:
        return http
    wrapper = RuntimeError(message)
    wrapper.__cause__ = http
    return wrapper


async def test_a_plain_403_is_transient_not_video_unavailable() -> None:
    # YouTube also answers 403 for IP blocks, proxy refusals and PO-token problems
    adapter = make(extract=FakeExtract(exc=_forbidden()))

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason != "video_unavailable"
    assert caught.value.status == 403
    assert is_transient_fetch(caught.value)


@pytest.mark.parametrize(
    "message",
    [
        "Sign in to confirm your age",
        "This video is private",
        "Join this channel to get access to members-only content",
    ],
)
async def test_a_403_confirmed_by_a_marker_text_is_video_unavailable(message: str) -> None:
    adapter = make(extract=FakeExtract(exc=_forbidden(message)))

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "video_unavailable"
    assert not is_transient_fetch(caught.value)


async def test_unavailable_error_carries_the_description_when_the_info_had_one() -> None:
    adapter = make(
        extract=FakeExtract(info(subtitles={"de": formats()}, description="About X")),
        subtitles=FakeSubtitles(exc=urllib.error.HTTPError(URL, 404, "x", {}, None)),  # type: ignore[arg-type]
    )

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "video_unavailable"
    assert caught.value.description == "About X"
    assert "About X" not in str(caught.value)


async def test_a_requested_format_error_is_no_audio_format() -> None:
    adapter = make(
        extract=FakeExtract(exc=RuntimeError("ERROR: Requested format is not available"))
    )

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "no_audio_format"
    assert not is_transient_fetch(caught.value)


async def test_a_timeout_error_from_a_seam_is_transient() -> None:
    adapter = make(extract=FakeExtract(exc=TimeoutError()))

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "timeout"
    assert is_transient_fetch(caught.value)


async def test_a_stalled_metadata_call_times_out() -> None:
    gate = threading.Event()
    adapter = make(extract=FakeExtract(gate=gate), timeout=0.05)

    try:
        with pytest.raises(TranscriptUnavailable) as caught:
            await fetch(adapter)
    finally:
        gate.set()

    assert caught.value.reason == "timeout"
    assert is_transient_fetch(caught.value)


async def test_a_stalled_subtitle_download_times_out() -> None:
    gate = threading.Event()

    class Stalling(FakeSubtitles):
        def __call__(self, *args: object) -> str:  # type: ignore[override]
            gate.wait(10)
            return ""

    adapter = make(
        extract=FakeExtract(info(subtitles={"de": formats()})), subtitles=Stalling(), timeout=0.05
    )

    try:
        with pytest.raises(TranscriptUnavailable) as caught:
            await fetch(adapter)
    finally:
        gate.set()

    assert caught.value.reason == "timeout"


async def test_unknown_failures_are_invalid_response_without_the_library_text() -> None:
    adapter = make(extract=FakeExtract(exc=RuntimeError(RAW_MESSAGE)))

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "invalid_response"
    assert is_transient_fetch(caught.value)
    assert caught.value.__cause__ is None
    assert RAW_MESSAGE not in repr(caught.value)


async def test_a_missing_cookies_file_is_a_plain_fetch_error(tmp_path: Path) -> None:
    extract = FakeExtract()
    adapter = make(extract=extract, cookies_file=tmp_path / "missing.txt")

    with pytest.raises(FetchError) as caught:
        await fetch(adapter)

    assert type(caught.value) is FetchError
    assert caught.value.reason == "cookies_unavailable"
    assert not is_transient_fetch(caught.value)
    assert extract.calls == []
    assert str(tmp_path) not in str(caught.value)


async def test_a_fetch_error_from_a_seam_passes_through() -> None:
    adapter = make(extract=FakeExtract(exc=FetchError("cookies_unavailable", url=URL)))

    with pytest.raises(FetchError) as caught:
        await fetch(adapter)

    assert type(caught.value) is FetchError


async def test_secrets_and_library_text_never_reach_errors_or_logs(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("x")
    leak = f"{RAW_MESSAGE} {PROXY} {cookies}"
    adapter = make(extract=FakeExtract(exc=RuntimeError(leak)), proxy=PROXY, cookies_file=cookies)

    with caplog.at_level(logging.DEBUG), pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    shown = f"{caught.value!s} {caught.value!r} {caught.value.args}"
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for secret in (PROXY, "secret", str(cookies), "sensitive-library-text"):
        assert secret not in shown
        assert secret not in logged
    failed = [r for r in caplog.records if r.getMessage() == "transcript.failed"]
    assert failed and failed[0].__dict__["error"] == "RuntimeError"


async def test_close_then_get_transcript_raises() -> None:
    adapter = make()
    adapter.close()

    with pytest.raises(RuntimeError, match="closed"):
        await fetch(adapter)


async def test_subtitle_logging(caplog: pytest.LogCaptureFixture) -> None:
    adapter = make(
        extract=FakeExtract(info(automatic={"de": formats()})),
        subtitles=FakeSubtitles({"de": vtt("x")}),
    )

    with caplog.at_level(logging.INFO, logger="invio.sources.transcript"):
        await fetch(adapter)

    (record,) = [r for r in caplog.records if r.getMessage() == "transcript.subtitles"]
    assert record.__dict__["video_id"] == VIDEO_ID
    assert record.__dict__["language"] == "de"
    assert record.__dict__["automatic"] is True


async def test_cleanup_failure_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    removed: list[str] = []
    real_rmtree = shutil.rmtree
    monkeypatch.setattr(shutil, "rmtree", lambda path, *a, **k: removed.append(str(path)))
    subtitles = FakeSubtitles({"de": vtt("x")})
    adapter = make(extract=FakeExtract(info(subtitles={"de": formats()})), subtitles=subtitles)

    with caplog.at_level(logging.WARNING, logger="invio.sources.transcript"):
        result = await fetch(adapter)

    assert result.text == "x"
    assert any(r.getMessage() == "transcript.cleanup_failed" for r in caplog.records)
    for leftover in subtitles.tmp_dirs:
        real_rmtree(leftover, ignore_errors=True)


# --- default subtitle seam --------------------------------------------------------------------


class RecordingYoutubeDL:
    """Stands in for ``yt_dlp.YoutubeDL``; ``download`` writes a VTT where ``outtmpl`` says."""

    instances: ClassVar[list["RecordingYoutubeDL"]] = []

    def __init__(self, params: Mapping[str, object]) -> None:
        self.params = dict(params)
        self.urls: list[str] = []
        type(self).instances.append(self)

    def __enter__(self) -> "RecordingYoutubeDL":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def download(self, urls: list[str]) -> int:
        self.urls += urls
        template = str(self.params["outtmpl"])
        target = Path(template.replace("%(ext)s", "de.vtt"))
        target.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nGrüße\n", encoding="utf-8")
        return 0


@pytest.fixture
def recording_ydl(monkeypatch: pytest.MonkeyPatch) -> type[RecordingYoutubeDL]:
    import yt_dlp

    RecordingYoutubeDL.instances = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", RecordingYoutubeDL)
    return RecordingYoutubeDL


@pytest.mark.parametrize("automatic", [False, True])
def test_default_subtitle_seam_options_and_result(
    recording_ydl: type[RecordingYoutubeDL], tmp_path: Path, automatic: bool
) -> None:
    track = SubtitleTrack("de", "de", automatic, False)

    text = _yt_dlp_subtitle(URL, track, {"quiet": True}, tmp_path)

    (ydl,) = recording_ydl.instances
    assert ydl.urls == [URL]
    assert ydl.params["skip_download"] is True
    assert ydl.params["subtitleslangs"] == ["de"]
    assert ydl.params["subtitlesformat"] == "vtt"
    assert ydl.params["writeautomaticsub"] is automatic
    assert ydl.params["writesubtitles"] is (not automatic)
    assert str(ydl.params["outtmpl"]).startswith(str(tmp_path))
    assert text.endswith("Grüße\n")


def test_default_subtitle_seam_returns_empty_text_when_no_file_was_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import yt_dlp

    class Silent(RecordingYoutubeDL):
        def download(self, urls: list[str]) -> int:
            return 0

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Silent)

    assert _yt_dlp_subtitle(URL, SubtitleTrack("de", "de", False, False), {}, tmp_path) == ""


def test_default_subtitle_seam_uses_a_private_cookie_copy(
    recording_ydl: type[RecordingYoutubeDL], tmp_path: Path
) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    out = tmp_path / "out"
    out.mkdir()

    _yt_dlp_subtitle(
        URL, SubtitleTrack("de", "de", False, False), {"cookiefile": str(cookies)}, out
    )

    (ydl,) = recording_ydl.instances
    assert ydl.params["cookiefile"] != str(cookies)


def test_subtitle_option_names_are_known_to_yt_dlp(
    recording_ydl: type[RecordingYoutubeDL], tmp_path: Path
) -> None:
    import importlib

    from invio.sources import ytdlp

    base = ytdlp.base_options(timeout=5.0, cookies_file=None, proxy=PROXY)
    _yt_dlp_subtitle(URL, SubtitleTrack("de", "de", True, False), base, tmp_path)
    (ydl,) = recording_ydl.instances
    # the class itself is replaced by the fixture: read the docstring of the real one
    doc = importlib.import_module("yt_dlp.YoutubeDL").YoutubeDL.__doc__ or ""
    documented = set(re.findall(r"^[ \t]*([a-z_]+):[ \t]", doc, re.MULTILINE))
    downloader = re.search(r"used by\s+the downloader[^:]*:\s*([a-z_,\s]+)\.", doc)
    assert downloader is not None
    documented |= {name.strip() for name in downloader.group(1).split(",")}

    assert {"writesubtitles", "writeautomaticsub", "subtitleslangs", "outtmpl"} <= documented
    assert sorted(set(ydl.params) - documented - {"ignoreconfig"}) == []


# --- local transcription fallback (US2) -------------------------------------------------------


def no_subtitles(**fields: object) -> FakeExtract:
    return FakeExtract(info(duration=600, **fields))  # type: ignore[arg-type]


async def test_without_subtitles_the_audio_is_transcribed_locally() -> None:
    audio = FakeAudio()
    transcriber = FakeTranscriber("hello from audio", "en")
    adapter = make(
        extract=no_subtitles(), audio=audio, transcriber=transcriber, model_size="medium"
    )

    result = await fetch(adapter)

    assert (result.text, result.language, result.method) == (
        "hello from audio",
        "en",
        "transcription",
    )
    ((audio_url, options, tmp),) = audio.calls
    assert audio_url == URL
    assert tmp.name.startswith("invio-audio-")
    assert options["allowed_extractors"] == ["youtube.*"]
    ((path, model_size, max_seconds),) = transcriber.calls
    assert path.parent == tmp and path.name == "audio.webm"
    assert (model_size, max_seconds) == ("medium", 90 * 60)
    assert transcriber.audio_existed == [True]


async def test_the_duration_limit_reaches_the_transcriber() -> None:
    transcriber = FakeTranscriber()
    adapter = make(extract=no_subtitles(), transcriber=transcriber, max_duration_minutes=30)

    await fetch(adapter)

    assert transcriber.calls[0][2] == 30 * 60


async def test_the_fallback_runs_on_the_whisper_worker_and_metadata_on_the_other_pool() -> None:
    audio, extract = FakeAudio(), no_subtitles()
    adapter = make(extract=extract, audio=audio)

    await fetch(adapter)

    assert audio.thread_names[0].startswith("invio-whisper")
    assert extract.threads[0].startswith("invio-transcript")


async def test_the_temp_dir_is_removed_after_success() -> None:
    audio = FakeAudio()
    adapter = make(extract=no_subtitles(), audio=audio)

    await fetch(adapter)

    assert audio.tmp_dirs and not audio.tmp_dirs[0].exists()


async def test_the_temp_dir_is_removed_after_a_failing_download() -> None:
    audio = FakeAudio(exc=RuntimeError("boom"))
    adapter = make(extract=no_subtitles(), audio=audio)

    with pytest.raises(TranscriptUnavailable):
        await fetch(adapter)

    assert audio.tmp_dirs and not audio.tmp_dirs[0].exists()


async def test_the_temp_dir_is_removed_after_a_failing_transcriber() -> None:
    audio = FakeAudio()
    adapter = make(
        extract=no_subtitles(), audio=audio, transcriber=FakeTranscriber(exc=RuntimeError("boom"))
    )

    with pytest.raises(TranscriptUnavailable):
        await fetch(adapter)

    assert audio.tmp_dirs and not audio.tmp_dirs[0].exists()


@pytest.mark.parametrize("text", ["", "   \n"])
async def test_a_silent_video_is_no_speech(text: str) -> None:
    adapter = make(extract=no_subtitles(), transcriber=FakeTranscriber(text))

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "no_speech"
    assert not is_transient_fetch(caught.value)
    assert caught.value.description == "About X"


async def test_no_audio_format_is_permanent() -> None:
    audio = FakeAudio(exc=RuntimeError("ERROR: Requested format is not available"))
    adapter = make(extract=no_subtitles(), audio=audio)

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "no_audio_format"
    assert not is_transient_fetch(caught.value)
    assert caught.value.description == "About X"


async def test_no_audio_format_from_the_seam_gets_the_description() -> None:
    # the default audio seam knows no description: the adapter fills it in
    seam_error = TranscriptUnavailable("no_audio_format", url=URL)
    adapter = make(extract=no_subtitles(), audio=FakeAudio(exc=seam_error))

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "no_audio_format"
    assert caught.value.description == "About X"
    assert "About X" not in str(caught.value)


async def test_a_description_already_on_a_seam_error_is_kept() -> None:
    seam_error = TranscriptUnavailable("no_audio_format", url=URL, description="Own")
    adapter = make(extract=no_subtitles(), audio=FakeAudio(exc=seam_error))

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.description == "Own"


async def test_an_audio_network_error_is_transient() -> None:
    audio = FakeAudio(exc=urllib.error.URLError("connection refused"))
    adapter = make(extract=no_subtitles(), audio=audio)

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "connection_failed"
    assert is_transient_fetch(caught.value)


async def test_a_missing_speech_library_inside_the_transcriber_is_whisper_not_installed() -> None:
    adapter = make(
        extract=no_subtitles(),
        transcriber=FakeTranscriber(exc=ModuleNotFoundError(name="faster_whisper")),
    )

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "whisper_not_installed"
    assert "uv sync --extra whisper" in str(caught.value)


async def test_a_missing_submodule_of_the_extra_is_whisper_not_installed() -> None:
    error = ModuleNotFoundError(name="faster_whisper.transcribe")
    adapter = make(extract=no_subtitles(), transcriber=FakeTranscriber(exc=error))

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "whisper_not_installed"


@pytest.mark.parametrize(
    "error",
    [
        ModuleNotFoundError(name="numpy"),  # a dependency of the extra is broken
        ImportError("cannot import name 'x' from 'faster_whisper'"),
        ImportError("libctranslate2.so: cannot open shared object file"),
    ],
    ids=["missing-dependency", "bad-name", "shared-library"],
)
async def test_any_other_import_error_is_a_broken_install_not_a_missing_extra(
    error: ImportError,
) -> None:
    adapter = make(extract=no_subtitles(), transcriber=FakeTranscriber(exc=error))

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason != "whisper_not_installed"
    assert "uv sync" not in str(caught.value)


async def test_whisper_not_installed_is_logged_as_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = make(extract=no_subtitles(), whisper=False)

    with (
        caplog.at_level(logging.INFO, logger="invio.sources.transcript"),
        pytest.raises(TranscriptUnavailable),
    ):
        await fetch(adapter)

    (record,) = [r for r in caplog.records if r.getMessage() == "transcript.unavailable"]
    assert record.levelno == logging.WARNING
    assert record.__dict__["reason"] == "whisper_not_installed"


async def test_other_permanent_reasons_stay_at_info(caplog: pytest.LogCaptureFixture) -> None:
    adapter = make(extract=no_subtitles(), transcription_enabled=False)

    with (
        caplog.at_level(logging.INFO, logger="invio.sources.transcript"),
        pytest.raises(TranscriptUnavailable),
    ):
        await fetch(adapter)

    (record,) = [r for r in caplog.records if r.getMessage() == "transcript.unavailable"]
    assert record.levelno == logging.INFO


async def test_whisper_not_installed_warns_once_per_adapter_then_logs_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = make(extract=no_subtitles(), whisper=False)

    with caplog.at_level(logging.INFO, logger="invio.sources.transcript"):
        for _ in range(3):
            with pytest.raises(TranscriptUnavailable):
                await fetch(adapter)

    levels = [r.levelno for r in caplog.records if r.getMessage() == "transcript.unavailable"]
    assert levels == [logging.WARNING, logging.INFO, logging.INFO]


# --- live streams and the download size cap ---------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"live_status": "is_live"},
        {"live_status": "is_upcoming"},
        {"live_status": "post_live"},
        {"is_live": True},
    ],
)
async def test_a_live_or_upcoming_stream_is_never_downloaded(fields: dict[str, object]) -> None:
    audio = FakeAudio()
    adapter = make(extract=FakeExtract({**info(duration=None), **fields}), audio=audio)

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "live_stream"
    assert caught.value.description == "About X"
    assert not is_transient_fetch(caught.value)
    assert audio.calls == []


@pytest.mark.parametrize("status", ["was_live", "not_live", None])
async def test_a_finished_stream_or_plain_video_is_transcribed(status: str | None) -> None:
    adapter = make(extract=FakeExtract({**info(), "live_status": status, "is_live": False}))

    transcript = await fetch(adapter)

    assert transcript.method == "transcription"


async def test_subtitles_of_a_live_stream_are_still_used() -> None:
    extract = FakeExtract(info(subtitles={"de": formats()}) | {"live_status": "post_live"})
    adapter = make(extract=extract, subtitles=FakeSubtitles({"de": vtt("Hallo")}))

    assert (await fetch(adapter)).method == "subtitles"


async def test_the_audio_download_is_capped_in_size_by_the_duration_limit() -> None:
    audio = FakeAudio()
    adapter = make(extract=no_subtitles(), audio=audio, max_duration_minutes=30)

    await fetch(adapter)

    ((_, options, _),) = audio.calls
    assert options["max_filesize"] == 30 * 60 * 48_000
    assert options["allowed_extractors"] == ["youtube.*"]


async def test_audio_longer_than_the_limit_reported_by_the_transcriber_is_too_long() -> None:
    adapter = make(
        extract=FakeExtract(info(duration=None)),
        transcriber=FakeTranscriber(exc=AudioTooLong(95 * 60 - 20)),
    )

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "too_long"
    assert str(caught.value).endswith("(95 min > limit 90 min)")
    assert not is_transient_fetch(caught.value)
    assert caught.value.description == "About X"


async def test_the_transcription_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    adapter = make(extract=no_subtitles(), transcriber=FakeTranscriber("x", "fr"))

    with caplog.at_level(logging.INFO, logger="invio.sources.transcript"):
        await fetch(adapter)

    (record,) = [r for r in caplog.records if r.getMessage() == "transcript.transcribed"]
    assert record.__dict__["video_id"] == VIDEO_ID
    assert record.__dict__["language"] == "fr"
    assert record.__dict__["seconds"] == 600


async def test_the_audio_temp_dir_cleanup_failure_is_logged(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    real_rmtree = shutil.rmtree
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)
    audio = FakeAudio()
    adapter = make(extract=no_subtitles(), audio=audio)

    with caplog.at_level(logging.WARNING, logger="invio.sources.transcript"):
        await fetch(adapter)

    assert any(r.getMessage() == "transcript.cleanup_failed" for r in caplog.records)
    real_rmtree(audio.tmp_dirs[0], ignore_errors=True)


async def test_a_cleanup_failure_is_logged_when_the_download_raised_too(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    real_rmtree = shutil.rmtree
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)
    audio = FakeAudio(exc=RuntimeError("boom"))
    adapter = make(extract=no_subtitles(), audio=audio)

    with (
        caplog.at_level(logging.WARNING, logger="invio.sources.transcript"),
        pytest.raises(TranscriptUnavailable) as caught,
    ):
        await fetch(adapter)

    assert caught.value.reason == "invalid_response"  # the original failure is not masked
    assert any(r.getMessage() == "transcript.cleanup_failed" for r in caplog.records)
    real_rmtree(audio.tmp_dirs[0], ignore_errors=True)


# --- one at a time, cancellation (FR-009) -----------------------------------------------------


async def test_three_concurrent_fallbacks_run_one_at_a_time() -> None:
    transcriber = FakeTranscriber(delay=0.03)
    adapter = make(extract=no_subtitles(), transcriber=transcriber)

    results = await asyncio.gather(fetch(adapter), fetch(adapter), fetch(adapter))

    assert [r.method for r in results] == ["transcription"] * 3
    assert len(transcriber.calls) == 3
    assert transcriber.max_active == 1


async def test_cancelling_callers_never_lets_two_jobs_run_at_once() -> None:
    gate, entered = threading.Event(), threading.Event()
    transcriber = FakeTranscriber(gate=gate, entered=entered)
    audio = FakeAudio()
    adapter = make(extract=no_subtitles(), audio=audio, transcriber=transcriber)

    first = asyncio.create_task(fetch(adapter))
    await asyncio.to_thread(entered.wait, 5)
    second = asyncio.create_task(fetch(adapter))
    await asyncio.sleep(0.15)  # the second caller waits for the worker
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    first.cancel()  # the running job cannot be stopped: it finishes in its thread
    with pytest.raises(asyncio.CancelledError):
        await first
    third = asyncio.create_task(fetch(adapter))
    await asyncio.sleep(0.15)

    assert len(audio.calls) == 1  # no download for the cancelled one; the third waits
    gate.set()
    result = await asyncio.wait_for(third, 10)

    assert result.method == "transcription"
    assert len(audio.calls) == 2
    assert transcriber.max_active == 1
    assert not audio.tmp_dirs[0].exists()  # the cancelled, running job still cleaned up


# --- guards and the run-time check ------------------------------------------------------------


async def test_a_full_cap_defers_without_any_download() -> None:
    audio, asked = FakeAudio(), []

    def may_transcribe() -> bool:
        asked.append(True)
        return False

    adapter = make(extract=no_subtitles(), audio=audio)

    with pytest.raises(TranscriptionDeferred) as caught:
        await fetch(adapter, may_transcribe=may_transcribe)

    assert caught.value.reason == "cap_reached"
    assert audio.calls == []
    assert asked == [True]  # once


async def test_the_cap_is_not_asked_when_a_track_yields_text() -> None:
    asked: list[bool] = []
    adapter = make(
        extract=FakeExtract(info(subtitles={"de": formats()})),
        subtitles=FakeSubtitles({"de": vtt("x")}),
    )

    await fetch(adapter, may_transcribe=lambda: asked.append(True) or True)

    assert asked == []


@pytest.mark.parametrize(
    ("duration", "minutes_left", "defer"),
    [
        (60 * 60, 40, True),  # US2 scenario 7
        (30 * 60, 41, False),  # 30 + 10 reserve <= 41
        (30 * 60, 40, False),  # exactly enough
        (30 * 60, 39, True),
        (None, 99, True),  # unknown duration needs the limit: 90 + 10
        (None, 100, False),
    ],
)
async def test_the_run_time_check(duration: int | None, minutes_left: int, defer: bool) -> None:
    audio, clock = FakeAudio(), FakeClock(NOW)
    adapter = make(extract=FakeExtract(info(duration=duration)), audio=audio, clock=clock)
    deadline = NOW + timedelta(minutes=minutes_left)

    if defer:
        with pytest.raises(TranscriptionDeferred) as caught:
            await fetch(adapter, deadline=deadline)
        assert caught.value.reason == "not_enough_time"
        assert audio.calls == []
    else:
        assert (await fetch(adapter, deadline=deadline)).method == "transcription"


async def test_without_a_deadline_there_is_no_run_time_check() -> None:
    clock = FakeClock(NOW)
    adapter = make(extract=FakeExtract(info(duration=80 * 60)), clock=clock)

    assert (await fetch(adapter, deadline=None)).method == "transcription"


async def test_the_run_time_check_uses_the_clock_when_the_job_starts() -> None:
    clock, started, gate = FakeClock(NOW), threading.Event(), threading.Event()
    durations = {"first": 10 * 60, "second": 20 * 60}

    def extract(url: str, options: Mapping[str, object]) -> Mapping[str, object]:
        return info(duration=durations["first" if "first" in url else "second"])

    def slow_first_download() -> None:
        clock.advance(timedelta(minutes=30))  # the first job takes half an hour
        started.set()
        gate.wait(5)

    audio = FakeAudio(on_call=slow_first_download)
    adapter = make(extract=extract, audio=audio, clock=clock)
    deadline = NOW + timedelta(minutes=55)  # fixed when both callers started

    first = asyncio.create_task(fetch(adapter, "https://youtu.be/first", deadline=deadline))
    await asyncio.to_thread(started.wait, 5)
    second = asyncio.create_task(fetch(adapter, "https://youtu.be/second", deadline=deadline))
    await asyncio.sleep(0.15)  # queued behind the first job
    gate.set()

    assert (await first).method == "transcription"
    # 30 min elapsed + 20 min video + 10 min reserve = 60 > 55: it fitted when it was queued only
    with pytest.raises(TranscriptionDeferred) as caught:
        await second
    assert caught.value.reason == "not_enough_time"
    assert len(audio.calls) == 1


async def test_deferrals_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    adapter = make(extract=no_subtitles())

    with (
        caplog.at_level(logging.INFO, logger="invio.sources.transcript"),
        pytest.raises(TranscriptionDeferred),
    ):
        await fetch(adapter, may_transcribe=lambda: False)

    (record,) = [r for r in caplog.records if r.getMessage() == "transcript.deferred"]
    assert record.__dict__["video_id"] == VIDEO_ID
    assert record.__dict__["reason"] == "cap_reached"


# --- default audio and Whisper seams ----------------------------------------------------------


class AudioYoutubeDL(RecordingYoutubeDL):
    """Writes the file(s) named in ``written`` where ``outtmpl`` says."""

    written: ClassVar[list[str]] = ["audio.webm"]

    def download(self, urls: list[str]) -> int:
        self.urls += urls
        folder = Path(str(self.params["outtmpl"])).parent
        for name in self.written:
            (folder / name).write_bytes(b"audio")
        return 0


@pytest.fixture
def audio_ydl(monkeypatch: pytest.MonkeyPatch) -> type[AudioYoutubeDL]:
    import yt_dlp

    AudioYoutubeDL.instances = []
    AudioYoutubeDL.written = ["audio.webm"]
    monkeypatch.setattr(yt_dlp, "YoutubeDL", AudioYoutubeDL)
    return AudioYoutubeDL


def test_default_audio_seam_downloads_audio_only_into_the_temp_dir(
    audio_ydl: type[AudioYoutubeDL], tmp_path: Path
) -> None:
    audio_ydl.written = ["audio.webm", "audio.webm.part"]

    path = _yt_dlp_audio(URL, {"quiet": True}, tmp_path)

    assert path == tmp_path / "audio.webm"
    (ydl,) = audio_ydl.instances
    assert ydl.urls == [URL]
    assert ydl.params["format"] == "bestaudio"
    assert "postprocessors" not in ydl.params
    assert str(ydl.params["outtmpl"]).startswith(str(tmp_path))


def test_default_audio_seam_without_a_file_is_no_audio_format(
    audio_ydl: type[AudioYoutubeDL], tmp_path: Path
) -> None:
    audio_ydl.written = []

    with pytest.raises(TranscriptUnavailable) as caught:
        _yt_dlp_audio(URL, {}, tmp_path)

    assert caught.value.reason == "no_audio_format"


def test_audio_option_names_are_known_to_yt_dlp(
    audio_ydl: type[AudioYoutubeDL], tmp_path: Path
) -> None:
    import importlib

    from invio.sources import ytdlp

    base = ytdlp.base_options(timeout=5.0, cookies_file=None, proxy=PROXY)
    _yt_dlp_audio(URL, base, tmp_path)
    (ydl,) = audio_ydl.instances
    doc = importlib.import_module("yt_dlp.YoutubeDL").YoutubeDL.__doc__ or ""
    documented = set(re.findall(r"^[ \t]*([a-z_]+):[ \t]", doc, re.MULTILINE))
    downloader = re.search(r"used by\s+the downloader[^:]*:\s*([a-z_,\s]+)\.", doc)
    assert downloader is not None
    documented |= {name.strip() for name in downloader.group(1).split(",")}

    assert {"format", "outtmpl"} <= documented
    assert sorted(set(ydl.params) - documented - {"ignoreconfig"}) == []


class FakeWhisperModel:
    """Stands in for ``faster_whisper.WhisperModel``."""

    instances: ClassVar[list["FakeWhisperModel"]] = []
    duration = 120.0
    language = "de"

    def __init__(self, size: str, *, device: str, compute_type: str) -> None:
        self.args = (size, device, compute_type)
        self.consumed = False
        type(self).instances.append(self)

    def transcribe(
        self, audio: str, *, language: str | None, vad_filter: bool
    ) -> tuple[Iterator[SimpleNamespace], SimpleNamespace]:
        self.call = (audio, language, vad_filter)

        def segments() -> Iterator[SimpleNamespace]:
            self.consumed = True
            yield SimpleNamespace(text=" Hallo ")
            yield SimpleNamespace(text="   ")
            yield SimpleNamespace(text="Welt\n")

        info = SimpleNamespace(duration=type(self).duration, language=type(self).language)
        return segments(), info


@pytest.fixture
def fake_faster_whisper(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeWhisperModel]]:
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    FakeWhisperModel.instances = []
    FakeWhisperModel.duration = 120.0
    _model.cache_clear()
    yield FakeWhisperModel
    _model.cache_clear()


def test_default_transcriber_returns_joined_text_and_language(
    fake_faster_whisper: type[FakeWhisperModel], tmp_path: Path
) -> None:
    audio = tmp_path / "audio.webm"

    text, language = _whisper_transcribe(audio, "small", 5400)

    assert (text, language) == ("Hallo Welt", "de")
    (model,) = fake_faster_whisper.instances
    assert model.args == ("small", "cpu", "int8")
    assert model.call == (str(audio), None, True)


def test_default_transcriber_loads_each_model_size_once(
    fake_faster_whisper: type[FakeWhisperModel], tmp_path: Path
) -> None:
    _whisper_transcribe(tmp_path / "a", "small", 5400)
    _whisper_transcribe(tmp_path / "b", "small", 5400)
    _whisper_transcribe(tmp_path / "c", "tiny", 5400)

    assert [m.args[0] for m in fake_faster_whisper.instances] == ["small", "tiny"]


def test_default_transcriber_holds_the_model_lock_while_decoding(
    fake_faster_whisper: type[FakeWhisperModel],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the cached model is shared by every adapter in the process: its use must be serialised
    held: list[bool] = []
    original = FakeWhisperModel.transcribe

    def transcribe(
        self: FakeWhisperModel, audio: str, *, language: str | None, vad_filter: bool
    ) -> tuple[Iterator[SimpleNamespace], SimpleNamespace]:
        segments, info = original(self, audio, language=language, vad_filter=vad_filter)

        def checked() -> Iterator[SimpleNamespace]:
            for segment in segments:
                held.append(transcript_module._MODEL_LOCK.locked())
                yield segment

        return checked(), info

    monkeypatch.setattr(FakeWhisperModel, "transcribe", transcribe)

    _whisper_transcribe(tmp_path / "a", "small", 5400)

    assert held and all(held)
    assert not transcript_module._MODEL_LOCK.locked()


def test_default_transcriber_stops_before_the_segments_when_the_audio_is_too_long(
    fake_faster_whisper: type[FakeWhisperModel], tmp_path: Path
) -> None:
    fake_faster_whisper.duration = 5401.0

    with pytest.raises(AudioTooLong) as caught:
        _whisper_transcribe(tmp_path / "a", "small", 5400)

    assert caught.value.seconds == 5401.0
    assert fake_faster_whisper.instances[0].consumed is False


def test_default_transcriber_without_the_extra_raises_import_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    _model.cache_clear()

    with pytest.raises(ImportError):
        _whisper_transcribe(tmp_path / "a", "small", 5400)


def test_a_missing_module_spec_means_the_extra_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    assert _whisper_available() is False


def test_availability_follows_the_module_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())

    assert _whisper_available() is True


# --- bounded and optional (US3) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("minutes", "limit", "detail"),
    [(120, 90, "120 min > limit 90 min"), (45, 30, "45 min > limit 30 min")],
)
async def test_a_video_over_the_limit_is_too_long_without_a_download(
    minutes: int, limit: int, detail: str
) -> None:
    audio, asked = FakeAudio(), []
    adapter = make(
        extract=FakeExtract(info(duration=minutes * 60)), audio=audio, max_duration_minutes=limit
    )

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter, may_transcribe=lambda: asked.append(1) or True)

    assert caught.value.reason == "too_long"
    assert str(caught.value) == f"too_long: {URL} ({detail})"
    assert not is_transient_fetch(caught.value)
    assert caught.value.description == "About X"
    assert audio.calls == [] and asked == []


async def test_minutes_are_rounded_up() -> None:
    adapter = make(extract=FakeExtract(info(duration=90 * 60 + 1)))

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert str(caught.value).endswith("(91 min > limit 90 min)")


async def test_a_long_video_with_subtitles_returns_the_subtitles() -> None:
    adapter = make(
        extract=FakeExtract(info(duration=120 * 60, subtitles={"de": formats()})),
        subtitles=FakeSubtitles({"de": vtt("Hallo")}),
    )

    result = await fetch(adapter)

    assert (result.text, result.method) == ("Hallo", "subtitles")


async def test_a_missing_extra_is_reported_with_the_install_hint_and_no_download() -> None:
    audio, asked = FakeAudio(), []
    adapter = make(extract=FakeExtract(info(duration=600)), audio=audio, whisper=False)

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter, may_transcribe=lambda: asked.append(1) or True)

    assert caught.value.reason == "whisper_not_installed"
    assert str(caught.value) == (
        f"whisper_not_installed: {URL} (install it with: uv sync --extra whisper)"
    )
    assert not is_transient_fetch(caught.value)
    assert caught.value.description == "About X"
    assert audio.calls == [] and asked == []  # no slot is spent on a video that cannot run


async def test_a_missing_extra_does_not_affect_subtitles() -> None:
    adapter = make(
        extract=FakeExtract(info(subtitles={"de": formats()})),
        subtitles=FakeSubtitles({"de": vtt("Hallo")}),
        whisper=False,
    )

    assert (await fetch(adapter)).method == "subtitles"


async def test_the_real_availability_check_reports_a_missing_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    backends = TranscriptBackends(extract=FakeExtract(info(duration=600)))
    adapter = YoutubeTranscripts(backends=backends)

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "whisper_not_installed"


async def test_a_disabled_fallback_is_a_permanent_reason_without_any_slot_or_download() -> None:
    audio, asked = FakeAudio(), []
    adapter = make(
        extract=FakeExtract(info(duration=600)), audio=audio, transcription_enabled=False
    )

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter, may_transcribe=lambda: asked.append(1) or True)

    assert caught.value.reason == "transcription_disabled"
    assert not is_transient_fetch(caught.value)
    assert caught.value.description == "About X"
    assert audio.calls == [] and asked == []


async def test_a_disabled_fallback_still_returns_subtitles() -> None:
    adapter = make(
        extract=FakeExtract(info(subtitles={"de": formats()})),
        subtitles=FakeSubtitles({"de": vtt("Hallo")}),
        transcription_enabled=False,
    )

    assert (await fetch(adapter)).method == "subtitles"


# --- construction from settings ---------------------------------------------------------------


def settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


async def test_from_settings_reads_the_youtube_and_whisper_settings(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    extract = FakeExtract(info(duration=600))
    audio, transcriber = FakeAudio(), FakeTranscriber()
    adapter = YoutubeTranscripts.from_settings(
        settings(
            youtube_cookies_file=cookies,
            youtube_proxy=PROXY,
            youtube_timeout_seconds=7.0,
            whisper_model_size="tiny",
            whisper_max_duration_minutes=5,
        ),
        clock=FakeClock(NOW),
    )
    adapter._extract = extract
    adapter._download_audio = audio
    adapter._transcribe = transcriber
    adapter._whisper_available = lambda: True

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)  # 10 min video, 5 min limit

    assert caught.value.reason == "too_long"
    adapter._extract = FakeExtract(info(duration=60))
    await fetch(adapter)
    options = adapter._options()
    assert options["proxy"] == PROXY
    assert options["cookiefile"] == str(cookies)
    assert options["socket_timeout"] == 7.0
    assert transcriber.calls[0][1:] == ("tiny", 300)
    adapter.close()


async def test_a_per_run_cap_of_zero_switches_the_fallback_off() -> None:
    adapter = YoutubeTranscripts.from_settings(settings(whisper_max_per_run=0))
    adapter._extract = FakeExtract(info(duration=60))

    with pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    assert caught.value.reason == "transcription_disabled"
    adapter.close()
