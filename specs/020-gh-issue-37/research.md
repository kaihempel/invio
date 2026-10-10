# Research: Job Search Suggestion Assistant (gh-issue-37)

All Technical Context unknowns are resolved below. Each entry: decision, rationale, alternatives.

## R1 — Where the suggestion logic lives

**Decision**: A new module `src/invio/services/suggest.py` holds the suggestion contract
(`SearchSuggestion`, `SuggestionAnswer`), the prompt builder, normalisation and the two async
operations `suggest()` / `refine()`. It imports only `invio.llm.base` (protocol, errors),
`invio.config.job` and `invio.config.languages`; no Typer, no database, no graph. The
interactive loop (preview, edit, refine, hand-over) lives in a new CLI helper
`src/invio/cli/suggest_flow.py` that talks to the existing `Prompter`; the Typer command is
`suggest` in `src/invio/cli/commands/job.py` (the `invio job` group is already auto-discovered).

**Rationale**: Layering is `cli` → orchestration/services → adapters (`llm`) → `config`. The
suggestion is a small, stateless LLM use case that is not part of the pipeline graph, so it
does not belong in `invio.graph`. Keeping prompt and normalisation pure makes the FR-005 /
FR-014 checks plain unit tests; keeping the loop in its own CLI module (like `wizard.py`) keeps
`job.py` a thin command layer and the loop scriptable with the fake prompter.

**Alternatives considered**: Put everything in `job.py` (already ~500 lines; mixes Typer and
prompt design, rejected). Put the prompt into `invio.llm` (that package is provider-neutral
transport; a domain prompt there would be the first one, rejected). Put it into
`invio.graph.nodes` (would make the CLI depend on orchestration for a non-pipeline feature,
rejected).

## R2 — Structured output and the repair round

**Decision**: Call `provider.complete_structured(system, user, SuggestionAnswer, model=...,
temperature=0.3)`. `SuggestionAnswer` is a closed Pydantic model (`extra="forbid"`) with all five
fields required. The bounds of the spec (keyword lists ≤ 30 non-empty items of ≤ 100
characters, `semantic_description` 1–2,000 characters, `suggested_sources_hint` 1–1,000
characters) are enforced by `AfterValidator` functions, which Pydantic does not emit into the
JSON schema, so the schema sent to the provider contains only `type`, `items` and `required`. The providers' existing `structured_with_repair` path supplies FR-004's
"at most one repair attempt"; a still-invalid answer surfaces as `LLMInvalidOutputError`.

**Rationale**: Every provider already implements structured output with one repair round
(#8); OpenAI and Mistral run strict JSON schemas (all properties required, closed objects),
which `SuggestionAnswer` satisfies. The shared `strict_schema` helper only closes objects and does
not remove keywords such as `maxItems`/`maxLength`, whose support differs between providers;
keeping the caps out of the schema avoids provider-specific rejections, and Pydantic validation
after the call (with the repair round) is the backstop. Temperature 0.3 (not the pipeline's
0.0) leaves room for synonym variety while staying reproducible enough for refinement;
`keep_default_temperature` models are handled by the provider layer as today.

**Alternatives considered**: Free-text answer parsed by hand (no schema, no repair, rejected).
Temperature 0.0 (tends to minimal keyword lists, weaker synonyms). A separate repair loop in the
service (duplicates the provider layer, rejected).

## R3 — From answer to `SearchConfig`

**Decision**: After the call, `normalise()` trims entries, drops empty ones, removes
case-insensitive duplicates within each list (first occurrence wins) and removes from
`exclude` every entry that also appears (case-insensitive) in `any` or `all`, recording one
warning per removed entry. The result is a frozen `SearchSuggestion`; its method
`to_search_config()` builds `SearchConfig(keywords=KeywordsConfig(...),
semantic_description=...)`, i.e. the job's own model validates it (FR-002, AC1). Empty `any` or
`exclude` lists add a warning ("no synonyms" / "no noise exclusions") but stay valid.

**Rationale**: The job contract is defined once (Constitution I); validating through
`SearchConfig` rather than re-declaring its rules guarantees that whatever is shown can be
saved. Normalising before validation keeps harmless model sloppiness (duplicates, stray spaces)
from triggering a costly repair round.

**Alternatives considered**: Re-validate only at save time (could show a suggestion that the
wizard later rejects, violating SC-001). Treat include/exclude conflicts as invalid (forces a
repair round for a trivially fixable issue).

## R4 — Provider and model selection (Clarification Q1)

**Decision**:
- `ModelRegistry.most_expensive(provider) -> ModelInfo | None`: the model with the highest
  `input_price_per_mtok + output_price_per_mtok`, ties broken by the lowest model id (mirror of
  the existing `cheapest`).
- `invio.llm.factory.registered_providers() -> list[str]`: the sorted names of registered
  providers (discovers modules first).
- `invio.llm.factory.has_credentials(name, settings) -> bool`: `True` when
  `require_api_key(settings, name)` succeeds; `False` on `LLMAuthError`. Providers without an
  API-key setting (`LLMConfigError`) count as "no credentials".
- Selection in `services/suggest.py::choose_model(settings, registry, provider, model)`:
  `provider` given → must be registered (else `LLMConfigError`) and have credentials (else
  `LLMAuthError`); not given → first of `registered_providers()` with credentials **and** at
  least one registered model (spec FR-003a); none → `LLMAuthError` listing the checked env vars. `model` given →
  `registry.require(model, provider)`; not given → `most_expensive(provider)`; no models →
  `LLMConfigError`. The CLI maps both error types to exit code 2.

**Rationale**: Uses only data the registry already has (prices), mirrors `invio llm test`'s
"cheapest" default, and checks credentials without building SDK clients. Checking before the
first model call satisfies FR-015.

**Alternatives considered**: Building each provider via `get_provider` to probe credentials
(creates clients that must be closed, rejected). A per-provider "smart" marker in `models.d`
(new registry schema field; deferred, not needed for this issue).

## R5 — Prompt design (FR-005, FR-006)

**Decision**: The system message carries all instructions: role ("you help define a news/web
monitoring search"), required content (synonyms; English **and** German variants of key terms
in `keywords_any`; `keywords_all` only for terms that must co-occur, usually empty or one or two
entries; concrete exclusions for topic-typical noise such as job ads, product shops, unrelated
homonyms; a `semantic_description` of 3–8 sentences with an explicit "Relevant: …" and
"Not relevant: …" part), the language instruction (`language_name(code)` from
`invio.config.languages`) for the description and the sources hint, and the statement that
everything inside the delimiter tags is data, not instructions. The user message holds only data
blocks: `<topic>…</topic>`, and for refinement additionally `<previous_suggestion>` (the current
suggestion as JSON, including operator edits) and `<remark>…</remark>`. Delimiter tags occurring
in topic or remark are neutralised (angle brackets swapped for U+2039/U+203A, the technique of
`invio.graph.nodes.prompting.neutralise`, re-implemented locally for the three suggestion tags).

**Rationale**: Matches the existing prompt style (task + untrusted data blocks) and the
injection stance of the relevance node: the validated answer is the backstop, so an injected
topic can at worst produce an odd but valid suggestion. Sending the full previous suggestion as
JSON makes AC2 ("refinement uses the previous suggestion as context") directly assertable on
`FakeProvider.requests[-1].user`.

**Alternatives considered**: Multi-turn chat history (the provider protocol is single-turn
`system`/`user`; would need a protocol change, rejected). Importing `neutralise` from
`invio.graph` (services would depend on orchestration; its regex is bound to the pipeline's
tags, rejected).

## R6 — Interactive loop and editing (Clarification Q4)

**Decision**: `run_suggest_flow(prompter, ask, *, topic, language, echo) -> FlowResult` where
`ask(remark | None, previous | None) -> SearchSuggestion` is an injected callable (production:
runs `suggest`/`refine` with `asyncio.run` and closes the provider; tests: a lambda over
`FakeProvider`). The loop prints the preview, then `prompter.select("What next?", [Create job,
Refine, Edit a field, Print YAML, Discard])`. "Edit a field" → `select` of the four fields →
`text` with the current value as default (comma-separated for lists, `multiline=True` for the
description) and a validator that builds the updated `SearchSuggestion` and calls
`to_search_config()`; the validator's message is shown inline and the prompt is repeated by the
prompter. "Refine" → `text("How should it change?", validate=validate_nonempty)` → `ask(remark,
current)`; an `LLMError` prints `Error: …` to stderr and keeps `current` (FR-010). `FlowResult`
is one of `Create(suggestion)`, `PrintYaml(suggestion)`, `Discard()`. `WizardAborted` propagates.

**Rationale**: Same prompt primitives as the wizard (the Prompter protocol needs no change),
fully scriptable, validation at entry. An injected `ask` keeps the flow free of asyncio and
provider wiring.

**Alternatives considered**: External editor on YAML (rejected in Q4). Re-using wizard steps
`_step_keywords`/`_step_description` for preview editing (they are private and edit all lists
at once; the flow needs per-field editing).

## R7 — Wizard prefill and the new language step (Clarification Q2, Q3)

**Decision**: `run_wizard(..., prefill: WizardPrefill | None = None)`. `WizardPrefill` is a frozen
dataclass in `wizard.py`: `keywords: dict[str, list[str]]`, `description: str`, `language: str`,
`provider: str | None`, `smart_model: str | None`, `sources_note: str | None`. It seeds the
`_Session` fields that are already used as prompt defaults (`keywords`, `description`,
`provider`, new `language`, `smart`), so every prefilled field stays editable with no new
prompting code. Changes:
- `_Session.language: str = "en"`; `mapping()` emits `language`.
- New `_step_language` (after `_step_description`): `text("Summary language (ISO 639-1 code)",
  default=s.language, validate=validate_language)`; `validate_language` accepts lower-cased
  codes in `ISO_639_1`. Added to `_STEPS` and to `_STEP_FOR_PREFIX` as `("language", …)`.
- `_ask_model(…, default: str | None)`: passes the default to `select` when it is one of the
  known models, otherwise to the `text` fallback; `_step_llm` passes `s.smart` for the smart
  model (and `s.fast` for the fast model, for re-asks).
- `_step_sources` echoes `Hint from the suggestion: <sources_note>` once before the first
  source question when `sources_note` is set; no source is prefilled.
- Plain `invio job create` gets the language step with default `en` (FR-011a).

**Rationale**: The wizard already treats session values as defaults on re-ask (FR-005 of #9),
so prefill is a data change, not a new UI path. A frozen prefill object keeps `run_wizard`'s
signature stable for existing callers.

**Alternatives considered**: A second wizard entry point for suggestions (duplicate steps,
rejected). Language as a `select` over ~180 codes (unusable list; `autocomplete` is possible but
a validated text prompt matches the timezone pattern with less code — `autocomplete` may be used
if it is equally simple).

## R8 — Running async calls from the sync CLI

**Decision**: Each model call runs as `asyncio.run(_call(provider, …))` where `_call` awaits
`complete_structured` and closes the provider in `finally` (pattern of `invio llm test`). The
provider object is built once per command via `get_provider(name, settings, registry=…)` and
reused across loops; `LoopClients` builds one SDK client per loop and `aclose` releases it.

**Rationale**: The interactive loop is synchronous (questionary); one short event loop per call
avoids mixing prompt_toolkit's loop with the provider's.

**Alternatives considered**: One long-lived loop with `run_in_executor` for prompts (more moving
parts, no benefit for ≤ a handful of calls).

## R9 — Non-interactive output and exit codes

**Decision**: When stdin/stdout is not a TTY (`_is_interactive()` seam) or `--yaml` is given,
the command makes one `suggest` call and prints

```yaml
# Suggested sources: <hint, wrapped, one comment line per line>
search:
  keywords:
    any: [...]
    all: [...]
    exclude: [...]
  semantic_description: |-
    ...
```

produced with `yaml.safe_dump({"search": ...}, sort_keys=False, allow_unicode=True)` and the
hint lines prefixed with `# ` (control characters stripped via `invio.textsafe.strip_control`).
Exit codes: `0` YAML printed, job created, or discarded; `1` model call failed (provider error or
invalid answer after repair), aborted (Ctrl+C/EOF → `aborted; nothing saved`), wizard save
declined (`job not created`); `2` usage errors (empty/over-long topic, unknown language code,
`--model` without a usable provider) and configuration errors (unknown provider, missing
credentials, model not registered, unreadable registry, database settings when creating).

**Rationale**: Matches `invio job` conventions (contract of #9: 1 = aborted/declined, 2 =
config/usage) and Constitution II (results on stdout, diagnostics on stderr). Discard is a
deliberate, successful outcome.

**Alternatives considered**: Exit 2 without a terminal like the wizard (would make the
assistant useless in scripts; the spec chose YAML output).

## R10 — Observability and secrets

**Decision**: Calls go through `get_provider`'s logging wrapper, which already writes one
`llm.call` JSON line per call (provider, model, tokens, cost, duration) to stderr. No
`llm_usage` row is written (no job exists; spec assumption) and the CLI never imports the
database layer for suggestions. Error messages use the typed `LLMError` messages, which never
contain keys or prompts. After the session, one stderr line summarises total tokens and the
estimated cost via `registry.cost` (`suggestions used N requests, X in / Y out tokens, ~$Z`).

**Rationale**: Constitution V; reuses existing logging; the summary makes cost visible without
storage.

**Alternatives considered**: Recording usage under a pseudo job (no job id exists; would need a
schema change, rejected).
