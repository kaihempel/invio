# Research: Digest Synthesis (gh-issue-18)

All Technical Context items were resolvable from the codebase; no NEEDS CLARIFICATION remained.
Each section records the decision, its rationale and the alternatives considered.

## R1 — Module location and layering

- **Decision**: new module `src/invio/graph/nodes/synthesize.py` (the issue's
  `scout/graph/nodes/synthesize.py`). It imports `invio.llm`, `invio.db` (only for the `Item`
  type and `UsageRepository`), `invio.config.languages`, `invio.domain` and the shared node
  helpers `llm_calls` / `prompting`, plus `ItemSummary` from `summarize_item`.
- **Rationale**: matches the relevance and summarization nodes; respects the dependency
  direction checked by `tests/test_graph_layering.py` (graph never imports `cli`,
  `scheduling` or `notify`).
- **Alternatives**: putting synthesis into `invio.notify` (rejected: notify is an adapter layer
  below graph and must not call LLMs); one shared "nodes" mega-module (rejected: simplicity).

## R2 — Free-text model call and usage recording

- **Decision**: the digest is requested as free Markdown with `LLMProvider.complete(...)`
  (`temperature=0.0`, `max_tokens=DIGEST_MAX_OUTPUT_TOKENS = 4000`) through a new helper
  `call_text(ctx, *, model, purpose, system, user, max_tokens) -> str` in
  `graph/nodes/llm_calls.py`, which records one `llm_usage` row (`purpose="synthesize"`) for
  the call. A failed call (exception) records no usage, as the provider returns no usage then.
- **Rationale**: the issue asks for Markdown output; a structured JSON digest would need a
  renderer and loses the model's prose freedom. Reusing the `CallContext` protocol keeps usage
  recording uniform with `call_structured`. `temperature=0.0` matches all other nodes and keeps
  fake-provider tests trivially deterministic.
- **Alternatives**: structured output (`sections: [{title, items: [{url, text}]}]`) rendered
  by invio — guarantees URLs and structure but contradicts the issue's "prompt for Markdown";
  kept as a possible follow-up if the post-check proves insufficient.

## R3 — Input entries and ordering

- **Decision**: a frozen `DigestEntry(item_id, url, title, relevance, published_at, summary)`
  built by `entries_from_items(items)` from stored `Item` rows: only items with status
  `summarized` whose `summary` JSON validates as `ItemSummary` are used; other items are
  skipped and counted in one `synthesize.skipped` log event (ids only). `relevance` is converted
  from `Decimal` to `float` (`None` stays `None`). Ordering key (FR-002):
  `(-(relevance or -1.0), published_at is None, -published_at.timestamp(), item_id)`.
- **Rationale**: the node then works on validated data only (Constitution I); the sort is a
  pure, total order, so prompts and the closing list are deterministic.
- **Alternatives**: passing raw `Item` rows to the prompt builder (rejected: summary JSON would
  be parsed in several places); sorting by DB query (rejected: the node must also work on
  in-memory lists from the graph state).

## R4 — Prompt layout and injection handling

- **Decision**: system message = task ("write a news digest for a reader with a research
  interest"), `<interest>` (job's semantic description), output rules (intro of 2–4 sentences
  on what is new; `##` headings for each theme; under each heading one bullet per item: title
  as Markdown inline link `[title](url)` + 1–2 sentences of what is new; every statement must
  come from the provided items; only the provided URLs, written exactly; no reference-style
  links, no raw HTML, no images, no closing / "Worth a closer look" section, no top-level
  `#` title), language instruction (`Write all text in {name} (ISO 639-1 code "{code}").`, same
  wording as #17) and the untrusted-data rule. User message = one `<document>` block per entry
  via the existing `document_message(title, content)`, where `content` lists `Item <n>`, `URL`,
  `Relevance`, `Published` (ISO date or `unknown`), the headline, bullets and takeaway. All
  entries are sent in relevance order.
- **Rationale**: reuses `prompting.neutralise` / `document_message`, so delimiter handling is
  identical to the other nodes and needs no change to the shared regex. Asking for inline links
  only keeps the post-check (R5) simple and makes "is this item linked" checkable.
- **Alternatives**: a new `<items>`/`<item>` tag family (rejected: would require extending the
  shared neutralise regex used by relevance and summarization).

## R5 — URL post-check

- **Decision**: a pure function `filter_urls(markdown, allowed) -> UrlFilterResult(text,
  removed, linked)` applied to the model answer, in this order:
  1. **Inline links and images** `[text](dest "title")` / `![alt](dest)` (destination in
     `<…>` or bare with one level of balanced parentheses): kept unchanged if `dest` is
     exactly an allowed URL (images are always replaced, see below); otherwise replaced by
     `text` (image → `alt`).
  2. **Reference definitions** `[label]: dest …` (own line): kept if `dest` is allowed,
     otherwise the line is removed; a `[text][label]` without a definition renders as literal
     text in CommonMark, so no further rewrite is needed.
  3. **Autolinks** `<scheme:…>`: kept if allowed, otherwise removed.
  4. **Raw HTML** (the notifier renders Markdown with `html=True`): every `<a …>` / `</a>`,
     `<img …>` and any other tag carrying `href`/`src` is removed regardless of its URL (tag
     only, inner text kept); raw HTML links never count as `linked`, because the prompt forbids
     HTML and only Markdown links are checked for missing items.
  5. **Bare URLs** (`scheme://…`, `www.…`, also inside code spans/blocks): a boundary-aware
     scanner keeps an allowed URL only where the occurrence ends at a URL boundary —
     whitespace, `)`, `]`, `>`, `<`, `"`, `'`, backtick, end of text, or trailing `.,;:!?`
     followed by one of those — and removes every other URL-like run (no mask/restore, so no
     placeholder can collide with model text).
  Steps 1–5 repeat until the text stops changing, because removing one construct can splice a
  new one together (`[[a](bad)](ok)`).
  6. **Rendered check** (added after QA found a leak): the settled text is parsed with
     the markdown-it-py parser the notifier renders with (`invio.markdown.MARKDOWN`, raw HTML
     on), shared so the check and the mail cannot drift apart. The regex passes only cover a
     subset of CommonMark (nested/escaped brackets in link text, `(title)` titles, definitions
     inside list items, `<` inside HTML attributes, destinations without `//` such as
     `https:host` or `mailto:`). If the parse still yields a link to a non-allowed
     destination, an image or raw HTML, every `[`, `]` and `<` outside the kept allowed links
     is removed and the passes run again; if nothing settles within the pass cap, all link
     syntax, tags and URL runs are stripped (fail closed).
  `removed` counts every dropped URL; `linked` is the set of allowed URLs that are link
  destinations in the parsed output of step 6. Comparison is exact string equality (after
  unwrapping one `<…>` pair); no normalisation.
  Images are never kept, even with an allowed URL (mail clients would load remote images); the
  alt text remains.
- **Rationale**: regex-level rewriting preserves the model's text exactly where it is valid;
  CommonMark parsers (markdown-it-py) give token positions only per line, so they cannot
  rewrite inline spans reliably and are used only as the oracle in step 6. Matching allowed
  URLs at the scan position prevents an allowed URL with parentheses or query strings from
  being cut, while the boundary rule makes an altered URL (`allowed + "/extra"`) count as
  unknown (spec US2-AS4).
- **Alternatives**: parse with markdown-it-py and re-render Markdown (rejected: re-rendering
  changes formatting; markdown-it-py is used for verification only); normalising URLs
  (rejected by spec: exact match only).
- **Safety net**: the notifier's nh3 allowlist (#20) still strips unsafe schemes and attributes
  downstream; the post-check guarantees provenance, nh3 guarantees safety.

## R6 — Structure check, missing items, appended sections

- **Decision**: after `filter_urls`, the answer is accepted when it is not blank and contains at
  least one ATX heading line (`^ {0,3}#{1,6}[ \t]+\S`) outside fenced code. One surrounding
  ```` ```markdown ```` / ```` ``` ```` fence is stripped before the check. Entries whose URL is
  not in `linked` are "missing" (FR-005b) and are rendered by invio into a `## More items`
  section; then the `## Worth a closer look` section (top three entries) is appended. Both use
  `- [title](url) — takeaway`, with titles and takeaways cleaned: URL-like runs removed, line breaks collapsed to spaces, and a
  backslash only before `` \ ` * _ [ ] < > & `` (the digest Markdown is also the plain-text mail
  body in #20, so other punctuation stays readable) and the URL written in
  bare form, or `<…>` only when it contains parentheses (`<`, `>`, space and line breaks are percent-encoded).
- **Rationale**: the escaping ensures untrusted titles cannot open links, emphasis, code or HTML
  (FR-005a "inserted as text"), and removing URL-like runs keeps the "only input URLs" invariant
  for text invio inserts itself. Escaping every punctuation character was rejected because the
  notifier sends the raw Markdown as the plain-text mail part, where backslashes would show.
- **Alternatives**: setext headings accepted too (rejected: the prompt asks for `##`; one rule
  keeps the check simple); counting a bare allowed URL as "linked" (rejected: with
  `linkify=False` in the notifier it would not be clickable).

## R7 — Fixed texts and their translations

- **Decision**: a module-level table `_TEXTS: Mapping[str, _FixedTexts]` with `en` and `de`
  entries (`closer_look`, `more_items`, `fallback_intro`, `fallback_heading`); unknown codes use
  `en`. Fallback intro: en "The automatic summary of this run could not be written. Here are all
  {n} items, most relevant first." / de "Die automatische Zusammenfassung dieses Laufs konnte
  nicht erstellt werden. Hier sind alle {n} Einträge, die relevantesten zuerst."
- **Rationale**: spec assumption (at least English and German; fallback English). Keeping the
  table in the node module avoids a new config surface.
- **Alternatives**: asking the model for the headings (rejected: the fallback has no model);
  gettext catalogs (rejected: overkill for four strings).

## R8 — Fallback and error classification

- **Decision**: `PER_ITEM_ERRORS` from `llm_calls` (`LLMUnavailableError`,
  `LLMRateLimitError`, `LLMInvalidRequestError`, `LLMInvalidOutputError`) and an unusable answer
  (blank / no heading after the post-check) produce the fallback digest; `LLMAuthError`,
  `LLMConfigError` and anything else propagate. The fallback is a fixed intro, one section with
  all entries (`- [title](url) — headline` + takeaway on the next line, escaped as in R6) and
  the closer-look section. `SynthesisResult.fallback=True`, `error` holds
  `failure_message(err)` (or `"unusable answer: <reason>"`), logged as `synthesize.fallback`
  with the error class only. `run_status_after_synthesis(planned, result)` returns `PARTIAL` for
  a planned `SUCCEEDED` with `fallback=True`, mirroring `notify.run_status_after_delivery`.
- **Rationale**: same split as #17 (per-request vs. setup errors); the status helper lets the
  later pipeline issue apply FR-016 without re-deriving the rule.
- **Alternatives**: a retry before falling back (rejected in clarification Q2; provider-level
  rate-limit retries already exist).

## R9 — Scale

- **Decision**: no cap on entries. With `limits.max_items_per_run` (default 100) and ~150
  estimated tokens per rendered entry, the request is ≈ 15k tokens — well within the `smart`
  models' context windows; `DIGEST_MAX_OUTPUT_TOKENS = 4000` bounds the answer. If the answer is
  cut at the limit the post-check and appended "More items" section still guarantee every item
  appears.
- **Rationale**: spec assumption (single request, no chunking); the missing-items section makes
  truncation of the model answer harmless.
- **Alternatives**: map-reduce synthesis per theme (rejected: themes are model-chosen, and
  volume does not require it).
