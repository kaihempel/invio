# Implementation Plan: Job Search Suggestion Assistant

**Branch**: `gh-issue-37` | **Date**: 2026-10-09 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/020-gh-issue-37/spec.md`

## Summary

Add `invio job suggest TOPIC [--language CODE] [--provider NAME] [--model ID] [--yaml]`. From a
short topic, the capable model drafts a job's search definition: `keywords.any`,
`keywords.all`, `keywords.exclude`, a `semantic_description` stating what is and is not
relevant, plus a free-text sources hint. The operator reviews the draft in a preview, edits
fields one at a time, refines it with remarks ("make it narrower"), and then either creates a
job through the existing wizard with the search fields prefilled, prints YAML, or discards it.

- **Suggestion service.** A new `invio.services.suggest` module holds:
  - the LLM answer schema (`SuggestionAnswer`) and the normalised `SearchSuggestion`, which is
    validated through the job's own `SearchConfig`;
  - the pure prompt builder: instructions in the system message, and topic, previous
    suggestion and remark as neutralised data blocks in the user message;
  - normalisation;
  - provider and model choice;
  - `suggest()` and `refine()` on top of the existing `complete_structured`, which already does
    one repair round.
- **Interactive loop.** A new `invio.cli.suggest_flow` runs the preview, per-field editing and
  refinement through the existing `Prompter`. The model call is injected, so the loop is
  scriptable in tests with `FakeProvider`.
- **Wizard.** `run_wizard` gains an optional `WizardPrefill`, which seeds the session values
  the wizard already uses as defaults. There is a new language step for all job creation, a
  default smart model in `_ask_model`, and the sources hint is shown at the sources step.
- **LLM layer.** `ModelRegistry.most_expensive`, `factory.registered_providers` and
  `factory.has_credentials` support the selection rule from Clarification Q1.

## Technical Context

**Language/Version**: Python 3.12+ (uv-managed)

**Primary Dependencies**: All of these already exist; no new runtime dependency.
- Typer, plus questionary through `Prompter`.
- Pydantic v2 and PyYAML.
- The `invio.llm` provider layer: `complete_structured` with repair, the logging wrapper, and
  `ModelRegistry`.

**Storage**: The existing `jobs` table via `JobService.create`, only after the wizard's
confirmation. No schema change. No `llm_usage` rows are written because no job exists yet.

**Testing**: pytest.
- `FakeProvider` with scripted `FakeReply`/`LLMError` steps, asserted on `.requests`.
- A scripted fake `Prompter` (existing wizard test helpers).
- `typer.testing.CliRunner` with monkeypatched seams (`_make_suggest_provider`,
  `_make_prompter`, `_is_interactive`, `_make_service`), and the SQLite job store.
- No network.

**Target Platform**: Linux server and developer machines (CLI).

**Project Type**: Single-project CLI tool (`src/invio`, `tests`).

**Performance Goals**: SC-006: the preview renders in under 1 s after the model answers.
Model latency is bounded by `llm_timeout_seconds` and the provider retry policy.

**Constraints**:
- All usage and configuration checks happen before the first model call (FR-015).
- Topic ≤ 500 characters. At most 30 keywords per list, each 1–100 characters. Description ≤
  2,000 characters, hint ≤ 1,000 characters.
- No job is written without the wizard's `Save this job?` (FR-012).
- No credentials or system prompt in any output.

**Scale/Scope**: One topic per invocation. Typically 1–5 model calls per session (first call
plus refinements, each with at most one repair request).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How |
|---|---|---|
| I. Strict contracts at boundaries | ✅ | CLI arguments are checked before use (topic length, `ISO_639_1`, registered provider and model). The LLM answer is parsed into the closed `SuggestionAnswer`, then normalised and validated through the job's existing `SearchConfig`/`KeywordsConfig`, so no second search schema exists. Operator edits are validated the same way. The created job goes through `validate_job` in the wizard. |
| II. CLI-first | ✅ | `suggest` is a new command in the already auto-discovered `invio job` group, so no registration edits are needed. Preview and YAML go to stdout; prompts, warnings, errors and logs go to stderr. Configuration and usage errors exit 2, aborts and failures exit 1 (contract). |
| III. Test-covered behaviour | ✅ | Every acceptance criterion and the rejection paths map to named tests (quickstart §2). `FakeProvider` and a scripted prompter are used; no network or real provider. Existing wizard tests are updated for the new language question. |
| IV. Quality gates mirror CI | ✅ | ruff, ruff format, strict mypy and pytest. No new dependencies; `uv.lock` is unchanged. |
| V. Secrets and observability | ✅ | Credentials are only checked via `require_api_key` (the message names the env var, never a value). Calls go through `get_provider`'s wrapper, so one `llm.call` JSON line is written per call. A usage summary line goes to stderr. Error output uses the typed `LLMError` messages, which contain no prompts or keys. |
| Layering | ✅ | `services/suggest.py` imports only `config.*` and `llm.*`: no Typer, `db`, `graph` or `cli`. `cli/suggest_flow.py` imports `cli.prompts` and `services.suggest`, not `db` (`test_cli_layering`). `invio.llm` changes import nothing above `config` (`test_llm_layering`). |
| Docs | ✅ | The README `invio job` section gets `invio job suggest` and the wizard's new language question (development workflow rule). |
| Simplicity | ✅ | No new abstractions beyond the issue's `SearchSuggestion`. The wizard reuses its default mechanism for prefill, and the Prompter protocol is unchanged. |

There are no violations, so Complexity Tracking is empty.

**Post-design re-check (after Phase 1)**: passes unchanged. The contracts keep services free of
Typer and the database. The one cross-cutting change is the wizard's language step, which
satisfies FR-011a and only adds a question; it is documented in the CLI contract and README.

## Project Structure

### Documentation (this feature)

```text
specs/020-gh-issue-37/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── cli-job-suggest.md
│   └── python-api.md
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks (not created here)
```

### Source Code (repository root)

```text
src/invio/
├── services/
│   └── suggest.py           # NEW  SuggestionAnswer, SearchSuggestion, normalise, build_messages,
│                            #      choose_model, suggest, refine, search_yaml
├── cli/
│   ├── suggest_flow.py      # NEW  preview rendering, action loop, per-field edit, refine
│   ├── wizard.py            # +WizardPrefill, +validate_language, +_step_language,
│   │                        #  _Session.language/sources_note, _ask_model default, hint at sources
│   └── commands/job.py      # +`suggest` command, +_make_suggest_provider seam
└── llm/
    ├── registry.py          # +ModelRegistry.most_expensive
    └── factory.py           # +registered_providers, +has_credentials

tests/
├── test_suggest.py          # NEW  schema, normalise, prompt, choose_model, suggest/refine (FakeProvider)
├── test_suggest_flow.py     # NEW  loop: edit, refine (AC2), error keeps previous, actions
├── test_cli_job_suggest.py  # NEW  CLI: --yaml, exit codes, create-job hand-over (AC3, AC4)
├── test_wizard.py           # +language step, +prefill, +smart default, +sources hint
├── test_cli_job_create.py   # scripted answers gain the language question
├── test_llm_registry.py     # +most_expensive
└── test_llm_factory.py      # +registered_providers, +has_credentials
README.md                    # CLI docs
```

**Structure Decision**: This is the existing single-project layout. The use case goes in
`invio.services`, the interactive presentation in `invio.cli` (next to `wizard.py`), the Typer
command in the existing `job` group, and the small registry and factory helpers in `invio.llm`.

## Implementation Notes (for /speckit-tasks)

1. **Foundations**, each with its own tests:
   - `ModelRegistry.most_expensive`;
   - `factory.registered_providers` and `has_credentials`;
   - `SuggestionAnswer`, `SearchSuggestion` and `normalise` (FR-014);
   - `build_messages` (FR-005/FR-006);
   - `choose_model` (FR-003a);
   - `search_yaml`.
2. **US1 (P1 MVP)**:
   - `suggest()`;
   - the `job suggest` command with argument checks in order, `_make_suggest_provider`, and
     `asyncio.run` per call with `aclose` in `finally`;
   - non-interactive or `--yaml` output;
   - the first interactive preview with the Discard and Print YAML actions;
   - exit-code mapping: `LLMConfigError`/`LLMAuthError`/`ModelRegistryError` → 2,
     `LLMError` → 1, `WizardAborted` → 1.
3. **US2**:
   - `refine()`;
   - the Refine action in `suggest_flow`, with the `ask` callable built in `job.py`;
   - keep the previous suggestion on `LLMError` (FR-010).
4. **US3**:
   - per-field editing in the flow (FR-008);
   - `WizardPrefill`, `_step_language` with `validate_language`, the `_ask_model` default and
     the sources hint;
   - the Create job hand-over in `job.py`: `_make_service()`, `existing_names`, `run_wizard(...,
     prefill=...)`, `service.create`, and the existing `job not created` / `aborted; nothing
     saved` paths.
   - Update the scripted answers in existing wizard and `job create` tests for the language
     question.
5. **US4**: the YAML action is already delivered in step 2. Add the round-trip test (YAML →
   job → `validate_job`).
6. **Docs**: README `invio job suggest` and the wizard language question.
7. **Usage summary**: sum the `Usage` of all calls, then `registry.cost(model, usage)`, and
   print one stderr line at the end.

## Risks

- **Strict-schema providers.** Support for `maxItems`/`maxLength` in strict JSON-schema modes
  differs between providers, and the shared `strict_schema` helper does not remove them. The caps
  are therefore `AfterValidator`s, which are not emitted into the schema. A unit test asserts
  that `SuggestionAnswer.model_json_schema()` contains no `maxItems`, `maxLength`, `minLength`
  or `minItems`, and that Pydantic still rejects over-long answers (repair round).
- **Language step breaks scripted wizard tests.** Every existing scripted answer sequence needs
  one extra answer. This is mechanical, but touches many tests; do it in one task.
- **Model quality varies.** The prompt cannot guarantee good synonyms; the validated shape and
  the warnings for empty `any`/`exclude` lists are the guaranteed part. Refinement and editing
  cover the rest.
- **Choosing the "most expensive" model** may pick a premium model with noticeable cost. The
  preview names provider and model, the usage summary shows the cost, and `--model` overrides.

## Complexity Tracking

No constitution violations.
