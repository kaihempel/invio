"""Fakes and builders for the transcript adapter tests (no network, no Whisper)."""

import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from invio.sources.transcript import (
    SubtitleTrack,
    Transcript,
    TranscriptBackends,
    YoutubeTranscripts,
)
from invio.sources.ytdlp import watch_url
from tests.conftest import FakeClock
from tests.youtube_helpers import FakeExtractor

VIDEO_ID: Final = "abc123XYZ_-"
URL: Final = watch_url(VIDEO_ID)
PROXY: Final = "http://user:secret@proxy.invalid:8080"
RAW_MESSAGE: Final = "ERROR: [youtube] sensitive-library-text"
NOW: Final = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


def vtt(*cues: str) -> str:
    """A minimal VTT document with one cue per argument."""
    body = "".join(
        f"\n00:00:{i:02d}.000 --> 00:00:{i + 1:02d}.000\n{cue}\n" for i, cue in enumerate(cues)
    )
    return "WEBVTT\n" + body


def formats(url: str = "https://subs.invalid/x.vtt") -> list[dict[str, str]]:
    """The format list yt-dlp offers for one language: a VTT entry and a json3 one."""
    return [{"ext": "json3", "url": url + ".json3"}, {"ext": "vtt", "url": url}]


def info(
    *,
    duration: object = 300,
    subtitles: Mapping[str, object] | None = None,
    automatic: Mapping[str, object] | None = None,
    language: object = None,
    description: object = "About X",
) -> dict[str, object]:
    """A yt-dlp info mapping as ``extract_info(download=False)`` returns it."""
    return {
        "id": VIDEO_ID,
        "duration": duration,
        "subtitles": dict(subtitles or {}),
        "automatic_captions": dict(automatic or {}),
        "language": language,
        "description": description,
    }


class FakeExtract(FakeExtractor):
    """:class:`FakeExtractor` whose default result is a video's :func:`info`."""

    def __init__(
        self,
        result: Mapping[str, object] | None = None,
        *,
        exc: BaseException | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        super().__init__(info() if result is None else result, exc=exc, gate=gate)


class FakeSubtitles:
    """Returns the VTT registered for a track key (``""`` when none); records every call."""

    def __init__(
        self, texts: Mapping[str, str] | None = None, *, exc: BaseException | None = None
    ) -> None:
        self.texts = dict(texts or {})
        self.exc = exc
        self.calls: list[tuple[str, str, bool]] = []
        self.options: list[Mapping[str, object]] = []
        self.tmp_dirs: list[Path] = []
        self.threads: list[str] = []

    @property
    def keys(self) -> list[str]:
        return [key for _, key, _ in self.calls]

    def __call__(
        self, url: str, track: SubtitleTrack, options: Mapping[str, object], tmp: Path
    ) -> str:
        assert tmp.is_dir()
        self.calls.append((url, track.key, track.automatic))
        self.options.append(options)
        self.tmp_dirs.append(tmp)
        self.threads.append(threading.current_thread().name)
        if self.exc is not None:
            raise self.exc
        return self.texts.get(track.key, "")


class FakeAudio:
    """Writes a small file into ``tmp`` and returns it; records the temp dirs it saw."""

    def __init__(
        self,
        *,
        exc: BaseException | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self.exc = exc
        self.on_call = on_call
        self.calls: list[tuple[str, Mapping[str, object], Path]] = []
        self.thread_names: list[str] = []

    @property
    def tmp_dirs(self) -> list[Path]:
        return [tmp for _, _, tmp in self.calls]

    def __call__(self, url: str, options: Mapping[str, object], tmp: Path) -> Path:
        assert tmp.is_dir()
        self.calls.append((url, options, tmp))
        self.thread_names.append(threading.current_thread().name)
        if self.on_call is not None:
            self.on_call()
        if self.exc is not None:
            raise self.exc
        path = tmp / "audio.webm"
        path.write_bytes(b"audio")
        return path


class FakeTranscriber:
    """Records calls and the highest number of concurrently active calls."""

    def __init__(
        self,
        text: str = "spoken words",
        language: str = "en",
        *,
        exc: BaseException | None = None,
        delay: float = 0.0,
        gate: threading.Event | None = None,
        entered: threading.Event | None = None,
    ) -> None:
        self.text = text
        self.language = language
        self.exc = exc
        self.delay = delay
        self.gate = gate
        self.entered = entered
        self.calls: list[tuple[Path, str, int]] = []
        self.audio_existed: list[bool] = []
        self.max_active = 0
        self._active = 0
        self._lock = threading.Lock()

    def __call__(self, path: Path, model_size: str, max_seconds: int) -> tuple[str, str]:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.calls.append((path, model_size, max_seconds))
            self.audio_existed.append(path.exists())
        try:
            if self.entered is not None:
                self.entered.set()
            if self.gate is not None:
                self.gate.wait(10)
            if self.delay:
                time.sleep(self.delay)
            if self.exc is not None:
                raise self.exc
            return self.text, self.language
        finally:
            with self._lock:
                self._active -= 1


def make(
    *,
    extract: Callable[[str, Mapping[str, object]], Mapping[str, object]] | None = None,
    subtitles: FakeSubtitles | None = None,
    audio: FakeAudio | None = None,
    transcriber: FakeTranscriber | None = None,
    whisper: bool | Callable[[], bool] = True,
    clock: Callable[[], datetime] | None = None,
    **kwargs: object,
) -> YoutubeTranscripts:
    """An adapter whose seams are fakes; ``kwargs`` go to the constructor."""
    backends = TranscriptBackends(
        extract=extract if extract is not None else FakeExtract(),
        subtitle=subtitles if subtitles is not None else FakeSubtitles(),
        audio=audio if audio is not None else FakeAudio(),
        transcribe=transcriber if transcriber is not None else FakeTranscriber(),
        whisper_available=whisper if callable(whisper) else (lambda: whisper),
    )
    return YoutubeTranscripts(backends=backends, clock=clock or FakeClock(NOW), **kwargs)  # type: ignore[arg-type]


async def fetch(
    adapter: YoutubeTranscripts,
    url: str = URL,
    *,
    language: str = "de",
    may_transcribe: Callable[[], bool] = lambda: True,
    deadline: datetime | None = None,
) -> Transcript:
    return await adapter.get_transcript(
        url, language=language, may_transcribe=may_transcribe, deadline=deadline
    )
