# Context: Article Text Extraction (gh-issue-13)

**Branch**: `gh-issue-13` · **Issue**: #13 · **Milestone**: M2 Pipeline · **Depends on**: #10 (safe HTTP client, merged)

## Goal

Summaries need clean article text without navigation, footers, cookie banners or ads. Turn the
HTML of an article page into its main text plus title, publish date and language, with a
fallback so weak pages do not fail entirely.

## Existing building blocks

| What | Where | Notes |
|------|-------|-------|
| `NON_TEXT_TAGS`, `BLOCK_TAGS`, `collapse()` | `src/invio/sources/text.py` | shared text rules of the RSS and web sources |
| `_parse()`, `_node_text()`, `_title()` | `src/invio/sources/web.py` | selectolax (lexbor) parsing, non-text tags stripped, block tags separate words |
| `redact_url()` | `src/invio/sources/urls.py` | errors store redacted URLs only |
| `FetchError` | `src/invio/sources/errors.py` | pattern for a typed error with `reason` + redacted `url` |

Layering (ruff `TID251`): `invio.sources` must not import `invio.db`, `invio.services` or
`httpx2`. Extraction is pure (no network): it works on HTML that was already fetched.

## Design

1. **`src/invio/sources/extract.py`** (the issue's `scout/sources/extract.py`):
   `extract_text(html, url, *, max_chars=MAX_CHARS, min_chars=MIN_CHARS,
   max_input_chars=MAX_INPUT_CHARS) -> ExtractedText`.
   - `ExtractedText`: frozen kw-only dataclass `title: str | None`, `text: str`,
     `published_at: datetime | None` (aware, UTC), `language: str | None`, `truncated: bool`.
   - `MAX_CHARS = 200_000`, `MIN_CHARS = 200`, `MAX_INPUT_CHARS = 4_000_000` (module constants;
     overridable per call).
2. **Primary path**: `trafilatura.bare_extraction(html, url=url, favor_precision=True,
   with_metadata=True, include_comments=False)` — one pass gives text and metadata.
3. **Fallback**: when trafilatura returns no (or blank) text, or raises on hostile input, take
   the plain text of `<body>` (non-text tags dropped, block elements separate words) — same
   rules as the web source.
4. **Metadata**: title from trafilatura, else `<title>`/`og:title`; `published_at` from the
   trafilatura date (ISO `YYYY-MM-DD` → midnight UTC; unparsable → `None`); language from
   trafilatura, else `<html lang>` (primary subtag, lower-case), else `None`.
5. **Text normalization**: whitespace collapsed within lines, paragraphs kept as `\n` separated
   lines, blank lines dropped.
6. **Truncation**: text longer than `max_chars` is cut to `max_chars` (at a word boundary where
   possible) and `truncated=True`.
7. **`ExtractionError`** (in `extract.py`): `reason` and redacted `url`; `"too_short"` when the
   text (after fallback) is shorter than `min_chars`, `"too_large"` when the HTML is longer than
   `max_input_chars` (checked before any parsing).

## Dependencies

- `trafilatura>=2.0` (runtime; ships `py.typed`) — added with `uv add`.

## Acceptance criteria → tests

| Criterion | Test |
|-----------|------|
| Boilerplate (nav, footer, cookie banners) absent from fixture pages | `tests/test_source_extract.py` with fixtures under `tests/fixtures/articles/` |
| Title and publish date extracted where present | same, fixture with `<title>`/`og:title` and `article:published_time` |
| Very short pages raise `ExtractionError` | same |
| Overlong texts are truncated with `truncated=True` | same, with a small `max_chars` |
| Fallback when trafilatura finds nothing | same, monkeypatched trafilatura returning `None` / raising |

## Quality gates

```bash
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```

Coverage must stay ≥ 95 %.

## Implementation decisions

- **Shared helpers moved, not copied**: `_parse()` and `_node_text()` moved from `web.py` to
  `text.py` as `parse_html()` and `node_text(root, *, separator=" ")`. The fallback calls
  `node_text(..., separator="\n")` to get one line per block element. `node_text` now folds
  whitespace runs inside a text node to one space (as a browser renders them), so source line
  breaks inside a `<p>` do not split a paragraph; the web source collapses all whitespace
  afterwards anyway, so its text, hashes and tests are unchanged. `web.py` imports
  `parse_html as _parse`, because its tests import that name.
- **Typing**: trafilatura 2.3 ships `py.typed`; `bare_extraction` returns
  `Document | dict[str, Any] | None`. The result is narrowed with `isinstance(result, Document)`
  (a dict, only produced with `as_dict=True`, counts as "nothing found"), so no `Any` leaks.
- **Fallback trigger**: any `Exception` from trafilatura, a `None` result or blank text. When the
  text is blank but trafilatura returned a document, its title/date/language are still used.
- **Fallback title order**: `og:title` before `<title>` (the `<title>` usually carries a site
  suffix such as "| Tagesblatt"); blank values are skipped.
- **Language**: trafilatura only reports a language when a language detector is installed
  (not the case here), so in practice it comes from `<html lang>`. Both sources are reduced to
  the lower-case primary subtag and must match `[a-z]{2,3}` (ISO 639; the 4–8 letter BCP 47
  subtags are reserved or registered names, so `lang="english"` gives `None`); `_` is read as `-`.
- **Dates**: only from trafilatura (`date.fromisoformat` of the first 10 characters, so a
  trailing time is ignored; midnight UTC); the fallback path does
  not parse `article:published_time` itself.
- **Normalization**: NFC (as in the web source) in addition to per-line whitespace collapse and
  dropping blank lines.
- **Limits**: `ValueError` when `max_chars < 1`, `min_chars < 1` or `min_chars > max_chars`.
  `min_chars` is checked against the full text, before truncation. A single word longer than
  `max_chars` is cut inside the word.
- **Hostile input**: extraction time grows with the page size (≈1 s per MB, far more for pages
  near the 10 MiB HTTP response cap), so HTML longer than `max_input_chars` is rejected with
  `"too_large"` before parsing. Extraction is synchronous and CPU-bound; async callers run it via
  `asyncio.to_thread`. A trafilatura exception is logged at debug level (type only, no message
  or URL), so a broken upgrade that silently degrades every page to the fallback is visible.
  The selectolax tree is built lazily, only when a fallback (text, title or language) needs it.
  Tests cover control characters, lone surrogates, 50 000
  nested `<div>`s, unclosed tags, XML, ~2 MB of text and Hypothesis-generated strings.
- **Fixture note**: trafilatura drops repeated identical paragraphs, so the large-input test
  uses distinct paragraphs.
