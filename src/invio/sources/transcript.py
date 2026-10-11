"""Video transcripts: existing subtitles first, optional local transcription as the fallback.

:class:`YoutubeTranscripts` reads a video's metadata once with yt-dlp, picks the best subtitle
track (job language, then English, then any; manual before automatic), downloads it as WebVTT
and converts it to plain text (:func:`vtt_to_text`). Audio is never downloaded while any track
yields text.

Only when no track does, the adapter may transcribe the audio locally with ``faster-whisper``
(the optional ``whisper`` extra). That fallback is bounded and optional: a video longer than the
configured limit, a live or upcoming stream, a missing extra, a full per-run cap
(``may_transcribe``) or a run that is out of time are reported without any download. Every
transcription (download plus recognition) runs on the adapter's single-worker pool, so at most
one runs per adapter; others wait. The audio download is capped in size as well (``max_filesize``
derived from the duration limit). Temporary audio lives in a directory that the worker thread
removes on success, error and interruption.

``yt_dlp`` and ``faster_whisper`` are imported lazily, inside the default seam functions only:
importing this module works without the extra. Failures are
:class:`~invio.sources.errors.TranscriptUnavailable` (a ``FetchError``: permanent or transient)
or :class:`~invio.sources.errors.TranscriptionDeferred`; neither the library's message text, the
proxy URL nor the cookie path is ever copied into an error or a log line.
"""

import asyncio
import html
import importlib.util
import logging
import math
import re
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache, partial
from pathlib import Path
from typing import Final, Literal, Protocol, Self, cast

from invio.config.settings import Settings, WhisperModelSize
from invio.sources import ytdlp
from invio.sources.errors import FetchError, TranscriptionDeferred, TranscriptUnavailable
from invio.sources.ytdlp import Extractor

__all__ = [
    "DEADLINE_RESERVE",
    "AudioTooLong",
    "SubtitleTrack",
    "Transcript",
    "TranscriptBackends",
    "VideoMetadata",
    "YoutubeTranscripts",
    "vtt_to_text",
]

type SubtitleDownloader = Callable[[str, "SubtitleTrack", Mapping[str, object], Path], str]
"""``(video_url, track, options, tmp_dir) -> VTT text`` (``""`` if nothing was written)."""

type AudioDownloader = Callable[[str, Mapping[str, object], Path], Path]
"""``(video_url, options, tmp_dir) -> path of the downloaded audio file``."""

type Transcriber = Callable[[Path, str, int], tuple[str, str]]
"""``(audio_path, model_size, max_seconds) -> (text, detected_language)``."""

DEADLINE_RESERVE: Final = timedelta(minutes=10)
"""Time kept free before the run's deadline for the stages after the items."""

_log = logging.getLogger("invio.sources.transcript")

_MAX_TRACKS: Final = 4
_TAG: Final = re.compile(r"<[^>]*>")
_NON_CUE_BLOCKS: Final = frozenset({"NOTE", "STYLE", "REGION"})
_UNAVAILABLE_MARKERS: Final = (
    "confirm your age",
    "age-restricted",
    "not available in your country",
    "video available in your country",  # "has not made this video available in your country"
    "members-only",
    "video unavailable",
    "this video is private",
)
_NO_AUDIO_MARKER: Final = "requested format is not available"
# yt-dlp ``live_status`` values of a stream whose audio has no end (yet) or is still processed.
_LIVE_STATUSES: Final = frozenset({"is_live", "is_upcoming", "post_live"})
# Upper bound of the audio bit rate (384 kbit/s, above YouTube's best audio-only formats): the
# duration limit times this rate caps the download size.
_MAX_AUDIO_BYTES_PER_SECOND: Final = 48_000
# Serialises every use of the process-wide cached Whisper model (``_model``) across adapters.
_MODEL_LOCK: Final = threading.Lock()


class AudioTooLong(Exception):
    """Raised by a transcriber when the audio is longer than the allowed ``max_seconds``."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"audio of {seconds:.0f} s is too long")
        self.seconds = seconds


@dataclass(frozen=True, slots=True)
class Transcript:
    """The spoken content of a video as plain text."""

    text: str
    language: str
    method: Literal["subtitles", "transcription"]

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("a transcript needs text")
        if not self.language.strip():
            raise ValueError("a transcript needs a language")


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    """One subtitle track yt-dlp offers (internal)."""

    key: str
    language: str
    automatic: bool
    original: bool


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    """What the single metadata extraction provides (internal)."""

    video_id: str
    duration_seconds: int | None
    tracks: list[SubtitleTrack]
    description: str | None
    live: bool = False  # a live, upcoming or still-processing stream


@dataclass(frozen=True, slots=True)
class TranscriptBackends:
    """The blocking functions behind the adapter; ``None`` selects the real implementation.

    They exist for tests. The defaults are resolved when the adapter is built, so a test that
    replaces the module-level defaults is honoured.
    """

    extract: Extractor | None = None
    subtitle: SubtitleDownloader | None = None
    audio: AudioDownloader | None = None
    transcribe: Transcriber | None = None
    whisper_available: Callable[[], bool] | None = None


# --- VTT to text -----------------------------------------------------------------------------


def vtt_to_text(vtt: str) -> str:
    """Plain text of a WebVTT document.

    Drops the header, ``NOTE``/``STYLE``/``REGION`` blocks, cue identifiers and timings; strips
    inline tags and timestamps; decodes entities; collapses whitespace per line and removes a
    line equal to the previously kept one (YouTube's rolling captions repeat lines). The lines
    are joined with single spaces.
    """
    kept: list[str] = []
    for block in _blocks(vtt.lstrip("\ufeff")):
        if block[0].split(None, 1)[0] in _NON_CUE_BLOCKS:
            continue
        timing = next((i for i, line in enumerate(block) if "-->" in line), None)
        if timing is None:
            continue
        for line in block[timing + 1 :]:
            text = " ".join(html.unescape(_TAG.sub("", line)).split())
            if text and (not kept or kept[-1] != text):
                kept.append(text)
    return " ".join(kept)


def _blocks(vtt: str) -> Iterator[list[str]]:
    """Non-empty blocks of lines, separated by blank lines."""
    block: list[str] = []
    for line in vtt.splitlines():
        if line.strip():
            block.append(line)
        elif block:
            yield block
            block = []
    if block:
        yield block


# --- metadata and track selection ------------------------------------------------------------


def _primary(language: str) -> str:
    """The primary subtag, lower case: ``de-DE`` and ``de_orig`` both give ``de``."""
    return re.split(r"[-_]", language.strip(), maxsplit=1)[0].lower()


def _metadata(info: Mapping[str, object], video_id: str) -> VideoMetadata:
    """Build :class:`VideoMetadata` defensively: values of the wrong type count as missing."""
    raw_language = info.get("language")
    video_language = _primary(raw_language) if isinstance(raw_language, str) else None
    tracks = _tracks(info.get("subtitles"), automatic=False, video_language=video_language)
    tracks += _tracks(info.get("automatic_captions"), automatic=True, video_language=video_language)
    description = info.get("description")
    return VideoMetadata(
        video_id=video_id,
        duration_seconds=_duration(info.get("duration")),
        tracks=tracks,
        description=description if isinstance(description, str) and description.strip() else None,
        live=info.get("is_live") is True or info.get("live_status") in _LIVE_STATUSES,
    )


def _duration(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return math.ceil(value)


def _tracks(mapping: object, *, automatic: bool, video_language: str | None) -> list[SubtitleTrack]:
    """Tracks that offer a VTT format."""
    if not isinstance(mapping, Mapping):
        return []
    tracks: list[SubtitleTrack] = []
    for key, formats in mapping.items():
        if not isinstance(key, str) or not _primary(key) or not isinstance(formats, list):
            continue
        if not any(isinstance(f, Mapping) and f.get("ext") == "vtt" for f in formats):
            continue
        language = _primary(key)
        original = automatic and (key.lower().endswith("-orig") or language == video_language)
        tracks.append(SubtitleTrack(key, language, automatic, original))
    return tracks


def _ordered_tracks(tracks: list[SubtitleTrack], job_language: str) -> list[SubtitleTrack]:
    """The tracks to try, best first (at most ``_MAX_TRACKS``).

    Manual in the job language, the video's own (original) automatic captions in the job
    language, manual English, original automatic English, any other manual, any other original
    automatic track, and only then YouTube's machine translations into the job language: the
    last subtitle tier before the audio fallback. Other machine translations are never used.
    ``-orig`` tracks come first within the automatic tiers.
    """
    job = _primary(job_language)
    manual = [t for t in tracks if not t.automatic]
    automatic = sorted(
        (t for t in tracks if t.automatic),
        key=lambda t: 0 if t.key.lower().endswith("-orig") else 1 if t.original else 2,
    )
    others = {job, "en"}
    tiers = [
        [t for t in manual if t.language == job],
        [t for t in automatic if t.original and t.language == job],
        [t for t in manual if t.language == "en"],
        [t for t in automatic if t.original and t.language == "en"],
        [t for t in manual if t.language not in others],
        [t for t in automatic if t.original and t.language not in others],
        [t for t in automatic if not t.original and t.language == job],
    ]
    seen: set[tuple[str, bool]] = set()
    ordered: list[SubtitleTrack] = []
    for track in (t for tier in tiers for t in tier):
        if (track.key, track.automatic) not in seen:
            seen.add((track.key, track.automatic))
            ordered.append(track)
    return ordered[:_MAX_TRACKS]


# --- the adapter -----------------------------------------------------------------------------


class YoutubeTranscripts:
    """Transcripts for YouTube videos: subtitles first.

    ``proxy`` is the plain proxy URL (the caller unwraps the secret). ``backends`` replaces the
    blocking yt-dlp calls and ``clock`` returns the current aware time; both exist for tests.
    Call :meth:`close` when done to shut the worker pool down.
    """

    def __init__(
        self,
        *,
        cookies_file: Path | None = None,
        proxy: str | None = None,
        timeout: float = 60.0,
        model_size: WhisperModelSize = "small",
        max_duration_minutes: int = 90,
        transcription_enabled: bool = True,
        backends: TranscriptBackends | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        backends = backends if backends is not None else TranscriptBackends()
        self._cookies_file = cookies_file
        self._proxy = proxy
        self._timeout = timeout
        self._model_size = model_size
        self._max_duration_minutes = max_duration_minutes
        self._transcription_enabled = transcription_enabled
        self._extract: Extractor = backends.extract or _yt_dlp_extract
        self._download_subtitle: SubtitleDownloader = backends.subtitle or _yt_dlp_subtitle
        self._download_audio: AudioDownloader = backends.audio or _yt_dlp_audio
        self._transcribe: Transcriber = backends.transcribe or _whisper_transcribe
        self._whisper_available: Callable[[], bool] = (
            backends.whisper_available or _whisper_available
        )
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        # threads are started lazily, on the first call
        self._executor = ThreadPoolExecutor(
            ytdlp.MAX_WORKERS, thread_name_prefix="invio-transcript"
        )
        # One worker: at most one download plus transcription runs per adapter, others queue.
        self._whisper_pool = ThreadPoolExecutor(1, thread_name_prefix="invio-whisper")
        self._closed = False
        # the missing extra is a warning once, then INFO: it is the default deployment
        self._missing_extra_reported = False

    @classmethod
    def from_settings(
        cls, settings: Settings, *, clock: Callable[[], datetime] | None = None
    ) -> Self:
        """The adapter configured from ``settings`` (blank cookies/proxy are already ``None``)."""
        proxy = settings.youtube_proxy
        return cls(
            cookies_file=settings.youtube_cookies_file,
            proxy=proxy.get_secret_value() if proxy is not None else None,
            timeout=settings.youtube_timeout_seconds,
            model_size=settings.whisper_model_size,
            max_duration_minutes=settings.whisper_max_duration_minutes,
            transcription_enabled=settings.whisper_max_per_run > 0,
            clock=clock,
        )

    def close(self) -> None:
        """Shut the worker pool down without waiting for (possibly hung) calls."""
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._whisper_pool.shutdown(wait=False, cancel_futures=True)

    async def get_transcript(
        self,
        video_url: str,
        /,
        *,
        language: str,
        may_transcribe: Callable[[], bool],
        deadline: datetime | None = None,
    ) -> Transcript:
        """The transcript of the video at ``video_url``; subtitles in the best language.

        ``language`` is the job's language. Raises :class:`TranscriptUnavailable` (see its
        reasons; permanence follows ``is_transient_fetch``), :class:`FetchError`
        (``cookies_unavailable``) and :class:`RuntimeError` after :meth:`close`.
        """
        if self._closed:
            raise RuntimeError("YoutubeTranscripts is closed")
        video_id = ytdlp.parse_video_url(video_url)
        if video_id is None:
            raise TranscriptUnavailable("unsupported_url", url=video_url)
        url = ytdlp.watch_url(video_id)
        ytdlp.check_cookies(self._cookies_file, url)
        try:
            return await self._retrieve(url, video_id, language, may_transcribe, deadline)
        except TranscriptUnavailable as err:
            # a missing extra is an operator problem (said once), the others are facts about a video
            level = logging.INFO
            if err.reason == "whisper_not_installed" and not self._missing_extra_reported:
                self._missing_extra_reported = True
                level = logging.WARNING
            _log.log(
                level, "transcript.unavailable", extra={"video_id": video_id, "reason": err.reason}
            )
            raise
        except TranscriptionDeferred as err:
            _log.info("transcript.deferred", extra={"video_id": video_id, "reason": err.reason})
            raise

    async def _retrieve(
        self,
        url: str,
        video_id: str,
        language: str,
        may_transcribe: Callable[[], bool],
        deadline: datetime | None,
    ) -> Transcript:
        description: str | None = None
        try:
            options = self._options()
            info = await self._in_pool(partial(self._extract, url, options))
            meta = _metadata(info, video_id)
            description = meta.description
            for track in _ordered_tracks(meta.tracks, language):
                vtt = await self._in_pool(partial(self._subtitle_job, url, track, options))
                text = vtt_to_text(vtt)
                if text:
                    _log.info(
                        "transcript.subtitles",
                        extra={
                            "video_id": video_id,
                            "language": track.language,
                            "automatic": track.automatic,
                        },
                    )
                    return Transcript(text, track.language, "subtitles")
            return await self._transcribe_locally(url, meta, may_transcribe, deadline, options)
        except TranscriptUnavailable as err:
            if err.description is None and description is not None:
                err.description = description  # e.g. no_audio_format from the audio seam
            raise
        except (FetchError, TranscriptionDeferred):
            raise
        except AudioTooLong as exc:
            raise self._too_long(url, exc.seconds, description) from None
        except ModuleNotFoundError as exc:
            if (exc.name or "").split(".")[0] != "faster_whisper":
                # a dependency of the extra is missing: a broken install, not an absent extra
                raise self._failed(exc, url, video_id, description) from None
            raise TranscriptUnavailable(
                "whisper_not_installed", url=url, description=description
            ) from None
        except Exception as exc:
            raise self._failed(exc, url, video_id, description) from None

    @staticmethod
    def _failed(
        exc: Exception, url: str, video_id: str, description: str | None
    ) -> TranscriptUnavailable:
        """Log the class of an unexpected failure and map it to a reason code."""
        _log.warning("transcript.failed", extra={"video_id": video_id, "error": type(exc).__name__})
        return _unavailable(exc, url, description)

    async def _transcribe_locally(
        self,
        url: str,
        meta: VideoMetadata,
        may_transcribe: Callable[[], bool],
        deadline: datetime | None,
        options: Mapping[str, object],
    ) -> Transcript:
        """The fallback: download the audio and transcribe it on the single whisper worker.

        The guards run before any download, in this order: fallback switched off, live or
        upcoming stream (its audio has no end), video longer than the limit, extra not installed,
        no slot left in the run. Waiting for the worker has no timeout: the job is bounded by the
        duration limit and the download size cap, and a thread cannot be stopped.
        """
        description = meta.description
        if not self._transcription_enabled:
            raise TranscriptUnavailable("transcription_disabled", url=url, description=description)
        if meta.live:
            raise TranscriptUnavailable("live_stream", url=url, description=description)
        limit = self._max_duration_minutes * 60
        if meta.duration_seconds is not None and meta.duration_seconds > limit:
            raise self._too_long(url, meta.duration_seconds, description)
        if not self._whisper_available():
            raise TranscriptUnavailable("whisper_not_installed", url=url, description=description)
        if not may_transcribe():
            raise TranscriptionDeferred("cap_reached")
        loop = asyncio.get_running_loop()
        job = partial(self._transcription_job, url, meta, deadline, options)
        text, language = await loop.run_in_executor(self._whisper_pool, job)
        if not text.strip():
            raise TranscriptUnavailable("no_speech", url=url, description=description)
        _log.info(
            "transcript.transcribed",
            extra={
                "video_id": meta.video_id,
                "language": language,
                "seconds": meta.duration_seconds,
            },
        )
        return Transcript(text.strip(), _primary(language) or language, "transcription")

    def _transcription_job(
        self,
        url: str,
        meta: VideoMetadata,
        deadline: datetime | None,
        options: Mapping[str, object],
    ) -> tuple[str, str]:
        """Runs on the whisper worker: the run-time check, then download and transcribe."""
        limit = self._max_duration_minutes * 60
        if deadline is not None:
            needed = timedelta(seconds=meta.duration_seconds or limit) + DEADLINE_RESERVE
            if self._clock() + needed > deadline:
                raise TranscriptionDeferred("not_enough_time")
        capped = {**options, "max_filesize": limit * _MAX_AUDIO_BYTES_PER_SECOND}
        with _temp_dir("invio-audio-") as tmp:
            path = self._download_audio(url, capped, tmp)
            return self._transcribe(path, self._model_size, limit)

    def _too_long(self, url: str, seconds: float, description: str | None) -> TranscriptUnavailable:
        minutes = math.ceil(seconds / 60)
        return TranscriptUnavailable(
            "too_long",
            url=url,
            description=description,
            detail=f"{minutes} min > limit {self._max_duration_minutes} min",
        )

    async def _in_pool[T](self, call: Callable[[], T]) -> T:
        """Run the blocking ``call`` on the metadata pool under the hard timeout."""
        return await ytdlp.run_in_pool(self._executor, call, self._timeout)

    def _options(self) -> dict[str, object]:
        return ytdlp.base_options(
            timeout=self._timeout, cookies_file=self._cookies_file, proxy=self._proxy
        )

    def _subtitle_job(self, url: str, track: SubtitleTrack, options: Mapping[str, object]) -> str:
        with _temp_dir("invio-sub-") as tmp:
            return self._download_subtitle(url, track, options, tmp)


def _unavailable(exc: Exception, url: str, description: str | None) -> TranscriptUnavailable:
    """Map a failure to a :class:`TranscriptUnavailable` using codes only, never its text."""
    status: int | None = None
    if isinstance(exc, TimeoutError):
        reason = "timeout"
    else:
        reason, status = ytdlp.classify_safely(exc)
        if reason == "http_status" and status in (404, 410):
            reason, status = "video_unavailable", None
        elif reason == "invalid_response" or (reason == "http_status" and status == 403):
            text = ytdlp.failure_text(exc)
            if any(marker in text for marker in _UNAVAILABLE_MARKERS):
                reason, status = "video_unavailable", None
            elif reason == "invalid_response" and _NO_AUDIO_MARKER in text:
                reason = "no_audio_format"
            elif status == 403:
                # YouTube also answers 403 for IP blocks, proxy refusals and PO-token problems:
                # worth another try. ``invalid_response`` is the transient reason that keeps
                # the status in the message.
                reason = "invalid_response"
    return TranscriptUnavailable(reason, url=url, status=status, description=description)


@contextmanager
def _temp_dir(prefix: str) -> Iterator[Path]:
    """A private temporary directory, removed on exit; a failed removal is logged, not raised."""
    directory = tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)
    path = Path(directory.name)
    try:
        yield path
    finally:
        directory.cleanup()
        if path.exists():
            _log.warning("transcript.cleanup_failed", extra={"prefix": prefix})


# --- default seams ---------------------------------------------------------------------------

_yt_dlp_extract = ytdlp.extract_info


def _yt_dlp_subtitle(
    url: str, track: SubtitleTrack, options: Mapping[str, object], tmp: Path
) -> str:
    """The default subtitle downloader: the track as VTT text, ``""`` if none was written."""
    params = dict(options)
    params.update(
        {
            "skip_download": True,
            "writesubtitles": not track.automatic,
            "writeautomaticsub": track.automatic,
            "subtitleslangs": [track.key],
            "subtitlesformat": "vtt",
            "outtmpl": str(tmp / "sub.%(ext)s"),
        }
    )
    ytdlp.download(url, params)
    files = sorted(tmp.glob("*.vtt"))
    return files[0].read_text(encoding="utf-8", errors="replace") if files else ""


def _yt_dlp_audio(url: str, options: Mapping[str, object], tmp: Path) -> Path:
    """The default audio downloader: the best audio-only format, no post-processing.

    A format larger than ``options["max_filesize"]`` is skipped by yt-dlp and leaves no file:
    reported as ``no_audio_format``.
    """
    params = dict(options)
    params.update({"format": "bestaudio", "outtmpl": str(tmp / "audio.%(ext)s")})
    ytdlp.download(url, params)
    files = [f for f in tmp.glob("audio.*") if f.suffix not in (".part", ".ytdl")]
    if len(files) != 1:
        raise TranscriptUnavailable("no_audio_format", url=url)
    return files[0]


class _Segment(Protocol):
    @property
    def text(self) -> str: ...


class _Info(Protocol):
    @property
    def duration(self) -> float: ...

    @property
    def language(self) -> str: ...


class _Model(Protocol):
    def transcribe(
        self, audio: str, *, language: str | None, vad_filter: bool
    ) -> tuple[Iterable[_Segment], _Info]: ...


@cache
def _model(size: str) -> _Model:
    """The Whisper model of ``size``, loaded once per process (CPU, int8).

    Shared by every adapter in the process: only use it while holding ``_MODEL_LOCK``.
    """
    from faster_whisper import WhisperModel

    return cast(_Model, WhisperModel(size, device="cpu", compute_type="int8"))


def _whisper_transcribe(path: Path, model_size: str, max_seconds: int) -> tuple[str, str]:
    """The default transcriber: ``(text, detected language)``.

    Raises :class:`AudioTooLong` before reading any segment when the audio is longer than
    ``max_seconds`` and :class:`ImportError` when the ``whisper`` extra is missing.
    """
    with _MODEL_LOCK:  # the segments decode lazily: hold the lock until they are read
        segments, info = _model(model_size).transcribe(str(path), language=None, vad_filter=True)
        if info.duration > max_seconds:
            raise AudioTooLong(info.duration)
        text = " ".join(part for segment in segments if (part := segment.text.strip()))
    return text, info.language


def _whisper_available() -> bool:
    """Whether the optional ``whisper`` extra is installed."""
    return importlib.util.find_spec("faster_whisper") is not None
