# Implementation Plan: Digest Synthesis

**Branch**: `gh-issue-18` | **Date**: 2026-10-06 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/013-gh-issue-18/spec.md`

## Summary

Add the synthesis node of the M2 pipeline. It turns the run's summarized items into one
Markdown digest. Stored items become validated `DigestEntry` values (URL, title, relevance,
publish date and the #17 `ItemSummary`), sorted by relevance, then publish date, then id. No
entries → an empty digest with no model call. Otherwise one free-text `smart` call
(`provider.complete`, recorded as `llm_usage` purpose `synthesize`) writes the intro and `##`
theme sections, with every item linked inline. A pure `filter_urls` post-check removes every URL
that is not exactly an input URL (inline links keep their text; images, autolinks, raw-HTML
links, reference definitions and bare URLs are covered). Invio then appends a "More items"
section for entries the model left unlinked and the "Worth a closer look" section (top 3). Both
are rendered deterministically with escaped text and headings in the job's language (en/de
table, English fallback). Per-request LLM errors or an unusable answer (blank, no heading)
produce a deterministic fallback digest flagged `fallback=True`, which
`run_status_after_synthesis` turns into a `partial` run. Credential and configuration errors
propagate.

## Technical Context

**Language/Version**: Python 3.12+

**Primary Dependencies**: existing `invio.llm` layer (`LLMProvider.complete`, typed
`LLMError`s, `ModelRegistry.cost`), shared node helpers (`llm_calls`, `prompting`),
`ItemSummary` from `summarize_item`, `config.languages.language_name`. No new dependencies
(URL post-check is regex-based, research R5).

**Storage**: none written by the node apart from `llm_usage`; reads `Item` rows passed in.
No migration.

**Testing**: pytest with the scripted `FakeProvider`, `db_session` fixture for
`entries_from_items`, `hypothesis` property tests for `filter_urls`, fixture answers in
`tests/fixtures/synthesize/`. No network or real provider.

**Target Platform**: Linux server / unattended cron or systemd runs.

**Project Type**: single Python package with CLI (`src/invio`).

**Performance Goals**: 0 calls for an empty run, exactly 1 `smart` call otherwise; request
≈ 150 estimated tokens per entry (≈ 15k for 100 entries); answer bounded by
`DIGEST_MAX_OUTPUT_TOKENS = 4000`; post-check linear in answer length (no backtracking-prone
patterns).

**Constraints**: deterministic prompts and appended sections (`temperature=0.0`, pure
builders), no item text, digest text or URLs in logs or `error`, only input URLs in the
returned body, flush-only persistence (caller commits usage rows).

**Scale/Scope**: ≤ `limits.max_items_per_run` (default 100) entries per run. Storing the digest,
its title, graph wiring and applying the run status are the pipeline issue's job.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Check | Status |
|-----------|-------|--------|
| I. Strict contracts at boundaries | Stored summaries re-validated as `ItemSummary` before use; the free-text LLM answer is checked by `filter_urls` + the structure check before it is used, and an unusable answer leads to the fallback | ✅ |
| II. CLI-first | No new command or setting; the digest reaches users via the existing notifier (#20) | ✅ |
| III. Test-covered behaviour | Every acceptance criterion and clarification maps to quickstart scenarios in `tests/test_synthesize.py` / `tests/test_synthesize_urls.py`, including rejection paths (unusable answer, provider errors, auth error); `FakeProvider` only; hypothesis tests are made deterministic with `@settings(derandomize=True)` on each property test | ✅ |
| IV. Quality gates mirror CI | ruff, ruff format, mypy strict, pytest; no `Any` / ignores planned | ✅ |
| V. Secrets secret, runs observable | Structured `synthesize.done` / `.fallback` / `.skipped` events with counts and ids only; `error` built by `failure_message` (no provider text) | ✅ |
| Layering | `graph` → `llm`, `db`, `config`, `domain`; no `notify` import (status helper mirrors, not imports, `run_status_after_delivery`) | ✅ |
| Docs for user-facing change | README gains a short "Digest synthesis" paragraph (digest layout, link guarantee, fallback → `partial`) | ✅ |
| Simplicity | One module + one helper in `llm_calls`; no new settings, no schema change, en/de text table only | ✅ |

**Post-design re-check**: unchanged. All gates pass and Complexity Tracking is empty.

## Project Structure

### Documentation (this feature)

```text
specs/013-gh-issue-18/
├── spec.md
├── plan.md              # this file
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   └── synthesize-node.md
├── checklists/
│   └── requirements.md
└── tasks.md             # /speckit-tasks (not created here)
```

### Source Code (repository root)

```text
src/invio/
└── graph/nodes/
    ├── llm_calls.py           # + call_text() (free-text call with usage recording)
    └── synthesize.py          # NEW: DigestEntry, SynthesisContext, UrlFilterResult,
                               #      SynthesisResult, entries_from_items, sort_entries,
                               #      build_messages, filter_urls, render_closing,
                               #      render_more_items, render_fallback, synthesize_digest,
                               #      run_status_after_synthesis

README.md                      # + "Digest synthesis" paragraph

tests/
├── fixtures/synthesize/
│   ├── valid.md               # NEW: intro + 2 theme sections, all links allowed
│   ├── invented_urls.md       # NEW: unknown inline/bare/autolink/HTML/image/altered URLs
│   ├── partial_links.md       # NEW: links only 3 of 5 entries
│   ├── no_heading.md          # NEW: prose without any heading
│   └── fenced.md              # NEW: valid answer wrapped in a ```markdown fence
├── test_synthesize_urls.py    # NEW: filter_urls cases + hypothesis properties
└── test_synthesize.py         # NEW: empty input, ordering, call/usage, sections, missing
                               #      items, fallback paths, language, injection, entries
```

**Structure Decision**: single-project layout as established. The node sits next to
`summarize_item.py` in `src/invio/graph/nodes/`; the issue's `scout/graph/nodes/synthesize.py`
maps to this package.

## Design Summary

- Module placement and layering: [research.md](research.md) R1.
- Free-text call and usage: R2.
- Entries, ordering, skipped items: R3, [data-model.md](data-model.md).
- Prompt layout and injection handling: R4.
- URL post-check algorithm: R5.
- Structure check, missing items, appended sections, escaping: R6.
- Fixed texts (en/de): R7.
- Fallback, error classes, run status: R8.
- Scale: R9.
- Public API: [contracts/synthesize-node.md](contracts/synthesize-node.md).
- Validation scenarios: [quickstart.md](quickstart.md).

## Risks

- **Regex post-check vs. exotic Markdown** (nested brackets in link text, destinations with
  unbalanced parentheses): mitigated by the bare-URL pass, which removes any unknown URL the
  structured passes miss, and by property tests. The worst case is lost link text, never a kept
  unknown URL.
- **Model ignores "no closing section"**: a duplicate closing list may appear (accepted in the
  spec's edge cases); the prompt states the rule explicitly.
- **Stricter future need for structure** (e.g. per-theme item counts): kept out by design. A
  structured-output variant is noted as an alternative in R2.

## Complexity Tracking

No constitution violations; nothing to justify.
