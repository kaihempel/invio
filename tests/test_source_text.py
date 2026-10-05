"""Tests for the text helpers shared by the RSS and web page sources."""

import pytest

from invio.sources.text import TEASER_MAX_CHARS, collapse, html_to_text, teaser


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ""),
        ("  a \n\t b  ", "a b"),
        ("a\u00a0\u00a0b", "a b"),  # no-break space
        ("a\u2003b\u3000c\u200ad", "a b c d"),  # em space, ideographic space, hair space
        ("\r\n\u00a0 ", ""),
    ],
)
def test_collapse(text: str, expected: str) -> None:
    assert collapse(text) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<p>a</p><p>b</p>", " a  b "),
        ("a</p>b", "a b"),
        ("a<br>b<br/>c", "a b c"),
        ("wo<b>rd</b>s", "words"),  # inline markup does not split words
        ("a<script>x()</script>b", "ab"),
        ("a<style>p{}</style>b", "ab"),
        ("a<noscript>enable js</noscript>b", "ab"),
        ("a<noscript><p>x</p></noscript>b", "ab"),
        ("a<template><template>x</template>y</template>b", "ab"),  # nested
        ("a<script/>b", "ab"),  # self-closed: nothing to skip
        ("a</script>b", "ab"),  # stray end tag
        ("a<template><p>t</p></template>b", "ab"),
        ("a<!-- hidden -->b", "ab"),
        ("a &amp; b &#233; &eacute;", "a & b é é"),
    ],
)
def test_html_to_text(html: str, expected: str) -> None:
    assert html_to_text(html) == expected


def test_block_elements_separate_words_after_collapse() -> None:
    assert collapse(html_to_text("<p>a</p><p>b</p>")) == "a b"
    assert collapse(html_to_text("wo<b>rd</b>")) == "word"


@pytest.mark.parametrize("text", ["", "   ", "\n\t\u00a0"])
def test_teaser_of_empty_text_is_none(text: str) -> None:
    assert teaser(text) is None


def test_short_text_is_returned_unchanged_but_collapsed() -> None:
    assert teaser("  Hello \n world ") == "Hello world"
    exact = "x" * TEASER_MAX_CHARS
    assert teaser(exact) == exact


def test_long_text_is_cut_at_a_word_boundary() -> None:
    words = " ".join(f"word{i}" for i in range(200))

    result = teaser(words)

    assert result is not None
    assert len(result) <= TEASER_MAX_CHARS
    assert result.endswith("…")
    assert words.startswith(result.removesuffix("…"))
    assert result.removesuffix("…").split(" ")[-1] in words.split(" ")


def test_long_text_without_spaces_is_cut_hard() -> None:
    assert teaser("x" * 900) == "x" * (TEASER_MAX_CHARS - 1) + "…"


def test_teaser_limit_is_500() -> None:
    assert TEASER_MAX_CHARS == 500
