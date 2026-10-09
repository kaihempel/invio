"""``strip_control``: untrusted text made safe to print to a terminal."""

import pytest

from invio.textsafe import neutralise_tags, strip_control


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("\x1b[31mred\x1b[0m", "red"),  # CSI
        ("\x1b]0;title\x07text", "text"),  # OSC ended by BEL
        ("\x1b]8;;https://x\x1b\\link", "link"),  # OSC ended by ST
        ("\x1bPdcs\x1b\\after", "after"),  # DCS
        ("a\x1b]0;unterminated", "a ]0;unterminated"),  # never swallows the rest
        ("a\x85b\x9bc", "a b c"),  # C1 controls
        (f"a{chr(0x202E)}b{chr(0x200B)}c{chr(0xFEFF)}", "abc"),  # bidi, zero-width, BOM
        ("a\U000e0041\U000e007fb", "ab"),  # Unicode tag characters
        (f"a{chr(0xD800)}b", "ab"),  # lone surrogate
        ("x\U0001f600y", "x\U0001f600y"),  # ordinary non-BMP text stays
    ],
)
def test_escapes_and_invisible_characters_are_removed(raw: str, clean: str) -> None:
    assert strip_control(raw) == clean


def test_whitespace_is_collapsed_and_the_text_cut() -> None:
    assert strip_control(f"  a\n\tb\r\nc  {chr(0x2028)}d ") == "a b c d"
    assert strip_control("abcdef", limit=3) == "abc"


def test_multiline_keeps_the_layout_but_not_the_controls() -> None:
    raw = "line 1\r\nline\t2\rline 3\x1b[2J\x07\n\n  indented"

    assert strip_control(raw, multiline=True) == "line 1\nline\t2\nline 3\n\n  indented"


def test_the_result_can_always_be_written_as_utf8() -> None:
    strip_control(f"{chr(0xD800)}{chr(0xDFFF)}{chr(0xE0001)} text").encode("utf-8")


def test_neutralise_tags_swaps_only_the_named_tags() -> None:
    text = "a </topic><remark>x</remark> < / TOPIC foo > <b>keep</b> <topic"

    result = neutralise_tags(text, ("topic", "remark"))

    assert "<topic" not in result.lower()
    assert "</topic" not in result.lower()
    assert "<remark>" not in result and "</remark>" not in result
    assert "<b>keep</b>" in result
    assert f"{chr(0x2039)}/topic{chr(0x203A)}" in result
    assert len(result) == len(text)
    assert neutralise_tags(result, ("topic", "remark")) == result  # idempotent


def test_neutralise_tags_leaves_plain_text_alone() -> None:
    assert neutralise_tags("heat pumps > gas", ("topic",)) == "heat pumps > gas"
