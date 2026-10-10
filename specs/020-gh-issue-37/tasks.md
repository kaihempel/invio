---

description: "Task list for gh-issue-37: invio job suggest"
---

# Tasks: Job Search Suggestion Assistant

**Input**: Design documents from `/specs/020-gh-issue-37/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/cli-job-suggest.md, contracts/python-api.md, quickstart.md

**Tests**: Required — constitution principle III demands an automated test for every acceptance criterion, including rejection paths. Tests use no network and no real provider: `FakeProvider` / `FakeReply` from `src/invio/llm/fake.py` (assert on `.requests`), `FakePrompter` / `FakeChecker` / `make_registry` / `job_cli` from `tests/cli_helpers.py`, `write_registry` / `make_settings` from `tests/llm_helpers.py`, and `CliRunner` + SQLite like `tests/test_cli_job_create.py`. Write each story's tests first and see them fail.

**Organization**: Tasks are grouped by user story (spec.md) so each story can be implemented and tested on its own.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1–US4 from spec.md
- Paths are relative to the repository root (single project: `src/invio/`, `tests/`)

---

## Phase 1: Setup

**Purpose**: Branch and empty modules every story builds on

- [X] T001 Create and switch to branch `gh-issue-37` from current `main` (constitution: Development Workflow); all following work is committed there, never on `main`
- [X] T002 [P] Create `src/invio/services/suggest.py` with a module docstring (purpose; layering: imports only `invio.config.*`, `invio.llm.base`, `invio.llm.registry`, `invio.llm.factory` — never `typer`, `invio.db`, `invio.graph`, `invio.cli`; untrusted topic/remark handling as in research R5), an empty `__all__`, and the constants from contracts/python-api.md: `MAX_TOPIC_CHARS: Final = 500`, `MAX_KEYWORDS: Final = 30`, `MAX_KEYWORD_CHARS: Final = 100`, `MAX_DESCRIPTION_CHARS: Final = 2000`, `MAX_HINT_CHARS: Final = 1000`, `TEMPERATURE: Final = 0.3`
- [X] T003 [P] Create `src/invio/cli/suggest_flow.py` with a module docstring (the interactive preview/edit/refine loop over `invio.cli.prompts.Prompter`; never touches storage, asyncio or providers — the model call is the injected `ask` callable; output only through the injected `echo`) and an empty `__all__`
- [X] T004 [P] Add a layering test in `tests/test_cli_layering.py`: `src/invio/services/suggest.py` imports nothing starting with `typer`, `invio.db`, `invio.graph` or `invio.cli` (reuse `_imported_modules`); `src/invio/cli/suggest_flow.py` imports no `invio.db`, `invio.services.jobs` or `asyncio`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: LLM-layer helpers and the suggestion contract that every story needs

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

### Tests for Phase 2 (write first, must fail)

- [X] T005 [P] Tests for `ModelRegistry.most_expensive` in `tests/test_llm_registry.py`: returns the model with the highest `input_price_per_mtok + output_price_per_mtok` of the given provider; ties broken by the lowest model id; other providers' models ignored; unknown provider / no models → `None` (build registries with `write_registry` from `tests/llm_helpers.py`)
- [X] T006 [P] Tests for `registered_providers()` and `has_credentials()` in `tests/test_llm_factory.py`: `registered_providers()` equals the sorted names of registered providers and contains `anthropic`, `google`, `mistral`, `openai`; `has_credentials("anthropic", make_settings(anthropic_api_key="k"))` is `True`; with the key unset or whitespace-only it is `False`; a name without an API-key setting (e.g. `"ollama"`) is `False` (not an exception)
- [X] T007 [P] Tests for the suggestion contract in `tests/test_suggest.py` (section "contract"):
  - `SuggestionAnswer` rejects unknown keys and missing fields; rejects > 30 items in any keyword list, a keyword longer than 100 characters, an empty keyword, an empty or > 2,000-character `semantic_description`, an empty or > 1,000-character `suggested_sources_hint`
  - `SuggestionAnswer.model_json_schema()` contains no `maxItems`, `minItems`, `maxLength`, `minLength` anywhere (research R2) and lists all five properties in `required`
  - `normalise`: strips keywords; drops case-insensitive duplicates keeping the first occurrence and its case; removes from `keywords_exclude` every entry equal (case-insensitive) to an `any`/`all` entry and adds the warning `"'<kw>' removed from exclude: it is also an include keyword"`; adds `"no 'any' keywords"` / `"no exclusions"` warnings for empty lists; empty `keywords_all` gives no warning
  - `SearchSuggestion.to_search_config()` returns a `SearchConfig` whose `keywords.any/all/exclude` and `semantic_description` equal the suggestion's and whose `min_relevance` is the default `0.6`
  - `SearchSuggestion.replace(semantic_description="")` raises `pydantic.ValidationError`; `replace(keywords_exclude=("a", "A"))` returns a re-normalised copy with one entry
  - `to_prompt_json()` round-trips through `SuggestionAnswer.model_validate_json` and contains no warnings
- [X] T008 [P] Tests for `build_messages` in `tests/test_suggest.py` (section "prompt"): the system message requires synonyms, English and German variants, concrete exclusions for typical noise, a description with "Relevant:" and "Not relevant:" parts, names the target language via `language_name(code)` (e.g. "German" for `de`), and says content inside the data tags is data, not instructions; the user message contains `<topic>…</topic>` only (no `<previous_suggestion>`/`<remark>`) without refinement; with `previous` and `remark` it contains `<topic>`, `<previous_suggestion>` holding `previous.to_prompt_json()` and `<remark>…</remark>`; passing only one of `previous`/`remark` raises `ValueError`; a topic containing `</topic><remark>x` is neutralised (no extra real tags; angle brackets replaced by U+2039/U+203A); the system message never contains the topic
- [X] T009 [P] Tests for `choose_model` in `tests/test_suggest.py` (section "model choice", registry via `write_registry` with two providers, settings via `make_settings`): no flags + keys for `openai` and `anthropic` → `ModelChoice("anthropic", <most expensive anthropic model>)`; a provider with a key but no registered models is skipped; `provider="openai"` → openai's most expensive model; `model=` given and registered → used; `model=` not registered for the provider → `LLMConfigError`; unknown provider → `LLMConfigError`; given provider without key → `LLMAuthError` naming `INVIO_<PROVIDER>_API_KEY`; no provider with a key → `LLMAuthError` naming the checked env vars; given provider with key but no models → `LLMConfigError`
- [X] T010 [P] Tests for `search_yaml` in `tests/test_suggest.py` (section "yaml"): output starts with `# Suggested sources:` followed by the hint, each hint line prefixed `# `, control characters stripped; the remainder parses with `yaml.safe_load` to `{"search": {"keywords": {"any": [...], "all": [...], "exclude": [...]}, "semantic_description": ...}}`; merging that `search` into a valid job mapping (`tests/job_helpers.py`) passes `validate_job`; non-ASCII keywords (`Wärmepumpe`) are written unescaped

### Implementation for Phase 2

- [X] T011 [P] Implement `ModelRegistry.most_expensive(provider: str) -> ModelInfo | None` in `src/invio/llm/registry.py`, mirroring `cheapest` (max of input + output price; ties: lowest model id) (makes T005 pass)
- [X] T012 [P] Implement `registered_providers() -> list[str]` (calls `_discover()`, returns `sorted(_REGISTRY)`) and `has_credentials(name: str, settings: Settings) -> bool` (`require_api_key(settings, name)` → `True`; `LLMAuthError` or `LLMConfigError` → `False`) in `src/invio/llm/factory.py`; export both (makes T006 pass; `tests/test_llm_layering.py` must stay green)
- [X] T013 Implement in `src/invio/services/suggest.py` (makes T007 pass):
  - `SuggestionAnswer(BaseModel)` with `ConfigDict(extra="forbid", str_strip_whitespace=True)` and the required fields `keywords_any`, `keywords_all`, `keywords_exclude` (`list[str]`), `semantic_description`, `suggested_sources_hint` (`str`); the rules "≤ 30 items, each 1–100 chars after strip", "1–2,000 chars after strip" (description) and "1–1,000 chars after strip" (hint) as `AfterValidator`s so they are **not** emitted into the JSON schema
  - frozen `SearchSuggestion` dataclass (`keywords_any/all/exclude: tuple[str, ...]`, `semantic_description`, `suggested_sources_hint`, `warnings: tuple[str, ...] = ()`) with `to_search_config()` (builds `SearchConfig(keywords=KeywordsConfig(any=…, all=…, exclude=…), semantic_description=…)`), `to_prompt_json()` and `replace(**fields)` (re-normalises and re-validates)
  - `normalise(answer)` per data-model.md steps 1–5
- [X] T014 Implement `build_messages(topic, language, *, previous=None, remark=None) -> tuple[str, str]` in `src/invio/services/suggest.py` per research R5: system template with the required content and `language_name(language)`; user message with `<topic>`, and for refinement `<previous_suggestion>` (JSON) and `<remark>`; a local `_neutralise` for the tags `topic`, `previous_suggestion`, `remark` (same technique as `invio.graph.nodes.prompting.neutralise`, not imported) (makes T008 pass)
- [X] T015 Implement `ModelChoice` (frozen: `provider_name`, `model`) and `choose_model(settings, registry, *, provider, model) -> ModelChoice` in `src/invio/services/suggest.py` per research R4 (makes T009 pass)
- [X] T016 Implement `search_yaml(suggestion) -> str` in `src/invio/services/suggest.py`: hint lines as `# ` comments (first line `# Suggested sources: …`, `invio.textsafe.strip_control`), then `yaml.safe_dump({"search": …}, sort_keys=False, allow_unicode=True)` with lists as lists (makes T010 pass)

**Checkpoint**: Contract, prompt, model choice and YAML rendering are tested in isolation — user stories can start.

---

## Phase 3: User Story 1 - Get a drafted search definition for a topic (Priority: P1) 🎯 MVP

**Goal**: `invio job suggest TOPIC` asks the capable model and shows (or prints) a valid suggestion with all four search fields plus the sources hint; Discard / Print YAML end the session without saving.

**Independent Test**: With `FakeProvider` returning a prepared answer, `invio job suggest "heat pumps" --yaml` exits 0 and prints a `search:` block that validates as part of a job; the recorded request contains the topic and the FR-005 requirements.

### Tests for User Story 1 (write first, must fail)

- [X] T017 [P] [US1] Tests for `suggest()` in `tests/test_suggest.py` (section "suggest"): with `FakeProvider([FakeReply(<valid JSON>)])` returns a normalised `SearchSuggestion` and the `Usage`; the single request uses the given model and `temperature == 0.3`, and its user message contains `<topic>`; an invalid first reply followed by a valid one → one repair request, success (2 requests); two invalid replies → `LLMInvalidOutputError` (AC1 rejection path); an `LLMUnavailableError` step propagates unchanged
- [X] T018 [P] [US1] Tests for `render_preview` and the Discard / Print YAML actions in `tests/test_suggest_flow.py`: preview contains provider/model, language, the three keyword lists (`(none)` for an empty list), the description, the sources hint and every warning prefixed `! `; with `FakePrompter(["Discard"])` `run_suggest_flow` returns `Discard()` and never calls `ask`; with `["Print YAML"]` returns `PrintYaml(first)`; the "What next?" choices include `Print YAML` and `Discard` (`FakePrompter.choices`); running out of answers raises `WizardAborted`
- [X] T019 [P] [US1] CLI tests in new `tests/test_cli_job_suggest.py` (fixture: `job_cli` from `tests/cli_helpers.py`, monkeypatch `job_module._make_suggest_provider` with `lambda name, registry: fake` returning a `FakeProvider`, `job_module.get_settings` to `make_settings(anthropic_api_key="k")`):
  - `--yaml` → exit 0, stdout is exactly `search_yaml(...)` output, no prompts asked (`forbid_prompts`), job store unchanged
  - non-interactive (`set_interactive(False)`) without `--yaml` → same YAML output, exit 0
  - `--language de` → the request's system message names German
  - usage errors exit 2 with **zero** provider requests: empty / whitespace topic, 501-character topic, `--language xx`
  - configuration errors exit 2 with zero requests: `--provider nope`, `--provider openai` without key, no key for any provider (message names the env vars), `--model unknown`, `_make_registry` returning `None`
  - provider failure (`LLMUnavailableError`) and invalid answer after repair → exit 1, stderr starts with `Error: anthropic/<model>: LLMUnavailableError:` (resp. `LLMInvalidOutputError:`), nothing saved
  - interactive with `FakePrompter(["Discard"])` → exit 0, `Nothing saved.` on stderr; with `["Print YAML"]` → YAML on stdout, exit 0
  - stderr never contains the API key value `k` nor the system prompt text
  - the production seam is wired to the logging wrapper: with `job_module.get_provider` monkeypatched to a recorder, calling the unpatched `_make_suggest_provider("anthropic")` calls `get_provider("anthropic", <settings>, registry=<registry>)` and returns its result (FR-016; the `llm.call` line itself is already covered by `tests/test_llm_logging.py` for every provider returned by `get_provider`)

### Implementation for User Story 1

- [X] T020 [US1] Implement `async def suggest(provider, model, *, topic, language) -> tuple[SearchSuggestion, Usage]` in `src/invio/services/suggest.py`: `build_messages` → `provider.complete_structured(system, user, SuggestionAnswer, model=model, temperature=TEMPERATURE)` → `normalise` (makes T017 pass)
- [X] T021 [US1] Implement `render_preview(s, *, choice, language) -> str`, the `Create`/`PrintYaml`/`Discard` result types, `FlowResult`, the `Ask` type alias and `run_suggest_flow(prompter, ask, *, first, choice, language, echo=typer.echo)` in `src/invio/cli/suggest_flow.py` with the action loop offering `Print YAML` and `Discard` (`Refine` is added in T026, `Edit a field` and `Create job` in T032, which also fixes the final choice order) (makes T018 pass)
- [X] T022 [US1] Add the `suggest` command to `src/invio/cli/commands/job.py` per contracts/cli-job-suggest.md:
  - options `TOPIC`, `--language` (default `en`, lower-cased), `--provider`, `--model`, `--yaml`
  - checks in order before any model call: topic stripped, 1–500 chars → else exit 2; language in `ISO_639_1` → else exit 2; `registry = _make_registry()` (`None` → `Configuration error: model registry unavailable`, exit 2); `choose_model(get_settings(), registry, provider=…, model=…)` with `LLMConfigError`/`LLMAuthError` → `Configuration error: …`, exit 2
  - new test seam `_make_suggest_provider(name: str, registry: ModelRegistry) -> LLMProvider`; production returns `get_provider(name, get_settings(), registry=registry)`, i.e. always the logging-wrapped provider (FR-016); tests replace the seam with `lambda name, registry: fake_provider` and do not assert log lines
  - helper `_call(coro_factory)` running `asyncio.run(...)` with `await provider.aclose()` in `finally`; stderr line `Asking <provider>/<model> …` before the first call
  - first call via `suggest()`; `LLMError` → `Error: <provider>/<model>: <Class>: <message>` (one shared formatter `format_llm_error(exc, choice)` in `src/invio/cli/suggest_flow.py`), exit 1
  - non-interactive or `--yaml`: print `search_yaml(...)` to stdout, exit 0
  - interactive: `run_suggest_flow(_make_prompter(), ask, first=…, choice=…, language=…)`; `PrintYaml` → stdout YAML, exit 0; `Discard` → `Nothing saved.` on stderr, exit 0; wrap in the existing `_errors()` so `WizardAborted`/Ctrl+C → `aborted; nothing saved`, exit 1
  - sum the `Usage` of all calls and print `suggestions used N requests, X in / Y out tokens, ~$Z` to stderr at the end (`registry.cost`; omit the cost when `None`)
  (makes T019 pass)

**Checkpoint**: US1 is a usable MVP — operators get a validated suggestion as YAML or in a preview.

---

## Phase 4: User Story 2 - Refine the suggestion iteratively (Priority: P2)

**Goal**: "Refine" re-asks the model with topic, current suggestion (including edits) and the operator's remark; failures keep the previous suggestion.

**Independent Test**: With a `FakeProvider` scripted for two answers, a session answering `Refine` → `make it narrower` → `Print YAML` sends a second request containing topic, the full first suggestion and the remark, and prints the second answer.

### Tests for User Story 2 (write first, must fail)

- [X] T023 [P] [US2] Tests for `refine()` in `tests/test_suggest.py` (section "refine"): the request's user message contains `<topic>`, `<previous_suggestion>` equal to `previous.to_prompt_json()` and `<remark>make it narrower</remark>`; returns the normalised second answer; `LLMError` propagates
- [X] T024 [P] [US2] Flow tests in `tests/test_suggest_flow.py` (section "refine", `ask` is a recording stub): `["Refine", "make it narrower", "Print YAML"]` → `ask("make it narrower", first)` called once and the result is `PrintYaml(<second>)`; two refinements pass the most recent suggestion as `previous` (US2 scenario 3); an empty remark is rejected by the validator and asked again with no `ask` call (scenario 5); `ask` raising `LLMUnavailableError` → `Error: <provider>/<model>: LLMUnavailableError: …` echoed to stderr, preview of the previous suggestion shown again, and a following `Print YAML` returns the previous suggestion (FR-010)
- [X] T025 [P] [US2] CLI test in `tests/test_cli_job_suggest.py` (AC2 end to end, `FakeProvider` with two replies): `FakePrompter(["Refine", "make it narrower", "Print YAML"])` → `provider.requests[1].user` contains the topic, every keyword and the description of the first reply, and `make it narrower`; stdout YAML equals the second reply; usage summary reports 2 requests

### Implementation for User Story 2

- [X] T026 [US2] Implement `async def refine(provider, model, *, topic, language, previous, remark)` in `src/invio/services/suggest.py` (same call path as `suggest` with `build_messages(..., previous=previous, remark=remark)`); add the `Refine` action to `run_suggest_flow` in `src/invio/cli/suggest_flow.py`: `text("How should it change?", validate=validate_nonempty)` → `ask(remark, current)`; catch `LLMError` → `echo(format_llm_error(exc, choice), err=True)` (format `Error: <provider>/<model>: <Class>: <message>`), keep `current` (makes T023, T024 pass)
- [X] T027 [US2] Build the `ask` callable in the `suggest` command in `src/invio/cli/commands/job.py`: `lambda remark, previous: _call(refine(...))` accumulating `Usage`; pass it to `run_suggest_flow` (makes T025 pass)

**Checkpoint**: US1 + US2 work; refinement context is verified with `FakeProvider` (AC2).

---

## Phase 5: User Story 3 - Edit the suggestion and turn it into a job (Priority: P2)

**Goal**: Per-field editing in the preview, then "Create job" hands over to the wizard with keywords, description, language, provider and smart model prefilled; the wizard gains a language step; nothing is saved without the wizard's confirmation.

**Independent Test**: With a scripted prompter and `FakeProvider`, accept a suggestion, choose `Create job`, change every prefilled field in the wizard and confirm → the saved job has the edited values; decline or abort instead → job store unchanged.

### Tests for User Story 3 (write first, must fail)

- [X] T028 [P] [US3] Flow tests in `tests/test_suggest_flow.py` (section "edit"): `["Edit a field", "Keywords — exclude", "a, b", "Print YAML"]` → result has `keywords_exclude == ("a", "b")` and the field's prompt default was the current comma-joined value (`FakePrompter.defaults`); description edit uses `multiline=True`; an empty description is rejected inline (`FakePrompter.errors`) and asked again; an exclude entry equal to an include keyword is dropped with the warning shown in the next preview; edits are what `ask` receives as `previous` on a following `Refine` (US2 scenario 2); `Create job` returns `Create(<current, edited suggestion>)`; the "What next?" choices are exactly `Create job`, `Refine`, `Edit a field`, `Print YAML`, `Discard` in that order (`FakePrompter.choices`); answering `CLEAR` (see T029) for `Keywords — exclude` empties the list and the next preview shows `(none)` plus the `no exclusions` warning
- [X] T029 [P] [US3] Wizard tests in `tests/test_wizard.py`:
  - first extend `FakePrompter` in `tests/cli_helpers.py` with a module constant `CLEAR` (a unique sentinel object) accepted as an answer to `text`/`autocomplete`: it returns `""` regardless of the default (the validator still runs on `""`), simulating an operator who deletes the prefilled text; add a unit test for the sentinel next to the existing `FakePrompter` tests
  - US3 scenario 4 "clear": with a prefill, answering `CLEAR` to the `any`, `all` and `exclude` keyword prompts yields empty lists in the returned `JobConfig`; answering `CLEAR` to the description is rejected (`validate_nonempty`) and asked again
  - plain wizard asks `Summary language (ISO 639-1 code)` after the description with default `en`; `xx` and `EN1` rejected, `de` accepted and present in the returned `JobConfig.language` (FR-011a)
  - with `WizardPrefill(keywords={"any": ["a"], "all": [], "exclude": ["x"]}, description="D", language="de", provider="anthropic", smart_model=<registered>, sources_note="Try trade portals")`: the keyword prompts default to `a` / `` / `x`, the description defaults to `D`, the language defaults to `de`, the provider select defaults to `anthropic`, the smart-model select defaults to the prefilled model; answering `""` everywhere keeps all prefilled values in the result; answering new values replaces each one (SC-005: all four search fields and the language)
  - `Hint from the suggestion: Try trade portals` is echoed once before the first source question and no source is prefilled (US3 scenario 3)
  - a prefilled smart model not in the registry falls back to the text prompt with it as default
  - re-ask after a validation error keeps the edited language
  - `WizardPrefill.from_suggestion(s, language="de", choice=ModelChoice("anthropic", "m"))` maps all fields
- [X] T030 [P] [US3] CLI tests in `tests/test_cli_job_suggest.py` (section "create job"; AC3, AC4):
  - `Create job` + full wizard answers + `Save this job? → True` → exit 0, `created job '<name>'`, stored job's `search` and `language` equal the (edited) suggestion; `llm.provider`/`models.smart` default to the suggestion's choice
  - `Save this job? → False` → exit 1, `job not created`, job store unchanged
  - answers run out inside the wizard (Ctrl+C) → exit 1, `aborted; nothing saved`, store unchanged
  - answers run out in the preview → exit 1, store unchanged
  - `Discard` after edits → exit 0, store unchanged
- [X] T031 [US3] Update every scripted answer sequence in `tests/test_wizard.py` and `tests/test_cli_job_create.py` (and any other test driving `run_wizard`, find with `grep -rn "run_wizard\|FakePrompter" tests/`) for the new language question placed after the description (answer `""` to keep `en`); all existing wizard/create tests must pass unchanged in intent

### Implementation for User Story 3

- [X] T032 [US3] Add per-field editing to `run_suggest_flow` in `src/invio/cli/suggest_flow.py`: `Edit a field` → `select("Which field?", ["Keywords — any of", "Keywords — all of", "Keywords — exclude", "Description"])` → `text` with the current value as default (lists comma-joined and split like the wizard's `_split_keywords`; description `multiline=True`) and a validator that tries `current.replace(...)` and returns the first `ValidationError` message on failure; add the `Create job` action returning `Create(current)`; final choice order `Create job`, `Refine`, `Edit a field`, `Print YAML`, `Discard` (makes T028 pass)
- [X] T033 [US3] Extend `src/invio/cli/wizard.py` per research R7 and contracts/python-api.md:
  - `validate_language(value)` (lower-case two-letter code in `ISO_639_1`, else message `unknown ISO 639-1 language code`), exported in `__all__`
  - `Q_LANGUAGE: Final = "Summary language (ISO 639-1 code)"`, `_Session.language: str = "en"`, `_Session.sources_note: str | None = None`, `mapping()` emits `"language"`
  - `_step_language` inserted in `_STEPS` right after `_step_description`; `("language", _step_language)` added to `_STEP_FOR_PREFIX`
  - `_ask_model(s, select_question, id_question, default: str | None = None)`: default used for `select` when in the known models, else as the `text` default; `_step_llm` passes `s.fast` / `s.smart`
  - `_step_sources` echoes `Hint from the suggestion: <note>` once when `sources_note` is set
  - frozen `WizardPrefill` with `from_suggestion`, and `run_wizard(..., prefill: WizardPrefill | None = None)` seeding `keywords`, `description`, `language`, `provider`, `smart`, `sources_note`
  (makes T029 pass; T031 keeps existing tests green)
- [X] T034 [US3] Implement the `Create` hand-over in the `suggest` command in `src/invio/cli/commands/job.py`: `service = _make_service()`, `existing = set(service.names())`, `run_wizard(_make_prompter(), _make_checker(), registry=registry, existing_names=existing, default_timezone=_default_timezone(), prefill=WizardPrefill.from_suggestion(result.suggestion, language=language, choice=choice))`; `None` → `job not created`, exit 1; else `service.create(name, config)` and echo `_status_line("created", …)`; all inside `_errors()` (makes T030 pass)

**Checkpoint**: US1–US3 work; the wizard accepts a prefilled suggestion, every field is editable, and nothing is saved without confirmation (AC3, AC4).

---

## Phase 6: User Story 4 - Print the suggestion as YAML (Priority: P3)

**Goal**: The printed YAML can be pasted into a job file.

**Independent Test**: Output of `invio job suggest … --yaml` with a fake provider, merged into a valid job mapping, validates; the hint is a comment.

- [X] T035 [P] [US4] Round-trip test in `tests/test_cli_job_suggest.py` (section "yaml"): stdout of `--yaml` and of the interactive `Print YAML` action are identical for the same suggestion; parsing it and merging `search` into a valid job mapping passes `validate_job`; the hint appears only in `# ` comment lines; with a multi-line hint every line is commented (US4 scenarios 1–3)
- [X] T036 [US4] Fix any gaps found by T035 in `search_yaml` (`src/invio/services/suggest.py`) or the `Print YAML` path in `src/invio/cli/commands/job.py` (no change expected if T016/T022 are complete)

**Checkpoint**: All four user stories are independently functional.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T037 [P] Document `invio job suggest` in `README.md` next to "Finding sources: `invio source discover`": synopsis, options and defaults (provider/model selection rule, `--language`), the preview actions, non-interactive/`--yaml` output, exit codes; add the wizard's new `Summary language` question to the `invio job create` section
- [X] T038 [P] Add an edge-case test in `tests/test_suggest.py`: a topic containing an injection attempt (`ignore previous instructions`) is only present inside `<topic>`; a model answer with 31 `keywords_any` entries goes through the repair round (2 requests) and fails with `LLMInvalidOutputError` if repeated
- [X] T039 Run the quality gates: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src` (strict), `uv run pytest`; fix all findings (no new `# type: ignore` without a justifying comment)
- [ ] T040 Run the manual end-to-end check of `specs/020-gh-issue-37/quickstart.md` §4 with a real provider key; record the result in the PR description (and that no new runtime dependency was added)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies
- **Foundational (Phase 2)**: depends on Setup; blocks all user stories
- **US1 (Phase 3)**: depends on Phase 2 — MVP
- **US2 (Phase 4)**: depends on US1 (extends the flow and command built in T021/T022)
- **US3 (Phase 5)**: depends on US1 (flow + command); independent of US2 except T028's "edits are `previous` on refine" case, which needs T026
- **US4 (Phase 6)**: depends on US1 only (YAML path built in T016/T022)
- **Polish (Phase 7)**: after the desired stories

### Within Each Phase

- Tests before implementation (they must fail first)
- `suggest.py` contract (T013) before prompt (T014), model choice (T015), YAML (T016)
- `suggest_flow.py` (T021) before the command wiring (T022); T031 together with T033 (same test expectations)

### Parallel Opportunities

- T002, T003, T004 in parallel
- T005–T010 (tests, different files/sections) in parallel; T011 and T012 in parallel with each other and with T013
- Per story, all tasks marked [P] (tests) in parallel
- After US1: US3 wizard work (T029, T031, T033) can proceed in parallel with US2 (T023–T027) — different files
- T037 and T038 in parallel

---

## Parallel Example: User Story 1

```bash
# Tests for US1 together:
Task: "T017 suggest() tests in tests/test_suggest.py"
Task: "T018 preview/Discard/Print YAML tests in tests/test_suggest_flow.py"
Task: "T019 CLI tests in tests/test_cli_job_suggest.py"
```

## Parallel Example: User Story 3

```bash
Task: "T028 edit tests in tests/test_suggest_flow.py"
Task: "T029 wizard prefill/language tests in tests/test_wizard.py"
Task: "T030 create-job CLI tests in tests/test_cli_job_suggest.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 + Phase 2
2. Phase 3 (US1) → validate with `invio job suggest "…" --yaml` and the Discard/Print YAML preview
3. Stop and demo: operators already get a validated draft to paste into job files

### Incremental Delivery

1. US1 → MVP (draft + YAML)
2. US2 → refinement (AC2)
3. US3 → editing + wizard hand-over + language step (AC3, AC4)
4. US4 → YAML round-trip guarantee
5. Polish → docs, gates, manual check

### Notes

- Commit after each task or logical group, on `gh-issue-37`
- Exit codes and stream rules are normative in `contracts/cli-job-suggest.md`
- Never print or log credentials or the system prompt
