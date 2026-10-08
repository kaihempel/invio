"""Tests for the discriminated source models."""

from typing import Any

import pytest

from invio.config.job import (
    JobConfig,
    JobConfigError,
    RssSource,
    SitemapSource,
    WebSource,
    YoutubeChannelSource,
    YoutubePlaylistSource,
    parse_youtube_channel,
    parse_youtube_playlist,
    validate_job,
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


def test_max_age_days_is_not_a_web_option(job_data: dict[str, Any]) -> None:
    errors = _errors(job_data, {"type": "web", "url": "https://example.com/f", "max_age_days": 7})

    assert [location for location, _ in errors] == [("sources", 0, "web", "max_age_days")]


# --- web source options (specs/008-gh-issue-12/contracts/job-file.md) -----------------------

WEB_URL = "https://example.com/news"
WEB_ALL_KEYS: dict[str, Any] = {
    "type": "web",
    "url": WEB_URL,
    "name": "News",
    "enabled": True,
    "selector": "main .post-list",
    "mode": "links",
    "url_pattern": r"/news/\d{4}/",
    "render": "js",
    "wait_for": ".post-list li",
}


def _web(job_data: dict[str, Any], **fields: Any) -> WebSource:
    job_data["sources"] = [{"type": "web", "url": WEB_URL, **fields}]
    source = JobConfig.model_validate(job_data).sources[0]
    assert isinstance(source, WebSource)
    return source


def test_web_source_with_all_keys_loads(job_data: dict[str, Any]) -> None:
    job_data["sources"] = [WEB_ALL_KEYS]

    source = JobConfig.model_validate(job_data).sources[0]

    assert isinstance(source, WebSource)
    assert source.selector == "main .post-list"
    assert source.mode == "links"
    assert source.url_pattern == r"/news/\d{4}/"
    assert source.render == "js"
    assert source.wait_for == ".post-list li"


def test_web_source_defaults(job_data: dict[str, Any]) -> None:
    source = _web(job_data)

    assert (source.selector, source.mode, source.url_pattern) == (None, "page", None)
    assert (source.render, source.wait_for) == ("static", None)


def test_old_web_entry_without_new_keys_loads(job_data: dict[str, Any]) -> None:
    source = _web(job_data, name="Old", enabled=False)

    assert source.name == "Old"
    assert source.enabled is False
    assert source.mode == "page"


def test_web_fields_are_written_in_contract_order() -> None:
    assert list(WebSource.model_fields) == [
        "type",
        "url",
        "name",
        "enabled",
        "selector",
        "mode",
        "url_pattern",
        "render",
        "wait_for",
    ]


WEB_ERRORS = [
    ({"selector": "div["}, "sources[0].selector: invalid CSS selector"),
    ({"render": "js", "wait_for": "div["}, "sources[0].wait_for: invalid CSS selector"),
    ({"mode": "links", "url_pattern": "("}, "sources[0].url_pattern: invalid regular expression:"),
    ({"url_pattern": "x"}, "sources[0].url_pattern: only allowed with mode: links"),
    (
        {"mode": "page", "url_pattern": "x"},
        "sources[0].url_pattern: only allowed with mode: links",
    ),
    ({"wait_for": ".x"}, "sources[0].wait_for: only allowed with render: js"),
    ({"render": "static", "wait_for": ".x"}, "sources[0].wait_for: only allowed with render: js"),
    ({"mode": "feed"}, "sources[0].mode: Input should be 'page' or 'links'"),
    ({"render": "browser"}, "sources[0].render: Input should be 'static' or 'js'"),
    ({"selector": ""}, "sources[0].selector: String should have at least 1 character"),
    ({"selector": "  "}, "sources[0].selector: String should have at least 1 character"),
    ({"selector": " \t\u00a0\n"}, "sources[0].selector: String should have at least 1 character"),
    (
        {"mode": "links", "url_pattern": " \u00a0 "},
        "sources[0].url_pattern: String should have at least 1 character",
    ),
    (
        {"render": "js", "wait_for": "\t \u00a0"},
        "sources[0].wait_for: String should have at least 1 character",
    ),
    (
        {"mode": "links", "url_pattern": ""},
        "sources[0].url_pattern: String should have at least 1 character",
    ),
    (
        {"render": "js", "wait_for": ""},
        "sources[0].wait_for: String should have at least 1 character",
    ),
    ({"foo": 1}, "sources[0].foo: Extra inputs are not permitted"),
]


@pytest.mark.parametrize(("fields", "line"), WEB_ERRORS)
def test_web_load_time_errors(job_data: dict[str, Any], fields: dict[str, Any], line: str) -> None:
    job_data["sources"] = [{"type": "web", "url": WEB_URL, **fields}]

    with pytest.raises(JobConfigError) as info:
        validate_job(job_data)

    assert len(info.value.errors) == 1
    assert info.value.errors[0].startswith(line)


def test_invalid_regex_message_names_the_regex_error(job_data: dict[str, Any]) -> None:
    job_data["sources"] = [{"type": "web", "url": WEB_URL, "mode": "links", "url_pattern": "("}]

    with pytest.raises(JobConfigError) as info:
        validate_job(job_data)

    assert "missing ), unterminated subpattern" in info.value.errors[0]


def test_invalid_pattern_is_reported_even_with_mode_page(job_data: dict[str, Any]) -> None:
    # The regex is compiled before the cross-field rule is checked: one error per field.
    job_data["sources"] = [{"type": "web", "url": WEB_URL, "url_pattern": "("}]

    with pytest.raises(JobConfigError) as info:
        validate_job(job_data)

    assert len(info.value.errors) == 1
    assert "invalid regular expression" in info.value.errors[0]


@pytest.mark.parametrize("selector", ["a", "main .post-list > li:nth-child(2)", "a[href^='/n']"])
def test_valid_selectors_are_accepted(job_data: dict[str, Any], selector: str) -> None:
    assert _web(job_data, selector=selector).selector == selector


def test_selector_validation_does_not_import_the_sources_package() -> None:
    import subprocess
    import sys

    code = (
        "import sys, invio.config.job as j;"
        "j.WebSource.model_validate({'type':'web','url':'https://e.com','selector':'a'});"
        "assert 'invio.sources' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


CHANNEL_OK = [
    ("UC123", ("channel_id", "UC123")),
    ("UC-_x", ("channel_id", "UC-_x")),
    ("@some.channel", ("handle", "some.channel")),
    ("https://www.youtube.com/channel/UCabc_1-x", ("channel_id", "UCabc_1-x")),
    ("https://youtube.com/@handle", ("handle", "handle")),
    ("https://m.youtube.com/@handle", ("handle", "handle")),
    ("https://music.youtube.com/channel/UC1", ("channel_id", "UC1")),
    ("https://www.youtube.com/@handle/videos", ("handle", "handle")),
    ("https://www.youtube.com/channel/UC1/videos", ("channel_id", "UC1")),
]
CHANNEL_BAD = [
    "",
    " UC1",
    "UC1 ",
    "U C1",
    "@",
    "@bad handle",
    "file:///etc/passwd",
    "http://youtube.com/@x",
    "https://169.254.169.254/",
    "https://evil.example/@x",
    "https://www.youtube.com.evil.example/@x",
    "https://user:pw@www.youtube.com/@x",
    "https://www.youtube.com:8443/@x",
    "https://www.youtube.com/",
    "https://www.youtube.com/watch?v=abc",
    "https://www.youtube.com/channel/",
    "https://www.youtube.com/@x/videos/extra",
    "UC1/../x",
    "\u00dcC1",
]
PLAYLIST_OK = [
    ("PL123", "PL123"),
    ("UU-_x", "UU-_x"),
    ("https://www.youtube.com/playlist?list=PLabc_1-x", "PLabc_1-x"),
    ("https://youtube.com/watch?v=vid&list=PL9", "PL9"),
    ("https://music.youtube.com/playlist?list=OLAK5", "OLAK5"),
]
PLAYLIST_BAD = [
    "",
    " PL1",
    "PL 1",
    "file:///etc/passwd",
    "http://www.youtube.com/playlist?list=PL1",
    "https://www.youtube.com/playlist",
    "https://www.youtube.com/playlist?list=",
    "https://www.youtube.com/playlist?list=a%20b",
    "https://169.254.169.254/playlist?list=PL1",
    "https://evil.example/playlist?list=PL1",
    "https://www.youtube.com/watch?v=abc",
]


@pytest.mark.parametrize(("value", "expected"), CHANNEL_OK)
def test_parse_youtube_channel_accepts(value: str, expected: tuple[str, str]) -> None:
    assert parse_youtube_channel(value) == expected


@pytest.mark.parametrize("value", CHANNEL_BAD)
def test_parse_youtube_channel_rejects(value: str) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        parse_youtube_channel(value)


@pytest.mark.parametrize(("value", "expected"), PLAYLIST_OK)
def test_parse_youtube_playlist_accepts(value: str, expected: str) -> None:
    assert parse_youtube_playlist(value) == expected


@pytest.mark.parametrize("value", PLAYLIST_BAD)
def test_parse_youtube_playlist_rejects(value: str) -> None:
    with pytest.raises(ValueError, match="playlist_id"):
        parse_youtube_playlist(value)


@pytest.mark.parametrize(("value", "_"), CHANNEL_OK)
def test_channel_model_keeps_original_value(
    job_data: dict[str, Any], value: str, _: object
) -> None:
    job_data["sources"] = [{"type": "youtube_channel", "channel_id": value}]

    parsed = JobConfig.model_validate(job_data).sources[0]

    assert isinstance(parsed, YoutubeChannelSource)
    assert parsed.channel_id == value


@pytest.mark.parametrize("value", CHANNEL_BAD)
def test_channel_model_rejects_bad_locator(job_data: dict[str, Any], value: str) -> None:
    errors = _errors(job_data, {"type": "youtube_channel", "channel_id": value})

    assert [loc for loc, _ in errors] == [("sources", 0, "youtube_channel", "channel_id")]


@pytest.mark.parametrize("value", PLAYLIST_BAD)
def test_playlist_model_rejects_bad_locator(job_data: dict[str, Any], value: str) -> None:
    errors = _errors(job_data, {"type": "youtube_playlist", "playlist_id": value})

    assert [loc for loc, _ in errors] == [("sources", 0, "youtube_playlist", "playlist_id")]


@pytest.mark.parametrize(("value", "_"), [(v, e) for v, e in PLAYLIST_OK])
def test_playlist_model_keeps_original_value(
    job_data: dict[str, Any], value: str, _: object
) -> None:
    job_data["sources"] = [{"type": "youtube_playlist", "playlist_id": value}]

    parsed = JobConfig.model_validate(job_data).sources[0]

    assert isinstance(parsed, YoutubePlaylistSource)
    assert parsed.playlist_id == value


@pytest.mark.parametrize("value", [None, 5, ["UC1"]])
def test_non_string_locator_is_a_type_error(job_data: dict[str, Any], value: object) -> None:
    errors = _errors(job_data, {"type": "youtube_channel", "channel_id": value})

    assert [loc for loc, _ in errors] == [("sources", 0, "youtube_channel", "channel_id")]


YT_KINDS = [
    ("youtube_channel", "channel_id", "UC123"),
    ("youtube_playlist", "playlist_id", "PL123"),
]


@pytest.mark.parametrize(("kind", "field", "value"), YT_KINDS)
def test_youtube_limit_defaults(
    job_data: dict[str, Any], kind: str, field: str, value: str
) -> None:
    job_data["sources"] = [{"type": kind, field: value}]

    parsed = JobConfig.model_validate(job_data).sources[0]

    assert isinstance(parsed, YoutubeChannelSource | YoutubePlaylistSource)
    assert parsed.max_age_days is None
    assert parsed.max_items == 20


@pytest.mark.parametrize(("kind", "field", "value"), YT_KINDS)
@pytest.mark.parametrize(("limit", "ok"), [(1, True), (200, True), (0, False), (201, False)])
def test_youtube_max_items_bounds(
    job_data: dict[str, Any], kind: str, field: str, value: str, limit: int, ok: bool
) -> None:
    job_data["sources"] = [{"type": kind, field: value, "max_items": limit}]

    if ok:
        JobConfig.model_validate(job_data)
    else:
        assert [loc for loc, _ in validation_errors(job_data)] == [
            ("sources", 0, kind, "max_items")
        ]


@pytest.mark.parametrize(("kind", "field", "value"), YT_KINDS)
@pytest.mark.parametrize("bad", [0, -1, True, "5", 1.5])
def test_youtube_max_items_rejects(
    job_data: dict[str, Any], kind: str, field: str, value: str, bad: object
) -> None:
    errors = _errors(job_data, {"type": kind, field: value, "max_items": bad})

    assert [loc for loc, _ in errors] == [("sources", 0, kind, "max_items")]


@pytest.mark.parametrize(("kind", "field", "value"), YT_KINDS)
def test_youtube_max_age_days_accepts_one(
    job_data: dict[str, Any], kind: str, field: str, value: str
) -> None:
    job_data["sources"] = [{"type": kind, field: value, "max_age_days": 1}]

    JobConfig.model_validate(job_data)


@pytest.mark.parametrize(("kind", "field", "value"), YT_KINDS)
@pytest.mark.parametrize("bad", [0, -1, 1.5, "7", True])
def test_youtube_max_age_days_rejects(
    job_data: dict[str, Any], kind: str, field: str, value: str, bad: object
) -> None:
    errors = _errors(job_data, {"type": kind, field: value, "max_age_days": bad})

    assert [loc for loc, _ in errors] == [("sources", 0, kind, "max_age_days")]
