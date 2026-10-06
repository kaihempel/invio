"""Tests for the digest URL post-check (filter_urls)."""

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import invio.graph.nodes.synthesize as synthesize_module
from invio.graph.nodes.synthesize import filter_urls
from tests.markdown_oracle import assert_renders_only_allowed, token_signature

A = "https://example.org/a"
B = "https://example.org/b"
ALLOWED = {A, B}
WIKI = "https://en.wikipedia.org/wiki/A_(b)"
QUERY = "https://example.org/q?x=1&y=2"

# --- Inline links and images ---------------------------------------------------------------


def test_unknown_inline_link_keeps_its_text() -> None:
    result = filter_urls("see [the post](https://evil.example/x) now", ALLOWED)
    assert result.text == "see the post now"
    assert (result.removed, result.linked) == (1, frozenset())


@pytest.mark.parametrize(
    "markdown",
    [
        f"[t]({A})",
        f'[t]({A} "a title")',
        f"[t](<{A}>)",
        f"[t]({A}  'single')",
        f"[t](<{WIKI}>)",
        f"[t]({WIKI})",
        f"[t]({QUERY})",
    ],
)
def test_allowed_inline_link_is_unchanged(markdown: str) -> None:
    allowed = {A, WIKI, QUERY}
    result = filter_urls(markdown, allowed)
    assert result.text == markdown
    assert result.removed == 0
    assert len(result.linked) == 1


def test_allowed_url_with_parentheses_and_query_survive_as_bare_urls() -> None:
    text = f"read {WIKI} and {QUERY}, then ({WIKI})."
    result = filter_urls(text, {WIKI, QUERY})
    assert result.text == text
    assert result.removed == 0


@pytest.mark.parametrize("url", [A, "https://evil.example/i.png"])
def test_image_becomes_its_alt_text(url: str) -> None:
    result = filter_urls(f"x ![a logo]({url}) y", ALLOWED)
    assert result.text == "x a logo y"
    assert result.removed == 1
    assert result.linked == frozenset()


def test_nested_removal_cannot_splice_a_new_link() -> None:
    result = filter_urls("[[a](https://evil.example/x)](https://evil.example/y)", ALLOWED)
    assert result.text == "a"
    assert result.removed == 2


def test_removal_that_creates_an_allowed_link_is_kept_only_if_allowed() -> None:
    result = filter_urls(f"![[x](https://evil.example/1)]({A})", ALLOWED)
    assert result.text == "x"  # the image is dropped in a later pass
    assert result.removed == 2


# --- Reference definitions and autolinks ---------------------------------------------------


def test_reference_definitions() -> None:
    text = f'[a]: https://evil.example/x "t"\n[b]: {A}\nbody'
    result = filter_urls(text, ALLOWED)
    assert result.text == f"\n[b]: {A}\nbody"
    assert result.removed == 1
    assert result.linked == frozenset()  # definitions do not count as links


def test_autolinks() -> None:
    result = filter_urls(
        f"<https://evil.example/x> and <{A}> and <HTTPS://EVIL.EXAMPLE/Y>", ALLOWED
    )
    assert result.text == f" and <{A}> and "
    assert result.removed == 2
    assert result.linked == frozenset({A})


# --- Raw HTML ------------------------------------------------------------------------------


@pytest.mark.parametrize("href", ["https://evil.example/x", A])
def test_raw_html_link_loses_its_tag_whatever_the_url(href: str) -> None:
    result = filter_urls(f'<a href="{href}">text</a>', ALLOWED)
    assert result.text == "text"
    assert result.removed == 1
    assert result.linked == frozenset()


def test_raw_html_image_and_other_url_tags_are_removed() -> None:
    result = filter_urls('x <img src="https://evil.example/i.png" alt="a"> y <b>bold</b>', ALLOWED)
    assert result.text == "x  y bold"
    assert result.removed == 1


def test_comment_and_unknown_tags_are_stripped() -> None:
    result = filter_urls("a <!-- hidden --> b <script>x</script> c <IFRAME srcdoc=q>", ALLOWED)
    assert "<" not in result.text
    assert result.removed == 0
    assert result.text == "a  b x c "


def test_comparison_operators_are_kept() -> None:
    text = "5 < 6 and 7 > 6"
    assert filter_urls(text, ALLOWED).text == text


# --- Bare URLs -----------------------------------------------------------------------------


def test_unknown_bare_urls_are_removed_everywhere() -> None:
    text = "a https://evil.example/1 b `https://evil.example/2` c\n```\nhttps://evil.example/3\n```"
    result = filter_urls(text, ALLOWED)
    assert result.text == "a  b `` c\n```\n\n```"
    assert result.removed == 3


@pytest.mark.parametrize(
    "url", ["www.evil.example", "HTTPS://EVIL.EXAMPLE/x", "ftp://evil.example/f", "x-y+z.1://e/a"]
)
def test_other_url_shapes_are_removed(url: str) -> None:
    result = filter_urls(f"go to {url} now", ALLOWED)
    assert result.text == "go to  now"
    assert result.removed == 1


def test_uppercase_scheme_of_an_allowed_url_is_not_the_allowed_url() -> None:
    result = filter_urls("go HTTPS://example.org/a now", ALLOWED)
    assert result.text == "go  now"


@pytest.mark.parametrize("suffix", ["/extra", "?x=1", "x", "#frag", "/", ".html", "..x"])
def test_altered_allowed_urls_are_removed(suffix: str) -> None:
    result = filter_urls(f"see {A}{suffix} here", ALLOWED)
    assert result.text == "see  here"
    assert result.removed == 1


@pytest.mark.parametrize(
    "after",
    ["", ".", ",", ";", ":", "!", "?", "...", ".)", ")", "]", ">", '"', "'", "<", "`", " x"],
)
def test_allowed_url_followed_by_a_boundary_is_kept(after: str) -> None:
    text = f"see {A}{after}"
    result = filter_urls(text, ALLOWED)
    assert result.text == text
    assert result.removed == 0


def test_allowed_url_in_front_of_a_glued_url_keeps_only_the_allowed_one() -> None:
    result = filter_urls(f"{A}).https://evil.example/q", ALLOWED)
    assert result.text == f"{A})."
    result = filter_urls(f"{A})https://evil.example/q", ALLOWED)
    assert result.text == f"{A})"
    assert result.removed == 1


def test_a_url_glued_to_a_prefix_is_removed() -> None:
    result = filter_urls(f"x{A} and https://evil.example/{A}", ALLOWED)
    assert result.text == " and "
    assert result.removed == 2


def test_removed_counts_each_url_and_linked_collects_destinations() -> None:
    text = f"[a]({A}) <{B}> [x](https://evil.example/1) https://evil.example/2 {A}"
    result = filter_urls(text, ALLOWED)
    assert result.text == f"[a]({A}) <{B}> x  {A}"
    assert result.removed == 2
    assert result.linked == frozenset({A, B})


def test_only_allowed_links_leave_the_answer_unchanged() -> None:
    text = f"Intro.\n\n## Topic\n\n- [One]({A}) - about it.\n- [Two]({B}) - more, see {B}.\n"
    result = filter_urls(text, ALLOWED)
    assert (result.text, result.removed) == (text, 0)


def test_empty_allowed_set_and_empty_text() -> None:
    assert filter_urls("", ALLOWED).text == ""
    assert filter_urls(f"[a]({A}) {A}", set()).text == "a "


def test_fixed_point_cap_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(synthesize_module, "_MAX_PASSES", 1)
    result = filter_urls(f"[[a](https://evil.example/x)]({A}) and {B}", ALLOWED)
    assert "://" not in result.text
    assert result.removed >= 1


# --- Properties ----------------------------------------------------------------------------

_URL_AT_START = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_BOUNDARY = re.compile(r"[.,;:!?]*(?:[\s)\]>\"'<`]|\Z)")
_WORDS = st.text(alphabet="abcdefg xyz.,", min_size=1, max_size=12)
_ALLOWED_LIST = ["https://example.org/a", "https://example.org/b", "https://example.org/c"]
_INVENTED = ["https://invented.example/1", "http://evil.example/x?y=1", "www.evil.example/z"]


def _links(url: str) -> list[str]:
    return [f"[Title]({url})", url, f"<{url}>", f"![alt]({url})", f'<a href="{url}">t</a>']


_PIECES = st.one_of(
    _WORDS,
    st.sampled_from(_ALLOWED_LIST).flatmap(lambda u: st.sampled_from(_links(u))),
    st.sampled_from(_INVENTED).flatmap(lambda u: st.sampled_from(_links(u))),
)
_CHAOS = st.lists(
    st.sampled_from(
        [
            "[",
            "]",
            "(",
            ")",
            "<",
            ">",
            "!",
            '"',
            " ",
            "\n",
            "https://",
            "HTTP://",
            "www.",
            "example.org/a",
            "example.org/b",
            "/extra",
            "a",
            "`",
            "<a ",
            "href=",
            "](",
            "[x](",
            "<https://",
            ".",
        ]
    ),
    max_size=40,
).map("".join)


def _assert_only_allowed_urls(text: str, allowed: list[str]) -> None:
    for match in _URL_AT_START.finditer(text):
        found = match[0]
        assert any(found.startswith(u) and _BOUNDARY.match(found, len(u)) for u in allowed), found


@settings(derandomize=True, max_examples=200, deadline=None)
@given(st.lists(_PIECES, max_size=12).map(" ".join))
def test_property_only_allowed_urls_remain_and_filtering_is_idempotent(text: str) -> None:
    result = filter_urls(text, _ALLOWED_LIST)
    _assert_only_allowed_urls(result.text, _ALLOWED_LIST)
    assert filter_urls(result.text, _ALLOWED_LIST).text == result.text


@settings(derandomize=True, max_examples=300, deadline=None)
@given(_CHAOS)
def test_property_chaotic_input_is_safe_and_idempotent(text: str) -> None:
    result = filter_urls(text, _ALLOWED_LIST)
    _assert_only_allowed_urls(result.text, _ALLOWED_LIST)
    again = filter_urls(result.text, _ALLOWED_LIST)
    assert again.text == result.text
    assert again.removed == 0


@settings(derandomize=True, max_examples=100, deadline=None)
@given(st.lists(st.sampled_from(_ALLOWED_LIST), min_size=1, max_size=3, unique=True), _WORDS)
def test_property_allowed_inline_links_are_preserved(urls: list[str], word: str) -> None:
    links = [f"[{word}]({u})" for u in urls]
    text = f"{word} " + "\n".join(links) + f"\n[x]({_INVENTED[0]})"
    result = filter_urls(text, _ALLOWED_LIST)
    for link in links:
        assert link in result.text
    assert result.linked == frozenset(urls)


# --- Adversarial input ---------------------------------------------------------------------
# No wall-clock assertion (tests stay deterministic): a pattern that backtracks catastrophically
# would hang the suite instead.


@pytest.mark.parametrize(
    "fragment",
    [
        "[",
        "(",
        "<",
        "<a ",
        "https://",
        "[a](",
        "![",
        "[a](x(",
        "<https://x ",
        "]: ",
        "[a]:",
        "<a href=",
        "[a](<",
        "<!",
        "  ",
        "[a](x ",
    ],
)
def test_adversarial_floods_finish_and_leave_no_unknown_url(fragment: str) -> None:
    text = fragment * (20_000 // len(fragment))
    result = filter_urls(text, ALLOWED)
    assert "://" not in result.text


def test_adversarial_mixed_flood() -> None:
    text = "[a](" + "(x" * 5_000 + "https://" * 1_000 + "<a " * 2_000 + "![" * 2_000
    assert "://" not in filter_urls(text, ALLOWED).text


# --- Rendered output (CommonMark oracle) ---------------------------------------------------
# The notifier renders the digest with markdown-it (CommonMark, raw HTML on). These tests parse
# the filtered text with the same grammar and check every link, image and HTML token, so they
# catch constructs the regex passes do not recognise. Destinations without "//" matter:
# browsers resolve ``https:evil.example`` to ``https://evil.example/``, and nh3 keeps it.


_SNEAKY = [
    "https:evil.example/x",
    "https:/evil.example/x",
    "//evil.example/x",
    "mailto:x@e.example",
]
_SNEAKY_FORMS = [
    pytest.param("[a [b] c]({d})", id="nested-brackets"),
    pytest.param("[a \\] b]({d})", id="escaped-bracket"),
    pytest.param("[`]`]({d})", id="code-span-bracket"),
    pytest.param("[a\nb]({d})", id="newline-in-text"),
    pytest.param("[a](\n{d})", id="newline-before-destination"),
    pytest.param('[a]({d}\n"t")', id="newline-before-title"),
    pytest.param("[a]({d} (title))", id="paren-title"),
    pytest.param("![a [b]]({d})", id="image-nested-brackets"),
    pytest.param("- [r]: {d}\n\n[click][r]", id="definition-in-list"),
    pytest.param("1. [r]: {d}\n\n[r]", id="definition-in-ordered-list"),
    pytest.param("> [r]: {d}\n\n[click][r]", id="definition-in-quote"),
    pytest.param("[r\\]x]: {d}\n\n[click][r\\]x]", id="definition-escaped-label"),
    pytest.param("[r]:\n{d}\n\n[r]", id="definition-next-line"),
    pytest.param('<a title="<" href={d}>x</a>', id="html-attribute-with-lt"),
    pytest.param("[a]({d})", id="plain-inline"),
    pytest.param("<{d}>", id="autolink"),
    pytest.param("[r]: {d}\n\n[r]", id="definition"),
    pytest.param(f"[x \\[a]({A}) y]({{d}})", id="allowed-link-inside-unknown-link"),
    pytest.param(f"[a\\]({A}) foo]({{d}})", id="escaped-allowed-link"),
]


@pytest.mark.parametrize("destination", _SNEAKY)
@pytest.mark.parametrize("form", _SNEAKY_FORMS)
def test_no_unknown_destination_survives_commonmark_rendering(form: str, destination: str) -> None:
    result = filter_urls(form.format(d=destination), ALLOWED)
    assert_renders_only_allowed(result.text, ALLOWED)
    again = filter_urls(result.text, ALLOWED)
    assert again.text == result.text
    assert again.removed == 0


def test_sneaky_link_is_counted_and_keeps_allowed_links() -> None:
    text = f"Intro [a [b] c](https:evil.example/x).\n\n## T\n\n- [One]({A}) - see <{B}>."
    result = filter_urls(text, ALLOWED)
    assert_renders_only_allowed(result.text, ALLOWED)
    assert result.removed == 1
    assert result.linked == frozenset({A, B})
    assert f"[One]({A})" in result.text
    assert f"<{B}>" in result.text
    assert "## T" in result.text


def test_sneaky_definition_does_not_leave_a_reference_link() -> None:
    result = filter_urls(f"- [r]: https:evil.example\n\n[click][r] and [ok]({A})", ALLOWED)
    assert_renders_only_allowed(result.text, ALLOWED)
    assert result.linked == frozenset({A})
    assert result.removed == 1


@pytest.mark.parametrize(
    "markdown",
    [
        "[a](javascript:alert(1))",
        "[a](JavaScript:alert(1))",
        "[a](data:text/html;base64,PHNjcmlwdD4=)",
        "<javascript:alert(1)>",
        "<data:text/html,x>",
        "<mailto:x@evil.example>",
        "<x@evil.example>",
        "[r]: javascript:alert(1)\n\n[r]",
        "[a](&#x6A;avascript:alert(1))",
        "[a](https&#58;//evil.example)",
        "[a](//evil.example)",
        "[a](evil.example/path)",
        "![x](data:image/png;base64,AAAA)",
        "<?php echo 1; ?>",
        "<div\nhref=x",
    ],
)
def test_other_schemes_and_relative_destinations_are_removed(markdown: str) -> None:
    result = filter_urls(f"before {markdown} after", ALLOWED)
    assert_renders_only_allowed(result.text, ALLOWED)
    assert result.text.startswith("before ")


@pytest.mark.parametrize(
    "lookalike",
    [
        "https://exаmple.org/a",  # noqa: RUF001 (Cyrillic a)
        "https://example.org/a​",
        "https://example.org／a",  # noqa: RUF001 (fullwidth solidus)
        "https://EXAMPLE.org/a",
        "https://example.org/A",
        "https://example.org/a%2F",
        "http://example.org/a",
    ],
)
def test_lookalikes_of_an_allowed_url_are_not_the_allowed_url(lookalike: str) -> None:
    result = filter_urls(f"[t]({lookalike}) and {lookalike} end", ALLOWED)
    assert result.linked == frozenset()
    assert result.removed == 2
    assert_renders_only_allowed(result.text, ALLOWED)


@pytest.mark.parametrize("after", ["https://evil.example/x", "www.evil.example", "<https://e.x>"])
def test_an_unknown_url_adjacent_to_an_allowed_one_is_removed(after: str) -> None:
    for joined in (f"{A} {after}", f"[t]({A}){after}", f"<{A}>{after}", f"{A}){after}"):
        result = filter_urls(joined, ALLOWED)
        assert "evil" not in result.text
        assert "e.x" not in result.text
        assert A in result.text
        assert_renders_only_allowed(result.text, ALLOWED)


def test_answer_without_sneaky_constructs_is_not_changed() -> None:
    text = f"Intro [sic] with 5 < 6.\n\n## T\n\n- [One]({A}) - a [note] here.\n<{B}>\n"
    result = filter_urls(text, ALLOWED)
    assert (result.text, result.removed, result.linked) == (text, 0, frozenset({A, B}))


def test_allowed_url_with_an_entity_or_percent_escape_stays_linked() -> None:
    amp = "https://example.org/q?x=1&amp;y=2"
    pct = "https://example.org/a%20b"
    text = f"[a]({amp}) [b]({pct}) <{amp}>"
    result = filter_urls(text, {amp, pct})
    assert (result.text, result.removed, result.linked) == (text, 0, frozenset({amp, pct}))


def test_linked_follows_the_rendered_links() -> None:
    in_code = filter_urls(f"`[a]({A})` and\n\n    [b]({B})", ALLOWED)
    assert in_code.linked == frozenset()  # code spans and indented code are no links
    used_reference = filter_urls(f"[x][r]\n\n[r]: {A}", ALLOWED)
    assert used_reference.linked == frozenset({A})


def test_strip_syntax_keeps_only_allowed_links_and_autolinks() -> None:
    text = f"![i]({A}) [x](https:e.example) <{B}> <https:e.example> [k]({A}) a<b [c]"
    stripped = synthesize_module._strip_syntax(text, frozenset(ALLOWED))
    assert "[" not in stripped.replace(f"[k]({A})", "")
    assert_renders_only_allowed(stripped, ALLOWED)


def test_rendered_check_fails_closed_when_stripping_cannot_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(synthesize_module, "_strip_syntax", lambda text, allowed: text)
    result = filter_urls(f"[a [b] c](https:evil.example) [ok]({A}) <i> https://e.x", ALLOWED)
    # like the pass cap, failing closed also strips the allowed URL runs
    assert "e.x" not in result.text
    assert A not in result.text
    assert result.linked == frozenset()
    assert result.removed >= 1
    assert_renders_only_allowed(result.text, set())


_ORACLE_CHAOS = st.lists(
    st.sampled_from(
        [
            "[",
            "]",
            "(",
            ")",
            "<",
            ">",
            "!",
            "\\",
            "`",
            '"',
            " ",
            "\n",
            "\n\n",
            "- ",
            "> ",
            "1. ",
            "]:",
            "https:evil.example",
            "//evil.example",
            "mailto:x@e.example",
            "https://evil.example",
            A,
            "a",
            "<a href=",
            "title=",
        ]
    ),
    max_size=30,
).map("".join)


@settings(derandomize=True, max_examples=500, deadline=None)
@given(_ORACLE_CHAOS)
def test_property_rendered_output_links_only_allowed_urls(text: str) -> None:
    result = filter_urls(text, ALLOWED)
    assert_renders_only_allowed(result.text, ALLOWED)
    again = filter_urls(result.text, ALLOWED)
    assert again.text == result.text
    assert again.removed == 0
    assert result.linked <= ALLOWED


# --- Parity with the notifier's renderer -----------------------------------------------------
# synthesize cannot import the notifier (layering), so it keeps its own renderer. It must
# tokenise links, images and raw HTML exactly like the notifier does.

_PARITY_INPUTS = [
    "[a [b] c](https://example.org/a)",
    "[a](https:evil.example) and [b](mailto:x@e.example)",
    "- item\n  - [r]: https:evil.example/3\n\n[click][r]",
    '<a title="<" href=https:evil.example/4>tag</a>',
    "<https://example.org/auto> and <mailto:x@e.example>",
    "![img](https://example.org/i.png) ![x][r]\n\n[r]: //evil.example/x",
    "> [q]: https:evil.example\n\n[q]",
    "[a](<https://example.org/a b> (t)) <b>bold</b>",
    "plain text without any markup",
]


@pytest.mark.parametrize("markdown", _PARITY_INPUTS)
def test_renderer_matches_the_notifiers(markdown: str) -> None:
    assert token_signature(markdown, synthesize_module._RENDERER) == token_signature(markdown)
