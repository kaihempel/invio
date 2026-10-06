"""Tests for the ISO 639-1 language table."""

import re

import pytest

from invio.config.languages import ISO_639_1, language_name


def test_codes_are_two_lowercase_letters() -> None:
    assert all(re.fullmatch(r"[a-z]{2}", code) for code in ISO_639_1)


def test_names_are_non_empty() -> None:
    assert all(name.strip() for name in ISO_639_1.values())


def test_language_name_known_codes() -> None:
    assert language_name("de") == "German"
    assert language_name("en") == "English"
    assert language_name("no") == "Norwegian"


@pytest.mark.parametrize("code", ["xx", "DE", "", "deu", "iw", "in", "ji"])
def test_language_name_unknown_code_raises(code: str) -> None:
    with pytest.raises(KeyError):
        language_name(code)


def test_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        ISO_639_1["zz"] = "Nothing"  # type: ignore[index]
