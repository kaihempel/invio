# Quickstart: Job Search Suggestion Assistant (gh-issue-37)

How to prove the feature works. Interfaces: [contracts/cli-job-suggest.md](contracts/cli-job-suggest.md),
[contracts/python-api.md](contracts/python-api.md); entities: [data-model.md](data-model.md).

## 1. Prerequisites

```bash
uv sync --locked
# For the manual run only (section 4): one provider key and a database
export INVIO_ANTHROPIC_API_KEY=…      # or OPENAI / MISTRAL / GOOGLE
export INVIO_DATABASE_URL=sqlite:///./invio.db && uv run invio db upgrade
```

## 2. Automated checks (no network, no real provider)

```bash
uv run pytest tests/test_suggest.py tests/test_cli_job_suggest.py \
              tests/test_wizard.py tests/test_cli_job_create.py tests/test_llm_registry.py tests/test_llm_factory.py
uv run ruff check && uv run ruff format --check && uv run mypy src
```

Acceptance criteria → tests (names are indicative):

| Criterion | Test | Expected |
|---|---|---|
| AC1 all four search fields, valid `SearchConfig` | `test_suggest_returns_all_fields_valid_for_search_config` | `FakeProvider` answer → `SearchSuggestion` with the four fields; `to_search_config()` succeeds; `--yaml` output loads into a job via `validate_job` |
| AC1 invalid answer | `test_invalid_answer_repaired_once_then_fails` | two invalid replies → `LLMInvalidOutputError`, exit 1, nothing saved |
| AC2 refinement uses previous suggestion | `test_refine_sends_topic_previous_and_remark` | `FakeProvider.requests[1].user` contains `<topic>`, the full first suggestion JSON and `<remark>make it narrower</remark>`; preview shows the second answer |
| AC2 edits are context | `test_refine_sends_edited_suggestion` | an edited `exclude` list appears in the second request |
| AC3 prefill, every field editable | `test_create_job_prefills_and_operator_edits_every_field` | scripted wizard changes any/all/exclude, description and language; saved job has the edited values; provider/smart default = suggestion's |
| AC3 sources hint | `test_sources_step_shows_hint_and_starts_empty` | hint echoed before first source question; no source prefilled |
| AC4 no save without confirmation | `test_declined_or_aborted_wizard_saves_nothing`, `test_discard_saves_nothing`, `test_yaml_mode_saves_nothing` | job store unchanged; exit codes 1 / 0 / 0 |
| FR-005 prompt content | `test_system_prompt_requires_synonyms_variants_exclusions_relevance` | system message mentions synonyms, English and German, exclusions, "Relevant"/"Not relevant", target language name |
| FR-006 untrusted data | `test_topic_tags_are_neutralised` | `</topic>` in the topic does not close the block |
| FR-003a selection | `test_choose_model_*` | alphabetical first provider with key; most expensive model; explicit flags win; missing key / unknown provider / unknown model → exit 2 before any request |
| FR-014 normalisation | `test_normalise_*` | trim, case-insensitive dedupe, include/exclude conflict removed with warning |
| FR-011a language step | `test_wizard_asks_language_default_en` | plain `job create` asks the language, default `en`, rejects `xx` |

## 3. Non-interactive smoke test (fake provider)

`tests/test_cli_job_suggest.py` patches `_make_suggest_provider` to a `FakeProvider` and runs
`CliRunner().invoke(app, ["job", "suggest", "heat pumps", "--language", "de", "--yaml"])`.
Expected: exit 0, stdout starts with `# Suggested sources:` and contains a `search:` block that
validates as part of a job.

## 4. Manual end-to-end check (real provider, terminal)

```bash
uv run invio job suggest "Wärmepumpen in Mehrfamilienhäusern" --language de
```

1. Preview shows provider/model, the three keyword lists (German and English variants), a
   description with "Relevant:" / "Not relevant:" and a sources hint.
2. Choose **Refine**, type `make it narrower` → a narrower suggestion appears.
3. Choose **Edit a field** → **Keywords — exclude**, add a term → preview updated.
4. Choose **Create job** → wizard: answer schedule, notification and one source (the hint is
   shown at the sources step); keywords, description and `de` are prefilled; at
   `Save this job?` answer **No** → `job not created`, `invio job list` unchanged.
5. Repeat and answer **Yes** → `created job '<name>'`; `invio job show <name>` shows the
   search section and `language: de`.
6. `uv run invio job suggest "heat pumps" | cat` → YAML only, no questions.
7. `INVIO_ANTHROPIC_API_KEY= uv run invio job suggest x --provider anthropic` → exit 2,
   message names `INVIO_ANTHROPIC_API_KEY`.
