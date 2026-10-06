# Implementation Plan: Item Summarization with Map-Reduce Chunking

**Branch**: `gh-issue-17` | **Date**: 2026-10-06 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/012-gh-issue-17/spec.md`

## Summary

Add the summarization node of the M2 pipeline. Every item handed to it (normally `relevant`
after #16) gets a validated `ItemSummary(headline, bullets[3..6], why_relevant)` written in the
job's language. The body text (teaser + extracted text) is measured with a deterministic
`ceil(chars / 4)` token estimate: at or below 3,000 tokens it is summarized with one `fast`
call; above, `split_text` cuts it into ≤ 3,000-token chunks (paragraph → sentence → word →
character boundaries, a guaranteed non-empty 200-token overlap, at most 2,800 new tokens per
chunk), at most 20 chunks are summarized with `fast` into looser `ChunkSummary` results (1–6
bullets; map), and the chunk summaries are combined with `smart` into the final `ItemSummary`
(reduce, grouped to `COMBINE_MAX_TOKENS` and repeated if they do not fit into one request).
`ItemSummary` stays in the `graph` layer; later consumers in `notify` get rendered plain data
(research R6). The final summary is stored as JSON in the existing `items.summary`
column and the status becomes `summarized`. Untrusted text is sent only inside neutralised
delimiters (shared helper extracted from the relevance node). Every call records one
`llm_usage` row; per-item LLM failures mark the item `failed` and the step continues. The job
file gains an optional top-level `language` (ISO 639-1, default `en`).

## Technical Context

**Language/Version**: Python 3.12+

**Primary Dependencies**: Pydantic v2 (summary schema, job config), existing `invio.llm` layer
(`LLMProvider.complete_structured`, typed `LLMError`s, `ModelRegistry.cost`), existing
repositories. No new dependencies (ISO 639-1 table is embedded, see research R7).

**Storage**: existing `items` (`summary` TEXT, `status`, `last_error`) and `llm_usage` tables;
no migration.

**Testing**: pytest with the scripted `FakeProvider` (`invio.llm.fake`), the `db_session`
fixture, fixture texts under `tests/fixtures/summarize/`; no network or real provider.

**Target Platform**: Linux server / unattended cron or systemd runs.

**Project Type**: single Python package with CLI (`src/invio`).

**Performance Goals**: short items: 1 call; long items: ≤ 20 `fast` calls + usually 1 `smart`
call; each request body ≤ ~3,000 estimated tokens (≈ 12,000 characters).

**Constraints**: deterministic prompts and splitting (`temperature=0`, pure `split_text`), no
document text in logs or `last_error`, flush-only persistence (caller commits), sequential
processing (items and chunks).

**Scale/Scope**: ≤ `limits.max_items_per_run` (default 100) items per run; graph wiring and
run-level token budget enforcement are later issues.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Check | Status |
|-----------|-------|--------|
| I. Strict contracts at boundaries | LLM answers parsed into `ItemSummary` (`extra="forbid"`, 3–6 non-blank bullets) or `ChunkSummary` (`extra="forbid"`, 1–6 non-blank bullets) before use; `language` validated against ISO 639-1 with a field-named error; job JSON schema regenerated | ✅ |
| II. CLI-first | No new command; the new job setting is reachable through existing job file / `job edit` flows | ✅ |
| III. Test-covered behaviour | Each acceptance criterion maps to tests in `tests/test_summarize_item.py`, `tests/test_split_text.py`, `tests/test_job_config.py`; rejection paths (invalid answer, bad language, bad splitter args) covered; `FakeProvider` only | ✅ |
| IV. Quality gates mirror CI | ruff, ruff format, mypy strict, pytest; no `Any`/ignores planned | ✅ |
| V. Secrets secret, runs observable | No prompt/document text in errors or logs; structured `summarize.*` log events incl. truncation | ✅ |
| Layering | `graph` → `llm`, `db`, `config`, `domain`; `config` gains a stdlib-only language table; `domain` untouched | ✅ |
| Docs for user-facing change | `language` documented in README "Job files", `docs/job.example.yaml`, `docs/job.schema.json` | ✅ |
| Simplicity | No schema migration, no concurrency, no new job settings beyond `language`; chunk constants are module constants; one shared prompt helper instead of duplication | ✅ |

**Post-design re-check**: unchanged — all gates pass; Complexity Tracking empty.

## Project Structure

### Documentation (this feature)

```text
specs/012-gh-issue-17/
├── spec.md
├── plan.md              # this file
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   ├── summarize-node.md
│   └── job-language.md
├── checklists/
│   └── requirements.md
└── tasks.md             # /speckit-tasks (not created here)
```

### Source Code (repository root)

```text
src/invio/
├── config/
│   ├── languages.py           # NEW: ISO_639_1 code → English name table (stdlib only)
│   └── job.py                 # JobConfig.language (optional, default "en", validated)
├── graph/nodes/
│   ├── prompting.py           # NEW: neutralise() + delimiter regex, moved from relevance.py
│   ├── relevance.py           # imports neutralise from prompting (behaviour unchanged)
│   └── summarize_item.py      # NEW: ItemSummary, ChunkSummary, SummaryOutcome, SummaryContext,
│                              #      estimate_tokens, split_text, build_messages,
│                              #      summarize_item, summarize_items
└── db/
    └── repositories.py        # ItemRepository.set_summary

docs/
├── job.example.yaml           # + language: en
└── job.schema.json            # regenerated

tests/
├── fixtures/summarize/
│   ├── short.txt              # NEW: below threshold
│   ├── long.txt               # NEW: several paragraphs, > threshold
│   └── injection.txt          # NEW: injection text + </document> / </summary>
├── test_split_text.py         # NEW: limits, overlap, boundaries, edge inputs
├── test_summarize_item.py     # NEW: call counts, models, language, storage, failures, usage
├── test_job_config.py         # extended: language default / valid / invalid
├── test_repositories.py       # extended: set_summary
└── test_relevance.py          # unchanged; guards the neutralise() move
```

**Structure Decision**: single-project layout as established; the node sits next to
`relevance.py` in `src/invio/graph/nodes/` (the issue's `scout/graph/nodes/summarize_item.py`
maps to this package).

## Design Summary

- Token estimate, thresholds and constants: [research.md](research.md) R1.
- Splitting algorithm and overlap: R2.
- Map/reduce flow, model tiers, grouping and chunk cap: R3–R4.
- Prompts, language instruction and injection handling: R5.
- Storage format, usage purposes, error classification: R6, R8.
- Job `language` setting: R7, [contracts/job-language.md](contracts/job-language.md).
- Entities and state transitions: [data-model.md](data-model.md).
- Public API of the node: [contracts/summarize-node.md](contracts/summarize-node.md).
- Validation scenarios: [quickstart.md](quickstart.md).

## Risks

- Adding a defaulted field changes `dump_yaml` output (every field is written): golden YAML
  expectations in job/wizard tests need `language: en` added.
- Moving `_neutralise` must keep the relevance prompt byte-identical for existing inputs;
  `tests/test_relevance.py` guards this.

## Complexity Tracking

No violations.
