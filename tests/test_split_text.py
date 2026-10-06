"""Tests for split_text and estimate_tokens (limits, overlap, boundaries)."""

import random
import re
import string
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

import pytest

from invio.graph.nodes.summarize_item import estimate_tokens, split_text

_FIXTURES = Path(__file__).parent / "fixtures" / "summarize"

# --- Helpers -------------------------------------------------------------------------------


def _numbered_sentences(count: int, per_paragraph: int | None = None) -> str:
    """Varied, non-periodic sentences so overlap tails cannot match by coincidence."""
    rng = random.Random(11)
    sentences = []
    for n in range(count):
        filler = " ".join(
            "".join(rng.choices(string.ascii_lowercase, k=rng.randint(2, 9)))
            for _ in range(rng.randint(4, 12))
        )
        sentences.append(f"Sentence {n} says {filler}.")
    if per_paragraph is None:
        return " ".join(sentences)
    groups = [sentences[i : i + per_paragraph] for i in range(0, count, per_paragraph)]
    return "\n\n".join(" ".join(g) for g in groups)


def _no_punctuation(chars: int) -> str:
    rng = random.Random(5)
    words = []
    total = 0
    while total < chars:
        word = "".join(rng.choices(string.ascii_lowercase, k=rng.randint(2, 9)))
        words.append(word)
        total += len(word) + 1
    return " ".join(words)[:chars]


def _cjk(chars: int) -> str:
    rng = random.Random(3)
    return "".join(chr(rng.randint(0x4E00, 0x9FA5)) for _ in range(chars))


def _huge_word(chars: int) -> str:
    return "".join(random.Random(9).choices(string.ascii_letters, k=chars))


_TEXTS: dict[str, Callable[[], str]] = {
    "long-fixture": lambda: (_FIXTURES / "long.txt").read_text(encoding="utf-8"),
    "one-paragraph": lambda: _numbered_sentences(400)[:20000],
    "no-punctuation": lambda: _no_punctuation(20000),
    "cjk": lambda: _cjk(20000),
    "huge-word": lambda: _huge_word(50000),
}
_LIMITS = [(3000, 200), (100, 10), (50, 0)]


def _tail(prev: str, nxt: str, overlap: int) -> str:
    """The longest suffix of ``prev`` of at most ``4 * overlap`` chars that prefixes ``nxt``."""
    for size in range(min(4 * overlap, len(prev), len(nxt)), 0, -1):
        if prev.endswith(nxt[:size]):
            return nxt[:size]
    return ""


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


# --- estimate_tokens -----------------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [("", 0), ("a", 1), ("abcd", 1), ("abcde", 2)])
def test_estimate_tokens(text: str, expected: int) -> None:
    assert estimate_tokens(text) == expected


# --- Arguments and trivial inputs ----------------------------------------------------------


@pytest.mark.parametrize(("max_tokens", "overlap"), [(0, 0), (-1, 0), (10, -1), (10, 10), (10, 11)])
def test_invalid_arguments_raise(max_tokens: int, overlap: int) -> None:
    with pytest.raises(ValueError, match=r"max_tokens|overlap"):
        split_text("some text", max_tokens, overlap)


@pytest.mark.parametrize("text", ["", "  \n ", "\t\n\n  "])
def test_blank_text_gives_no_chunks(text: str) -> None:
    assert split_text(text, 10, 2) == []


def test_text_within_limit_is_one_chunk() -> None:
    text = "x" * 40
    assert split_text(text, 10, 2) == [text]


def test_text_just_over_limit_is_split() -> None:
    assert len(split_text("x" * 41, 10, 0)) > 1


# --- Properties ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(_TEXTS))
@pytest.mark.parametrize(("max_tokens", "overlap"), _LIMITS)
def test_chunks_respect_limits_and_overlap(name: str, max_tokens: int, overlap: int) -> None:
    text = _TEXTS[name]()
    chunks = split_text(text, max_tokens, overlap)
    assert len(chunks) > 1
    assert all(chunk.strip() for chunk in chunks)
    assert all(estimate_tokens(chunk) <= max_tokens for chunk in chunks)
    for prev, nxt in pairwise(chunks):
        if overlap > 0:
            tail = _tail(prev, nxt, overlap)
            assert tail != ""
            assert estimate_tokens(tail) <= overlap
            new_text = nxt[len(tail) :]
            assert new_text != ""
            assert estimate_tokens(new_text) <= max_tokens - overlap
    if overlap == 0:
        assert _squash("".join(chunks)) == _squash(text)


def test_overlap_tail_starts_at_word_boundary_when_possible() -> None:
    text = _numbered_sentences(200)
    chunks = split_text(text, 100, 10)
    assert len(chunks) > 1
    for prev, nxt in pairwise(chunks):
        tail = _tail(prev, nxt, 10)
        start = len(prev) - len(tail)
        assert start == 0 or prev[start - 1].isspace()


def test_paragraphs_are_not_cut_when_they_fit() -> None:
    paragraphs = [_numbered_sentences(3)[: 150 + 10 * i] for i in range(30)]
    assert all(estimate_tokens(p) <= 90 for p in paragraphs)
    chunks = split_text("\n\n".join(paragraphs), 100, 10)
    assert len(chunks) > 1
    for prev, nxt in pairwise(chunks):
        new_text = nxt[len(_tail(prev, nxt, 10)) :]
        assert new_text.startswith("\n\n")  # the paragraph break before the new text is kept
        assert any(new_text[2:].startswith(p) for p in paragraphs)


def test_sentences_are_not_cut_when_they_fit() -> None:
    chunks = split_text(_numbered_sentences(300), 100, 0)
    assert len(chunks) > 1
    assert all(chunk.endswith(".") for chunk in chunks)


def test_is_deterministic() -> None:
    text = _TEXTS["long-fixture"]()
    assert split_text(text, 100, 10) == split_text(text, 100, 10)


def test_slices_of_one_cut_word_join_without_separator() -> None:
    word = _huge_word(1000)
    assert "".join(split_text(word, 50, 0)) == word


def test_paragraph_separator_is_kept_between_units_of_one_chunk() -> None:
    chunks = split_text("Alpha one.\n\nBeta two.\n\nGamma three.", 6, 0)
    assert chunks == ["Alpha one.\n\nBeta two.", "Gamma three."]


# --- Seeded random texts (property-style) --------------------------------------------------

_SEPARATORS = (" ", " ", " ", "  ", "\n", "\t", "\n\n", "\n \n", "\r\n\r\n", "\n\n\n")


def _random_text(seed: int) -> str:
    """A seeded mix of words, sentence ends, odd whitespace, huge words and CJK runs."""
    rng = random.Random(seed)
    parts: list[str] = []
    for _ in range(rng.randint(1, 400)):
        roll = rng.random()
        if roll < 0.02:
            parts.append(_huge_word(rng.randint(50, 3000)))
        elif roll < 0.05:
            parts.append(
                "".join(chr(rng.randint(0x4E00, 0x9FA5)) for _ in range(rng.randint(5, 400)))
            )
        else:
            word = "".join(rng.choices(string.ascii_letters + "äöüß", k=rng.randint(1, 12)))
            parts.append(word + rng.choice(("", "", "", ".", "!", "?", ",", "。")))
        parts.append(rng.choice(_SEPARATORS))
    return "".join(parts)


def _random_limits(seed: int) -> tuple[int, int]:
    rng = random.Random(seed * 7919)
    max_tokens = rng.choice((1, 2, 3, 5, 8, 13, 40, 100, 250))
    return max_tokens, rng.randint(0, max_tokens - 1)


@pytest.mark.parametrize("seed", range(150))
def test_random_texts_keep_every_split_property(seed: int) -> None:
    text = _random_text(seed)
    max_tokens, overlap = _random_limits(seed)
    chunks = split_text(text, max_tokens, overlap)
    if estimate_tokens(text) <= max_tokens:
        assert chunks == ([text] if text.strip() else [])
        return
    assert chunks
    assert all(chunk.strip() for chunk in chunks)
    assert all(estimate_tokens(chunk) <= max_tokens for chunk in chunks)
    rebuilt = [chunks[0]]
    for prev, nxt in pairwise(chunks):
        if overlap == 0:
            rebuilt.append(nxt)
            continue
        tail = _tail(prev, nxt, overlap)
        assert tail != ""
        assert estimate_tokens(tail) <= overlap
        new_text = nxt[len(tail) :]
        assert new_text != ""
        assert estimate_tokens(new_text) <= max_tokens - overlap
        rebuilt.append(new_text)
    # nothing is lost or invented: only whitespace may change
    assert _squash("".join(rebuilt)) == _squash(text)
    assert split_text(text, max_tokens, overlap) == chunks


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("\n".join(_numbered_sentences(300).split(". ")), id="only-line-breaks"),
        pytest.param(_no_punctuation(20000).replace(" ", "\n"), id="one-word-per-line"),
        pytest.param("x" + " " * 40000 + "y", id="huge-whitespace-run"),
        pytest.param("\t".join(_numbered_sentences(300).split()), id="tabs-only"),
    ],
)
def test_unusual_whitespace_still_respects_the_limit(text: str) -> None:
    chunks = split_text(text, 50, 5)
    assert chunks
    assert all(estimate_tokens(chunk) <= 50 for chunk in chunks)
    assert _squash("".join(chunks)).startswith(_squash(chunks[0]))
    assert set(_squash(text)) == set(_squash("".join(chunks)))


def test_long_sentence_is_cut_at_word_boundaries() -> None:
    words = _no_punctuation(4000).split()
    chunks = split_text(" ".join(words) + ".", 50, 0)
    assert len(chunks) > 1
    pieces = [w for chunk in chunks for w in chunk.split()]
    assert pieces == [*words[:-1], words[-1] + "."]  # no word was cut


def test_overlap_zero_never_repeats_text() -> None:
    text = _numbered_sentences(200, per_paragraph=4)
    chunks = split_text(text, 80, 0)
    sentence_ids = [int(m) for chunk in chunks for m in re.findall(r"Sentence (\d+)", chunk)]
    assert sentence_ids == list(range(200))


def test_overlap_keeps_the_separator_so_words_never_run_together() -> None:
    words = [f"w{n}" for n in range(2000)]
    chunks = split_text(" ".join(words), 20, 5)
    assert len(chunks) > 1
    known = set(words)
    for chunk in chunks:
        assert set(chunk.split()) <= known  # a glued "w12w13" would not be a known word
    for prev, nxt in pairwise(chunks):
        tail = _tail(prev, nxt, 5)
        assert nxt[len(tail)] == " "


def test_overlap_inside_a_cut_word_adds_no_separator() -> None:
    word = _huge_word(2000)
    chunks = split_text(word, 50, 10)
    assert len(chunks) > 1
    assert all(chunk in word for chunk in chunks)  # each chunk is one contiguous slice
