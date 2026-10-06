# Feature Specification: Item Summarization with Map-Reduce Chunking

**Feature Branch**: `gh-issue-17`

**Created**: 2026-10-06

**Status**: Draft

**Input**: User description: "GitHub issue #17: [FEAT] Add item summarization with map-reduce chunking. Video transcripts and long articles exceed what is sensible for one LLM call. Long texts are split, summarized per chunk, then combined into one item summary. Split at paragraph/sentence boundaries with a token estimate; short texts use one `fast` call producing 3–6 bullet points plus a one-sentence takeaway in the job's language; long texts are summarized per chunk and the chunk summaries are summarized again; output is a structured `ItemSummary(headline, bullets, why_relevant)`; the summary is stored on the item and its status becomes `summarized`. Acceptance: texts under the threshold use exactly one LLM call; longer texts are split and combined (verified with FakeProvider call counts); no chunk exceeds `max_tokens` and overlap is applied; summary language follows `job.language`."

## Clarifications

### Session 2026-10-06

- Q: Where should the summary language come from, and what should it default to when a job doesn't set it? → A: A new optional top-level `language` job setting holding an ISO 639-1 code (e.g. `en`, `de`), defaulting to `en`.
- Q: In what form should the finished summary be saved on the item? → A: As JSON (`headline`, `bullets`, `why_relevant`) in the existing `summary` text column; no schema change.
- Q: Which model tier should summarize the chunks of long texts and combine their summaries? → A: `fast` for short texts and chunk calls, `smart` for all combining calls.
- Q: Should there be a maximum number of chunks per item? → A: Yes, a fixed limit (default 20 chunks); text beyond it is not summarized and the combining input is marked as truncated.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Relevant items get a short, readable summary (Priority: P1)

A job owner receives a digest of items the relevance step marked as relevant. For each such
item, the summarization step produces a headline, 3–6 bullet points with the item's key
content, and a one-sentence takeaway explaining why the item matters for the job's research
interest. For a normal article that fits comfortably into one request, this is done with a
single call to the job's `fast` model. The summary is stored on the item and the item's status
becomes `summarized`.

**Why this priority**: The digest is only useful if readers can grasp each item in seconds.
This is the core value of the feature and covers the large majority of items (normal-length
articles).

**Independent Test**: Run the summarization step over a stored relevant item with a short text
and a fake model returning a fixed summary; check that exactly one model call was made, the
stored summary matches, and the status is `summarized`.

**Acceptance Scenarios**:

1. **Given** a relevant item whose text is below the chunking threshold, **When** it is
   summarized, **Then** exactly one model call is made using the job's `fast` model.
2. **Given** that call returns a valid summary, **When** the step finishes, **Then** the item's
   stored summary contains the headline, the bullet points and the takeaway, and its status is
   `summarized`.
3. **Given** a model answer with fewer than 3 or more than 6 bullet points, or an empty
   headline or takeaway, even after the provider's repair attempt, **When** the item is
   summarized, **Then** no summary is stored and the item is marked `failed` with a readable
   error.
4. **Given** several items to summarize, **When** the step runs, **Then** one outcome per item
   is returned to the caller in input order.

---

### User Story 2 - Long texts are summarized in parts and combined (Priority: P1)

Video transcripts and long articles are too long to send in one request. The system splits
such a text into chunks no larger than a configured token limit, cutting at paragraph
boundaries where possible and at sentence boundaries otherwise, with a small overlap between
neighbouring chunks so no statement is lost at a cut. Each chunk is summarized on its own
with the `fast` model (map), and the chunk summaries are then combined by the `smart` model
into one final item summary (reduce) of the same shape as for short texts.

**Why this priority**: Without this, long items either fail, get silently truncated, or blow
up cost. It is the defining capability of the issue and part of its acceptance criteria.

**Independent Test**: Summarize a long fixture text with a fake model and count the calls:
one per chunk plus the combining call(s). Separately, test the splitting function on fixture
texts and check chunk sizes and overlap.

**Acceptance Scenarios**:

1. **Given** a text estimated above the threshold that splits into N chunks, **When** it is
   summarized, **Then** the fake model receives N chunk-summary calls with the `fast` model
   followed by one combining call with the `smart` model (N + 1 calls in total, when the chunk
   summaries fit into one combining request).
2. **Given** any text and a token limit, **When** it is split, **Then** no chunk's estimated
   token count exceeds the limit.
3. **Given** a text split into several chunks with an overlap setting greater than zero,
   **When** consecutive chunks are compared, **Then** a non-empty end of each chunk reappears
   at the start of the next one, and the overlapping part never exceeds the overlap setting
   (this also holds for texts without spaces or punctuation).
4. **Given** a text made of paragraphs that are each below the limit, **When** it is split,
   **Then** no chunk starts or ends in the middle of a paragraph unless the overlap requires it.
5. **Given** a single paragraph larger than the limit, **When** it is split, **Then** it is cut
   at sentence boundaries; a single sentence larger than the limit is cut at word boundaries
   (and, as a last resort, by characters) so the limit is still respected.
6. **Given** the combined chunk summaries are themselves larger than the limit, **When** the
   combining step runs, **Then** they are combined in groups that fit the limit, repeated until
   one final summary remains (a group always holds at least two partial summaries, so a group
   may exceed the limit only when single partial summaries are very large).
7. **Given** a text that splits into more chunks than the chunk limit (default 20), **When** it
   is summarized, **Then** only the first chunks up to the limit are summarized (at most 20
   chunk calls), the combining request states that the source text was truncated, and the
   truncation (number of dropped chunks) is logged.

---

### User Story 3 - Summaries are written in the job's language (Priority: P2)

A job owner who reads digests in German wants every summary in German, even when the source
article or transcript is in English (and vice versa). Every summarization request, for short
texts, chunks and the combining step, instructs the model to answer in the job's configured
language.

**Why this priority**: Required by the acceptance criteria and important for readability, but
the feature already delivers value with a default language.

**Independent Test**: Summarize an item for a job configured with language "de" using a fake
model and inspect the recorded requests: each one asks for output in German.

**Acceptance Scenarios**:

1. **Given** a job with language `de`, **When** a short item is summarized, **Then** the request
   instructs the model to write the summary in German.
2. **Given** a job with language `de` and a long item, **When** it is summarized, **Then** every
   chunk request and every combining request instructs the model to write in German.
3. **Given** a job file without a language setting, **When** items are summarized, **Then** the
   default language (English) is used and the job file remains valid.
4. **Given** a job file with an unsupported or malformed language value, **When** it is loaded,
   **Then** loading fails with a message naming the `language` field.

---

### User Story 4 - Item content cannot steer the summary step (Priority: P2)

Item content comes from untrusted sources. As in the relevance step, the text (or chunk) is
only sent to the model inside clearly marked document delimiters, delimiter sequences inside
the text are neutralised, and the system message states that instructions inside the document
must be ignored. Model answers are always validated before they are stored.

**Why this priority**: Prevents source authors from injecting content into the digest
structure; consistent with the security posture already established for relevance scoring.

**Independent Test**: Summarize a fixture whose text contains "ignore previous instructions"
and a closing delimiter tag; inspect the prompts sent to the fake model.

**Acceptance Scenarios**:

1. **Given** an item whose text contains a closing document delimiter, **When** it is
   summarized, **Then** the text cannot end the data section early in any request (chunk or
   short).
2. **Given** any summarization request, **When** it is built, **Then** the item text or chunk
   appears only in the user message, and the system message says that document content is
   untrusted data whose instructions must be ignored.

---

### Edge Cases

- An item with no extracted text: it is summarized from its title and teaser in one call.
- Text exactly at the threshold: treated as short (one call).
- Very long text (e.g. a multi-hour transcript) producing more chunks than the chunk limit:
  only the first chunks up to the limit are summarized and the result is marked as based on a
  truncated source in the combining request.
- Overlap setting of zero: chunks are adjacent with no repeated text.
- Overlap setting greater than or equal to the token limit: rejected as invalid input to the
  splitter (it could otherwise never make progress).
- A token limit below 1: rejected as invalid input.
- One chunk call fails (invalid answer, unavailable, rate limited, request rejected): the whole
  item is marked `failed` with a readable error; no partial summary is stored; the step
  continues with the next item.
- Credential or configuration errors: they stop the step instead of marking items one by one.
- Texts with unusual whitespace (only line breaks, no paragraph separators, very long lines):
  splitting still respects the limit.
- Non-Latin scripts or text without sentence punctuation: splitting falls back to word or
  character boundaries and still respects the limit.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST define an item summary consisting of a headline (non-empty, one
  line), 3 to 6 bullet points (each non-empty), and `why_relevant`, a non-empty one-sentence
  takeaway explaining why the item matters for the job's research interest (the sentence count
  is requested from the model, not validated).
- **FR-002**: The system MUST provide a text splitting function taking a text, a maximum chunk
  size in tokens, and an overlap in tokens, returning an ordered list of chunks.
- **FR-003**: Splitting MUST prefer paragraph boundaries, then sentence boundaries, then word
  boundaries, and only as a last resort cut inside a word, so that no chunk's estimated token
  count exceeds the maximum.
- **FR-004**: With an overlap setting greater than zero, every chunk after the first MUST begin
  with a non-empty piece of text taken from the end of the previous chunk, no larger than the
  overlap setting; the overlap MUST always fit, so new text per chunk is limited to the token
  limit minus the overlap. With overlap 0 chunks MUST NOT repeat text.
- **FR-005**: Token counts MUST be estimated with one deterministic, documented method (e.g. a
  characters-per-token heuristic); the same estimate MUST be used for the short/long threshold,
  for splitting, and for checking that no chunk exceeds the limit.
- **FR-006**: Items whose estimated text size is at or below the threshold MUST be summarized
  with exactly one model call using the job's `fast` model.
- **FR-007**: Items above the threshold MUST be split; each chunk MUST be summarized with one
  `fast` model call into a chunk summary (1–6 bullet points, no headline or takeaway required,
  so short or off-topic chunks need not invent content), and the chunk summaries MUST then be
  combined with the job's `smart` model
  into one final item summary of the shape defined in FR-001. If the chunk summaries do not fit
  into one combining request, they MUST be combined in groups (each holding at least two
  partial summaries) repeatedly until one summary
  remains. All combining calls, including intermediate group combinations, use the `smart`
  model.
- **FR-007a**: The number of chunks summarized per item MUST be capped by a fixed limit
  (default 20). Chunks beyond the limit MUST NOT be sent to the model; every combining request
  (in every round) MUST state that the source text was truncated, and the truncation, with the number of
  dropped chunks, MUST be logged. The stored summary keeps the shape defined in FR-001.
- **FR-008**: Every summarization request (short, chunk and combining) MUST instruct the model
  to write in the job's configured language.
- **FR-009**: The job configuration MUST offer an optional top-level `language` setting holding
  a two-letter lower-case ISO 639-1 code (e.g. `en`, `de`), defaulting to `en` when omitted;
  existing job files without it MUST remain valid, and values that are not a known ISO 639-1
  code MUST be rejected with a message naming the `language` field.
- **FR-010**: Every model answer MUST be validated against the expected summary shape before
  use; an answer that is still invalid after the provider's repair attempt MUST NOT be stored.
- **FR-011**: The item text or chunk MUST appear only inside document delimiters in the user
  message, with delimiter sequences in the content neutralised; the system message MUST include
  the job's research interest and state that document content is untrusted data whose
  instructions must be ignored.
- **FR-012**: On success the system MUST store the final summary on the item as a JSON object
  with the keys `headline`, `bullets` and `why_relevant` in the item's existing `summary` text
  field (no database schema change), such that reading it back yields an equal item summary,
  and set the item status to `summarized`.
- **FR-013**: The system MUST store one usage record per model call (chunk, combining and short
  calls alike), including calls whose answer was ultimately invalid, with a purpose that
  identifies summarization.
- **FR-014**: When any model call for an item fails with a per-request error (invalid answer
  after repair, unavailable, rate limited, request rejected), the system MUST mark that item
  `failed` with a readable error that never contains document text, store no summary, and
  continue with the next item.
- **FR-015**: Errors affecting every item equally (missing or rejected credentials,
  configuration errors) MUST stop the summarization step.
- **FR-016**: The step MUST return one outcome per item, in input order, carrying the item's
  final status, its summary (if any), its error (if any) and the number of model calls made.

### Key Entities

- **Item summary**: the validated result for one item — headline, 3–6 bullet points,
  `why_relevant` takeaway sentence.
- **Chunk summary**: the validated intermediate result for one chunk — 1–6 bullet points;
  never stored.
- **Chunk**: a contiguous part of an item's text, within the token limit, possibly beginning
  with overlap from the previous chunk; exists only during summarization.
- **Item**: a stored source entry with title, teaser, extracted text, status, summary and last
  error; this feature sets summary, status (`summarized`, `failed`) and last error.
- **Job language**: new optional top-level job setting, an ISO 639-1 code naming the language
  summaries are written in (default `en`).
- **Usage record**: tokens, model, run, purpose and cost of one model call.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of items at or below the threshold are summarized with exactly one model
  call.
- **SC-002**: For long fixture texts, the number of model calls equals the number of chunks plus
  the number of combining calls, in 100% of test runs.
- **SC-003**: Across all splitting tests (including texts with very long paragraphs, very long
  sentences, no punctuation and non-Latin scripts), 0 chunks exceed the token limit.
- **SC-004**: With a non-zero overlap, 100% of consecutive chunk pairs share non-empty
  overlapping text within the configured overlap size, including texts without spaces or
  punctuation.
- **SC-005**: 100% of summarization requests for a job carry an instruction to write in that
  job's configured language.
- **SC-006**: In a batch where one item's model call fails, every other item in the batch still
  reaches a final status (`summarized` or `failed`).
- **SC-007**: Every successfully summarized item has 3–6 bullet points, a headline and a
  takeaway, and status `summarized`.

## Assumptions

- The step summarizes the items it is given (normally those with status `relevant` after
  issue #16); selecting items and wiring the step into the pipeline graph belongs to the
  pipeline issue.
- The issue's file path `scout/graph/nodes/summarize_item.py` refers to the project's graph
  nodes package (`src/invio/graph/nodes/`), following the existing relevance and keyword nodes.
- The issue's "one-sentence takeaway" is the `why_relevant` field of the item summary.
- Defaults: chunk limit in the low thousands of tokens (e.g. 3,000) with an overlap of a few
  hundred tokens (e.g. 200); the short/long threshold equals the chunk limit; at most 20 chunks
  are summarized per item. These are module constants, not job settings.
- Token estimation uses a simple characters-per-token heuristic; no provider token counter is
  required.
- Chunk summaries are intermediate results and are not persisted.
- Enforcement of the per-run token budget (`max_llm_tokens_per_run`) is out of scope.
- Adding the optional top-level `language` field with a default does not require a job schema
  version bump. The README / docs describe the new
  setting.
- Provider behaviour (one repair request on invalid structured output, rate-limit retries) is
  reused from the LLM layer, as in the relevance step.
- Dependencies: #7 (LLM provider layer) and #8 (fake provider / model roles) are merged.
