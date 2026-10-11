"""Full runs with the real ``YoutubeTranscripts`` adapter behind ``fetch_transcript`` (#29).

``test_graph_video_path.py`` drives the ``video_path`` node with a scripted port. Here the port
is the real adapter over fake yt-dlp/Whisper seams, so the job run, the node, the per-run cap,
the run deadline and the adapter's error mapping are exercised together: subtitles, a permanent
and a transient failure, the cap, the run-time check, a cap of zero (D2) and secret redaction.
"""

import dataclasses
import logging
import urllib.error
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from invio.config.job import ScheduleConfig
from invio.config.settings import Settings
from invio.db.models import Item, Job
from invio.db.session import session_factory, session_scope
from invio.domain import ItemStatus, RunStatus
from invio.pipeline.run import RunResult, run_job
from invio.sources import transcript as transcript_module
from invio.sources.transcript import SubtitleTrack, TranscriptBackends, YoutubeTranscripts
from tests.conftest import FakeClock
from tests.db_helpers import make_candidate
from tests.pipeline_helpers import Env, FakePorts, build_env, make_job_config
from tests.transcript_helpers import PROXY, RAW_MESSAGE, FakeAudio, FakeTranscriber, vtt

pytestmark = pytest.mark.usefixtures("clean_jobs")

NextRun = Callable[[ScheduleConfig, datetime], datetime]
FEED = "https://example.com/feed.xml"


def watch(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def video(number: int) -> Any:
    """Candidate ``Video <number>`` (no teaser, like a YouTube listing); lower numbers first."""
    return make_candidate(
        watch(f"vid{number}"),
        title=f"Video {number}",
        type="video",
        teaser=None,
        content_hash=None,
        published_at=datetime(2026, 10, 1, tzinfo=UTC) - timedelta(hours=number),
    )


def video_info(
    *, duration: int | None = 600, manual: Mapping[str, str] | None = None, description: str = ""
) -> dict[str, object]:
    """A yt-dlp info mapping; ``manual`` maps a track key to its VTT text."""
    return {
        "duration": duration,
        "subtitles": {key: [{"ext": "vtt", "url": "https://s.invalid"}] for key in manual or {}},
        "automatic_captions": {},
        "language": None,
        "description": description,
    }


class Videos:
    """Fake metadata and subtitle seams keyed by video URL; records every call."""

    def __init__(
        self,
        infos: Mapping[str, Mapping[str, object]],
        *,
        vtts: Mapping[tuple[str, str], str] | None = None,
        subtitle_error: BaseException | None = None,
    ) -> None:
        self.infos = dict(infos)
        self.vtts = dict(vtts or {})
        self.subtitle_error = subtitle_error
        self.extracts: list[str] = []
        self.subtitles: list[tuple[str, str]] = []

    def extract(self, url: str, options: Mapping[str, object]) -> Mapping[str, object]:
        self.extracts.append(url)
        return self.infos[url]

    def subtitle(
        self, url: str, track: SubtitleTrack, options: Mapping[str, object], tmp: Path
    ) -> str:
        self.subtitles.append((url, track.key))
        if self.subtitle_error is not None:
            raise self.subtitle_error
        return self.vtts.get((url, track.key), "")


def adapter_for(
    videos: Videos,
    clock: Callable[[], datetime],
    *,
    audio: FakeAudio | None = None,
    transcriber: FakeTranscriber | None = None,
    whisper: bool = True,
    **kwargs: Any,
) -> YoutubeTranscripts:
    backends = TranscriptBackends(
        extract=videos.extract,
        subtitle=videos.subtitle,
        audio=audio or FakeAudio(),
        transcribe=transcriber or FakeTranscriber(),
        whisper_available=lambda: whisper,
    )
    return YoutubeTranscripts(backends=backends, clock=clock, **kwargs)


@pytest.fixture
def adapters() -> Iterator[list[YoutubeTranscripts]]:
    """Adapters to close at teardown (their worker pools)."""
    created: list[YoutubeTranscripts] = []
    yield created
    for adapter in created:
        adapter.close()


def wire(
    engine: Engine,
    clock: FakeClock,
    next_run: NextRun,
    adapter: YoutubeTranscripts,
    candidates: list[Any],
    **kwargs: Any,
) -> Env:
    """A job over ``candidates`` whose ``fetch_transcript`` is ``adapter``.

    ``lock_ttl`` (the run's deadline) is applied on top of :func:`build_env`.
    """
    lock_ttl = kwargs.pop("lock_ttl", None)
    env = build_env(engine, clock, next_run, ports=FakePorts({FEED: candidates}), **kwargs)
    changes: dict[str, Any] = {"fetch_transcript": adapter.get_transcript}
    if lock_ttl is not None:
        changes["lock_ttl"] = lock_ttl
    return dataclasses.replace(env, deps=dataclasses.replace(env.deps, **changes))


def items_by_title(engine: Engine) -> dict[str, Item]:
    with session_scope(session_factory(engine)) as session:
        rows = list(session.scalars(select(Item)))
        session.expunge_all()
        return {row.title: row for row in rows}


async def run_again(env: Env, clock: FakeClock) -> RunResult:
    """Make the job due and unlocked again, then run it."""
    with session_scope(env.factory) as session:
        job = session.get(Job, env.job_id)
        assert job is not None
        job.next_run_at = clock() - timedelta(minutes=1)
        job.locked_until = None
    return await run_job(env.job_id, deps=env.deps)


def records(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == message]


# --- subtitles: content, no audio, logged ---------------------------------------------------


async def test_a_video_with_subtitles_is_summarized_from_them_without_any_audio(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    adapters: list[YoutubeTranscripts],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caption = vtt("<c>Hello</c> &amp; welcome", "<c>Hello</c> &amp; welcome", "to the talk")
    videos = Videos(
        {watch("vid1"): video_info(manual={"de": caption})}, vtts={(watch("vid1"), "de"): caption}
    )
    audio, transcriber = FakeAudio(), FakeTranscriber()
    adapter = adapter_for(videos, fake_clock, audio=audio, transcriber=transcriber)
    adapters.append(adapter)
    env = wire(
        db_engine,
        fake_clock,
        recording_next_run,
        adapter,
        [video(1)],
        config=make_job_config(language="de"),
    )

    with caplog.at_level(logging.INFO):
        result = await run_job(env.job_id, deps=env.deps)

    item = items_by_title(db_engine)["Video 1"]
    assert item.raw_content == "Hello & welcome to the talk"
    assert item.status == ItemStatus.SUMMARIZED
    assert audio.calls == [] and transcriber.calls == []
    assert env.ports.page_calls == {}
    (rating,) = env.provider.requests_for("relevance")
    assert "Hello & welcome to the talk" in rating.user
    assert result.status == RunStatus.SUCCEEDED
    (logged,) = records(caplog, "extract.transcript")
    assert logged.__dict__["item_id"] == item.id
    assert (logged.__dict__["method"], logged.__dict__["language"]) == ("subtitles", "de")


# --- permanent: rated from title + description, never retaken --------------------------------


async def test_a_permanently_unavailable_video_is_rated_from_its_description_once(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    adapters: list[YoutubeTranscripts],
    caplog: pytest.LogCaptureFixture,
) -> None:
    videos = Videos({watch("vid1"): video_info(description="A talk about  X\n\nand Y")})
    audio = FakeAudio()
    adapter = adapter_for(videos, fake_clock, audio=audio, whisper=False)
    adapters.append(adapter)
    env = wire(db_engine, fake_clock, recording_next_run, adapter, [video(1)])

    with caplog.at_level(logging.INFO):
        result = await run_job(env.job_id, deps=env.deps)

    item = items_by_title(db_engine)["Video 1"]
    assert item.raw_content is None
    assert item.teaser == "A talk about X and Y"
    assert item.status not in (ItemStatus.FAILED, ItemStatus.NEW)
    assert item.attempts == 1
    assert audio.calls == []
    assert result.status == RunStatus.SUCCEEDED and result.stats["failed"] == 0
    (rating,) = env.provider.requests_for("relevance")
    assert "A talk about X and Y" in rating.user
    (logged,) = records(caplog, "extract.transcript_unavailable")
    assert (logged.__dict__["item_id"], logged.__dict__["reason"]) == (
        item.id,
        "whisper_not_installed",
    )

    # SC-009: a later run does not look at the video again
    await run_again(env, fake_clock)
    assert videos.extracts == [watch("vid1")]
    assert audio.calls == []


async def test_a_too_long_video_without_subtitles_is_rated_without_any_download(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    adapters: list[YoutubeTranscripts],
    caplog: pytest.LogCaptureFixture,
) -> None:
    videos = Videos({watch("vid1"): video_info(duration=120 * 60, description="Long one")})
    audio = FakeAudio()
    adapter = adapter_for(videos, fake_clock, audio=audio)
    adapters.append(adapter)
    env = wire(db_engine, fake_clock, recording_next_run, adapter, [video(1)])

    with caplog.at_level(logging.INFO):
        result = await run_job(env.job_id, deps=env.deps)

    item = items_by_title(db_engine)["Video 1"]
    assert item.status != ItemStatus.FAILED and item.teaser == "Long one"
    assert audio.calls == []
    assert result.status == RunStatus.SUCCEEDED
    (logged,) = records(caplog, "extract.transcript_unavailable")
    assert logged.__dict__["reason"] == "too_long"


# --- transient: retried, then only that item fails; no secret in the error ------------------


async def test_a_transient_subtitle_failure_fails_only_that_item_without_leaking_secrets(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    adapters: list[YoutubeTranscripts],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    cookies = tmp_path / "private-cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    leak = f"{RAW_MESSAGE} {PROXY} {cookies}"
    error = urllib.error.HTTPError(watch("vid1"), 503, leak, {}, None)  # type: ignore[arg-type]
    caption = vtt("fine")
    videos = Videos(
        {
            watch("vid1"): video_info(manual={"de": caption}),
            watch("vid2"): video_info(manual={"en": caption}),
        },
        vtts={(watch("vid2"), "en"): caption},
    )
    failing = Videos(videos.infos, subtitle_error=error)

    def subtitle(url: str, track: SubtitleTrack, options: Mapping[str, object], tmp: Path) -> str:
        source = failing if url == watch("vid1") else videos
        return source.subtitle(url, track, options, tmp)

    audio, transcriber = FakeAudio(), FakeTranscriber()
    adapter = adapter_for(
        videos, fake_clock, audio=audio, transcriber=transcriber, proxy=PROXY, cookies_file=cookies
    )
    adapter._download_subtitle = subtitle
    adapters.append(adapter)
    env = wire(db_engine, fake_clock, recording_next_run, adapter, [video(1), video(2)])

    with caplog.at_level(logging.DEBUG):
        result = await run_job(env.job_id, deps=env.deps)

    items = items_by_title(db_engine)
    failed = items["Video 1"]
    assert failed.status == ItemStatus.FAILED
    assert (failed.last_error or "").startswith("TranscriptUnavailable: http_status")
    assert len(failing.subtitles) == 3  # RetrySettings default: three attempts
    assert audio.calls == [] and transcriber.calls == []  # transient never falls back
    assert items["Video 2"].status == ItemStatus.SUMMARIZED
    assert result.status == RunStatus.PARTIAL
    shown = " ".join([failed.last_error or "", repr(result.errors), repr(result.stats)])
    logged = " ".join(f"{r.getMessage()} {r.__dict__} {r.exc_text}" for r in caplog.records)
    for secret in (PROXY, "user:secret", str(cookies), "sensitive-library-text"):
        assert secret not in shown
        assert secret not in logged

    # the failed item is retried by a later run (attempts left)
    failing.subtitle_error = None
    failing.vtts[(watch("vid1"), "de")] = caption
    await run_again(env, fake_clock)
    assert items_by_title(db_engine)["Video 1"].raw_content == "fine"


# --- the per-run cap: deferral through the real adapter -------------------------------------


async def test_the_cap_defers_videos_without_subtitles_and_subtitles_do_not_count(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    adapters: list[YoutubeTranscripts],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caption = vtt("captioned")
    videos = Videos(
        {
            watch("vid1"): video_info(manual={"en": caption}),
            watch("vid2"): video_info(),
            watch("vid3"): video_info(),
            watch("vid4"): video_info(),
        },
        vtts={(watch("vid1"), "en"): caption},
    )
    audio, transcriber = FakeAudio(), FakeTranscriber("transcribed words")
    adapter = adapter_for(videos, fake_clock, audio=audio, transcriber=transcriber)
    adapters.append(adapter)
    env = wire(
        db_engine,
        fake_clock,
        recording_next_run,
        adapter,
        [video(n) for n in range(1, 5)],
        max_transcriptions_per_run=1,
    )

    with caplog.at_level(logging.INFO):
        result = await run_job(env.job_id, deps=env.deps)

    items = items_by_title(db_engine)
    assert items["Video 1"].raw_content == "captioned"
    transcribed = [t for t in ("Video 2", "Video 3", "Video 4") if items[t].raw_content]
    deferred = [t for t in ("Video 2", "Video 3", "Video 4") if items[t].status == ItemStatus.NEW]
    assert len(transcribed) == 1 and len(deferred) == 2
    assert all(items[t].attempts == 0 and items[t].run_id is None for t in deferred)
    assert len(audio.calls) == 1 and len(transcriber.calls) == 1
    assert result.status == RunStatus.SUCCEEDED
    assert result.stats["failed"] == 0 and result.errors == ()
    reasons = [r.__dict__["reason"] for r in records(caplog, "item.transcription_deferred")]
    assert reasons == ["cap_reached", "cap_reached"]

    # the next run picks the deferred videos up (one more per run with a cap of 1)
    await run_again(env, fake_clock)
    items = items_by_title(db_engine)
    assert sum(1 for t in deferred if items[t].raw_content) == 1
    assert len(audio.calls) == 2


async def test_a_video_that_does_not_fit_the_run_time_is_deferred_and_frees_its_slot(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    adapters: list[YoutubeTranscripts],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 30 min to the deadline: 25 min + 10 min reserve does not fit, 5 + 10 does
    videos = Videos(
        {watch("vid1"): video_info(duration=25 * 60), watch("vid2"): video_info(duration=5 * 60)}
    )
    audio = FakeAudio()
    adapter = adapter_for(videos, fake_clock, audio=audio)
    adapters.append(adapter)
    env = wire(
        db_engine,
        fake_clock,
        recording_next_run,
        adapter,
        [video(1), video(2)],
        max_transcriptions_per_run=1,
        concurrency=1,
        lock_ttl=timedelta(minutes=30),
    )

    with caplog.at_level(logging.INFO):
        result = await run_job(env.job_id, deps=env.deps)

    items = items_by_title(db_engine)
    assert (items["Video 1"].status, items["Video 1"].attempts) == (ItemStatus.NEW, 0)
    assert items["Video 2"].raw_content  # it got the slot video 1 gave back
    assert [url for url, _, _ in audio.calls] == [watch("vid2")]
    assert result.status == RunStatus.SUCCEEDED
    (logged,) = records(caplog, "item.transcription_deferred")
    assert logged.__dict__["reason"] == "not_enough_time"


# --- D2: a cap of zero switches the fallback off (rated, not deferred) ----------------------


async def test_a_cap_of_zero_from_the_settings_rates_videos_without_subtitles(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    adapters: list[YoutubeTranscripts],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    videos = Videos({watch("vid1"): video_info(description="About Z")})
    audio = FakeAudio()
    monkeypatch.setattr(transcript_module, "_yt_dlp_extract", videos.extract)
    monkeypatch.setattr(transcript_module, "_yt_dlp_subtitle", videos.subtitle)
    monkeypatch.setattr(transcript_module, "_yt_dlp_audio", audio)
    monkeypatch.setattr(transcript_module, "_whisper_available", lambda: True)
    settings = Settings(_env_file=None, whisper_max_per_run=0)
    adapter = YoutubeTranscripts.from_settings(settings, clock=fake_clock)
    adapters.append(adapter)
    env = wire(
        db_engine,
        fake_clock,
        recording_next_run,
        adapter,
        [video(1)],
        max_transcriptions_per_run=settings.whisper_max_per_run,
    )

    with caplog.at_level(logging.INFO):
        result = await run_job(env.job_id, deps=env.deps)

    item = items_by_title(db_engine)["Video 1"]
    assert item.status not in (ItemStatus.NEW, ItemStatus.FAILED)  # rated, not deferred
    assert item.teaser == "About Z"
    assert audio.calls == []
    assert records(caplog, "item.transcription_deferred") == []
    (logged,) = records(caplog, "extract.transcript_unavailable")
    assert logged.__dict__["reason"] == "transcription_disabled"
    assert result.status == RunStatus.SUCCEEDED
