# Data Model: Job Search Suggestion Assistant (gh-issue-37)

No database change. All entities are in-memory; the only persisted result is an ordinary job
created through the existing wizard and `JobService.create`.

## SuggestionAnswer (LLM boundary, `invio.services.suggest`)

The schema sent to the model for structured output and the first validation of its answer
(Constitution I). Pydantic v2, `ConfigDict(extra="forbid", str_strip_whitespace=True)`, all
fields required (strict-schema providers need every property required). The length and count
rules below are implemented as `AfterValidator`s so they do not appear in the JSON schema sent
to the provider (research R2).

| Field | Type | Rules |
|---|---|---|
| `keywords_any` | `list[str]` | ≤ 30 items, each 1–100 chars after strip |
| `keywords_all` | `list[str]` | ≤ 30 items, each 1–100 chars after strip; may be empty |
| `keywords_exclude` | `list[str]` | ≤ 30 items, each 1–100 chars after strip |
| `semantic_description` | `str` | 1–2,000 chars after strip |
| `suggested_sources_hint` | `str` | 1–1,000 chars after strip |

Field names are those of the issue (`SearchSuggestion(keywords_any, …)`), so the JSON the model
returns and the JSON sent back on refinement are the same shape.

A violation → the provider layer's one repair round → `LLMInvalidOutputError` (FR-004).

## SearchSuggestion (`invio.services.suggest`)

The normalised, displayable suggestion. Frozen dataclass (or frozen Pydantic model with the
same fields as `SuggestionAnswer`) plus:

| Member | Meaning |
|---|---|
| `warnings: tuple[str, ...]` | Normalisation notes (removed conflicts, empty `any` / `exclude`) shown in the preview; not sent to the model |
| `to_search_config() -> SearchConfig` | Builds `SearchConfig(keywords=KeywordsConfig(any=…, all=…, exclude=…), semantic_description=…)`; raises `pydantic.ValidationError` if invalid. `min_relevance` keeps the job default. |
| `to_prompt_json() -> str` | The five fields as JSON (no warnings) for the `<previous_suggestion>` block |
| `replace(**fields) -> SearchSuggestion` | Returns a re-normalised copy (used by per-field editing) |

**Normalisation** (`normalise(answer) -> SearchSuggestion`, pure, FR-014):

1. Strip each keyword; drop empty ones.
2. Within each list, drop case-insensitive duplicates (keep the first occurrence and its case).
3. Drop from `keywords_exclude` every entry that equals (case-insensitive) an entry of
   `keywords_any` or `keywords_all`; add warning `"'<kw>' removed from exclude: it is also an
   include keyword"`.
4. Warn `"no 'any' keywords"` if `keywords_any` is empty and `"no exclusions"` if
   `keywords_exclude` is empty.
5. Validate via `to_search_config()` (FR-002); failure here is a programming error for model
   answers (the schema is stricter) and a validation message for operator edits.

## SuggestionRequest (implicit, `invio.services.suggest.build_messages`)

| Part | Where it goes | Notes |
|---|---|---|
| `topic: str` | user message `<topic>` | 1–500 chars after strip (checked by the CLI before any call) |
| `language: str` | system message | ISO 639-1 code in `ISO_639_1`; description and hint are written in it |
| `previous: SearchSuggestion \| None` | user message `<previous_suggestion>` (JSON) | refinement only; includes operator edits |
| `remark: str \| None` | user message `<remark>` | refinement only; non-empty |

`build_messages(topic, language, previous=None, remark=None) -> tuple[str, str]` is pure;
`previous` and `remark` must be given together. Delimiter tags (`topic`, `previous_suggestion`,
`remark`) inside topic and remark are neutralised.

## ModelChoice (`invio.services.suggest.choose_model`)

| Field | Meaning |
|---|---|
| `provider_name: str` | Registered provider name actually used |
| `model: str` | Registered model id used as the capable model |

Selection rules: see research R4 / spec FR-003a.

## FlowResult (`invio.cli.suggest_flow`)

Outcome of the interactive loop: `Create(suggestion)`, `PrintYaml(suggestion)` or `Discard()`.
Aborts raise `WizardAborted` instead.

**Loop states**

```text
          ┌────────── edit field (valid) ─────────┐
          ▼                                        │
 ask() ─► Preview ── refine(remark) ─► ask() ──────┤ (on LLMError: stay on Preview, previous kept)
          │
          ├── Create job ─► run_wizard(prefill) ─► confirm? ─► saved │ declined (exit 1)
          ├── Print YAML ─► stdout, exit 0
          └── Discard ────► "Nothing saved.", exit 0
```

## WizardPrefill (`invio.cli.wizard`)

Frozen dataclass passed to `run_wizard(..., prefill=...)`.

| Field | Seeds `_Session.` | Default without prefill |
|---|---|---|
| `keywords: dict[str, list[str]]` (`any`/`all`/`exclude`) | `keywords` | `{}` |
| `description: str` | `description` | `""` |
| `language: str` | `language` (new field) | `"en"` |
| `provider: str \| None` | `provider` | `""` |
| `smart_model: str \| None` | `smart` | `""` |
| `sources_note: str \| None` | `sources_note` (new field) | `None` |

`WizardPrefill.from_suggestion(suggestion, *, language, choice)` builds it. Every seeded value
is only a prompt default; the operator can keep, change or clear it and the usual validators
apply.

## Changes to existing entities

- `_Session` (wizard): `language: str = "en"`, `sources_note: str | None = None`; `mapping()`
  adds `"language": self.language`.
- `ModelRegistry`: `most_expensive(provider) -> ModelInfo | None`.
- `invio.llm.factory`: `registered_providers() -> list[str]`,
  `has_credentials(name, settings) -> bool`.
- `JobConfig`, `SearchConfig`, `KeywordsConfig`: unchanged.
