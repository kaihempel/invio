"""Tests for ``invio.sources.extract``: article text, metadata, fallback, limits and errors."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from trafilatura.settings import Document

from invio.sources import extract
from invio.sources.extract import (
    MAX_CHARS,
    MIN_CHARS,
    ExtractedText,
    ExtractionError,
    extract_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "articles"
URL = "https://example.org/lokales/radwege"

ARTICLE_PARAGRAPHS = (
    "Der Stadtrat von Musterstadt hat am Dienstagabend mit großer Mehrheit beschlossen,",
    "Nach Angaben der Verwaltung kostet das Vorhaben rund 18 Millionen Euro.",
    "Die Händlervereinigung befürchtet, dass durch den Wegfall von rund 300 Parkplätzen",
    "Bürgermeisterin Sabine Beispiel sprach nach der Abstimmung",
)
BOILERPLATE = (
    "Wir verwenden Cookies",  # cookie banner
    "Alle akzeptieren",
    "Jetzt abonnieren",  # navigation
    "Wirtschaft",
    "Meistgelesen",  # sidebar
    "Sturmwarnung",
    "Newsletter",
    "Impressum",  # footer
    "Datenschutzerklärung",
    "Alle Rechte vorbehalten",
    "dataLayer",  # script
    "position: fixed",  # style
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def page(body: str, *, head: str = "", lang: str | None = None) -> str:
    lang_attr = f' lang="{lang}"' if lang is not None else ""
    return f"<!DOCTYPE html><html{lang_attr}><head>{head}</head><body>{body}</body></html>"


@pytest.fixture
def no_trafilatura(monkeypatch: pytest.MonkeyPatch) -> None:
    """trafilatura finds nothing: every extraction takes the fallback path."""
    monkeypatch.setattr(extract.trafilatura, "bare_extraction", lambda *a, **k: None)


def trafilatura_returns(monkeypatch: pytest.MonkeyPatch, document: Document) -> None:
    monkeypatch.setattr(extract.trafilatura, "bare_extraction", lambda *a, **k: document)


# --- main path -------------------------------------------------------------------------------


def test_news_article_text_is_the_article_without_boilerplate() -> None:
    result = extract_text(fixture("news_article.html"), URL)

    for text in ARTICLE_PARAGRAPHS:
        assert text in result.text
    for text in BOILERPLATE:
        assert text not in result.text
    assert result.truncated is False


def test_news_article_metadata() -> None:
    result = extract_text(fixture("news_article.html"), URL)

    assert result.title == "Stadtrat beschließt Ausbau der Radwege"  # og:title, no site suffix
    assert result.published_at == datetime(2026, 3, 14, tzinfo=UTC)
    assert result.language == "de"


def test_paragraphs_are_lines_with_collapsed_whitespace() -> None:
    text = extract_text(fixture("news_article.html"), URL).text
    lines = text.split("\n")

    assert len(lines) == len(ARTICLE_PARAGRAPHS)
    assert all(line == " ".join(line.split()) and line for line in lines)


def test_page_without_metadata() -> None:
    result = extract_text(fixture("no_metadata.html"), URL)

    assert "Conditional requests let a client ask a server" in result.text
    assert "persist the validators together with the content hash" in result.text
    assert "Archive" not in result.text
    assert result.published_at is None
    assert result.language is None


def test_result_is_frozen() -> None:
    result = extract_text(fixture("news_article.html"), URL)

    with pytest.raises(AttributeError):
        result.text = "changed"  # type: ignore[misc]


# --- too short -------------------------------------------------------------------------------


def test_short_page_raises_extraction_error() -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extract_text(fixture("short_page.html"), URL)

    assert excinfo.value.reason == "too_short"
    assert excinfo.value.url == URL


def test_extraction_error_redacts_the_url() -> None:
    url = "https://user:secret@example.org/a?token=abc#frag"

    with pytest.raises(ExtractionError) as excinfo:
        extract_text(fixture("short_page.html"), url)

    assert excinfo.value.url == "https://example.org/a?token=abc#frag"
    assert str(excinfo.value) == "too_short: https://example.org/a"
    assert "secret" not in str(excinfo.value)


def test_min_chars_can_be_lowered() -> None:
    result = extract_text(fixture("short_page.html"), URL, min_chars=5)

    assert "This page has moved." in result.text


@pytest.mark.parametrize("html", ["", "   ", "<html></html>", "<!-- only a comment -->"])
def test_empty_documents_are_too_short(html: str) -> None:
    with pytest.raises(ExtractionError):
        extract_text(html, URL, min_chars=1)


# --- truncation ------------------------------------------------------------------------------


def test_overlong_text_is_truncated_at_a_word_boundary() -> None:
    full = extract_text(fixture("news_article.html"), URL).text

    result = extract_text(fixture("news_article.html"), URL, max_chars=250)

    assert result.truncated is True
    assert len(result.text) <= 250
    assert full.startswith(result.text)
    assert full[len(result.text)].isspace()


def test_text_at_the_limit_is_not_truncated() -> None:
    full = extract_text(fixture("news_article.html"), URL).text

    result = extract_text(fixture("news_article.html"), URL, max_chars=len(full))

    assert result.text == full
    assert result.truncated is False


def test_cut_that_ends_on_whitespace_keeps_the_last_word(no_trafilatura: None) -> None:
    result = extract_text(page("<p>aaaa bbbb cccc</p>"), URL, max_chars=10, min_chars=1)

    assert result.text == "aaaa bbbb"
    assert result.truncated is True


def test_word_longer_than_the_limit_is_cut_inside(no_trafilatura: None) -> None:
    result = extract_text(page(f"<p>{'x' * 50}</p>"), URL, max_chars=10, min_chars=1)

    assert result.text == "x" * 10
    assert result.truncated is True


# --- fallback --------------------------------------------------------------------------------


def test_fallback_when_trafilatura_finds_nothing(no_trafilatura: None) -> None:
    result = extract_text(fixture("news_article.html"), URL)

    for text in ARTICLE_PARAGRAPHS:
        assert text in result.text
    assert "dataLayer" not in result.text  # non-text elements are still dropped
    assert "position: fixed" not in result.text
    assert result.title == "Stadtrat beschließt Ausbau der Radwege"  # og:title
    assert result.published_at is None  # dates come from trafilatura only
    assert result.language == "de"


def test_fallback_when_trafilatura_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise ValueError("hostile input")

    monkeypatch.setattr(extract.trafilatura, "bare_extraction", explode)

    result = extract_text(fixture("news_article.html"), URL)

    assert ARTICLE_PARAGRAPHS[0] in result.text


def test_fallback_when_trafilatura_returns_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract.trafilatura, "bare_extraction", lambda *a, **k: {"text": "x"})

    result = extract_text(fixture("news_article.html"), URL)

    assert ARTICLE_PARAGRAPHS[0] in result.text


def test_fallback_keeps_trafilatura_metadata_when_its_text_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trafilatura_returns(
        monkeypatch, Document(title="Its Title", text=" \n ", date="2026-01-02", language="en-US")
    )

    result = extract_text(fixture("news_article.html"), URL)

    assert ARTICLE_PARAGRAPHS[0] in result.text
    assert result.title == "Its Title"
    assert result.published_at == datetime(2026, 1, 2, tzinfo=UTC)
    assert result.language == "en"  # trafilatura's language wins over <html lang>


def test_fallback_text_has_one_line_per_block(no_trafilatura: None) -> None:
    html = page(
        "<h1>Head</h1><p>first\n   paragraph <b>bo</b>ld</p><ul><li>one</li><li>two</li></ul>"
        "<p>a<br>b</p><script>evil()</script><!-- note --><p>&nbsp;</p>"
    )

    result = extract_text(html, URL, min_chars=1)

    assert result.text == "Head\nfirst paragraph bold\none\ntwo\na\nb"


def test_fallback_text_is_nfc(no_trafilatura: None) -> None:
    result = extract_text(page("<p>Café</p>"), URL, min_chars=1)

    assert result.text == "Café"


# --- metadata --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        ('<meta property="og:title" content=" OG  Title "><title>Doc</title>', "OG Title"),
        ('<meta property="og:title" content=""><title> Doc\n Title </title>', "Doc Title"),
        ('<meta property="og:title"><title>Doc</title>', "Doc"),
        ("<title>   </title>", None),
        ("", None),
    ],
)
def test_fallback_title(no_trafilatura: None, head: str, expected: str | None) -> None:
    result = extract_text(page("<p>text</p>", head=head), URL, min_chars=1)

    assert result.title == expected


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("de-DE", "de"),
        ("EN_us", "en"),
        (" fr ", "fr"),
        ("zh-Hant-TW", "zh"),
        ("", None),
        ("x", None),
        ("123", None),
        ("deä", None),
        (None, None),
    ],
)
def test_language_from_html_lang(
    no_trafilatura: None, lang: str | None, expected: str | None
) -> None:
    result = extract_text(page("<p>text</p>", lang=lang), URL, min_chars=1)

    assert result.language == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-03-14", datetime(2026, 3, 14, tzinfo=UTC)),
        ("2026-02-30", None),
        ("14.03.2026", None),
        ("", None),
        (None, None),
    ],
)
def test_published_at(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: datetime | None
) -> None:
    trafilatura_returns(monkeypatch, Document(text="Some article text.", date=value))

    result = extract_text(page("<p>x</p>"), URL, min_chars=1)

    assert result.published_at == expected
    assert result.published_at is None or result.published_at.tzinfo is UTC


# --- parameters and hostile input ------------------------------------------------------------


def test_defaults() -> None:
    assert (MAX_CHARS, MIN_CHARS) == (200_000, 200)


@pytest.mark.parametrize(
    ("max_chars", "min_chars"),
    [(0, 1), (-1, 1), (10, 0), (10, -5), (10, 11)],
)
def test_invalid_limits_raise_value_error(max_chars: int, min_chars: int) -> None:
    with pytest.raises(ValueError, match="chars"):
        extract_text(fixture("news_article.html"), URL, max_chars=max_chars, min_chars=min_chars)


@pytest.mark.parametrize(
    "html",
    [
        pytest.param("\x00\x01<<<>>>&&&", id="control-characters"),
        pytest.param("<p>Hallo \ud800 Welt</p>", id="lone-surrogate"),
        pytest.param("<div>" * 50_000 + "deep" + "</div>" * 50_000, id="deep-nesting"),
        pytest.param("<p>unclosed <b><i><table><tr><td>cell", id="unclosed-tags"),
        pytest.param("<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'b'>]><x>&a;</x>", id="xml"),
    ],
)
def test_garbage_does_not_crash(html: str) -> None:
    try:
        result = extract_text(html, URL, min_chars=1)
    except ExtractionError:
        return
    assert isinstance(result, ExtractedText)


def test_large_input_is_extracted_and_truncated() -> None:
    # About 2 MB of text; distinct paragraphs, since trafilatura drops repeated ones.
    paragraphs = "".join(f"<p>Absatz {i}: {'wort ' * 200}</p>" for i in range(2_000))
    html = page(f"<article>{paragraphs}</article>")

    result = extract_text(html, URL)

    assert result.truncated is True
    assert len(result.text) <= MAX_CHARS


@settings(max_examples=50, deadline=None)
@given(st.text())
def test_any_text_gives_a_result_or_extraction_error(html: str) -> None:
    try:
        result = extract_text(html, URL, min_chars=1)
    except ExtractionError:
        return
    assert result.text
    assert "\n\n" not in result.text
