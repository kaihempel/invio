# Implementation brief — gh-issue-37 (architect + python-reviewer, reconciled)

Binding decisions that OVERRIDE tasks.md / contracts where they differ. Record each deviation in the PR.

## Corrections to tasks.md (code reality)
1. `invio source discover` is NOT an LLM command. Precedents: `invio llm test` (src/invio/cli/commands/llm.py:25-74) and
   `structured_with_repair` (src/invio/llm/base.py:226-268) which every provider incl. `FakeProvider.complete_structured`
   already uses. `suggest()`/`refine()` just call `provider.complete_structured(...)`; no repair code in the service.
   A repaired call shows up as 2 entries in `FakeProvider.requests`.
2. `job_cli` fixture registry (`make_registry()`, tests/cli_helpers.py:192-217) has only openai+mistral models. In
   tests/test_cli_job_suggest.py add a local fixture patching `job_module._make_registry` with a registry holding ≥2
   anthropic models at different prices + openai ones. Do not change `make_registry()`.
3. Stderr gets an `Asking <provider>/<model> …` line and `llm.error` JSON lines first: assert
   `any(l.startswith("Error: ...") for l in stderr.splitlines())`, not "stderr starts with".
4. The valid job mapping is the `job_data` fixture in tests/conftest.py:115 (not tests/job_helpers.py).
5. Do not reuse `config.job._Keyword` (emits `minLength`). Local `_Kw = Annotated[str, AfterValidator(...)]` and list caps
   via `AfterValidator`; the JSON schema must contain no `maxItems/maxLength/minLength/minItems` (test it).
6. `mapped_errors` / `job._errors()` don't map `LLMConfigError`/`LLMAuthError`: add explicit
   `except (LLMAuthError, LLMConfigError)` → `fail(f"Configuration error: {exc}", 2)` BEFORE the generic `LLMError` → 1.
7. Add module-level `from invio.llm.factory import get_provider` in job.py (monkeypatch seam), or a `_make_suggest_provider` seam.
8. No new module under cli/commands/ (auto-discovered as a group). Command lives in job.py.
9. T031 is small: add `language: str = ""` param to `script()` (tests/test_wizard.py:70) emitted right after description;
   insert `Q_LANGUAGE` after `Q_DESC` in `test_questions_in_documented_order`; add `w.Q_LANGUAGE` to the Ctrl+C
   parametrization in tests/test_cli_job_create.py:269-283.

## Design decisions (reconciled)
- **Models**: `SuggestionAnswer` = Pydantic, `ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)`, the
  single schema source. `SearchSuggestion` = `@dataclass(frozen=True, slots=True, kw_only=True)` (+ `warnings`).
  `replace` is typed (explicit keyword params or `dataclasses.replace`) followed by `normalise`. `normalise` also
  validates via `SuggestionAnswer.model_validate` AND `to_search_config()` so operator edits obey the same caps as
  model answers. `to_search_config()` is the only bridge to `SearchConfig`.
- **Commas in keywords**: `normalise` splits keywords containing commas into separate keywords (and adds a warning),
  because editing/prefill uses comma-joined text. Test it.
- **Control chars**: `normalise` runs `strip_control` (multiline for description and hint). Topic and remark are trimmed
  + `strip_control`ed before use. The preview shows the full description (no truncation).
- **Prompt injection**: extract a generic `neutralise_tags(text, tags)` into `invio/textsafe.py`; make
  `graph/nodes/prompting.py`'s `neutralise` delegate to it (behaviour unchanged). In `build_messages`, neutralise the
  topic, the remark AND the previous suggestion JSON for tags `topic|previous_suggestion|remark`.
- **Refine callable**: `Refine = Callable[[str, SearchSuggestion], SearchSuggestion]` (remark, previous) — only used for
  refinement, no optional args. Usage is accumulated in a small `_UsageTally` in job.py (include `exc.usage` from
  `LLMInvalidOutputError`). The flow knows nothing about usage. Print the usage summary whenever ≥1 request was made,
  including on exit-1 paths.
- **Async edge**: one generic `_call[T](provider, make)` in job.py next to `_execute`: runs the coroutine and
  `await provider.aclose()` in `finally` inside the same `asyncio.run`. Add a test that two calls on one provider work.
- **choose_model**: explicit provider must be in `registered_providers()` else `LLMConfigError`; credentials via
  `require_api_key` (raise its exact `LLMAuthError`). With `--model` but no `--provider`, derive the provider from
  `registry.get(model).provider` then check credentials. Else first registered provider (alphabetical) with
  credentials and ≥1 registered model; model = `most_expensive` (ties → lowest id). Errors list `INVIO_<P>_API_KEY`.
- **Registry/factory**: `ModelRegistry.most_expensive` mirrors `cheapest` (share the price key). `registered_providers()
  -> tuple[str, ...]` = `_discover(); tuple(sorted(_REGISTRY))`. `has_credentials` catches `LLMAuthError` and
  `LLMConfigError` → False (test both, incl. "ollama").
- **Flow result types**: `@dataclass(frozen=True, slots=True)` Create / PrintYaml / Discard; consume with `match` +
  `assert_never`.
- **Wizard**: drop `WizardPrefill.from_suggestion` — build the `WizardPrefill` in job.py (wizard must not import
  services.suggest). Use `Mapping[str, Sequence[str]]` for keywords; copy into `_Session` lists. New `_step_language` after
  `_step_description`; `validate_language` uses `ISO_639_1` with lower-casing (same rule as `JobConfig.language`); add
  `"language"` to `mapping()` and `("language", _step_language)` to `_STEP_FOR_PREFIX`. `_ask_model` uses the prefilled
  smart model as default only if it is in `known`. Sources step echoes the hint once (then clears `sources_note`).
  Fast model is not prefilled.
- **FakePrompter**: add `CLEAR` sentinel (answer → ""), checked before the str assert (tests/cli_helpers.py:25-55).
- **Command order of checks** (all before any model call): topic → language → registry (`default_registry()`-style
  failure must exit 2, don't use the warn-and-None path for suggest) → choose_model → provider construction.
  `_make_prompter()` only in the interactive branch; `_make_service()` only in the Create path (`--yaml` never touches
  the DB). Wrap interactive branch in `_errors()`. `format_llm_error` lives in cli/errors.py and `strip_control`s.
- **Imports**: alias `invio.llm.base.LLMProvider` vs the enum `invio.config.job.LLMProvider`.
- **Layering test (T004)**: services/suggest.py must not import typer, invio.db, invio.graph, invio.cli, invio.pipeline,
  invio.sources.

## Conventions
mypy --strict with pydantic plugin, PEP 695 generics, `X | None`, `Final`, `__all__`, no `Any` except boundaries.
One-line imperative docstrings; module docstrings state layering. Results → stdout, diagnostics → stderr. Exit codes
0/1/2. `asyncio.run` only at CLI edge; always aclose.
