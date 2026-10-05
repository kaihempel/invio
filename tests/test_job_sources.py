"""Tests for the discriminated source models."""

from typing import Any

import pytest

from invio.config.job import (
    JobConfig,
    RssSource,
    SitemapSource,
    WebSource,
    YoutubeChannelSource,
    YoutubePlaylistSource,
)
from tests.job_helpers import validation_errors

VALID_SOURCES = [
    ({"type": "rss", "url": "https://example.com/feed.xml"}, RssSource),
    ({"type": "web", "url": "https://example.com/news"}, WebSource),
    ({"type": "sitemap", "url": "https://example.com/sitemap.xml"}, SitemapSource),
    ({"type": "youtube_channel", "channel_id": "UC123"}, YoutubeChannelSource),
    ({"type": "youtube_playlist", "playlist_id": "PL123"}, YoutubePlaylistSource),
]


@pytest.mark.parametrize(("source", "cls"), VALID_SOURCES)
def test_each_source_kind_is_accepted(
    job_data: dict[str, Any], source: dict[str, Any], cls: type
) -> None:
    job_data["sources"] = [source]

    parsed = JobConfig.model_validate(job_data).sources[0]

    assert isinstance(parsed, cls)
    assert parsed.enabled is True
    assert parsed.name is None


def test_source_name_and_enabled(job_data: dict[str, Any]) -> None:
    job_data["sources"] = [
        {"type": "rss", "url": "https://example.com/feed.xml", "name": "Blog", "enabled": False}
    ]

    parsed = JobConfig.model_validate(job_data).sources[0]

    assert parsed.name == "Blog"
    assert parsed.enabled is False


def test_all_sources_disabled_is_accepted(job_data: dict[str, Any]) -> None:
    job_data["sources"] = [{**src, "enabled": False} for src, _ in VALID_SOURCES]

    job = JobConfig.model_validate(job_data)

    assert all(not s.enabled for s in job.sources)


ALL_KINDS = [
    ("rss", "url", "https://example.com/feed.xml"),
    ("web", "url", "https://example.com/news"),
    ("sitemap", "url", "https://example.com/sitemap.xml"),
    ("youtube_channel", "channel_id", "UC123"),
    ("youtube_playlist", "playlist_id", "PL123"),
]
URL_KINDS = [k for k in ALL_KINDS if k[1] == "url"]
ID_KINDS = [k for k in ALL_KINDS if k[1] != "url"]


def _errors(job_data: dict[str, Any], source: dict[str, Any]) -> list[tuple[Any, str]]:
    job_data["sources"] = [source]
    return validation_errors(job_data)


@pytest.mark.parametrize(("kind", "field", "value"), ALL_KINDS)
def test_missing_locator(job_data: dict[str, Any], kind: str, field: str, value: str) -> None:
    errors = _errors(job_data, {"type": kind})

    assert errors == [(("sources", 0, kind, field), "Field required")]


@pytest.mark.parametrize(("kind", "field", "value"), ALL_KINDS)
def test_foreign_locator_rejected(
    job_data: dict[str, Any], kind: str, field: str, value: str
) -> None:
    foreign = "channel_id" if field == "url" else "url"

    errors = _errors(job_data, {"type": kind, field: value, foreign: "x"})

    assert errors == [(("sources", 0, kind, foreign), "Extra inputs are not permitted")]


@pytest.mark.parametrize(("kind", "field", "value"), ALL_KINDS)
def test_unknown_key_rejected(job_data: dict[str, Any], kind: str, field: str, value: str) -> None:
    errors = _errors(job_data, {"type": kind, field: value, "foo": 1})

    assert errors == [(("sources", 0, kind, "foo"), "Extra inputs are not permitted")]


@pytest.mark.parametrize(("kind", "field", "value"), ALL_KINDS)
def test_enabled_must_be_bool(job_data: dict[str, Any], kind: str, field: str, value: str) -> None:
    errors = _errors(job_data, {"type": kind, field: value, "enabled": 1})

    assert [loc for loc, _ in errors] == [("sources", 0, kind, "enabled")]


@pytest.mark.parametrize(("kind", "field", "value"), URL_KINDS)
@pytest.mark.parametrize(
    ("bad", "message"),
    [("ftp://x", "URL scheme should be 'http' or 'https'"), ("/relative", "relative URL")],
)
def test_bad_url_rejected(
    job_data: dict[str, Any], kind: str, field: str, value: str, bad: str, message: str
) -> None:
    errors = _errors(job_data, {"type": kind, "url": bad})

    assert [loc for loc, _ in errors] == [("sources", 0, kind, "url")]
    assert message in errors[0][1]


@pytest.mark.parametrize(("kind", "field", "value"), ID_KINDS)
def test_empty_id_rejected(job_data: dict[str, Any], kind: str, field: str, value: str) -> None:
    errors = _errors(job_data, {"type": kind, field: ""})

    assert [loc for loc, _ in errors] == [("sources", 0, kind, field)]


def test_unknown_type_lists_allowed_tags(job_data: dict[str, Any]) -> None:
    errors = _errors(job_data, {"type": "podcast", "url": "https://example.com/"})

    assert len(errors) == 1
    for tag in ("rss", "web", "sitemap", "youtube_channel", "youtube_playlist"):
        assert f"'{tag}'" in errors[0][1]


def test_blank_name_rejected(job_data: dict[str, Any]) -> None:
    errors = _errors(job_data, {"type": "rss", "url": "https://example.com/f", "name": "  "})

    assert [loc for loc, _ in errors] == [("sources", 0, "rss", "name")]


def test_rss_max_age_days_defaults_to_none(job_data: dict[str, Any]) -> None:
    job_data["sources"] = [{"type": "rss", "url": "https://example.com/feed.xml"}]

    parsed = JobConfig.model_validate(job_data).sources[0]

    assert isinstance(parsed, RssSource)
    assert parsed.max_age_days is None


def test_rss_max_age_days_is_accepted_and_saved_last(job_data: dict[str, Any]) -> None:
    job_data["sources"] = [
        {"type": "rss", "url": "https://example.com/feed.xml", "max_age_days": 7}
    ]

    parsed = JobConfig.model_validate(job_data).sources[0]

    assert isinstance(parsed, RssSource)
    assert parsed.max_age_days == 7
    assert list(parsed.model_dump()) == ["type", "url", "name", "enabled", "max_age_days"]


@pytest.mark.parametrize("value", [0, -1, 1.5, "7", True])
def test_rss_max_age_days_rejects_invalid(job_data: dict[str, Any], value: object) -> None:
    errors = _errors(
        job_data, {"type": "rss", "url": "https://example.com/f", "max_age_days": value}
    )

    assert [location for location, _ in errors] == [("sources", 0, "rss", "max_age_days")]


@pytest.mark.parametrize("kind", ["web", "sitemap"])
def test_max_age_days_is_rss_only(job_data: dict[str, Any], kind: str) -> None:
    errors = _errors(job_data, {"type": kind, "url": "https://example.com/f", "max_age_days": 7})

    assert [location for location, _ in errors] == [("sources", 0, kind, "max_age_days")]
