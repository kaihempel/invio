# Feature Specification: Digest Synthesis

**Feature Branch**: `gh-issue-18`

**Created**: 2026-10-06

**Status**: Draft

**Input**: User description: "GitHub issue #18 — Add digest synthesis node. The `smart` model turns all item summaries of a run into one digest: grouped by topic, with source links and a short "what is new" framing. Implementation: create `scout/graph/nodes/synthesize.py`; input is a list of `ItemSummary` with URL, title, relevance and publish date, sorted by relevance; prompt for Markdown output with a short intro, thematic sections, each item with link, and a closing "Worth a closer look" list of the top 3 items; enforce that every statement is attributable to a given item and only provided URLs appear in the output; post-check extracts all URLs from the output and drops any not present in the input; if there are no items, return an empty digest without an LLM call and let the notify step (#20) decide. Acceptance criteria: output is Markdown with at least one section per theme when items exist; URLs not present in input are removed by the post-check (tested with fake output); empty input produces an empty digest without an LLM call; digest language follows `job.language`. Depends on #7, #8. Branch: issue/18-add-digest-synthesis-node."

## Clarifications

### Session 2026-10-06

- Q: Who writes the closing "Worth a closer look" list — the model or invio after the call? → A: invio appends it deterministically from the top 3 items by relevance (title linked to URL, heading in the job's language); the model writes only the intro and the thematic sections.
- Q: What should the run deliver when the `smart` model call fails? → A: invio builds a plain fallback digest without the model (one section, all items in relevance order with linked title, headline and takeaway, plus the closing list); the run ends `partial`.
- Q: What happens when the model's answer leaves out input items? → A: invio appends a "More items" section (heading in the job's language) before the closing list, with each missing item's linked title and takeaway; an item counts as missing when no link to its URL remains after the post-check; the count is logged.

## User Scenarios & Testing *(mandatory)*

The "user" of this feature is the **digest reader**: a recipient of a job's digest who wants to
see, in a few minutes of reading, what is new in their research area since the last run. The
**operator** is a secondary user who relies on the digest being trustworthy (no invented
sources) and on the run behaving predictably when nothing was found.

### User Story 1 - Read one digest that groups the run's findings by topic (Priority: P1)

After a run has summarized its relevant items, the reader receives a single digest instead of a
flat list of summaries. The digest opens with a short introduction framing what is new, then
presents the items in thematic sections (each with a heading naming the topic). Every item
appears in a section with its title linked to its source, followed by a short description of
what is new about it. The intro and the sections are written by the job's `smart` model in one
request, using the items in order of relevance. After the call, invio itself appends a closing
"Worth a closer look" list with the three most relevant items, so that list is always present
and always matches the relevance ranking.

**Why this priority**: This is the end product of the research pipeline; without it the item
summaries never reach the reader in a readable form. It is the core of the issue.

**Independent Test**: Run the synthesis step with five summarized items and a fake model that
returns a fixed, well-formed Markdown digest; check that exactly one `smart` model call was made,
that the request lists the items ordered by relevance, and that the returned digest contains the
model's intro and thematic sections followed by the appended "Worth a closer look" list.

**Acceptance Scenarios**:

1. **Given** a run with at least one summarized item, **When** the digest is synthesized,
   **Then** exactly one model call is made, using the job's `smart` model.
2. **Given** items with relevance 0.4, 0.9 and 0.7, **When** the request is built, **Then** the
   items appear in it in the order 0.9, 0.7, 0.4.
3. **Given** items with equal relevance, **When** the request is built, **Then** the more
   recently published item comes first, and items without a publish date come after dated ones
   (ties beyond that keep a stable, deterministic order).
4. **Given** the model returns an intro and two thematic sections, **When** synthesis finishes,
   **Then** the returned digest is that Markdown text followed by the appended "Worth a closer
   look" section, and lists the identifiers of all input items it was built from.
5. **Given** five items, **When** synthesis finishes, **Then** the "Worth a closer look" section
   lists exactly the three most relevant items in relevance order, each as its title linked to
   its URL followed by its why-relevant takeaway.
6. **Given** a run with fewer than three items, **When** synthesis finishes, **Then** the
   "Worth a closer look" section lists all items (one or two entries).
7. **Given** the model answer contains no thematic section heading, **When** synthesis finishes,
   **Then** the answer is rejected and the fallback digest (User Story 6) is returned instead.
8. **Given** any synthesis request, **When** it is built, **Then** it asks the model not to write
   a closing or "Worth a closer look" section itself.
9. **Given** five items and a model answer that links only three of them, **When** synthesis
   finishes, **Then** a "More items" section listing the two missing items (linked title and
   takeaway, in relevance order) is inserted before the "Worth a closer look" section, and the
   number of missing items is logged.
10. **Given** a model answer that links every input item, **When** synthesis finishes, **Then** no
    "More items" section is added.

---

### User Story 2 - Only real sources appear in the digest (Priority: P1)

The reader must be able to trust every link in the digest. The model is instructed to make only
statements that come from the provided item summaries and to link only the provided URLs. Because
a model can still invent or alter links, every URL in the model's answer is checked against the
URLs of the input items after the call. Any URL that is not one of the input URLs is removed: a
Markdown link with an unknown target keeps its visible text but loses the link, and a bare
unknown URL is deleted. The number of removed URLs is logged.

**Why this priority**: An invented link in a research digest is worse than no link. This
safeguard is an explicit acceptance criterion of the issue.

**Independent Test**: Feed the step a fake model answer containing one valid input URL, one
invented URL as a Markdown link and one invented bare URL; check the returned digest.

**Acceptance Scenarios**:

1. **Given** a model answer containing a Markdown link whose target is not an input URL,
   **When** the post-check runs, **Then** the link target is removed and its visible text
   remains as plain text.
2. **Given** a model answer containing a bare URL (autolink or plain text) that is not an input
   URL, **When** the post-check runs, **Then** that URL no longer appears in the digest.
3. **Given** a model answer whose Markdown links all point to input URLs and that contains no
   images or raw HTML, **When** the post-check runs, **Then** the digest is returned unchanged.
4. **Given** a model answer that contains an input URL with a slightly altered form (e.g. an
   extra path segment or different query string), **When** the post-check runs, **Then** it is
   treated as unknown and removed (only exact input URLs are kept).
5. **Given** URLs were removed, **When** synthesis finishes, **Then** a log entry records how
   many were removed, without logging digest text.
6. **Given** any synthesis request, **When** it is built, **Then** the system message instructs
   the model that every statement must be attributable to one of the provided items and that
   no URLs other than the provided ones may appear.

---

### User Story 3 - A run without findings produces an empty digest at no cost (Priority: P1)

When a run has no summarized items, there is nothing to synthesize. The step returns an empty
digest (empty text, no items) immediately, without calling any model, so no tokens are spent.
Whether an empty digest is mailed is decided by the notification step (#20, `send_if_empty`).

**Why this priority**: Most scheduled runs on quiet topics find nothing new; they must not cost
money or produce a hallucinated digest. Explicit acceptance criterion of the issue.

**Independent Test**: Run the step with an empty item list and a fake model; check the fake model
recorded zero calls and the returned digest is empty.

**Acceptance Scenarios**:

1. **Given** no items, **When** synthesis runs, **Then** no model call is made and no usage is
   recorded.
2. **Given** no items, **When** synthesis runs, **Then** the returned digest has empty text and
   an empty item list, which the notification step recognises as an empty digest.

---

### User Story 4 - The digest is written in the job's language (Priority: P2)

A job owner who reads in German receives a German digest, even when the sources and their
summaries are in English. The synthesis request instructs the model to write the whole digest,
including the intro and section headings, in the job's configured language. The heading of the
closing list, which invio appends itself, is also shown in that language.

**Why this priority**: Required by the acceptance criteria and consistent with item summaries
(#17), but the digest already delivers value in the default language.

**Independent Test**: Synthesize a digest for a job with language `de` using a fake model and
inspect the recorded request.

**Acceptance Scenarios**:

1. **Given** a job with language `de`, **When** the digest is synthesized, **Then** the request
   instructs the model to write the digest in German.
2. **Given** a job without a language setting, **When** the digest is synthesized, **Then** the
   request asks for English (the job default).
3. **Given** a job with language `de`, **When** synthesis finishes, **Then** the appended closing
   section uses the German heading; for a language without a built-in heading translation, the
   English heading "Worth a closer look" is used.

---

### User Story 5 - Item content cannot steer the digest (Priority: P2)

Item titles and summaries derive from untrusted web content. As in the relevance and
summarization steps, they are sent to the model only inside clearly marked data delimiters,
delimiter sequences inside them are neutralised, and the system message states that instructions
inside the data must be ignored.

**Why this priority**: The digest is mailed to people; injected content must not be able to
rewrite its structure or add links (the URL post-check is the second line of defence).

**Independent Test**: Synthesize with an item whose title contains "ignore previous instructions"
and a closing delimiter; inspect the request sent to the fake model.

**Acceptance Scenarios**:

1. **Given** an item title or summary containing a closing data delimiter, **When** the request
   is built, **Then** it cannot end the data section early.
2. **Given** any synthesis request, **When** it is built, **Then** item data appears only in the
   user message and the system message says that the data is untrusted and its instructions
   must be ignored.

---

### User Story 6 - Readers still get the findings when the model fails (Priority: P2)

When the synthesis call fails (provider unavailable, rate limited, request rejected) or its answer
is unusable (empty or without a thematic section), the run's findings are not lost. Invio builds a
plain digest itself: a short fixed intro, one section listing all items in relevance order (each
with its title linked to its URL, its headline and its why-relevant takeaway), and the usual
"Worth a closer look" section. The digest is marked as a fallback, the failure is logged, and the
run ends `partial` so the operator sees that synthesis did not work.

**Why this priority**: The item summaries already cost money and contain the run's value; a
temporary provider problem should degrade the digest's quality, not drop it.

**Independent Test**: Run the step with three items and a fake model that raises an
"unavailable" error (and, separately, one that returns text without any heading); check the
returned digest and its fallback flag.

**Acceptance Scenarios**:

1. **Given** the model call fails with a per-request error, **When** synthesis finishes, **Then**
   a fallback digest is returned that contains every input item in relevance order with its
   linked title, headline and takeaway, followed by the "Worth a closer look" section.
2. **Given** the model answer is empty or has no thematic section heading, **When** synthesis
   finishes, **Then** the fallback digest is returned instead of the model answer.
3. **Given** a fallback digest was returned, **When** the run finishes, **Then** a run that would
   have ended `succeeded` ends `partial`; a run already `partial` or `failed` keeps its status.
4. **Given** a fallback digest was returned, **When** the logs are inspected, **Then** one entry
   records that the fallback was used and the error category, without item text.
5. **Given** a job with language `de`, **When** a fallback digest is built, **Then** its fixed
   intro and headings use the German wording (English when no built-in translation exists).

---

### Edge Cases

- Exactly one item: the digest has an intro, one section and a closing list with that one item.
- The model writes its own closing or "Worth a closer look" section despite the instruction: it is
  kept as model text (its URLs are post-checked like all others); the appended section is still
  added, so the digest may show two closing lists.
- All items on one topic: a single thematic section is valid.
- Items without a publish date: included normally; the date is shown as unknown in the request.
- Items without relevance score: treated as lowest relevance for ordering.
- Two input items with the same URL: the URL is listed once per item in the request; the
  post-check treats it as allowed.
- Model answer containing a link with an input URL but a different visible text: kept (only the
  target is checked).
- Model answer containing URLs inside code spans or code blocks: checked and removed the same way.
- Model answer that is empty or whitespace-only: rejected; the fallback digest is returned.
- Model call fails with a per-request error (unavailable, rate limited, rejected): the fallback
  digest is returned and the run ends `partial`.
- Credential or configuration error (missing/rejected credentials, unknown model): the step fails
  with a readable error that never contains item text, as in #17; no fallback is built, because
  the job's setup is broken rather than the request.
- After URL removal, a section loses all its links: the digest is still returned; items whose
  only link was removed count as missing and are listed under "More items".
- An item is mentioned by the model only in plain text without its link: it counts as missing and
  is listed under "More items" (the check is by link, not by wording).
- Two input items share one URL: one remaining link to that URL covers both.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The step MUST accept a list of digest entries, each combining an item's identifier,
  URL, title, relevance score, publish date (optional) and its item summary (headline, bullets,
  why-relevant takeaway).
- **FR-002**: Before building the request, entries MUST be ordered by relevance (highest first),
  then by publish date (newest first, undated last), then by a stable tiebreak (item identifier).
- **FR-003**: If the list is empty, the step MUST return an empty digest (empty text, empty item
  list) without any model call and without recording usage.
- **FR-004**: Otherwise the step MUST make exactly one model call with the job's `smart` model.
- **FR-005**: The request MUST ask for Markdown output consisting of a short introduction framing
  what is new, followed by one or more thematic sections, each with a heading and each listing its
  items with the item title linked to its URL. The request MUST ask the model not to write a
  closing or "Worth a closer look" section.
- **FR-005b**: After the post-check (FR-009), an entry counts as missing when no link to its URL
  remains in the model's answer. If any entries are missing, the system MUST append a
  "More items" section (heading in the job's language, English when no built-in translation
  exists) listing each missing entry in the order of FR-002 with its title linked to its URL and
  its why-relevant takeaway (inserted as text), placed before the section from FR-005a, and MUST
  log the number of missing entries. If none are missing, no such section is added.
- **FR-005a**: After the post-check (FR-009), the system MUST append a "Worth a closer look"
  section built without the model: a heading in the job's language (English when no built-in
  translation exists for that language) followed by the three most relevant entries in the order
  of FR-002 (all entries when fewer than three exist), each as its title linked to its URL and its
  why-relevant takeaway. Titles and takeaways MUST be inserted as text, so Markdown or links in
  them cannot change the digest's structure.
- **FR-006**: The request MUST instruct the model that every statement must be attributable to
  one of the provided items, that no information beyond the provided data may be added, and that
  only the provided URLs may appear.
- **FR-007**: The request MUST instruct the model to write the digest in the job's configured
  language, using the same language handling as item summarization.
- **FR-008**: Item data MUST appear only inside data delimiters in the user message, with
  delimiter sequences in the data neutralised; the system message MUST state that the data is
  untrusted and that instructions inside it must be ignored.
- **FR-009**: After the call, the system MUST extract every URL in the answer (Markdown link
  targets, autolinks, bare URLs, including inside code) and remove each one that is not exactly
  equal to an input URL. A Markdown link with an unknown target MUST keep its visible text; a bare
  or autolinked unknown URL MUST be removed entirely. Images MUST always be replaced by their alt
  text (no remote images in mails), and raw HTML link or image tags MUST be removed, keeping their
  inner text. Item text that invio inserts itself (FR-005a, FR-005b, FR-015) MUST NOT carry URLs
  other than the entry's own link.
- **FR-010**: The number of removed URLs MUST be logged; digest text and URLs from item content
  MUST NOT be logged.
- **FR-011**: An answer that is empty or has no thematic section heading MUST be rejected; the
  fallback digest (FR-015) is returned instead.
- **FR-012**: The step MUST return the digest text (model answer after the post-check, followed by
  the "More items" section when needed and the appended closing section) together with the identifiers of the input items, in relevance
  order.
- **FR-013**: The system MUST record one usage record for the synthesis call, with a purpose that
  identifies digest synthesis, including when the answer is rejected afterwards.
- **FR-014**: Per-request model errors (unavailable, rate limited, request rejected, invalid
  answer) MUST lead to the fallback digest (FR-015). Credential or configuration errors MUST make
  the step fail with a readable error that contains no item text.
- **FR-015**: The fallback digest MUST be built without any model call: a fixed short intro and
  section heading in the job's language (English when no built-in translation exists), one section
  listing every entry in the order of FR-002 with its title linked to its URL, its headline and its
  why-relevant takeaway (inserted as text), followed by the section from FR-005a. It contains only
  input URLs.
- **FR-016**: The returned digest MUST indicate whether it is a fallback. When it is, the system
  MUST log that the fallback was used with the error category (no item text), and a run that would
  otherwise end `succeeded` MUST end `partial`; a run already `partial` or `failed` keeps its
  status.

### Key Entities

- **Digest entry**: one input item for synthesis — item identifier, URL, title, relevance score,
  publish date (optional) and its item summary from #17.
- **Item summary**: headline, 3–6 bullet points and a why-relevant takeaway (defined in #17).
- **Digest** (code: `SynthesisResult`): the result of synthesis — Markdown text, the list of item identifiers it covers, and
  whether it is a fallback digest; empty text, an empty list and no fallback when there were no
  items.
- **Usage record**: tokens, model, run, purpose and cost of the synthesis call.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In 100% of test runs with an empty item list, zero model calls are made and an
  empty digest is returned.
- **SC-002**: In 100% of test runs with at least one item, exactly one `smart` model call is made.
- **SC-003**: Across all post-check tests with fabricated model output, 0 URLs not present in the
  input remain in the returned digest, and 100% of valid input URLs are preserved.
- **SC-004**: 100% of accepted digests contain an intro, at least one thematic section and a
  "Worth a closer look" section whose entries are exactly the top three items by relevance (or
  all items when fewer exist).
- **SC-005**: 100% of synthesis requests carry an instruction to write in the job's configured
  language.
- **SC-006**: In 100% of returned digests (model-written or fallback), every input item appears
  with a link to its own source URL.
- **SC-007**: In 100% of test runs where the model call fails with a per-request error or returns
  an unusable answer, a digest containing every input item is still returned and the run ends
  `partial`.

## Assumptions

- The issue's path `scout/graph/nodes/synthesize.py` refers to the project's graph nodes package
  (`src/invio/graph/nodes/`), following the existing relevance and summarization nodes.
- The issue's "list of `ItemSummary` with URL, title, relevance and publish date" means the
  existing item summary (#17) combined with the item's stored metadata; the item summary model
  itself is not changed.
- Thematic grouping is done by the model; the number and names of themes are not fixed in
  advance. "At least one section per theme" is verified as "at least one thematic section heading
  when items exist".
- The fixed texts invio writes itself (closing section heading, "More items" heading, fallback
  intro and fallback section heading) come from a small built-in table of translations (at least English and
  German); other languages fall back to English.
- The step reports the fallback flag; setting the run status to `partial` is done where the run
  status is decided (the pipeline), following the same rule as delivery failures in #20. The rest of the
  closing section (titles, takeaways) is already in the job's language from #17 or the source.
- All entries of one run fit into a single synthesis request (item summaries are short); no
  chunking or map-reduce is applied to synthesis, and no cap on the number of items is introduced.
- The step returns the digest; storing it, giving it a title and wiring the step into the graph
  belong to the pipeline issue. The empty-digest decision belongs to the notifier (#20), which
  treats a digest with no items as empty.
- URL comparison is exact string equality after removing surrounding whitespace and trailing
  sentence punctuation captured by URL extraction; no URL normalisation is performed.
- Provider behaviour (rate-limit retries) is reused from the LLM layer, as in the relevance and
  summarization steps.
- Dependencies: #7 (LLM provider layer), #8 (fake provider / model roles) and #17 (item
  summaries) are merged.
