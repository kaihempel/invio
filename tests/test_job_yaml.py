"""Tests for YAML loading, error reporting, and saving of job files."""

import copy
import itertools
import os
import pickle
import re
import stat
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from invio.config import job as job_module
from invio.config.job import (
    JobConfig,
    JobConfigError,
    JobYamlLoader,
    RssSource,
    SitemapSource,
    WebSource,
    YoutubeChannelSource,
    YoutubePlaylistSource,
    clean_keywords,
    dump_yaml,
    dump_yaml_data,
    job_json_schema,
    load_yaml,
    loads_yaml,
    write_yaml,
)
from tests.job_helpers import BAD_JOB, EXAMPLE

MINIMAL = """\
schedule: {{frequency: daily, time: {time}, timezone: Europe/Berlin}}
notification: {{to: [a@example.com], subject: "{subject}", send_if_empty: true}}
sources:
  - {{type: rss, url: "https://example.com/feed.xml"}}
search:
  keywords: {{any: {keywords}}}
  semantic_description: x
llm: {{provider: openai, models: {{fast: a, smart: b}}}}
"""


def _write(
    tmp_path: Path, *, time: str = "07:30", subject: str = "s", keywords: str = "[a]"
) -> Path:
    path = tmp_path / "job.yaml"
    path.write_text(MINIMAL.format(time=time, subject=subject, keywords=keywords), encoding="utf-8")
    return path


def test_example_loads_with_five_source_kinds() -> None:
    job = load_yaml(EXAMPLE)

    assert isinstance(job, JobConfig)
    assert [type(s) for s in job.sources] == [
        RssSource,
        WebSource,
        SitemapSource,
        YoutubeChannelSource,
        YoutubePlaylistSource,
    ]
    assert job.schedule.time == "07:30"


def test_load_yaml_accepts_str_and_path() -> None:
    assert load_yaml(str(EXAMPLE)) == load_yaml(EXAMPLE)


def test_unquoted_time_stays_string(tmp_path: Path) -> None:
    assert load_yaml(_write(tmp_path, time="17:30")).schedule.time == "17:30"


def test_yaml11_keywords_stay_strings(tmp_path: Path) -> None:
    job = load_yaml(_write(tmp_path, keywords="[on, yes, no, 2026-10-04]"))

    assert job.search.keywords.any == ["on", "yes", "no", "2026-10-04"]


def test_true_loads_as_bool(tmp_path: Path) -> None:
    assert load_yaml(_write(tmp_path)).notification.send_if_empty is True


def test_umlauts_load_unchanged(tmp_path: Path) -> None:
    assert load_yaml(_write(tmp_path, subject="Grüße Ärger")).notification.subject == "Grüße Ärger"


def _load_text(tmp_path: Path, text: str) -> JobConfigError:
    path = tmp_path / "job.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(JobConfigError) as info:
        load_yaml(path)
    return info.value


def test_undecodable_file(tmp_path: Path) -> None:
    path = tmp_path / "job.yaml"
    path.write_bytes(b"\xff\xfe\x00bad")

    with pytest.raises(JobConfigError, match="cannot read job file"):
        load_yaml(path)


def test_invalid_explicit_tag_value(tmp_path: Path) -> None:
    err = _load_text(tmp_path, "a: !!int 08x\n")

    assert "invalid YAML in job file" in str(err)


def test_invalid_source_url_location(tmp_path: Path) -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "https://example.net/sitemap.xml", "ftp://example.net/sitemap.xml"
    )

    err = _load_text(tmp_path, text)

    assert len(err.errors) == 1
    assert err.errors[0].startswith("sources[2].url:")


def test_unknown_key_line(tmp_path: Path) -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace("fallback_provider", "frequncy")

    err = _load_text(tmp_path, text)

    assert "llm.frequncy: Extra inputs are not permitted" in err.errors


@pytest.mark.parametrize("as_str", [True, False])
def test_error_path_is_path(tmp_path: Path, as_str: bool) -> None:
    path = tmp_path / "job.yaml"
    path.write_text("- x\n", encoding="utf-8")

    with pytest.raises(JobConfigError) as info:
        load_yaml(str(path) if as_str else path)

    assert info.value.path == path
    assert isinstance(info.value.path, Path)
    assert str(info.value).startswith(f"invalid job file {path}:\n  ")


def test_field_and_cross_field_errors_in_different_sections(tmp_path: Path) -> None:
    text = (
        EXAMPLE.read_text(encoding="utf-8")
        .replace("min_relevance: 0.6", "min_relevance: 1.5")
        .replace("fallback_provider: anthropic", "fallback_provider: openai")
    )

    err = _load_text(tmp_path, text)

    assert any(e.startswith("search.min_relevance:") for e in err.errors)
    assert "llm: fallback_provider must differ from provider" in err.errors


def test_two_pass_cross_field_hidden_by_field_error(tmp_path: Path) -> None:
    text = (
        EXAMPLE.read_text(encoding="utf-8")
        .replace("time: 07:30", 'time: "25:00"')
        .replace("  weekday: monday\n", "")
    )

    err = _load_text(tmp_path, text)

    assert [e.split(":")[0] for e in err.errors] == ["schedule.time"]


def test_never_leaks_validation_error(tmp_path: Path) -> None:
    err = _load_text(tmp_path, "schedule: 1\n")

    assert isinstance(err, JobConfigError)
    assert err.errors[0].startswith("schedule:")


def _roundtrip(job: JobConfig, tmp_path: Path) -> JobConfig:
    out = tmp_path / "out.yaml"
    write_yaml(job, out)
    return load_yaml(out)


def test_roundtrip_example(tmp_path: Path) -> None:
    job = load_yaml(EXAMPLE)

    assert _roundtrip(job, tmp_path) == job


def test_roundtrip_monthly_and_daily(job_data: dict[str, Any], tmp_path: Path) -> None:
    monthly = copy.deepcopy(job_data)
    monthly["schedule"] = {
        "frequency": "monthly",
        "time": "17:30",
        "day_of_month": 31,
        "timezone": "UTC",
    }
    daily = copy.deepcopy(job_data)
    daily["schedule"] = {"frequency": "daily", "time": "00:00", "timezone": "UTC"}

    for data in (monthly, daily):
        job = JobConfig.model_validate(data)
        assert _roundtrip(job, tmp_path) == job


def test_roundtrip_web_source_with_all_keys(job_data: dict[str, Any], tmp_path: Path) -> None:
    job_data["sources"] = [
        {
            "type": "web",
            "url": "https://example.org/news",
            "name": "News",
            "selector": "main .post-list > li:not(.ad)",
            "mode": "links",
            "url_pattern": r"^https://example\.org/news/\d{4}/",
            "render": "js",
            "wait_for": ".post-list li",
        }
    ]
    job = JobConfig.model_validate(job_data)

    assert _roundtrip(job, tmp_path) == job
    text = dump_yaml(job)
    for key in ("selector:", "mode: links", "url_pattern:", "render: js", "wait_for:"):
        assert key in text


def test_roundtrip_tricky_keywords(job_data: dict[str, Any], tmp_path: Path) -> None:
    job_data["search"]["keywords"] = {
        "any": ["1e3", "tRuE", "07", "on", "2026-10-04", "null"],
        "all": ["~", "1.5", "x"],
    }
    job = JobConfig.model_validate(job_data)

    reloaded = _roundtrip(job, tmp_path)

    assert reloaded == job
    assert reloaded.search.keywords.any == ["1e3", "tRuE", "07", "on", "2026-10-04", "null"]


def test_dump_key_order(job_data: dict[str, Any]) -> None:
    dumped = yaml.safe_load(dump_yaml(JobConfig.model_validate(job_data)))

    assert list(dumped) == [
        "schema_version",
        "language",
        "schedule",
        "notification",
        "sources",
        "search",
        "llm",
        "limits",
        "archive",
    ]


def test_dump_includes_defaults_and_nulls(job_data: dict[str, Any]) -> None:
    job_data["schedule"] = {
        "frequency": "monthly",
        "time": "07:30",
        "day_of_month": 5,
        "timezone": "UTC",
    }

    dumped = yaml.safe_load(dump_yaml(JobConfig.model_validate(job_data)))

    assert dumped["notification"]["send_if_empty"] is False
    assert dumped["schedule"]["weekday"] is None
    assert dumped["sources"][0]["name"] is None
    assert dumped["limits"] == {
        "max_items_per_source": 20,
        "max_items_per_run": 100,
        "baseline_items": 10,
        "max_items_in_notification": 20,
        "max_llm_tokens_per_run": 200000,
    }


def test_dump_unicode_unescaped(job_data: dict[str, Any]) -> None:
    job_data["notification"]["subject"] = "Grüße"

    assert "Grüße" in dump_yaml(JobConfig.model_validate(job_data))


def test_dump_writes_a_multi_line_string_as_a_block_scalar(job_data: dict[str, Any]) -> None:
    data = copy.deepcopy(job_data)
    data["search"]["semantic_description"] = "Relevant: a.\nNot relevant: b."

    text = dump_yaml(JobConfig.model_validate(data))

    assert "semantic_description: |-\n    Relevant: a.\n    Not relevant: b.\n" in text
    assert loads_yaml(text).search.semantic_description == "Relevant: a.\nNot relevant: b."


def test_dump_yaml_data_keeps_the_given_key_order() -> None:
    assert dump_yaml_data({"b": 1, "a": "x\ny"}) == "b: 1\na: |-\n  x\n  y\n"


# --- clean_keywords --------------------------------------------------------------------------


def test_clean_keywords_splits_strips_and_drops_empty_and_duplicate_entries() -> None:
    lists, notes = clean_keywords(["a, b", " A ", "", ","], ["c"], [])

    assert lists == {"any": ["a", "b"], "all": ["c"], "exclude": []}
    assert notes == [
        "'a, b' split at commas into separate keywords",
        "',' split at commas into separate keywords",
    ]


def test_clean_keywords_drops_excludes_that_are_includes() -> None:
    lists, notes = clean_keywords(["a"], ["B"], ["b", "A", "c"], note_splits=False)

    assert lists["exclude"] == ["c"]
    assert notes == [
        "'b' removed from exclude: it is also an include keyword",
        "'A' removed from exclude: it is also an include keyword",
    ]


def test_clean_keywords_is_idempotent() -> None:
    lists, _ = clean_keywords(["x, y", "Y"], [], ["x", "z"])

    again, notes = clean_keywords(lists["any"], lists["all"], lists["exclude"])

    assert again == lists and notes == []


def test_dump_time_quoted(job_data: dict[str, Any]) -> None:
    job_data["schedule"]["time"] = "17:30"

    text = dump_yaml(JobConfig.model_validate(job_data))

    assert "'17:30'" in text
    assert yaml.load(text, Loader=JobYamlLoader)["schedule"]["time"] == "17:30"


def test_comments_lost_on_save(tmp_path: Path) -> None:
    source = tmp_path / "in.yaml"
    source.write_text("# my comment\n" + EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "out.yaml"

    write_yaml(load_yaml(source), out)

    assert "# my comment" not in out.read_text(encoding="utf-8")


def test_weekday_saved_lowercase(job_data: dict[str, Any]) -> None:
    job_data["schedule"]["weekday"] = "MONDAY"

    dumped = yaml.safe_load(dump_yaml(JobConfig.model_validate(job_data)))

    assert dumped["schedule"]["weekday"] == "monday"


def test_global_safe_loader_untouched() -> None:
    assert yaml.safe_load("on") is True
    assert yaml.safe_load("17:30") == 1050


def test_error_pickle_roundtrip() -> None:
    err = JobConfigError(Path("x.yaml"), ["a: b", "c: d"])

    copy_ = pickle.loads(pickle.dumps(err))

    assert copy_.path == err.path
    assert copy_.errors == err.errors
    assert str(copy_) == str(err)


def test_bom_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "bom.yaml"
    path.write_bytes(b"\xef\xbb\xbf" + EXAMPLE.read_bytes())

    assert load_yaml(path) == load_yaml(EXAMPLE)


def test_missing_type_and_non_mapping_source(tmp_path: Path) -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    text = text.replace(
        "  - type: web\n    url: https://example.org/news", "  - url: https://x.org/"
    )
    text = text.replace("  - type: sitemap\n", "  - just-a-string\n  - type: sitemap\n")

    err = _load_text(tmp_path, text)

    assert any(e.startswith("sources[1]: ") for e in err.errors)
    assert any(e.startswith("sources[2]: ") for e in err.errors)


def test_write_yaml_leaves_no_temp_files(tmp_path: Path) -> None:
    out = tmp_path / "out.yaml"

    write_yaml(load_yaml(EXAMPLE), out)

    assert [p.name for p in tmp_path.iterdir()] == ["out.yaml"]


def test_write_yaml_returns_written_text(tmp_path: Path) -> None:
    out = tmp_path / "out.yaml"

    text = write_yaml(load_yaml(EXAMPLE), out)

    assert text == out.read_text(encoding="utf-8") == dump_yaml(load_yaml(EXAMPLE))


def test_write_yaml_missing_directory_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        write_yaml(load_yaml(EXAMPLE), tmp_path / "nope" / "out.yaml")


# --- Property-style round-trips (SC-003, FR-024, FR-025) -------------------------------------

# Strings that some YAML reader (1.1 or 1.2) would resolve to a non-string scalar.
YAML_LOOKALIKES = [
    *("true", "True", "TRUE", "false", "yes", "No", "ON", "off", "y", "n", "Y", "N"),
    *("null", "Null", "NULL", "~", "<<", "="),
    *("0", "-0", "+1", "07", "010", "0o17", "0x1F", "0b101", "1_000", "190:20:30"),
    *("1.5", "1.", ".5", "-.5", "+.5", "-.5e+3", "+.5E-2", "1e3", "1.0e+3", "6.8523015e+5"),
    *(".inf", "-.inf", "+.Inf", ".NaN", ".nan"),
    *("2026-10-04", "2026-10-04T07:30:00Z", "2026-10-04 07:30:00 +02:00", "07:30", "17:30"),
    *("[a]", "{a: 1}", "a: b", "- x", "? q", "#c", "&x", "*x", "!x", "%x", "@x", "`x", "|", ">"),
    *("'", '"', "a'b\"c", "a #b", "a\\b"),
]

UNICODE_TEXTS = [
    "Grüße Ärger ß",
    "日本語 😀",
    "a\x85b",  # NEL: PyYAML folds it to a space inside single quotes when allow_unicode=True
    "a\u2028b\u2029c",
    "a\tb",
    "line one\nline two",
    "zero\u200bwidth",
    "\ufeffbom-inside",
]

TEXT_FIELDS: list[tuple[str, Any]] = [
    ("notification.subject", lambda j: j.notification.subject),
    ("search.semantic_description", lambda j: j.search.semantic_description),
    ("search.keywords.any", lambda j: j.search.keywords.any[0]),
    ("search.keywords.exclude", lambda j: j.search.keywords.exclude[0]),
    ("llm.models.fast", lambda j: j.llm.models.fast),
    ("sources.name", lambda j: j.sources[0].name),
]


def _with_text(job_data: dict[str, Any], field: str, value: str) -> JobConfig:
    if field.startswith("search.keywords."):
        job_data["search"]["keywords"] = {field.rsplit(".", 1)[1]: [value]}
    elif field == "sources.name":
        job_data["sources"][0]["name"] = value
    else:
        *parents, last = field.split(".")
        node = job_data
        for key in parents:
            node = node[key]
        node[last] = value
    return JobConfig.model_validate(job_data)


@pytest.mark.parametrize(("field", "read"), TEXT_FIELDS, ids=[f for f, _ in TEXT_FIELDS])
@pytest.mark.parametrize("value", YAML_LOOKALIKES + UNICODE_TEXTS)
def test_roundtrip_text_fields(
    job_data: dict[str, Any], tmp_path: Path, field: str, read: Any, value: str
) -> None:
    job = _with_text(job_data, field, value)

    reloaded = _roundtrip(job, tmp_path)

    assert reloaded == job
    assert read(reloaded) == value.strip()


def test_roundtrip_generated_number_like_keywords(job_data: dict[str, Any], tmp_path: Path) -> None:
    """Every string of length <= 3 over a number-ish alphabet survives save + load as a string."""
    alphabet = "+-.0159eE_:x"
    words = ["".join(t) for n in (1, 2, 3) for t in itertools.product(alphabet, repeat=n)]
    job_data["search"]["keywords"] = {"any": words}
    job = JobConfig.model_validate(job_data)

    reloaded = _roundtrip(job, tmp_path)

    mismatched = [
        (a, b) for a, b in zip(words, reloaded.search.keywords.any, strict=True) if a != b
    ]
    assert mismatched == []


def _variant(job_data: dict[str, Any], **sections: Any) -> dict[str, Any]:
    data = copy.deepcopy(job_data)
    for key, value in sections.items():
        data[key] = value
    return data


SCHEDULES = [
    {"frequency": "daily", "time": "00:00", "timezone": "UTC"},
    {"frequency": "daily", "time": "23:59", "timezone": "America/Argentina/Buenos_Aires"},
    {"frequency": "weekly", "time": "12:00", "weekday": "SUNDAY", "timezone": "Asia/Kolkata"},
    {"frequency": "weekly", "time": "09:05", "weekday": "Wednesday", "timezone": "Etc/GMT+12"},
    {"frequency": "monthly", "time": "17:30", "day_of_month": 1, "timezone": "Europe/Berlin"},
    {"frequency": "monthly", "time": "06:00", "day_of_month": 31, "timezone": "Pacific/Chatham"},
]
SOURCE_SETS = [
    [{"type": "rss", "url": "https://example.com/feed.xml"}],
    [{"type": "web", "url": "http://example.com", "name": "Root", "enabled": False}],
    [{"type": "sitemap", "url": "https://user:pw@example.com:8443/a/b?q=1&r=ä#frag"}],
    [
        {"type": "youtube_channel", "channel_id": "UC-_x", "name": "Kanal Ü"},
        {"type": "youtube_playlist", "playlist_id": "PL123", "enabled": False},
        {"type": "rss", "url": "https://xn--bcher-kva.example/feed"},
        {"type": "web", "url": "https://bücher.example/news"},
    ],
]
SEARCHES = [
    {"semantic_description": "x"},
    {"semantic_description": "Agenten", "min_relevance": 0},
    {"semantic_description": "y", "min_relevance": 1, "keywords": {"all": ["a", "a"]}},
    {"semantic_description": "z", "min_relevance": 0.1 + 0.2},
    {"semantic_description": "z", "min_relevance": 1e-7},
    {"semantic_description": "z", "min_relevance": 5e-324},
]
LLMS = [
    {"provider": p, "models": {"fast": "f", "smart": "s"}, "fallback_provider": fb}
    for p, fb in [("mistral", None), ("ollama", "openai"), ("google", "anthropic")]
]
LIMITS = [
    None,
    {"max_items_per_source": 1},
    {
        "max_items_per_source": 1,
        "max_items_per_run": 1,
        "max_items_in_notification": 999999,
        "max_llm_tokens_per_run": 2**62,
    },
]
NOTIFICATIONS = [
    {"to": ["a@example.com"], "subject": "s"},
    {"to": ["a@example.com", "a@example.com", "b.c+tag@sub.example.org"], "subject": "{date}"},
    {"to": ["x@example.de"], "subject": "Grüße \u2013 {date}", "send_if_empty": True},
]

CONFIG_VARIANTS = [
    *({"schedule": s} for s in SCHEDULES),
    *({"sources": s} for s in SOURCE_SETS),
    *({"search": s} for s in SEARCHES),
    *({"llm": s} for s in LLMS),
    *({"limits": s} for s in LIMITS if s is not None),
    *({"notification": s} for s in NOTIFICATIONS),
    {"schema_version": 1},
]


@pytest.mark.parametrize("sections", CONFIG_VARIANTS)
def test_roundtrip_config_variants(
    job_data: dict[str, Any], tmp_path: Path, sections: dict[str, Any]
) -> None:
    job = JobConfig.model_validate(_variant(job_data, **sections))

    assert _roundtrip(job, tmp_path) == job


@pytest.mark.parametrize("sections", CONFIG_VARIANTS)
def test_dump_is_idempotent(job_data: dict[str, Any], sections: dict[str, Any]) -> None:
    job = JobConfig.model_validate(_variant(job_data, **sections))
    text = dump_yaml(job)

    again = JobConfig.model_validate(yaml.load(text, Loader=JobYamlLoader))

    assert dump_yaml(again) == text


@pytest.mark.parametrize("sections", CONFIG_VARIANTS)
def test_saved_form_validates_against_schema(
    job_data: dict[str, Any], sections: dict[str, Any]
) -> None:
    text = dump_yaml(JobConfig.model_validate(_variant(job_data, **sections)))

    jsonschema.validate(yaml.load(text, Loader=JobYamlLoader), job_json_schema())


def test_saved_file_is_readable_by_yaml11_tools(job_data: dict[str, Any]) -> None:
    job_data["search"]["keywords"] = {"any": YAML_LOOKALIKES}
    job = JobConfig.model_validate(job_data)

    assert JobConfig.model_validate(yaml.safe_load(dump_yaml(job))) == job


def test_omitted_defaults_written_to_file(tmp_path: Path) -> None:
    out = tmp_path / "out.yaml"

    write_yaml(load_yaml(_write(tmp_path)), out)
    saved = yaml.load(out.read_text(encoding="utf-8"), Loader=JobYamlLoader)

    assert saved["schema_version"] == 1
    assert saved["schedule"]["weekday"] is None
    assert saved["schedule"]["day_of_month"] is None
    assert saved["search"]["keywords"] == {"any": ["a"], "all": [], "exclude": []}
    assert saved["search"]["min_relevance"] == 0.6
    assert saved["llm"]["fallback_provider"] is None
    assert saved["sources"] == [
        {
            "type": "rss",
            "url": "https://example.com/feed.xml",
            "name": None,
            "enabled": True,
            "max_age_days": None,
        }
    ]
    assert saved["limits"] == {
        "max_items_per_source": 20,
        "max_items_per_run": 100,
        "baseline_items": 10,
        "max_items_in_notification": 20,
        "max_llm_tokens_per_run": 200000,
    }


def test_dump_nested_key_order() -> None:
    dumped = yaml.load(dump_yaml(load_yaml(EXAMPLE)), Loader=JobYamlLoader)

    assert list(dumped["schedule"]) == ["frequency", "time", "weekday", "day_of_month", "timezone"]
    assert list(dumped["notification"]) == ["to", "subject", "send_if_empty"]
    assert [list(s) for s in dumped["sources"]] == [
        ["type", "url", "name", "enabled", "max_age_days"],
        ["type", "url", "name", "enabled", "selector", "mode", "url_pattern", "render", "wait_for"],
        ["type", "url", "name", "enabled", "url_pattern", "max_age_days"],
        ["type", "channel_id", "name", "enabled", "max_age_days", "max_items"],
        ["type", "playlist_id", "name", "enabled", "max_age_days", "max_items"],
    ]
    assert list(dumped["search"]) == ["keywords", "semantic_description", "min_relevance"]
    assert list(dumped["search"]["keywords"]) == ["any", "all", "exclude"]
    assert list(dumped["llm"]) == ["provider", "models", "fallback_provider"]
    assert list(dumped["llm"]["models"]) == ["fast", "smart"]
    assert list(dumped["limits"]) == [
        "max_items_per_source",
        "max_items_per_run",
        "baseline_items",
        "max_items_in_notification",
        "max_llm_tokens_per_run",
    ]


def test_write_yaml_overwrites_existing_file(tmp_path: Path) -> None:
    out = tmp_path / "out.yaml"
    out.write_text("stale: true\n", encoding="utf-8")
    job = load_yaml(EXAMPLE)

    write_yaml(job, out)

    assert load_yaml(out) == job
    assert out.read_bytes().decode("utf-8") == dump_yaml(job)


# --- YAML-level strict types (FR-002a) -------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "location"),
    [
        ("send_if_empty: false", "send_if_empty: yes", "notification.send_if_empty"),
        ("send_if_empty: false", "send_if_empty: on", "notification.send_if_empty"),
        ("send_if_empty: false", "send_if_empty: 0", "notification.send_if_empty"),
        ("    enabled: false", "    enabled: off", "sources[2].enabled"),
        ("min_relevance: 0.6", 'min_relevance: "0.6"', "search.min_relevance"),
        ("min_relevance: 0.6", "min_relevance: true", "search.min_relevance"),
        ("min_relevance: 0.6", "min_relevance: .inf", "search.min_relevance"),
        ("max_items_per_run: 100", 'max_items_per_run: "100"', "limits.max_items_per_run"),
        ("max_items_per_run: 100", "max_items_per_run: 0x64", "limits.max_items_per_run"),
        ("max_items_per_run: 100", "max_items_per_run: 1_000", "limits.max_items_per_run"),
        ("max_items_per_run: 100", "max_items_per_run: 100.0", "limits.max_items_per_run"),
        ("max_items_per_run: 100", "max_items_per_run: 1e3", "limits.max_items_per_run"),
        ("schema_version: 1", "schema_version: 01", "schema_version"),
        ("schema_version: 1", "schema_version: 2", "schema_version"),
    ],
)
def test_yaml_values_not_coerced(tmp_path: Path, old: str, new: str, location: str) -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    assert old in text

    err = _load_text(tmp_path, text.replace(old, new, 1))

    assert [e.split(": ", 1)[0] for e in err.errors] == [location]


@pytest.mark.parametrize(("raw", "value"), [("0", 0.0), ("1", 1.0), (".5", 0.5), ("5e-1", None)])
def test_yaml_min_relevance_numbers(tmp_path: Path, raw: str, value: float | None) -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "min_relevance: 0.6", f"min_relevance: {raw}"
    )
    path = tmp_path / "job.yaml"
    path.write_text(text, encoding="utf-8")

    if value is None:  # no dot: a string under YAML-1.2-core-like rules, hence rejected
        with pytest.raises(JobConfigError):
            load_yaml(path)
    else:
        assert load_yaml(path).search.min_relevance == value


# --- Error message contract (FR-027, SC-002, SC-007; contracts/job-file.md) -----------------

ERROR_LINE = re.compile(r"^[a-z_]+(?:\.[a-z_]+|\[\d+\])*: \S[^\n]*$")


def test_error_str_layout(tmp_path: Path) -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace("time: 07:30", "time: 7:5")
    path = tmp_path / "job.yaml"
    path.write_text(text.replace("timezone: Europe/Berlin", "timezone: Europe/Atlantis"), "utf-8")

    with pytest.raises(JobConfigError) as info:
        load_yaml(path)

    header, *lines = str(info.value).split("\n")
    assert header == f"invalid job file {path}:"
    assert lines == [f"  {e}" for e in info.value.errors]
    assert [e.split(": ", 1)[0] for e in info.value.errors] == [
        "schedule.time",
        "schedule.timezone",
    ]


def test_error_str_without_path() -> None:
    assert str(JobConfigError(None, ["a: b", "c"])) == "invalid job file:\n  a: b\n  c"


def test_all_field_errors_reported_in_one_pass(tmp_path: Path) -> None:
    err = _load_text(tmp_path, BAD_JOB)

    assert [e.split(": ", 1)[0] for e in err.errors] == [
        "schedule.time",
        "schedule.timezone",
        "notification.to[0]",
        "sources",
        "search.semantic_description",
        "search.min_relevance",
        "llm.frequncy",
    ]
    for line in err.errors:
        assert ERROR_LINE.match(line), line
        assert "Value error" not in line
        assert "errors.pydantic.dev" not in line
        assert "not-an-email" not in line.split(": ", 1)[0]


def test_cross_field_errors_surface_on_second_pass(tmp_path: Path) -> None:
    fixed = (
        BAD_JOB.replace('"25:00"', '"07:30"')
        .replace("Europe/Atlantis", "UTC")
        .replace("not-an-email", "a@example.com")
        .replace("sources: []", "sources: [{type: rss, url: 'https://example.com/f'}]")
        .replace('" ", min_relevance: 1.5', "x, min_relevance: 0.5")
        .replace(", frequncy: 1", "")
    )

    err = _load_text(tmp_path, fixed)

    assert err.errors == [
        "schedule: weekday is required when frequency is 'weekly'",
        "llm: fallback_provider must differ from provider",
    ]


@pytest.mark.parametrize(
    ("schedule", "message"),
    [
        ("{frequency: weekly, time: 07:30, timezone: UTC}", "weekday is required when frequency"),
        (
            "{frequency: monthly, time: 07:30, timezone: UTC}",
            "day_of_month is required when frequency",
        ),
        (
            "{frequency: daily, time: 07:30, weekday: Monday, timezone: UTC}",
            "weekday is only allowed when frequency",
        ),
        (
            "{frequency: daily, time: 07:30, day_of_month: 3, timezone: UTC}",
            "day_of_month is only allowed when frequency",
        ),
    ],
)
def test_cross_field_schedule_line(tmp_path: Path, schedule: str, message: str) -> None:
    text = MINIMAL.format(time="07:30", subject="s", keywords="[a]").replace(
        "schedule: {frequency: daily, time: 07:30, timezone: Europe/Berlin}",
        f"schedule: {schedule}",
    )

    err = _load_text(tmp_path, text)

    assert len(err.errors) == 1
    assert err.errors[0].startswith(f"schedule: {message}")


@pytest.mark.parametrize(
    ("old", "new", "line"),
    [
        (
            'channel_id: "@somechannel"',
            'channel_id: "@somechannel"\n    url: https://youtube.com/c/x',
            "sources[3].url: Extra inputs are not permitted",
        ),
        (
            "playlist_id: PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "playlist_id: PLx\n    foo: 1",
            "sources[4].foo: Extra inputs are not permitted",
        ),
        (
            "    - research@example.com",
            "    - research@example.com\n    - bad",
            "notification.to[1]",
        ),
        ("any: [LangGraph, agent]", "any: [LangGraph, '']", "search.keywords.any[1]"),
        ("    fast: gpt-small", "    fast: ''", "llm.models.fast"),
        ("schema_version: 1", "schema_version: 1\nfoo: 1", "foo: Extra inputs are not permitted"),
    ],
)
def test_error_line_location(tmp_path: Path, old: str, new: str, line: str) -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    assert old in text

    err = _load_text(tmp_path, text.replace(old, new, 1))

    assert len(err.errors) == 1
    assert err.errors[0].startswith(line)
    assert ERROR_LINE.match(err.errors[0])


def test_missing_file_message_names_path(tmp_path: Path) -> None:
    path = tmp_path / "nope.yaml"

    with pytest.raises(JobConfigError) as info:
        load_yaml(path)

    assert info.value.errors == [info.value.errors[0]]
    assert info.value.errors[0].startswith(f"cannot read job file {path}: ")


def test_directory_path_is_read_error(tmp_path: Path) -> None:
    with pytest.raises(JobConfigError) as info:
        load_yaml(tmp_path)

    assert info.value.errors[0].startswith(f"cannot read job file {tmp_path}: ")


def test_yaml_error_is_one_line_with_file_and_position(tmp_path: Path) -> None:
    err = _load_text(tmp_path, "a: 1\nb: [unclosed\n")

    assert len(err.errors) == 1
    assert "\n" not in err.errors[0]
    assert err.errors[0].startswith(f"invalid YAML in job file {tmp_path / 'job.yaml'}: ")
    assert re.search(r"\(line \d+, column \d+\)$", err.errors[0])


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "just text\n"])
def test_not_mapping_message_names_file(tmp_path: Path, text: str) -> None:
    err = _load_text(tmp_path, text)

    assert err.errors == [
        f"job file {tmp_path / 'job.yaml'} must contain a mapping at the top level"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "a: 1\na: 2\n",
        "schedule:\n  timezone: Europe/Berlin\n  timezone: UTC\n",
    ],
)
def test_duplicate_keys_rejected(tmp_path: Path, text: str) -> None:
    err = _load_text(tmp_path, text)

    assert "found duplicate key" in err.errors[0]


@pytest.mark.parametrize("text", ["? [a]\n: 1\n", "? {a: 1}\n: 1\n", "schedule:\n  ? [a]\n  : 1\n"])
def test_unhashable_keys_rejected(tmp_path: Path, text: str) -> None:
    err = _load_text(tmp_path, text)

    assert err.errors[0].startswith("invalid YAML in job file")
    assert "found unhashable key" in err.errors[0]
    assert re.search(r"\(line \d+, column \d+\)$", err.errors[0])


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_write_yaml_keeps_existing_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "job.yaml"
    target.write_text("", encoding="utf-8")
    target.chmod(0o644)

    write_yaml(load_yaml(EXAMPLE), target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_write_yaml_new_file_is_private(tmp_path: Path) -> None:
    target = tmp_path / "job.yaml"
    umask = os.umask(0o022)
    try:
        write_yaml(load_yaml(EXAMPLE), target)
    finally:
        os.umask(umask)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_write_yaml_keeps_restrictive_mode(tmp_path: Path) -> None:
    target = tmp_path / "job.yaml"
    target.write_text("", encoding="utf-8")
    target.chmod(0o600)

    write_yaml(load_yaml(EXAMPLE), target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_yaml_leaves_process_umask_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(mask: int) -> int:
        raise AssertionError("write_yaml must not change the process-wide umask")

    monkeypatch.setattr(os, "umask", forbidden)

    write_yaml(load_yaml(EXAMPLE), tmp_path / "job.yaml")


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_write_yaml_follows_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real" / "job.yaml"
    real.parent.mkdir()
    real.write_text("old", encoding="utf-8")
    link = tmp_path / "job.yaml"
    link.symlink_to(real)
    job = load_yaml(EXAMPLE)

    write_yaml(job, link)

    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == dump_yaml(job)
    assert [p.name for p in real.parent.iterdir()] == ["job.yaml"]


def test_write_yaml_failure_removes_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)

    with pytest.raises(OSError, match="disk full"):
        write_yaml(load_yaml(EXAMPLE), tmp_path / "job.yaml")

    assert list(tmp_path.iterdir()) == []


def test_write_yaml_retries_temp_name_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".job.yaml.taken.tmp").write_text("", encoding="utf-8")
    names = iter(["taken", "free"])
    monkeypatch.setattr(job_module.secrets, "token_hex", lambda n: next(names))

    write_yaml(load_yaml(EXAMPLE), tmp_path / "job.yaml")

    assert sorted(p.name for p in tmp_path.iterdir()) == [".job.yaml.taken.tmp", "job.yaml"]


# --- loads_yaml ----------------------------------------------------------------------------


def test_loads_yaml_matches_load_yaml() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")

    assert loads_yaml(text) == load_yaml(EXAMPLE)


def test_loads_yaml_strips_bom() -> None:
    assert loads_yaml("\ufeff" + EXAMPLE.read_text(encoding="utf-8")) == load_yaml(EXAMPLE)


def test_loads_yaml_malformed_yaml_has_position() -> None:
    with pytest.raises(JobConfigError) as info:
        loads_yaml("a: 1\nb: [unclosed\n")

    assert len(info.value.errors) == 1
    assert info.value.errors[0].startswith("invalid YAML: ")
    assert re.search(r"\(line \d+, column \d+\)$", info.value.errors[0])
    assert info.value.path is None


@pytest.mark.parametrize("text", ["", "- a\n", "just text\n"])
def test_loads_yaml_requires_mapping(text: str) -> None:
    with pytest.raises(JobConfigError) as info:
        loads_yaml(text)

    assert info.value.errors == ["job file must contain a mapping at the top level"]


def test_loads_yaml_field_errors_match_load_yaml(tmp_path: Path) -> None:
    file = tmp_path / "bad.yaml"
    file.write_text(BAD_JOB, encoding="utf-8")
    with pytest.raises(JobConfigError) as from_file:
        load_yaml(file)
    with pytest.raises(JobConfigError) as from_text:
        loads_yaml(BAD_JOB)

    assert from_text.value.errors == from_file.value.errors
    assert from_text.value.path is None


def test_loads_yaml_rejects_duplicate_keys() -> None:
    with pytest.raises(JobConfigError) as info:
        loads_yaml("a: 1\na: 2\n")

    assert "found duplicate key" in info.value.errors[0]


def test_loads_yaml_source_is_reflected_in_error_path() -> None:
    with pytest.raises(JobConfigError) as info:
        loads_yaml(BAD_JOB, source=Path("x.yaml"))

    assert info.value.path == Path("x.yaml")
    assert str(info.value).startswith("invalid job file x.yaml:")


def test_loads_yaml_source_keeps_file_messages() -> None:
    with pytest.raises(JobConfigError) as info:
        loads_yaml("a: [x\n", source=Path("x.yaml"))
    assert info.value.errors[0].startswith("invalid YAML in job file x.yaml: ")

    with pytest.raises(JobConfigError) as info:
        loads_yaml("- a\n", source=Path("x.yaml"))
    assert info.value.errors == ["job file x.yaml must contain a mapping at the top level"]


# --- language ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("de", "de"), ("'no'", "no"), ("no", "no"), ('"en"', "en")],
    ids=["de", "no-quoted", "no-bare", "en-double-quoted"],
)
def test_language_roundtrip(tmp_path: Path, raw: str, expected: str) -> None:
    path = tmp_path / "job.yaml"
    path.write_text(
        f"language: {raw}\n" + MINIMAL.format(time="07:30", subject="s", keywords="[a]"),
        encoding="utf-8",
    )
    job = load_yaml(path)
    assert job.language == expected

    reloaded = _roundtrip(job, tmp_path)

    assert reloaded == job
    assert reloaded.language == expected
    # YAML 1.1 readers must not see the boolean false in a saved "no"
    assert yaml.safe_load(dump_yaml(job))["language"] == expected


def test_language_omitted_loads_as_en_and_is_written(tmp_path: Path) -> None:
    job = load_yaml(_write(tmp_path))
    assert job.language == "en"
    assert "\nlanguage: en\n" in dump_yaml(job)


@pytest.mark.parametrize("raw", ["xx", "DE", "deu", "''", "1", "null", "[de]"])
def test_language_invalid_in_file_names_the_field(tmp_path: Path, raw: str) -> None:
    error = _load_text(
        tmp_path, f"language: {raw}\n" + MINIMAL.format(time="07:30", subject="s", keywords="[a]")
    )
    assert any(line.lstrip().startswith("language") for line in str(error).splitlines())
