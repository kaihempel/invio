# Research: Item Summarization with Map-Reduce Chunking

All Technical Context items were resolvable from the spec, its clarifications and the existing
code; no open NEEDS CLARIFICATION remain.

## R1 — Token estimate and constants

- **Decision**: `estimate_tokens(text) = ceil(len(text) / 4)` (0 for the empty string). Module
  constants in `summarize_item.py`: `CHUNK_MAX_TOKENS = 3000`, `CHUNK_OVERLAP_TOKENS = 200`,
  `SHORT_TEXT_MAX_TOKENS = CHUNK_MAX_TOKENS`, `COMBINE_MAX_TOKENS = CHUNK_MAX_TOKENS`,
  `MAX_CHUNKS = 20`.
- **Rationale**: the spec allows a simple heuristic; ~4 characters per token is the common
  rule of thumb for English and close for German. The estimate is pure and deterministic, so
  "no chunk exceeds `max_tokens`" is checked against the same function the splitter uses
  (FR-005). 3,000 tokens is far below every registered context window, so heuristic error in
  either direction is harmless for the provider. 20 × 3,000 ≈ 60k tokens caps one item at
  under a third of the default run budget (200k).
- **Alternatives considered**: provider token counter (`mistral-common` tokenizer) — a new
  dependency and provider-specific, rejected; word count × 1.3 — worse for code/URLs and
  non-Latin scripts, rejected.

## R2 — `split_text(text, max_tokens, overlap)` algorithm

- **Decision**:
  1. Validate: `max_tokens >= 1`, `0 <= overlap < max_tokens`, else `ValueError`. Empty or
     whitespace-only text → `[]`. Text with `estimate_tokens(text) <= max_tokens` → `[text]`.
  2. Break the text into atomic *units* by descending boundary level: paragraphs (split on
     blank lines `\n\s*\n`); a paragraph above the budget is split into sentences
     (`(?<=[.!?…。！？])\s+`); a sentence above the budget into words (`\s+`); a word above the
     budget into fixed character slices of `4 * budget` characters. Each unit remembers the
     separator that followed it (`"\n\n"` between paragraphs, `" "` otherwise).
  3. Pack units greedily into the *new text* of a chunk with a fixed budget
     `new_budget = max_tokens - overlap`: a unit is added only if
     `estimate_tokens(new_text + separator + unit) <= new_budget`. The budget used to
     sub-split oversized units in step 2 is the same `new_budget`, so every unit fits on its
     own.
  4. Overlap (only when `overlap > 0`, never for the first chunk): the next chunk is
     `tail + new_text`, where `tail` is taken from the end of the previous chunk and
     `estimate_tokens(tail) <= overlap`:
     - prefer the longest non-empty tail that starts at a word boundary (after whitespace);
     - if there is none within the last `4 * overlap` characters (CJK text, a single huge
       word, or the only whitespace is the final character), use the last `4 * overlap`
       characters as a character tail.
     Units are stripped of leading/trailing whitespace when they are created, so chunks do not
     end in whitespace.
     The tail is therefore never empty when `overlap > 0`, and because new text is capped at
     `new_budget`, `estimate_tokens(tail + new_text) <= max_tokens` always holds (the estimate
     is subadditive up to rounding: `ceil(a/4) + ceil(b/4) >= ceil((a+b)/4)`). No prefix
     shortening is needed, and every chunk adds non-empty new text, so the loop terminates.
  5. Separators: between packed units the remembered separator is used (`"\n\n"` after a
     paragraph, `" "` otherwise); original whitespace runs between units are normalised to
     these separators. The tail is joined to the new text with no extra separator (it already
     ends where the previous chunk ended), so the overlap is a literal prefix.
- **Rationale**: satisfies FR-002–FR-004 strictly — a non-empty overlap for every chunk pair
  when `overlap > 0`, including texts without spaces or punctuation — and all edge cases (no
  punctuation, single huge word, non-Latin scripts fall back to word/character cuts). Overlap
  is taken from the *end of the previous chunk* so tests can assert `next.startswith(tail)`,
  `tail != ""` and `estimate_tokens(tail) <= overlap`. Pure function, easy to property-test.
  Cost: chunks carry at most `max_tokens - overlap` new tokens (2,800 with the defaults).
- **Alternatives considered**: LangChain `RecursiveCharacterTextSplitter` — new dependency for
  ~60 lines, rejected; fixed character windows — violates paragraph/sentence preference,
  rejected; overlap measured in units (sentences) — harder to bound by tokens, rejected.

## R3 — Map/reduce flow and model tiers

- **Decision**:
  - Body = `item_text(None, teaser, raw_content)` (reused from `keyword_filter`); title sent
    separately in `<title>`.
  - `estimate_tokens(body) <= SHORT_TEXT_MAX_TOKENS` (also empty body) → **one** `fast` call
    with the whole body → final `ItemSummary`.
  - Otherwise → `chunks = split_text(body, CHUNK_MAX_TOKENS, CHUNK_OVERLAP_TOKENS)`;
    `kept = chunks[:MAX_CHUNKS]`; one `fast` call per kept chunk (map, output
    `ChunkSummary(bullets: 1–6)`, sequential, in order) → list of partial summaries.
  - Reduce: render partials as numbered plain-text parts (`ChunkSummary` → bullets only;
    `ItemSummary` from an earlier round → headline, bullets, why_relevant); greedily group
    consecutive parts so each group's rendered text is `<= COMBINE_MAX_TOKENS` (module constant,
    default `CHUNK_MAX_TOKENS`; separate so tests can lower it), but always ≥ 2 parts per group,
    so each round shrinks the list; one `smart` call per group with output `ItemSummary` → new
    partials; repeat until one remains. With ≤ 20 partials of ~100 tokens each, this is one
    `smart` call in practice.
  - Call counts for tests: short = 1; long = `len(kept)` + reduce calls (= 1 when the parts
    fit one request).
- **Rationale**: implements FR-006/FR-007 and clarification B (fast map, smart reduce). A
  dedicated, looser `ChunkSummary` lets a short tail chunk or an off-topic section answer with
  one or two bullets instead of being forced to invent three bullets and a relevance sentence
  (which would invite fabricated content or invalid answers that fail the whole item). The
  headline and `why_relevant` are only produced where the whole document is visible: the short
  call and the combine calls. Every answer is still schema-validated.
- **Alternatives considered**: reusing `ItemSummary` for chunks — forces 3 bullets and a
  relevance sentence per chunk, rejected (see rationale); merging a tiny last piece into the
  previous chunk — would break the `max_tokens` guarantee or need a second packing pass, and
  does not help off-topic chunks, rejected; free-text map outputs — unvalidated model text
  would be fed into the smart call, rejected; parallel map with `asyncio.gather` — faster but
  contradicts the sequential, rate-limit-friendly pattern of #16 and makes fake-script order
  non-deterministic, rejected for now.

## R4 — Chunk cap and truncation signal

- **Decision**: if `len(chunks) > MAX_CHUNKS`, only the first 20 are mapped; every reduce
  system prompt (all rounds, all groups) adds "The source text was truncated: only the first
  {kept} of {total} parts were summarized.", so the final combining request always carries
  the signal. A `summarize.truncated` log event carries `item_id`, `kept`,
  `dropped`. `ItemSummary` shape is unchanged; the outcome exposes `truncated: bool`.
- **Rationale**: clarification A (truncate, mark in combining input, log) without changing the
  stored shape.
- **Alternatives considered**: storing a `truncated` flag in the JSON — not requested, would
  widen the stored contract, rejected.

## R5 — Prompts, language and injection handling

- **Decision**:
  - Move `_DELIMITER`/`_neutralise` from `relevance.py` to a new
    `graph/nodes/prompting.py` as public `neutralise()`; `relevance.py` imports it (behaviour
    and prompts unchanged).
  - System message (all three kinds): task description, the job's
    `search.semantic_description` inside `<interest>`, the shape rules (headline one line,
    3–6 bullets, one-sentence `why_relevant` relating the item to the interest), "Write all
    text in {language_name} (ISO 639-1 code `{code}`)", and the untrusted-data rule from #16.
    Map prompts add "This is part {i} of {n} of a longer document"; reduce prompts say the
    input consists of partial summaries of one document, which are also untrusted data.
  - User message: `<document>\n<title>…</title>\n<content>…</content>\n</document>`, with
    title and content neutralised; reduce content is the numbered parts (`Part 1:` …), each
    rendered from a validated `ItemSummary` and neutralised again.
  - `temperature=0.0` for every call.
- **Rationale**: FR-008, FR-011 and US4 with one shared, already-tested neutralisation; no
  new delimiter tags so the regex stays identical.
- **Alternatives considered**: duplicating the regex in the new node — two copies to keep in
  sync, rejected; sending only the code (`de`) — models handle names more reliably, rejected.

## R6 — Storage and usage

- **Decision**: `ItemRepository.set_summary(item, summary_json)` sets `summary`, status
  `SUMMARIZED`, clears `last_error`, flushes. The JSON is
  `ItemSummary.model_dump_json()` (keys `headline`, `bullets`, `why_relevant`, UTF-8, no
  indentation); `ItemSummary.model_validate_json(item.summary)` restores an equal object.
  Usage: one `llm_usage` row per provider call (`complete_structured` incl. its repair
  request), `purpose` = `summarize` (short), `summarize_chunk` (map), `summarize_combine`
  (reduce); invalid answers record `err.usage` before failing.
- **Rationale**: clarification A (JSON in existing column, no migration) and FR-013; purpose
  values fit `String(64)` and distinguish map/reduce costs.
- **Layering of the read-back type**: `ItemSummary` stays in
  `invio.graph.nodes.summarize_item` (it is a Pydantic model, and `invio.domain` is stdlib-only
  by design). The later digest issue reads summaries back inside the `graph` layer (e.g. a
  digest node calling `ItemSummary.model_validate_json(item.summary)`) and hands `notify` only
  plain, already-rendered data (strings or stdlib dataclasses); `notify` never imports
  `ItemSummary`. The stored JSON keys (`headline`, `bullets`, `why_relevant`) are the stable
  contract between the two issues.
- **Alternatives considered**: one aggregated usage row per item — loses per-model cost (fast
  vs. smart priced differently), rejected; moving `ItemSummary` into `invio.domain` — breaks
  the stdlib-only rule of the domain module, rejected; a stdlib dataclass mirror in `domain`
  now — speculative until the digest issue needs it (constitution: simplicity first),
  deferred.

## R7 — Job `language` setting

- **Decision**: `JobConfig.language: str = "en"`, declared right after `schema_version` (so
  `dump_yaml` writes it near the top). Validator: must match `^[a-z]{2}$` and be a key of
  `invio.config.languages.ISO_639_1` (embedded `Mapping[str, str]` of all ISO 639-1 codes
  to English names); error `language: unknown ISO 639-1 language code 'xx'`. JSON schema:
  `pattern` + `enum`-free (keeps the schema readable); regenerated `docs/job.schema.json`;
  `docs/job.example.yaml` gains `language: en`; README "Job files" documents it. Schema
  version stays 1 (optional field with default). The wizard does not ask for it (default
  applies; editable via `job edit`).
- **Rationale**: clarification A of Q1; strict contract with field-named error (Principle I);
  no new dependency (`pycountry`/`langcodes` rejected for one lookup table).
- **Alternatives considered**: accepting any case and normalising — the spec fixes lower-case
  codes; `enum` with ~180 values in the schema — noisy for editors, rejected.

## R8 — Error classification

- **Decision**: identical to #16: `LLMInvalidOutputError` (after recording its usage),
  `LLMUnavailableError`, `LLMRateLimitError`, `LLMInvalidRequestError` in any call of an item
  → item `failed`, `last_error = "<ErrorClass>: <message>"` (fixed message for invalid
  output), no summary stored, remaining calls for that item skipped, next item continues.
  `LLMAuthError`, `LLMConfigError` and other exceptions propagate.
- **Rationale**: FR-014/FR-015; consistent behaviour across pipeline nodes.
- **Alternatives considered**: storing a partial summary from successful chunks — spec says no
  partial summary, rejected.
