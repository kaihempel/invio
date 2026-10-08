"""Tests for job-name slugs and the job directory choice (path safety, collisions)."""

import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from invio.notify.archive import job_directory, slugify_job_name

SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

HOSTILE_NAMES = [
    "../../etc/x",
    "..",
    ".",
    "../",
    "/etc/passwd",
    "a/b/c",
    "a\\b\\c",
    "..\\..\\windows",
    "C:\\Windows\\System32",
    "CON",
    "nul",
    "aux.txt",
    "name\x00with-nul",
    "ctrl\x01\x02\x1f chars",
    "line\nbreak",
    "tab\tname",
    "日本語/ニュース",
    "ñandú ÅÄÖ",
    "\u202eevil",
    "a" * 300,
    "x/" * 200,
    "   ",
    "",
    "~root",
    "$(rm -rf /)",
    "a;b|c&d",
    "\ud800lone-surrogate",
    ".hidden/../..",
]


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Weekly AI News", "weekly-ai-news"),
        ("../../etc/x", "etc-x"),
        ("日本語/ニュース", "job"),
        ("!!!", "job"),
        ("", "job"),
        ("Crème brûlée", "creme-brulee"),
        ("  --a--  ", "a"),
    ],
)
def test_slugify_examples(name: str, slug: str) -> None:
    assert slugify_job_name(name) == slug


def test_slugify_is_bounded_and_has_no_trailing_dash() -> None:
    slug = slugify_job_name("word " * 100)

    assert len(slug) <= 48
    assert not slug.endswith("-")
    assert SLUG.match(slug)
    assert len(slugify_job_name("a" * 300)) <= 48


@given(st.text())
def test_slugify_always_yields_a_safe_slug(name: str) -> None:
    slug = slugify_job_name(name)

    assert SLUG.match(slug)
    assert len(slug) <= 48


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_hostile_names_stay_inside_the_archive_dir(tmp_path: Path, name: str) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    directory = job_directory(archive_dir, name)

    assert directory.parent == archive_dir
    assert SLUG.match(directory.name)
    assert directory.resolve().is_relative_to(archive_dir.resolve())


def test_hostile_name_list_is_long_enough() -> None:
    assert len(HOSTILE_NAMES) >= 20


def test_same_name_always_gets_the_same_directory(tmp_path: Path) -> None:
    assert job_directory(tmp_path, "Weekly News") == job_directory(tmp_path, "Weekly News")


def test_colliding_slugs_get_distinct_directories(tmp_path: Path) -> None:
    first = job_directory(tmp_path, "A b")
    first.mkdir()
    (first / ".job-name").write_text("A b", encoding="utf-8")

    second = job_directory(tmp_path, "a-b")

    assert first.name == "a-b"
    assert second != first
    assert re.fullmatch(r"a-b-[0-9a-f]{8}", second.name)
    # Stable once both exist, and the first name keeps its directory.
    second.mkdir()
    (second / ".job-name").write_text("a-b", encoding="utf-8")
    assert job_directory(tmp_path, "A b") == first
    assert job_directory(tmp_path, "a-b") == second


def test_directory_without_marker_and_content_is_reused(tmp_path: Path) -> None:
    (tmp_path / "news").mkdir()

    assert job_directory(tmp_path, "News").name == "news"


def test_foreign_directory_without_marker_is_not_taken_over(tmp_path: Path) -> None:
    (tmp_path / "news").mkdir()
    (tmp_path / "news" / "other.txt").write_text("x", encoding="utf-8")

    assert job_directory(tmp_path, "News").name != "news"
