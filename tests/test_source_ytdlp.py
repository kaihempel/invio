"""Shared yt-dlp plumbing: URL parsing, base options, the default seams and failure text."""

from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

import pytest

from invio.sources import ytdlp
from invio.sources.errors import FetchError
from tests.transcript_helpers import PROXY, VIDEO_ID


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://www.youtube.com/watch?v={VIDEO_ID}", VIDEO_ID),
        (f"  https://www.youtube.com/watch?v={VIDEO_ID}&t=3 ", VIDEO_ID),
        (f"https://YOUTUBE.com/shorts/{VIDEO_ID}/", VIDEO_ID),
        (f"https://youtu.be/{VIDEO_ID}?si=x", VIDEO_ID),
        ("https://www.youtube.com/watch?v=a&v=b", "a"),
        ("https://www.youtube.com/watch", None),
        ("https://www.youtube.com/watch?v=bad id", None),
        ("https://www.youtube.com/watch?v=" + "a" * 65, None),
        ("https://www.youtube.com/playlist?list=PL1", None),
        ("https://music.youtube.com/watch?v=abc", None),
        ("https://evil.example/watch?v=abc", None),
        ("file:///etc/passwd", None),
        ("javascript:alert(1)", None),
        ("", None),
        ("https://[::1/watch?v=abc", None),
    ],
)
def test_parse_video_url(url: str, expected: str | None) -> None:
    assert ytdlp.parse_video_url(url) == expected


def test_the_worker_bound_is_shared_by_both_adapters() -> None:
    from invio.sources import transcript, youtube

    assert youtube._MAX_WORKERS is ytdlp.MAX_WORKERS
    assert transcript._MAX_WORKERS is ytdlp.MAX_WORKERS


def test_watch_url_is_canonical() -> None:
    assert ytdlp.watch_url("abc") == "https://www.youtube.com/watch?v=abc"


def test_base_options_without_cookies_or_proxy() -> None:
    options = ytdlp.base_options(timeout=60.0, cookies_file=None, proxy=None)

    assert "cookiefile" not in options and "proxy" not in options
    assert options["socket_timeout"] == 20.0  # capped
    assert options["allowed_extractors"] == ["youtube.*"]
    assert (options["retries"], options["extractor_retries"], options["cachedir"]) == (0, 0, False)
    assert isinstance(options["logger"], ytdlp.NullLogger)


def test_base_options_with_cookies_and_proxy(tmp_path: Path) -> None:
    options = ytdlp.base_options(timeout=5.0, cookies_file=tmp_path / "c.txt", proxy=PROXY)

    assert options["cookiefile"] == str(tmp_path / "c.txt")
    assert options["proxy"] == PROXY
    assert options["socket_timeout"] == 5.0


def test_null_logger_swallows_everything() -> None:
    logger = ytdlp.NullLogger()

    for method in (logger.debug, logger.info, logger.warning, logger.error):
        assert method(f"secret {PROXY}") is None


class _Ydl:
    """A ``YoutubeDL`` stand-in returning ``result`` and recording downloads."""

    result: object = {"id": "x"}
    downloaded: ClassVar[list[str]] = []

    def __init__(self, params: Mapping[str, object]) -> None:
        self.params = params

    def __enter__(self) -> "_Ydl":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, *, download: bool) -> object:
        assert download is False
        return type(self).result

    def download(self, urls: list[str]) -> int:
        type(self).downloaded += urls
        return 0


def test_extract_info_returns_the_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    import yt_dlp

    _Ydl.result = {"id": "x"}
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _Ydl)

    assert ytdlp.extract_info("u", {}) == {"id": "x"}


def test_extract_info_rejects_a_non_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    import yt_dlp

    _Ydl.result = ["not", "a", "mapping"]
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _Ydl)

    with pytest.raises(TypeError, match="no info mapping"):
        ytdlp.extract_info("u", {})


def test_download_runs_yt_dlp_download(monkeypatch: pytest.MonkeyPatch) -> None:
    import yt_dlp

    _Ydl.downloaded = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _Ydl)

    ytdlp.download("https://www.youtube.com/watch?v=abc", {})

    assert _Ydl.downloaded == ["https://www.youtube.com/watch?v=abc"]


@pytest.mark.parametrize("call", [ytdlp.extract_info, ytdlp.download])
def test_an_unreadable_cookies_file_is_cookies_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, call: object
) -> None:
    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _Ydl)

    with pytest.raises(FetchError) as caught:
        call("u", {"cookiefile": str(tmp_path / "missing.txt")})  # type: ignore[operator]

    assert caught.value.reason == "cookies_unavailable"
    assert str(tmp_path) not in str(caught.value)


class _Cyclic(Exception):
    pass


def test_failure_text_follows_causes_once_and_lowercases() -> None:
    inner = RuntimeError("Inner TEXT")
    outer = _Cyclic("Outer")
    outer.__cause__ = inner
    inner.__cause__ = outer  # a cycle must not loop

    assert ytdlp.failure_text(outer) == "outer inner text"


def test_failure_text_survives_a_message_that_cannot_be_rendered() -> None:
    class Broken(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no text")

    assert ytdlp.failure_text(Broken()) == ""


def test_classify_safely_falls_back_when_the_message_cannot_be_rendered() -> None:
    class Broken(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no text")

    assert ytdlp.classify_safely(Broken()) == ("invalid_response", None)


def test_chain_depth_is_bounded() -> None:
    root = RuntimeError("0")
    node = root
    for depth in range(1, 40):
        nxt = RuntimeError(str(depth))
        node.__cause__ = nxt
        node = nxt

    assert "39" not in ytdlp.failure_text(root)
