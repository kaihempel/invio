"""``YoutubeTranscripts``: track bound, secret redaction per seam, temp-dir cleanup (offline).

Complements ``test_source_transcript.py``: D4 (at most four tracks, no machine translations as
"other automatic") seen through the adapter, FR-015 for every seam that can fail, FR-012 for
the audio download, and FR-008 / SC-004 for cancellation and shutdown.
"""

import asyncio
import logging
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from invio.sources.errors import TranscriptUnavailable, is_transient_fetch
from invio.sources.transcript import YoutubeTranscripts
from tests.transcript_helpers import (
    PROXY,
    RAW_MESSAGE,
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


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``tempfile`` at a private directory so leftovers can be listed."""
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    yield root


def leftovers(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir())


async def drain(adapter: YoutubeTranscripts) -> None:
    """Wait until every worker thread of ``adapter`` has finished its job."""
    await asyncio.to_thread(adapter._executor.shutdown, True)
    await asyncio.to_thread(adapter._whisper_pool.shutdown, True)


# --- D4: the track bound and machine translations, through the adapter ------------------------


async def test_at_most_four_empty_tracks_are_downloaded_before_the_fallback() -> None:
    manual = {key: formats() for key in ("aa", "bb", "cc", "dd", "ee", "ff")}
    # the fifth track would have text, but it is never requested
    subtitles = FakeSubtitles({"ee": vtt("never read")})
    audio = FakeAudio()
    adapter = make(extract=FakeExtract(info(subtitles=manual)), subtitles=subtitles, audio=audio)

    result = await fetch(adapter)

    assert len(subtitles.calls) == 4
    assert "ee" not in subtitles.keys and "ff" not in subtitles.keys
    assert result.method == "transcription"
    assert len(audio.calls) == 1


async def test_the_fourth_track_still_counts() -> None:
    manual = {key: formats() for key in ("aa", "bb", "cc", "dd")}
    subtitles = FakeSubtitles({"dd": vtt("fourth")})
    audio = FakeAudio()
    adapter = make(extract=FakeExtract(info(subtitles=manual)), subtitles=subtitles, audio=audio)

    result = await fetch(adapter)

    assert (result.text, result.language, result.method) == ("fourth", "dd", "subtitles")
    assert audio.calls == []


async def test_machine_translations_alone_are_not_subtitles() -> None:
    # no video language reported and no ``-orig`` track: every automatic track is a translation
    subtitles = FakeSubtitles({"fr": vtt("traduit"), "ja": vtt("honyaku")})
    audio = FakeAudio()
    adapter = make(
        extract=FakeExtract(info(automatic={"fr": formats(), "ja": formats()}, language=None)),
        subtitles=subtitles,
        audio=audio,
    )

    result = await fetch(adapter, language="de")

    assert subtitles.calls == []
    assert result.method == "transcription"
    assert len(audio.calls) == 1


async def test_the_original_recognition_in_another_language_beats_the_audio() -> None:
    subtitles = FakeSubtitles({"fr-orig": vtt("original"), "ja": vtt("honyaku")})
    audio = FakeAudio()
    adapter = make(
        extract=FakeExtract(info(automatic={"fr-orig": formats(), "ja": formats()})),
        subtitles=subtitles,
        audio=audio,
    )

    result = await fetch(adapter, language="de")

    assert subtitles.keys == ["fr-orig"]
    assert (result.text, result.language, result.method) == ("original", "fr", "subtitles")
    assert audio.calls == []


async def test_a_translation_into_the_job_language_is_used() -> None:
    subtitles = FakeSubtitles({"de": vtt("übersetzt")})
    adapter = make(
        extract=FakeExtract(info(automatic={"fr-orig": formats(), "de": formats()})),
        subtitles=subtitles,
    )

    result = await fetch(adapter, language="de")

    # the original recognition is tried first, the translation is the last subtitle tier
    assert subtitles.keys == ["fr-orig", "de"]
    assert (result.text, result.language) == ("übersetzt", "de")


# --- FR-012: the audio download uses the YouTube access settings ------------------------------


async def test_the_audio_download_uses_cookies_proxy_and_timeout(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    audio = FakeAudio()
    adapter = make(
        extract=FakeExtract(info(duration=600)),
        audio=audio,
        proxy=PROXY,
        cookies_file=cookies,
        timeout=9.0,
    )

    await fetch(adapter)

    ((_, options, _),) = audio.calls
    assert options["proxy"] == PROXY
    assert options["cookiefile"] == str(cookies)
    assert options["socket_timeout"] == 9.0


# --- FR-015: no secret from any seam reaches an error or a log line ---------------------------


def _leaky(exc_type: type[Exception], cookies: Path) -> Exception:
    return exc_type(f"{RAW_MESSAGE} via {PROXY} with {cookies}")


@pytest.mark.parametrize("seam", ["extract", "subtitle", "audio", "transcriber"])
@pytest.mark.parametrize("exc_type", [RuntimeError, TimeoutError, OSError, ValueError])
async def test_no_secret_from_any_seam_reaches_the_error_or_the_logs(
    seam: str,
    exc_type: type[Exception],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cookies = tmp_path / "secret-cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    leak = _leaky(exc_type, cookies)
    with_subtitles = seam == "subtitle"
    extract = FakeExtract(
        info(duration=600, subtitles={"de": formats()} if with_subtitles else None),
        exc=leak if seam == "extract" else None,
    )
    adapter = make(
        extract=extract,
        subtitles=FakeSubtitles(exc=leak if seam == "subtitle" else None),
        audio=FakeAudio(exc=leak if seam == "audio" else None),
        transcriber=FakeTranscriber(exc=leak if seam == "transcriber" else None),
        proxy=PROXY,
        cookies_file=cookies,
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(TranscriptUnavailable) as caught:
        await fetch(adapter)

    err = caught.value
    shown = f"{err!s} {err!r} {err.args} {err.url} {err.detail} {err.description}"
    logged = " ".join(f"{r.getMessage()} {r.__dict__} {r.exc_text}" for r in caplog.records)
    for secret in (PROXY, "secret", "proxy.invalid", str(cookies), "sensitive-library-text"):
        assert secret not in shown
        assert secret not in logged
    assert err.__cause__ is None
    if exc_type is TimeoutError:
        assert err.reason == "timeout" and is_transient_fetch(err)


async def test_a_cookies_problem_never_shows_the_cookie_path_in_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cookies = tmp_path / "secret-cookies.txt"  # missing
    adapter = make(cookies_file=cookies, proxy=PROXY)

    with caplog.at_level(logging.DEBUG), pytest.raises(Exception) as caught:
        await fetch(adapter)

    shown = f"{caught.value!s} {caught.value!r}"
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for secret in (str(cookies), "secret", PROXY):
        assert secret not in shown and secret not in logged


# --- FR-008 / SC-004: temp files after cancellation and shutdown ------------------------------


async def test_cancelling_the_caller_during_the_audio_download_removes_the_audio(
    temp_root: Path,
) -> None:
    started, gate = threading.Event(), threading.Event()

    def block() -> None:
        started.set()
        gate.wait(10)

    audio, transcriber = FakeAudio(on_call=block), FakeTranscriber()
    adapter = make(extract=FakeExtract(info(duration=600)), audio=audio, transcriber=transcriber)

    task = asyncio.create_task(fetch(adapter))
    await asyncio.to_thread(started.wait, 5)
    assert [name for name in leftovers(temp_root) if name.startswith("invio-audio-")]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await drain(adapter)

    assert leftovers(temp_root) == []
    assert transcriber.max_active <= 1


async def test_cancelling_the_caller_during_the_transcription_removes_the_audio(
    temp_root: Path,
) -> None:
    gate, entered = threading.Event(), threading.Event()
    transcriber = FakeTranscriber(gate=gate, entered=entered)
    adapter = make(extract=FakeExtract(info(duration=600)), transcriber=transcriber)

    task = asyncio.create_task(fetch(adapter))
    await asyncio.to_thread(entered.wait, 5)
    assert transcriber.audio_existed == [True]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await drain(adapter)

    assert leftovers(temp_root) == []


async def test_a_queued_job_whose_caller_is_cancelled_creates_no_temp_dir(
    temp_root: Path,
) -> None:
    gate, entered = threading.Event(), threading.Event()
    audio = FakeAudio()
    transcriber = FakeTranscriber(gate=gate, entered=entered)
    adapter = make(extract=FakeExtract(info(duration=600)), audio=audio, transcriber=transcriber)

    first = asyncio.create_task(fetch(adapter))
    await asyncio.to_thread(entered.wait, 5)
    second = asyncio.create_task(fetch(adapter))
    await asyncio.sleep(0.1)  # queued behind the running job
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert len([n for n in leftovers(temp_root) if n.startswith("invio-audio-")]) == 1
    gate.set()

    assert (await asyncio.wait_for(first, 10)).method == "transcription"
    await drain(adapter)
    assert len(audio.calls) == 1
    assert leftovers(temp_root) == []


async def test_close_drops_a_queued_job_and_the_running_one_cleans_up(temp_root: Path) -> None:
    gate, entered = threading.Event(), threading.Event()
    audio = FakeAudio()
    transcriber = FakeTranscriber(gate=gate, entered=entered)
    adapter = make(extract=FakeExtract(info(duration=600)), audio=audio, transcriber=transcriber)

    first = asyncio.create_task(fetch(adapter))
    await asyncio.to_thread(entered.wait, 5)
    second = asyncio.create_task(fetch(adapter))
    await asyncio.sleep(0.1)
    adapter.close()
    gate.set()

    results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 10)
    await drain(adapter)

    assert getattr(results[0], "method", None) == "transcription"
    assert isinstance(results[1], BaseException)  # dropped, never started
    assert len(audio.calls) == 1
    assert leftovers(temp_root) == []


async def test_a_timed_out_subtitle_download_removes_its_temp_dir(temp_root: Path) -> None:
    gate = threading.Event()

    class Stalling(FakeSubtitles):
        def __call__(self, *args: object) -> str:
            gate.wait(10)
            return ""

    adapter = make(
        extract=FakeExtract(info(subtitles={"de": formats()})),
        subtitles=Stalling(),
        timeout=0.05,
    )

    try:
        with pytest.raises(TranscriptUnavailable) as caught:
            await fetch(adapter)
        assert [n for n in leftovers(temp_root) if n.startswith("invio-sub-")]
    finally:
        gate.set()
    await drain(adapter)

    assert caught.value.reason == "timeout"
    assert leftovers(temp_root) == []
