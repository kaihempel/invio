"""The default yt-dlp extractor of the YouTube source: cookie copies and logging."""

import stat
from pathlib import Path

import pytest

from invio.sources import youtube as youtube_module
from invio.sources.errors import FetchError
from invio.sources.youtube import YoutubeSource
from tests.youtube_helpers import (
    LISTING_URL,
    NOW,
    PROXY,
    FakeExtractor,
    FakeYoutubeDL,
    channel,
    entry,
    listing,
    source,
    watch,
)


async def test_default_extractor_passes_url_and_options_to_yt_dlp(
    fake_ydl: FakeYoutubeDL,
) -> None:
    fake_ydl.result = listing(entry("zz", "Real path"))

    result = await YoutubeSource(now=lambda: NOW).fetch(channel())

    assert [c.url for c in result] == [watch("zz")]
    assert fake_ydl.url == "https://www.youtube.com/channel/UC123/videos"
    assert fake_ydl.download is False
    assert fake_ydl.closed is True
    assert fake_ydl.params is not None
    assert "cookiefile" not in fake_ydl.params


async def test_default_extractor_gives_yt_dlp_a_private_copy_of_the_cookies(
    fake_ydl: FakeYoutubeDL, tmp_path: Path
) -> None:
    cookies = tmp_path / "cookies.txt"
    original = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tv\n"
    cookies.write_bytes(original)
    cookies.chmod(0o400)  # read-only: yt-dlp's write-back in __exit__ must not need it writable
    try:
        result = await YoutubeSource(cookies_file=cookies, now=lambda: NOW).fetch(channel())
    finally:
        cookies.chmod(0o600)

    assert result == []
    assert fake_ydl.params is not None
    copy = Path(str(fake_ydl.params["cookiefile"]))
    assert copy != cookies
    assert fake_ydl.cookie_copy_text == original.decode()
    assert fake_ydl.cookie_copy_mode == 0o600
    assert cookies.read_bytes() == original  # byte-identical despite the write-back
    assert not copy.parent.exists()


def test_cookie_copies_are_private_separate_and_removed(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    seen: list[Path] = []

    with (
        youtube_module._private_cookie_copy(cookies) as first,
        youtube_module._private_cookie_copy(cookies) as second,
    ):
        seen += [first, second]
        assert first != second
        assert first.read_text() == second.read_text() == "# Netscape HTTP Cookie File\n"
        assert stat.S_IMODE(first.stat().st_mode) == 0o600
        assert stat.S_IMODE(first.parent.stat().st_mode) == 0o700
        first.write_text("rewritten")

    assert not any(path.parent.exists() for path in seen)
    assert cookies.read_text() == "# Netscape HTTP Cookie File\n"


def test_cookie_copy_is_removed_when_the_body_fails(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("x")
    seen: list[Path] = []

    with pytest.raises(RuntimeError), youtube_module._private_cookie_copy(cookies) as copy:
        seen.append(copy)
        raise RuntimeError

    assert not seen[0].parent.exists()


async def test_cookies_vanishing_before_the_copy_is_cookies_unavailable(
    fake_ydl: FakeYoutubeDL, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookies = tmp_path / "cookies-secret-name.txt"
    cookies.write_text("x")
    adapter = YoutubeSource(cookies_file=cookies, now=lambda: NOW)
    monkeypatch.setattr(adapter, "_check_cookies", lambda url: cookies.unlink())

    with pytest.raises(FetchError) as info:
        await adapter.fetch(channel())

    assert (info.value.reason, info.value.url) == ("cookies_unavailable", LISTING_URL)
    assert "cookies-secret-name" not in str(info.value)
    assert fake_ydl.params is None  # yt-dlp was never constructed


async def test_default_extractor_never_logs_yt_dlp_output() -> None:
    extract = FakeExtractor()

    await source(extract).fetch(channel())

    logger = extract.calls[0][1]["logger"]
    for method in ("debug", "info", "warning", "error"):
        assert getattr(logger, method)(f"proxy {PROXY}") is None
