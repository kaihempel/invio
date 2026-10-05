"""Tests for the pure page functions of ``invio.sources.web``: text, hash, decoding, HTML check."""

import hashlib
import string
import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st

from invio.sources.http import FetchResult
from invio.sources.web import (
    _content_hash,
    _decode,
    _is_html,
    _parse,
    _region_text,
    _regions,
    _title,
)

URL = "https://example.org/page"


def text_of(html: str, selector: str | None = None) -> str:
    return _region_text(_regions(_parse(html), selector, url=URL))


def hash_of(html: str, selector: str | None = None) -> str:
    return _content_hash(text_of(html, selector))


def result(content: bytes, content_type: str | None = None) -> FetchResult:
    headers = {} if content_type is None else {"content-type": content_type}
    return FetchResult(
        url=URL,
        requested_url=URL,
        status=200,
        headers=headers,
        content=content,
        etag=None,
        last_modified=None,
    )


# --- region text -----------------------------------------------------------------------------


def test_body_text_excludes_non_text_content() -> None:
    html = (
        "<html><head><title>T</title><style>p{}</style><script>var x;</script></head><body>"
        "<!-- hidden --><p>Hello</p><script>alert(1)</script><style>b{}</style>"
        "<noscript><p>enable js</p></noscript><template><p>tpl</p></template><p>world</p>"
        "</body></html>"
    )

    assert text_of(html) == "Hello world"


def test_head_content_is_not_part_of_the_body_text() -> None:
    assert text_of("<html><head><title>Title</title></head><body>Body</body></html>") == "Body"


def test_fragment_without_body_is_parsed_into_one() -> None:
    assert text_of("<p>a</p><p>b</p>") == "a b"


def test_empty_document_has_empty_text() -> None:
    assert text_of("") == ""
    assert text_of("<html><body>  \n </body></html>") == ""


def test_nfc_makes_composed_and_decomposed_text_equal() -> None:
    composed = "<p>café</p>"
    decomposed = "<p>café</p>"

    assert text_of(composed) == text_of(decomposed) == "café"
    assert hash_of(composed) == hash_of(decomposed)


def test_whitespace_is_collapsed_including_nbsp() -> None:
    assert text_of("<p>  a \n\t b&nbsp;&nbsp;c </p>") == "a b c"


def test_inline_markup_keeps_words_together_and_blocks_separate_them() -> None:
    assert text_of("<p>wo<b>rd</b>s</p><p>next</p>") == "words next"
    assert text_of("<div>a</div><div>b</div>") == "a b"


def test_entities_are_decoded() -> None:
    assert text_of("<p>a &amp; b &lt;c&gt; &eacute;</p>") == "a & b <c> é"


def test_case_and_punctuation_are_kept() -> None:
    assert text_of("<p>Hello, World!</p>") != text_of("<p>hello world</p>")


def test_selector_picks_matches_in_document_order() -> None:
    html = (
        "<body><p>skip</p><div class=x>one</div><p>skip</p><div class=x>two</div><p>skip</p></body>"
    )

    assert text_of(html, ".x") == "one two"


def test_selector_matching_nothing_raises_selector_not_found() -> None:
    from invio.sources.errors import FetchError

    with pytest.raises(FetchError) as info:
        text_of("<body><p>a</p></body>", ".missing")

    assert info.value.reason == "selector_not_found"
    assert info.value.url == URL


def test_selector_may_match_the_body_itself() -> None:
    assert text_of("<body class=a><p>x</p></body>", "body.a") == "x"


# --- title -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<head><title>  My \n  Title </title></head>", "My Title"),
        ("<head><title>A &amp; B</title></head>", "A & B"),
        ("<head><title></title></head>", URL),
        ("<head><title>   </title></head>", URL),
        ("<body>no title</body>", URL),
        ("<body><svg><title>icon</title></svg></body>", URL),  # only head > title counts
    ],
)
def test_title(html: str, expected: str) -> None:
    assert _title(_parse(html), URL) == expected


# --- hash ------------------------------------------------------------------------------------


def test_content_hash_is_sha256_of_utf8_text() -> None:
    assert _content_hash("héllo") == hashlib.sha256("héllo".encode()).hexdigest()


def test_empty_text_hashes_the_empty_string() -> None:
    assert _content_hash("") == hashlib.sha256(b"").hexdigest()
    assert _content_hash("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# Golden vectors lock the normalization: a change of any rule would change these hashes and so
# make every stored fingerprint look "changed".
GOLDEN = [
    (
        "<html><head><title>T</title><script>x()</script></head><body>"
        "<h1>News</h1><p>First  post</p><!-- c --><p>Second&nbsp;post</p></body></html>",
        "News First post Second post",
    ),
    ("<body><p>café</p></body>", "café"),
    ("<body><b>wo</b>rd <i>x</i></body>", "word x"),
]


@pytest.mark.parametrize(("html", "expected_text"), GOLDEN)
def test_golden_text(html: str, expected_text: str) -> None:
    assert text_of(html) == expected_text


def test_golden_hashes() -> None:
    assert [hash_of(html) for html, _ in GOLDEN] == [
        "bddcb215316450960a08a87d6bb76a211b0600b0688a89aa2178d48af848765a",
        "850f7dc43910ff890f8879c0ed26fe697c93a067ad93a7d50f466a7028a9bf4e",
        "49abda8147f8b3002589b43161fc4b3554f23307cf78eaa7d6cea3d00961b194",
    ]


# --- properties ------------------------------------------------------------------------------

WORD = st.text(alphabet=string.ascii_letters + string.digits + "äöüé", min_size=1, max_size=8)
WORDS = st.lists(WORD, min_size=1, max_size=8)
SPACE = st.text(alphabet=" \t\r\n\u00a0\u2003", max_size=6)


def paragraphs(words: list[str], gap: str = "") -> str:
    return gap.join(f"<p>{word}</p>" for word in words)


@given(WORDS, SPACE)
def test_whitespace_between_block_tags_does_not_change_the_hash(words: list[str], gap: str) -> None:
    assert hash_of(paragraphs(words)) == hash_of(paragraphs(words, gap))


@given(WORDS, st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6))
def test_attributes_do_not_change_the_hash(words: list[str], value: str) -> None:
    plain = paragraphs(words)
    decorated = "".join(
        f'<p class="{value}" data-x="{value}" id="{value}{i}">{w}</p>' for i, w in enumerate(words)
    )

    assert hash_of(plain) == hash_of(decorated)


@given(WORDS)
def test_wrapping_words_in_spans_does_not_change_the_hash(words: list[str]) -> None:
    plain = "<p>" + " ".join(words) + "</p>"
    wrapped = "<p>" + " ".join(f"<span>{w}</span>" for w in words) + "</p>"

    assert hash_of(plain) == hash_of(wrapped)


@given(WORDS, st.text(alphabet=string.ascii_letters + " ", max_size=20))
def test_scripts_styles_and_comments_do_not_change_the_hash(words: list[str], junk: str) -> None:
    plain = paragraphs(words)
    noisy = (
        f"<script>var s = '{junk}';</script><style>.a{{content:'{junk}'}}</style>"
        f"<!-- {junk} -->{paragraphs(words)}<noscript>{junk}</noscript><script>2</script>"
    )

    assert hash_of(plain) == hash_of(noisy)


@given(WORDS, st.data())
def test_changing_a_word_changes_the_hash(words: list[str], data: st.DataObject) -> None:
    index = data.draw(st.integers(0, len(words) - 1))
    replacement = data.draw(WORD.filter(lambda w: unicodedata.normalize("NFC", w) != words[index]))
    changed = [*words[:index], replacement, *words[index + 1 :]]

    assert hash_of(paragraphs(words)) != hash_of(paragraphs(changed))


# --- decoding --------------------------------------------------------------------------------

TEXT = "Grüße aus Köln, café"


def page(text: str = TEXT, head: str = "") -> str:
    return f"<html><head>{head}</head><body><p>{text}</p></body></html>"


def test_default_encoding_is_utf8() -> None:
    assert TEXT in _decode(result(page().encode("utf-8"), "text/html"))
    assert TEXT in _decode(result(page().encode("utf-8")))


def test_header_charset_is_honoured() -> None:
    content = page().encode("iso-8859-1")

    assert "Grüße aus Köln, café" in _decode(result(content, "text/html; charset=ISO-8859-1"))


def test_header_charset_beats_meta_charset() -> None:
    content = page(head='<meta charset="iso-8859-1">').encode("utf-8")

    assert TEXT in _decode(result(content, 'text/html; charset="utf-8"'))


def test_meta_charset_is_used_without_header_charset() -> None:
    content = page(head='<meta charset="iso-8859-1">').encode("iso-8859-1")

    assert "Grüße aus Köln, café" in _decode(result(content, "text/html"))


def test_meta_http_equiv_content_type_is_used() -> None:
    meta = '<meta http-equiv="Content-Type" content="text/html; charset=ISO-8859-1">'
    content = page(head=meta).encode("iso-8859-1")

    assert "Grüße aus Köln, café" in _decode(result(content, "text/html"))


def test_meta_beyond_the_first_1024_bytes_is_ignored() -> None:
    padding = "<!--" + "x" * 1100 + "-->"
    content = page(head=padding + '<meta charset="iso-8859-1">').encode("utf-8")

    assert TEXT in _decode(result(content, "text/html"))


def test_bom_beats_header_and_meta() -> None:
    content = b"\xef\xbb\xbf" + page(head='<meta charset="iso-8859-1">').encode("utf-8")

    decoded = _decode(result(content, "text/html; charset=iso-8859-1"))

    assert TEXT in decoded
    assert not decoded.startswith("﻿")


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_utf16_bom_is_honoured(encoding: str) -> None:
    bom = {"utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}[encoding]
    content = bom + page().encode(encoding)

    assert TEXT in _decode(result(content, "text/html; charset=utf-8"))


@pytest.mark.parametrize("label", ["bogus-charset", "utf-16", "utf-32"])
def test_unknown_or_unusable_meta_charset_falls_back_to_utf8(label: str) -> None:
    content = page(head=f'<meta charset="{label}">').encode("utf-8")

    assert TEXT in _decode(result(content, "text/html"))


def test_unknown_header_charset_falls_back_to_the_meta_or_utf8() -> None:
    content = page().encode("utf-8")

    assert TEXT in _decode(result(content, "text/html; charset=bogus"))


def test_invalid_bytes_are_replaced_not_raised() -> None:
    assert "�" in _decode(result(b"<p>\xff\xfe\xfa</p>", "text/html; charset=utf-8"))


def test_same_text_in_utf8_and_latin1_hashes_equal() -> None:
    text = "Grüße aus Köln, café"
    utf8 = _decode(result(page(text).encode("utf-8"), "text/html; charset=utf-8"))
    latin1 = _decode(result(page(text).encode("iso-8859-1"), "text/html; charset=iso-8859-1"))

    assert hash_of(utf8) == hash_of(latin1)


# --- is_html ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "TEXT/HTML; charset=utf-8", "application/xhtml+xml", " text/html ;q=1"],
)
def test_html_content_types_are_accepted(content_type: str) -> None:
    assert _is_html({"content-type": content_type}, b"anything")


@pytest.mark.parametrize(
    "content_type",
    ["application/pdf", "application/json", "image/png", "text/plain", "application/octet-stream"],
)
def test_other_content_types_are_rejected_even_if_the_body_looks_like_html(
    content_type: str,
) -> None:
    assert not _is_html({"content-type": content_type}, b"<!doctype html><html>")


@pytest.mark.parametrize(
    "body",
    [
        b"<!doctype html><html></html>",
        b"<!DOCTYPE HTML PUBLIC ...>",
        b"  \n\t<HTML lang=en>",
        b"\xef\xbb\xbf<html>",
        b"\xef\xbb\xbf \n<!DocType html>",
        b"<!-- generated --><html>",
        b"<head><title>T</title>",
        b"<p>fragment</p>",
        b"<BODY\n>",
        "\ufeff<!doctype html>".encode("utf-16-le"),
        "\ufeff\n<html>".encode("utf-16-be"),
    ],
)
def test_missing_content_type_sniffs_for_an_html_start(body: bytes) -> None:
    assert _is_html({}, body)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"%PDF-1.7",
        b'{"a": 1}',
        b"\x89PNG\r\n",
        b"<?xml?><rss/>",
        b"<pre>text</pre>",  # a listed tag name must end there
        b"<html",  # nor may the start be cut off
        "<html>".encode("utf-16-le"),  # UTF-16 without a BOM is not recognised
    ],
)
def test_missing_content_type_rejects_other_bodies(body: bytes) -> None:
    assert not _is_html({}, body)


# --- charset allowlist -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("windows-1251", "Привет"),
        ("windows-1250", "zażółć"),
        ("koi8-r", "Привет"),
        ("koi8-u", "Привіт"),
        ("iso-8859-2", "zażółć"),
        ("iso-8859-15", "€uro"),
        ("shift_jis", "日本語"),
        ("euc-jp", "日本語"),
        ("iso-2022-jp", "日本語"),
        ("euc-kr", "한국어"),
        ("gbk", "中文"),
        ("gb18030", "中文"),
        ("big5", "中文"),
        ("utf-16le", "héllo"),
        ("utf-16be", "héllo"),
    ],
)
def test_web_encodings_are_honoured(label: str, text: str) -> None:
    content = f"<p>{text}</p>".encode(label)

    assert text in _decode(result(content, f"text/html; charset={label}"))


@pytest.mark.parametrize(
    ("label", "codec", "text"),
    [
        ("gb2312", "gbk", "中文"),
        ("GB2312", "gbk", "中文"),
        ("x-gbk", "gbk", "中文"),
        ("chinese", "gbk", "中文"),
        ("ks_c_5601-1987", "cp949", "한국어"),
        ("windows-949", "cp949", "똠방각하"),  # outside EUC-KR
        ("euc-kr", "cp949", "똠방각하"),
        ("windows-31j", "cp932", "①日本"),
        ("shift_jis", "cp932", "①日本"),  # NEC extension, outside plain Shift_JIS
        ("x-sjis", "cp932", "日本"),
        ("tis-620", "cp874", "ภาษาไทย"),
        ("iso-8859-11", "cp874", "ภาษาไทย"),
        ("windows-874", "cp874", "ภาษาไทย"),
        ("ibm866", "cp866", "Привет"),
        ("x-mac-cyrillic", "mac-cyrillic", "Привет"),
        ("macintosh", "mac-roman", "café"),
        ("x-x-big5", "big5hkscs", "中文"),
    ],
)
def test_legacy_labels_are_read_as_browsers_read_them(label: str, codec: str, text: str) -> None:
    content = f"<p>{text}</p>".encode(codec)

    assert text in _decode(result(content, f"text/html; charset={label}"))
    meta = f'<meta charset="{label}">'.encode() + content
    assert text in _decode(result(meta, "text/html"))


@pytest.mark.parametrize("label", ["iso-8859-1", "latin1", "l1", "ascii", "us-ascii"])
def test_latin1_and_ascii_labels_mean_windows_1252(label: str) -> None:
    assert "€ é" in _decode(result(b"<p>\x80 \xe9</p>", f"text/html; charset={label}"))


def test_utf16_header_without_bom_is_little_endian() -> None:
    content = "<p>héllo</p>".encode("utf-16-le")

    assert "héllo" in _decode(result(content, "text/html; charset=utf-16"))


@pytest.mark.parametrize(
    "label", ["utf-7", "unicode_escape", "idna", "punycode", "rot13", "zlib", "raw_unicode_escape"]
)
def test_other_codecs_are_rejected_like_unknown_labels(label: str) -> None:
    content = "<p>caf\u00e9 +AGE- \\u0041</p>".encode()

    assert "caf\u00e9 +AGE- \\u0041" in _decode(result(content, f"text/html; charset={label}"))
    meta = f'<meta charset="{label}">'.encode() + content
    assert "caf\u00e9 +AGE- \\u0041" in _decode(result(meta, "text/html"))
