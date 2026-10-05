"""Tests for the keyword prefilter node (rules, Unicode matching, persistence)."""

import unicodedata

import pytest
from sqlalchemy.orm import Session

from invio.config.job import KeywordsConfig
from invio.db.repositories import ItemRepository
from invio.domain import ItemStatus
from invio.graph.nodes.keyword_filter import MatchResult, item_text, keyword_filter, matches
from tests.db_helpers import make_item, make_job


def _kw(
    any_: list[str] | None = None,
    all_: list[str] | None = None,
    exclude: list[str] | None = None,
) -> KeywordsConfig:
    return KeywordsConfig(any=any_ or [], all=all_ or [], exclude=exclude or [])


# --- Rules ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keywords", "text", "matched"),
    [
        pytest.param(_kw(), "anything at all", True, id="empty-config-passes"),
        pytest.param(_kw(), "", True, id="empty-config-empty-text-passes"),
        pytest.param(_kw(any_=["llm", "agent"]), "an agent framework", True, id="any-one-hit"),
        pytest.param(_kw(any_=["llm", "agent"]), "a cooking blog", False, id="any-no-hit"),
        pytest.param(_kw(all_=["llm", "agent"]), "llm agent", True, id="all-every-hit"),
        pytest.param(_kw(all_=["llm", "agent"]), "llm only", False, id="all-one-missing"),
        pytest.param(_kw(exclude=["sponsored"]), "plain news", True, id="exclude-only-pass"),
        pytest.param(_kw(exclude=["sponsored"]), "Sponsored post", False, id="exclude-only-hit"),
        pytest.param(
            _kw(any_=["llm"], all_=["python"]), "llm in python", True, id="any-and-all-both"
        ),
        pytest.param(
            _kw(any_=["llm"], all_=["python"]), "python only", False, id="any-and-all-any-missing"
        ),
        pytest.param(
            _kw(any_=["llm"], all_=["python"]), "llm only", False, id="any-and-all-all-missing"
        ),
        pytest.param(
            _kw(any_=["llm"], exclude=["crypto"]), "llm and crypto", False, id="exclude-beats-any"
        ),
        pytest.param(
            _kw(all_=["llm"], exclude=["crypto"]), "llm news", True, id="all-and-exclude-pass"
        ),
        pytest.param(
            _kw(any_=["a1"], all_=["b1", "b2"], exclude=["x"]),
            "a1 b1 b2",
            True,
            id="all-three-pass",
        ),
        pytest.param(
            _kw(any_=["a1"], all_=["b1", "b2"], exclude=["x"]),
            "a1 b1 b2 x",
            False,
            id="all-three-excluded",
        ),
    ],
)
def test_rules(keywords: KeywordsConfig, text: str, matched: bool) -> None:
    assert matches(text, keywords).matched is matched


# --- Unicode, case and word boundaries -----------------------------------------------------


@pytest.mark.parametrize(
    ("term", "text", "matched"),
    [
        pytest.param("Äpfel", "Frische äpfel", True, id="umlaut-lower"),
        pytest.param("Äpfel", "FRISCHE ÄPFEL", True, id="umlaut-upper"),
        pytest.param("äpfel", "Äpfel aus der Region", True, id="umlaut-term-lower"),
        pytest.param(
            "Äpfel",
            unicodedata.normalize("NFD", "Äpfel im Angebot"),
            True,
            id="umlaut-nfd-text",
        ),
        pytest.param(
            unicodedata.normalize("NFD", "Äpfel"), "äpfel im Angebot", True, id="umlaut-nfd-term"
        ),
        pytest.param("Äpfel", "Apfel", False, id="umlaut-not-plain-vowel"),
        pytest.param("Straße", "STRASSE", True, id="sharp-s-casefold"),
        pytest.param("LLM", "new llm release", True, id="case-insensitive"),
        pytest.param("app", "Apfel", False, id="no-prefix-match"),
        pytest.param("app", "apples", False, id="no-word-prefix"),
        pytest.param("app", "webapp", False, id="no-word-suffix"),
        pytest.param("app", "the app.", True, id="punctuation-boundary"),
        pytest.param("app", "(app)", True, id="parenthesis-boundary"),
        pytest.param("app", "app-store", True, id="hyphen-boundary"),
        pytest.param("app", "app_store", False, id="underscore-is-word"),
        pytest.param("C++", "written in c++ today", True, id="cpp-lower"),
        pytest.param("C++", "C++20 features", False, id="cpp-followed-by-digit"),
        pytest.param("C++", "pure C code", False, id="cpp-not-c"),
        pytest.param("C#", "a C# library", True, id="csharp"),
        pytest.param(".NET", "built on .net core", True, id="leading-dot"),
        pytest.param(".NET", "x.NET", False, id="leading-dot-needs-non-word-before"),
        pytest.param("-foo", "a -foo", True, id="leading-hyphen"),
        pytest.param("-foo", "a-foo", False, id="leading-hyphen-needs-non-word-before"),
        pytest.param("x", "x\u0301 y", False, id="combining-mark-after-is-word"),
        pytest.param("y", "x\u0301y", False, id="combining-mark-before-is-word"),
        pytest.param("x", "x\u0301 x", True, id="combining-mark-later-match"),
        pytest.param("x\u0301", "x\u0301 y", True, id="term-with-combining-mark"),
        pytest.param("\u0390", "\u03aa\u0301", True, id="casefold-renormalized"),
        pytest.param("İstanbul", "İSTANBUL", True, id="turkish-dotted-i-upper"),
        pytest.param("İstanbul", "istanbul", False, id="turkish-dotted-i-not-plain-i"),
        pytest.param("large language model", "Large Language Model", True, id="phrase"),
        pytest.param(
            "large language model", "large\nlanguage\r\n  model", True, id="phrase-line-breaks"
        ),
        pytest.param("large language model", "large\tlanguage model", True, id="phrase-tab"),
        pytest.param(
            "large   language model", "large language model", True, id="phrase-term-spaces"
        ),
        pytest.param("large language model", "large language models", False, id="phrase-bound"),
        pytest.param("large language model", "large model language", False, id="phrase-order"),
        pytest.param("large language", "largelanguage", False, id="phrase-needs-whitespace"),
        pytest.param("  llm  ", "llm", True, id="term-stripped"),
        pytest.param("a.b", "axb", False, id="term-is-escaped"),
    ],
)
def test_term_matching(term: str, text: str, matched: bool) -> None:
    assert matches(text, _kw(any_=[term])).matched is matched


def test_blank_terms_are_ignored() -> None:
    # Validation rejects empty terms; whitespace-only ones can still reach the matcher.
    keywords = KeywordsConfig.model_construct(any=["   "], all=["\t"], exclude=[" "])
    assert matches("anything", keywords) == MatchResult(matched=True, hits=[])


# --- Hits ----------------------------------------------------------------------------------


def test_hits_in_config_order_with_original_spelling() -> None:
    keywords = _kw(any_=["Agent", "LLM", "missing"], all_=["Python"], exclude=["Crypto"])
    result = matches("crypto python llm agent", keywords)
    assert result == MatchResult(matched=False, hits=["Agent", "LLM", "Python", "Crypto"])


def test_hits_are_deduplicated() -> None:
    keywords = _kw(any_=["LLM", "llm"], all_=["LLM"], exclude=["Llm"])
    assert matches("llm", keywords) == MatchResult(matched=False, hits=["LLM"])


def test_hits_empty_without_matches() -> None:
    assert matches("nothing", _kw(any_=["llm"])) == MatchResult(matched=False, hits=[])


def test_hits_reported_when_passing() -> None:
    result = matches("Äpfel und Birnen", _kw(any_=["äpfel", "Kirschen"], all_=["Birnen"]))
    assert result == MatchResult(matched=True, hits=["äpfel", "Birnen"])


# --- item_text -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "teaser", "text", "expected"),
    [
        pytest.param("T", "Te", "Body", "T\nTe\nBody", id="all-parts"),
        pytest.param("T", None, None, "T", id="title-only"),
        pytest.param("T", None, "Body", "T\nBody", id="no-teaser"),
        pytest.param("T", "Te", None, "T\nTe", id="no-text"),
        pytest.param("T", "  ", "", "T", id="blank-parts-skipped"),
        pytest.param(None, "Te", "Body", "Te\nBody", id="no-title"),
        pytest.param(None, None, None, "", id="nothing"),
    ],
)
def test_item_text(title: str | None, teaser: str | None, text: str | None, expected: str) -> None:
    assert item_text(title, teaser, text) == expected


def test_item_text_parts_never_glue_into_one_word() -> None:
    # Joined by newlines, so the end of the title and the start of the teaser are separate words.
    assert matches(item_text("rust", "lang", None), _kw(any_=["rustlang"])).matched is False


def test_phrase_may_span_item_parts() -> None:
    assert matches(item_text("large", "language", None), _kw(any_=["large language"])).matched


# --- keyword_filter (persistence) ----------------------------------------------------------


@pytest.mark.db
def test_keyword_filter_rejects_and_stores_skipped_keyword(db_session: Session) -> None:
    job = make_job(db_session)
    item = make_item(db_session, job, title="Cooking tips", teaser="Recipes")
    repo = ItemRepository(db_session)
    result = keyword_filter(item, _kw(any_=["llm"]), repo)
    assert result == MatchResult(matched=False, hits=[])
    db_session.expire_all()
    assert repo.list_for_job(job.id, status=ItemStatus.SKIPPED_KEYWORD) == [item]


@pytest.mark.db
def test_keyword_filter_excluded_item_is_skipped(db_session: Session) -> None:
    job = make_job(db_session)
    item = make_item(db_session, job, title="LLM news", teaser="Sponsored")
    result = keyword_filter(
        item, _kw(any_=["LLM"], exclude=["sponsored"]), ItemRepository(db_session)
    )
    assert result == MatchResult(matched=False, hits=["LLM", "sponsored"])
    assert item.status == ItemStatus.SKIPPED_KEYWORD


@pytest.mark.db
@pytest.mark.parametrize("status", [ItemStatus.NEW, ItemStatus.EXTRACTED])
def test_keyword_filter_passing_item_keeps_status(db_session: Session, status: ItemStatus) -> None:
    job = make_job(db_session)
    item = make_item(db_session, job, title="New LLM agent", status=status)
    result = keyword_filter(item, _kw(any_=["agent"]), ItemRepository(db_session))
    assert result == MatchResult(matched=True, hits=["agent"])
    db_session.expire_all()
    assert item.status == status


@pytest.mark.db
def test_keyword_filter_empty_config_passes(db_session: Session) -> None:
    job = make_job(db_session)
    item = make_item(db_session, job, title="Anything")
    assert keyword_filter(item, _kw(), ItemRepository(db_session)).matched is True
    assert item.status == ItemStatus.NEW


@pytest.mark.db
def test_keyword_filter_uses_raw_content(db_session: Session) -> None:
    job = make_job(db_session)
    item = make_item(
        db_session,
        job,
        title="Weekly digest",
        teaser=None,
        raw_content="Deep in the article:\nÄpfel und große\nSprachmodelle.",
        status=ItemStatus.EXTRACTED,
    )
    keywords = _kw(all_=["äpfel", "große Sprachmodelle"])
    result = keyword_filter(item, keywords, ItemRepository(db_session))
    assert result == MatchResult(matched=True, hits=["äpfel", "große Sprachmodelle"])
    assert item.status == ItemStatus.EXTRACTED
