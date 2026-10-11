"""``vtt_to_text``: table tests on real-world-shaped VTT plus Hypothesis properties."""

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from invio.sources.transcript import vtt_to_text

CONTRACT_EXAMPLE = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000 align:start position:0%
hello<00:00:00.500><c> world</c>

00:00:02.000 --> 00:00:02.010
hello world

00:00:02.010 --> 00:00:04.000
hello world
this is &amp; more
"""


def test_contract_example() -> None:
    assert vtt_to_text(CONTRACT_EXAMPLE) == "hello world this is & more"


@pytest.mark.parametrize(
    ("vtt", "expected"),
    [
        ("", ""),
        ("WEBVTT\n", ""),
        ("WEBVTT\nKind: captions\nLanguage: de\n\n", ""),
        ("﻿WEBVTT\n\n00:00.000 --> 00:01.000\nHi\n", "Hi"),
        ("WEBVTT\r\n\r\n00:00.000 --> 00:01.000\r\nHi\r\nthere\r\n", "Hi there"),
        # NOTE / STYLE / REGION blocks are dropped
        (
            "WEBVTT\n\nNOTE this is a comment\nacross two lines\n\n"
            "STYLE\n::cue { color: red }\n\n"
            "REGION\nid:fred\nwidth:40%\n\n"
            "00:00.000 --> 00:01.000\nkept\n",
            "kept",
        ),
        ("WEBVTT\n\nNOTE\n\n00:00.000 --> 00:01.000\nkept\n", "kept"),
        # cue identifiers, numeric and named
        (
            "WEBVTT\n\n1\n00:00.000 --> 00:01.000\none\n\nintro\n00:01.000 --> 00:02.000\ntwo\n",
            "one two",
        ),
        # cue settings after the timing
        ("WEBVTT\n\n00:00.000 --> 00:01.000 align:start position:0%\nhi\n", "hi"),
        # inline timestamps and tags
        (
            "WEBVTT\n\n00:00.000 --> 00:01.000\n"
            "<v Speaker>he<00:00:00.500>llo</v> <c.color>big</c> <b>b</b><i>i</i><u>u</u> "
            "<lang de>da</lang>\n",
            "hello big biu da",
        ),
        # entities
        ("WEBVTT\n\n00:00.000 --> 00:01.000\nit&#39;s a&nbsp;b &amp; c\n", "it's a b & c"),
        # an escaped tag is text, not markup
        ("WEBVTT\n\n00:00.000 --> 00:01.000\n&lt;b&gt; stays\n", "<b> stays"),
        # multi-line cues are joined
        ("WEBVTT\n\n00:00.000 --> 00:01.000\nline one\nline two\n", "line one line two"),
        # whitespace is collapsed
        ("WEBVTT\n\n00:00.000 --> 00:01.000\n  a    b \t c  \n", "a b c"),
        # a cue that is only tags is empty
        ("WEBVTT\n\n00:00.000 --> 00:01.000\n<c></c>\n", ""),
        # several blank lines between blocks
        (
            "WEBVTT\n\n\n\n00:00.000 --> 00:01.000\nhi\n\n\n   \n\n00:01.000 --> 00:02.000\nyo",
            "hi yo",
        ),
        # a block without timing is not a cue
        ("WEBVTT\n\nstray text without timing\n", ""),
    ],
)
def test_conversion_table(vtt: str, expected: str) -> None:
    assert vtt_to_text(vtt) == expected


def test_a_byte_order_mark_is_ignored() -> None:
    assert vtt_to_text("\ufeffWEBVTT\n\n00:00.000 --> 00:01.000\nHi\n") == "Hi"


def test_rolling_auto_captions_collapse_to_one_occurrence() -> None:
    rolling = (
        "WEBVTT\nKind: captions\nLanguage: en\n\n"
        "00:00:00.000 --> 00:00:02.000 align:start position:0%\n"
        "first line<00:00:01.000><c> here</c>\n\n"
        "00:00:02.000 --> 00:00:02.010 align:start position:0%\nfirst line here\n\n"
        "00:00:02.010 --> 00:00:04.000 align:start position:0%\n"
        "first line here\nsecond<00:00:03.000><c> line</c>\n\n"
        "00:00:04.000 --> 00:00:04.010 align:start position:0%\nsecond line\n\n"
        "00:00:04.010 --> 00:00:06.000 align:start position:0%\nsecond line\nthird\n"
    )

    assert vtt_to_text(rolling) == "first line here second line third"


def test_a_repeat_that_is_not_adjacent_is_kept() -> None:
    text = vtt_to_text("WEBVTT\n\n00:00.000 --> 00:01.000\nyes\nno\nyes\n")

    assert text == "yes no yes"


_WORDS = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc", "Zs", "Zl", "Zp"), blacklist_characters="<>&-"
    ),
    min_size=1,
    max_size=8,
)
_LINES = st.lists(_WORDS, min_size=1, max_size=4).map(" ".join)
_CUES = st.lists(st.lists(_LINES, min_size=1, max_size=3), max_size=8)


def _document(cues: list[list[str]]) -> str:
    parts = ["WEBVTT", ""]
    for index, lines in enumerate(cues):
        parts += [
            f"{index + 1}",
            f"00:00:{index:02d}.000 --> 00:00:{index + 1:02d}.000",
            *lines,
            "",
        ]
    return "\n".join(parts)


@given(_CUES)
def test_output_has_no_timing_markup_or_repeated_lines(cues: list[list[str]]) -> None:
    text = vtt_to_text(_document(cues))

    assert "-->" not in text
    assert re.search(r"<[^>]*>", text) is None
    assert "WEBVTT" not in text
    assert text == " ".join(text.split())  # single spaces, trimmed


@given(_CUES)
def test_no_two_consecutive_lines_are_equal(cues: list[list[str]]) -> None:
    kept: list[str] = []
    for lines in cues:
        for line in lines:
            collapsed = " ".join(line.split())
            if collapsed and (not kept or kept[-1] != collapsed):
                kept.append(collapsed)

    assert vtt_to_text(_document(cues)) == " ".join(kept)
