---

description: "Task list for issue #3 — validated research job configuration"
---

# Tasks: Validated Research Job Configuration

**Input**: Design documents from `specs/001-gh-issue-3/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/job-file.md,
contracts/python-api.md, quickstart.md

**Tests**: Included — required by spec FR-033 and constitution principle III. Write each story's
tests first and confirm they fail before implementing.

**Organization**: Tasks are grouped by user story (spec.md) in priority order:
US1 (P1) → US2 (P1) → US3 (P2) → US5 (P2) → US4 (P3).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: User story from spec.md (US1–US5)

## Conventions that apply to every task

- Package is `invio` (issue's `scout/` → `src/invio/`). Models: `src/invio/config/job.py`.
  Records: `src/invio/domain.py`.
- `tests/conftest.py` has an **autouse fixture that `chdir`s into `tmp_path`**. Tests must locate
  repo files absolutely: `REPO_ROOT = Path(__file__).resolve().parents[1]`.
- Match existing style: module docstrings, `from __future__`-free Python 3.12 typing
  (`X | None`, `StrEnum`), ruff line length 100, `mypy --strict` clean.
- Pydantic runs model validators only when all field validation succeeded — cross-field tests
  must use otherwise-valid input.
- Every `int` / `float` / `bool` config field is `StrictInt` / `StrictFloat` / `StrictBool`
  (FR-002a); `StrictFloat` still accepts whole numbers such as `min_relevance: 1`.
- Test files are split by area so `[P]` tasks never share a file: `test_job_config.py`
  (root/defaults/unknown keys), `test_job_schedule.py`, `test_job_sections.py`
  (notification/search/llm/limits), `test_job_sources.py`, `test_job_providers.py`,
  `test_job_yaml.py`, `test_job_schema.py`, `test_domain.py`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Dependencies and empty modules

- [X] T001 Add runtime dependencies `pyyaml>=6.0` and `pydantic[email]>=2.13` (pulls `email-validator`) and dev dependencies `types-PyYAML`, `jsonschema>=4`, `types-jsonschema` in pyproject.toml; run `uv lock && uv sync` to update uv.lock
- [X] T002 Run the baseline gates `uv run mypy src && uv run pytest` after T001 to confirm the dependency change breaks nothing before feature work starts

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared pieces every configuration story builds on

**⚠️ CRITICAL**: US1–US4 cannot start until this phase is complete (US5 does not depend on it)

- [X] T003 Create src/invio/config/job.py with module docstring (purpose: job file contract; comments not preserved on save), `SUPPORTED_SCHEMA_VERSION: Final = 1`, and `StrEnum`s exactly: `Frequency` = `daily`, `weekly`, `monthly`; `Weekday` = `monday` … `sunday`; `LLMProvider` = `mistral`, `openai`, `anthropic`, `google`, `ollama`
- [X] T004 Add private base `_StrictModel(BaseModel)` in src/invio/config/job.py with `model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)`; every config model in later tasks inherits from it
- [X] T005 Add `JobConfigError(Exception)` in src/invio/config/job.py with attributes `path: Path | None` and `errors: list[str]`; `__str__` returns the header line followed by each error indented two spaces, per contracts/job-file.md "Error contract"

**Checkpoint**: Foundation ready — story phases can begin

---

## Phase 3: User Story 1 — Load a valid research job definition (Priority: P1) 🎯 MVP

**Goal**: A complete, valid job file loads into an immutable `JobConfig` with defaults applied.

**Independent Test**: `load_yaml(REPO_ROOT / "docs/job.example.yaml")` succeeds and exposes all
sections; a minimal file gets defaults `send_if_empty=False`, `min_relevance=0.6`,
`schema_version=1`, `LimitsConfig()`.

### Tests for User Story 1 ⚠️ (write first, must fail)

- [X] T006 [P] [US1] Add fixture `job_data() -> dict[str, Any]` to tests/conftest.py returning a minimal valid job mapping (weekly schedule `time: "07:30"`, `weekday: "monday"`, `timezone: "Europe/Berlin"`; one recipient; one `rss` source; `semantic_description`; `llm` with provider `openai`, models `fast`/`smart`); tests deep-copy and mutate it
- [X] T007 [P] [US1] In tests/test_job_config.py add happy-path tests: `JobConfig.model_validate(job_data)` succeeds; omitted optionals get defaults (`schema_version == 1`, `notification.send_if_empty is False`, `search.min_relevance == 0.6`, `search.keywords.any == []`, `limits == LimitsConfig()` with `20/100/20/200000`); models are frozen (assigning a field raises `ValidationError`)
- [X] T008 [P] [US1] Create tests/test_job_sources.py with a parametrized test with one valid entry per source kind — `{"type": "rss", "url": ...}`, `web`, `sitemap`, `{"type": "youtube_channel", "channel_id": "UC123"}`, `{"type": "youtube_playlist", "playlist_id": "PL123"}` — asserting `isinstance` of the matching class, `enabled is True`, `name is None`; plus one with `name` and `enabled: false` set; plus a job whose sources are **all** `enabled: false` is accepted (spec edge case)
- [X] T009 [P] [US1] In tests/test_job_yaml.py add tests: `load_yaml(REPO_ROOT / "docs/job.example.yaml")` returns a `JobConfig` with 5 sources of the 5 kinds; `load_yaml` accepts `str` and `Path`; YAML-1.1 pitfalls are neutralised — a file with unquoted `time: 17:30` loads as `"17:30"`, keyword list `[on, yes, no, 2026-10-04]` loads as those strings, `send_if_empty: true` loads as `True`; umlauts in `subject` load unchanged

### Implementation for User Story 1

- [X] T010 [US1] Add `ScheduleConfig(_StrictModel)` in src/invio/config/job.py with fields in order: `frequency: Frequency`, `time: str`, `weekday: Weekday | None = None`, `day_of_month: StrictInt | None = None`, `timezone: str` (constraints added in US2); `Weekday` docstring notes that input is case-insensitive while the JSON Schema lists lowercase only
- [X] T011 [US1] Add `NotificationConfig` (`to: list[EmailStr]`, `subject: str`, `send_if_empty: StrictBool = False`), `KeywordsConfig` (`any: list[str] = []`, `all: list[str] = []`, `exclude: list[str] = []` via `Field(default_factory=list)`), `SearchConfig` (`keywords: KeywordsConfig = Field(default_factory=KeywordsConfig)`, `semantic_description: str`, `min_relevance: StrictFloat = 0.6`), `LLMModels` (`fast: str`, `smart: str`), `LLMConfig` (`provider: LLMProvider`, `models: LLMModels`, `fallback_provider: LLMProvider | None = None`), `LimitsConfig` (`max_items_per_source: StrictInt = 20`, `max_items_per_run: StrictInt = 100`, `max_items_in_notification: StrictInt = 20`, `max_llm_tokens_per_run: StrictInt = 200000`) in src/invio/config/job.py, field order exactly as data-model.md
- [X] T012 [US1] Add source models in src/invio/config/job.py (no shared base class, so each variant keeps the field order) with fields in order `type`, locator, `name: str | None = None`, `enabled: StrictBool = True`: `RssSource(type: Literal["rss"], url: HttpUrl)`, `WebSource(type: Literal["web"], url: HttpUrl)`, `SitemapSource(type: Literal["sitemap"], url: HttpUrl)`, `YoutubeChannelSource(type: Literal["youtube_channel"], channel_id: str)`, `YoutubePlaylistSource(type: Literal["youtube_playlist"], playlist_id: str)`; then `SourceConfig = Annotated[RssSource | WebSource | SitemapSource | YoutubeChannelSource | YoutubePlaylistSource, Field(discriminator="type")]`
- [X] T013 [US1] Add `JobConfig(_StrictModel)` in src/invio/config/job.py with fields in order: `schema_version: StrictInt = 1`, `schedule: ScheduleConfig`, `notification: NotificationConfig`, `sources: list[SourceConfig]`, `search: SearchConfig`, `llm: LLMConfig`, `limits: LimitsConfig = Field(default_factory=LimitsConfig)`
- [X] T014 [P] [US1] Create docs/job.example.yaml with exactly the reference example from contracts/job-file.md (all sections, all five source kinds, unquoted `time: 07:30`)
- [X] T015 [US1] Add `JobYamlLoader(yaml.SafeLoader)` in src/invio/config/job.py per research.md R1: copy `SafeLoader.yaml_implicit_resolvers`, then keep only resolvers so that `null`/`~`/empty → None, `true`/`false` (any case) → bool, decimal ints (`^[-+]?[0-9]+$`) → int, decimal floats → float; drop sexagesimal int/float, `yes/no/on/off` bools, and timestamp resolvers (everything else stays `str`)
- [X] T016 [US1] Implement `load_yaml(path: str | os.PathLike[str]) -> JobConfig` in src/invio/config/job.py: read UTF-8 text, `yaml.load(text, Loader=JobYamlLoader)`, `JobConfig.model_validate(data)`; wrap `OSError` as `JobConfigError(path, [f"cannot read job file {path}: {reason}"])`, `yaml.YAMLError` as `invalid YAML in job file {path}: {message}`, non-dict/None top level as `job file {path} must contain a mapping at the top level` (full validation-error formatting comes in T024)

**Checkpoint**: US1 tests pass; reference example loads (SC-001)

---

## Phase 4: User Story 2 — Reject invalid job definitions with clear messages (Priority: P1)

**Goal**: Every invalid input from spec US2 and Edge Cases is rejected with `<field.path>: <message>`.

**Independent Test**: Each broken mapping/file in tests raises `ValidationError` (models) or
`JobConfigError` (files) whose message names the offending field and rule.

### Tests for User Story 2 ⚠️ (write first, must fail)

- [X] T017 [P] [US2] Create tests/test_job_schedule.py with parametrized schedule rejection tests (assert error loc and message substring): weekly without weekday → `weekday is required when frequency is 'weekly'`; monthly without day_of_month → `day_of_month is required when frequency is 'monthly'`; daily with weekday → `weekday is only allowed when frequency is 'weekly'`; weekly with day_of_month and monthly with weekday → `... is only allowed ...`; `day_of_month` 0 and 32; `time` `25:00`, `7:5`, `24:00`, `12:60`; `timezone` `Europe/Atlantis` → `unknown timezone`; weekday `Funday`, `mon`, `1`; `day_of_month: true` and `day_of_month: 15.0` (FR-002a). Plus acceptance: weekday `Monday`/`MONDAY` → `Weekday.MONDAY`; monthly with `day_of_month: 31` accepted
- [X] T018 [P] [US2] Create tests/test_job_sections.py with rejection tests for notification/search/llm/limits/root: `to: []`, `to: ["not-an-email"]`; duplicate recipients accepted; `subject: "  "`; `semantic_description: " "`; `min_relevance` `1.5` and `-0.1` rejected, `0` and `1` accepted; keyword item `""` rejected; all keyword lists empty accepted; provider `OpenAI` and `cohere` rejected; `fallback_provider == provider` → `fallback_provider must differ from provider`; `models.fast: ""` rejected; each limit `0`, `-1`, `1.5` rejected; `schema_version: 2` → `schema_version 2 is not supported (max 1)`; `schema_version: 0` rejected; `sources: []` rejected. Strict types (FR-002a): `max_items_per_run: true`, `min_relevance: false`, `send_if_empty: 0`, `schema_version: true` rejected; `min_relevance: 1` (int) accepted as `1.0`. Accepted edge cases: `max_items_in_notification: 500` with `max_items_per_run: 100`, and `max_items_per_source: 500` with `max_items_per_run: 100` (no cross-limit rule)
- [X] T019 [P] [US2] In tests/test_job_sources.py add per-source-kind rejection tests (parametrized over all five kinds, covering FR-033 "invalid case per source kind"): missing locator; foreign locator (e.g. `rss` with `channel_id`, `youtube_channel` with `url`) → extra field error; unknown key `foo`; for `rss`/`web`/`sitemap` also `url: "ftp://x"` and `url: "/relative"`; `channel_id: ""`/`playlist_id: ""`; `enabled: 1` (FR-002a); unknown `type: "podcast"` → message lists the five allowed tags
- [X] T020 [P] [US2] In tests/test_job_config.py add `test_unknown_keys_rejected` parametrized over every section (`frequncy` in schedule, `cc` in notification, `foo` at root, in keywords, in models, in limits) asserting "Extra inputs are not permitted" (FR-002)
- [X] T021 [P] [US2] Create tests/test_job_providers.py with `test_providers_match_settings`: `{p.value for p in LLMProvider}` equals `{n.removesuffix("_api_key") for n in Settings.model_fields if n.endswith("_api_key")} | {n.removesuffix("_base_url") for n in Settings.model_fields if n.endswith("_base_url")}` (import `Settings` from `invio.config.settings`); plus `test_provider_without_api_key_is_accepted`: with no `INVIO_*` variables set (guaranteed by the autouse fixture), a job with `provider: anthropic` and `fallback_provider: google` validates
- [X] T022 [P] [US2] In tests/test_job_yaml.py add `JobConfigError` tests: missing file → `cannot read job file`; YAML syntax error → `invalid YAML in job file` with line info; empty file and top-level list → `must contain a mapping at the top level`; invalid source url at index 2 → error line starts `sources[2].url:` (no `rss` tag segment); misspelt `llm.frequncy` → line `llm.frequncy: Extra inputs are not permitted`; `exc.path == Path(given)` for both `str` and `Path` input; a file with field errors **and** a cross-field error in another section reports both (different sections), while a schedule with a bad `time` and a missing weekday reports only `schedule.time` (documented two-pass behaviour, FR-027); never leaks `ValidationError`

### Implementation for User Story 2

- [X] T023 [US2] Add constraints in src/invio/config/job.py: `ScheduleConfig.time` `Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")`; `day_of_month` `Field(default=None, ge=1, le=31)`; `timezone` `field_validator` calling `zoneinfo.ZoneInfo(value)`, mapping `ZoneInfoNotFoundError`/`ValueError` to `ValueError(f"unknown timezone {value!r}")`; `weekday` `field_validator(mode="before")` lower-casing `str` input; `model_validator(mode="after")` emitting the four messages from data-model.md "Cross-field" table
- [X] T024 [US2] Add constraints in src/invio/config/job.py: `NotificationConfig.to` `Field(min_length=1)`; `subject`, `semantic_description`, `LLMModels.fast/smart`, `channel_id`, `playlist_id` `Field(min_length=1)` (whitespace stripped by base config); keyword items `list[Annotated[str, Field(min_length=1)]]`; `min_relevance` `Field(default=0.6, ge=0, le=1)`; every `LimitsConfig` field `Field(default=<default>, ge=1)` on its `StrictInt` type; `JobConfig.sources` `Field(min_length=1)`; `schema_version` `Field(default=1, ge=1)` plus `field_validator` raising `schema_version {v} is not supported (max {SUPPORTED_SCHEMA_VERSION})`; `LLMConfig` `model_validator(mode="after")` raising `fallback_provider must differ from provider`
- [X] T025 [US2] Add `_format_validation_error(exc: ValidationError) -> list[str]` in src/invio/config/job.py rendering each error as `"<loc>: <msg>"`: string segments joined with `.`, int segments as `[i]`, and the discriminator-tag segment directly following `("sources", i)` removed (`("sources", 2, "rss", "url")` → `sources[2].url`); model-level errors with empty loc render as `<msg>`; strip Pydantic's `"Value error, "` prefix from custom messages (verified: `ValueError("weekday is required…")` surfaces as `Value error, weekday is required…`) so lines read `schedule: weekday is required when frequency is 'weekly'`; wire it into `load_yaml` so `ValidationError` becomes `JobConfigError(Path(path), lines)` with header `invalid job file {path}:`; every `JobConfigError` raised by `load_yaml` stores `Path(path)`

**Checkpoint**: US1 + US2 tests pass; all US2 acceptance scenarios and config edge cases covered (SC-002)

---

## Phase 5: User Story 3 — Save a job definition back to a file (Priority: P2)

**Goal**: Lossless, complete, ordered YAML output.

**Independent Test**: load → `write_yaml` → load yields an equal `JobConfig`; output contains
every field in definition order.

### Tests for User Story 3 ⚠️ (write first, must fail)

- [X] T026 [P] [US3] In tests/test_job_yaml.py add round-trip tests: for `docs/job.example.yaml`, a monthly job and a daily job (built from `job_data`), `load_yaml(write_yaml(...))` equals original (SC-003); `dump_yaml` top-level keys are exactly `["schema_version", "schedule", "notification", "sources", "search", "llm", "limits"]` in order (check via `yaml.safe_load` + `list(d)`); omitted defaults appear explicitly (`send_if_empty: false`, full `limits` block, `weekday: null` for monthly, `name: null` for unnamed source); `Grüße` appears unescaped in the text; time dumps quoted (`'17:30'`) and reloads as `"17:30"`; a file with `# comment` loses the comment after save (documented behaviour); weekday `MONDAY` is saved as `monday`

### Implementation for User Story 3

- [X] T027 [US3] Implement `dump_yaml(config: JobConfig) -> str` in src/invio/config/job.py: `yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False, allow_unicode=True, default_flow_style=False)` — includes all fields, defaults and `None` (FR-024)
- [X] T028 [US3] Implement `write_yaml(config: JobConfig, path: str | os.PathLike[str]) -> None` in src/invio/config/job.py writing `dump_yaml(config)` as UTF-8

**Checkpoint**: US3 tests pass independently of US4/US5

---

## Phase 6: User Story 5 — Shared research item records (Priority: P2)

**Goal**: Stdlib-only `Candidate` and `ProcessedItem` for all tracks. Independent of Phases 2–5.

**Independent Test**: Records construct with all fields; importing `invio.domain` in a fresh
interpreter loads no `pydantic`, `yaml`, `sqlalchemy`, `httpx`, `requests`, `langgraph`.

### Tests for User Story 5 ⚠️ (write first, must fail)

- [X] T029 [P] [US5] Create tests/test_domain.py: construct `Candidate` with all 7 fields and `ProcessedItem` with required 5 fields (assert `relevance`, `summary`, `error` default to `None`); records are frozen (`dataclasses.FrozenInstanceError` on assignment) and keyword-only (positional construction raises `TypeError`); `dataclasses.replace` produces an updated copy; `subprocess.run([sys.executable, "-c", "import sys, invio.domain; bad = {'pydantic','yaml','sqlalchemy','httpx','requests','langgraph'} & {m.split('.')[0] for m in sys.modules}; sys.exit(sorted(bad) or 0)"], check=True)` succeeds (SC-006)

### Implementation for User Story 5

- [X] T030 [P] [US5] Create src/invio/domain.py (stdlib imports only: `dataclasses`, `datetime`, `typing`) with `ItemType = Literal["article", "video"]` and `@dataclass(frozen=True, slots=True, kw_only=True)` classes: `Candidate(url: str, url_hash: str, title: str, published_at: datetime | None, type: ItemType, teaser: str | None, content_hash: str | None)` and `ProcessedItem(id: int, url: str, type: ItemType, title: str, raw_content: str, relevance: float | None = None, summary: str | None = None, error: str | None = None)`; docstrings state they are dependency-free shared records

**Checkpoint**: US5 tests pass; other tracks can import `invio.domain`

---

## Phase 7: User Story 4 — Publish a machine-readable schema (Priority: P3)

**Goal**: `docs/job.schema.json` generated from `JobConfig`, verified current in CI.

**Independent Test**: Regenerated schema equals committed file; reference example validates
against it with `jsonschema`.

### Tests for User Story 4 ⚠️ (write first, must fail)

- [X] T031 [P] [US4] Create tests/test_job_schema.py: (a) `json.loads((REPO_ROOT / "docs/job.schema.json").read_text())` equals `job_json_schema()`, failure message `"docs/job.schema.json is stale; run: uv run python -m invio.config.job"` (FR-029); (b) `jsonschema.validate(yaml.load(example_text, Loader=JobYamlLoader), schema)` passes for `docs/job.example.yaml` (SC-005); (c) schema has `"additionalProperties": false` on `JobConfig` and the source `discriminator`/`oneOf` lists the five `type` values

### Implementation for User Story 4

- [X] T032 [US4] Implement `job_json_schema() -> dict[str, Any]` in src/invio/config/job.py returning `JobConfig.model_json_schema()`; add `def main() -> None` writing `json.dumps(job_json_schema(), indent=2, ensure_ascii=False) + "\n"` to `Path("docs/job.schema.json")` and printing the path, plus `if __name__ == "__main__": main()`
- [X] T033 [US4] Run `uv run python -m invio.config.job` from the repo root to generate docs/job.schema.json and commit it alongside the code

**Checkpoint**: All user stories functional

---

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T034 [P] Add a "Job files" section to README.md: purpose, link to docs/job.example.yaml and docs/job.schema.json, `load_yaml`/`dump_yaml` usage snippet, note that comments are not preserved and every field is written on save, note that `weekday` is accepted in any capitalisation although the schema lists lowercase only, note that cross-field errors may appear only after field errors are fixed, sample `JobConfigError` output, schema regeneration command (constitution: docs updated with user-facing format)
- [X] T035 [P] Add `config/job.py` and `domain.py` lines to the "Layout" block in README.md
- [X] T036 Export public names via `__all__` in src/invio/config/job.py matching contracts/python-api.md (enums, all models, `SourceConfig`, `JobConfigError`, `load_yaml`, `dump_yaml`, `write_yaml`, `job_json_schema`, `SUPPORTED_SCHEMA_VERSION`)
- [X] T037 Run all quality gates: `uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest`; fix every finding
- [X] T038 Execute quickstart.md steps 2–6 and confirm expected outcomes
- [X] T039 Prepare PR description for issue #3 justifying new runtime deps (PyYAML: file format; email-validator: required by `EmailStr`) per constitution, mapping each issue acceptance criterion to its test

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none
- **Foundational (Phase 2)**: after Setup — blocks US1, US2, US3, US4
- **US1 (Phase 3)**: after Foundational
- **US2 (Phase 4)**: after US1 (adds constraints to US1's models in the same file)
- **US3 (Phase 5)**: after US1 (needs models + `load_yaml`); independent of US2
- **US5 (Phase 6)**: after Setup only — fully independent, can run any time
- **US4 (Phase 7)**: after US2 (schema must include final constraints, else T033 regenerates a stale file)
- **Polish (Phase 8)**: after all stories

### Story graph

```text
Setup ─┬─▶ Foundational ─▶ US1 ─┬─▶ US2 ─▶ US4 ─┐
       │                        └─▶ US3 ────────┼─▶ Polish
       └─▶ US5 ─────────────────────────────────┘
```

### Within each story

Tests first (fail) → models → functions → checkpoint. Tasks touching
`src/invio/config/job.py` are sequential (single file).

### Parallel Opportunities

- US5 (T029, T030) in parallel with everything after Setup
- US1 tests T006–T009 together (conftest, test_job_config, test_job_sources, test_job_yaml); T014 (docs example) alongside T010–T013
- US2 tests T017–T022 together (each writes a different test file)
- US3 (T026–T028) in parallel with US2 once US1 is done (different functions; US3 tests live in
  tests/test_job_yaml.py, which US2's T022 also edits — coordinate)
- Polish T034/T035 in parallel

---

## Parallel Example: User Story 1

```bash
Task: "T007 happy-path model tests in tests/test_job_config.py"
Task: "T009 load_yaml + YAML pitfall tests in tests/test_job_yaml.py"
Task: "T014 Create docs/job.example.yaml from contracts/job-file.md"
```

## Parallel Example: After US1

```bash
Task: "US2 — T017..T025 (validation rules + error formatting)"
Task: "US3 — T026..T028 (dump_yaml / write_yaml)"
Task: "US5 — T029..T030 (src/invio/domain.py) — may already be done"
```

---

## Implementation Strategy

### MVP (User Story 1)

1. Setup → Foundational → US1
2. **Validate**: reference example loads; defaults applied
3. Note: MVP alone does not yet reject invalid values — ship together with US2 (both P1)

### Incremental Delivery

1. US1 + US2 → strict, validated loading (both P1; the real minimum for the contract)
2. US5 → unblocks other tracks early (can land first if another track is waiting)
3. US3 → save support
4. US4 → schema + docs
5. Polish → README, gates, PR

### Acceptance criteria → tasks (issue #3)

| Issue acceptance criterion | Tests |
|---|---|
| Valid example config loads | T007, T009 |
| weekly w/o weekday, monthly w/o day_of_month rejected with clear messages | T017, T022 |
| Invalid timezone, e-mail, min_relevance outside 0..1 rejected | T017, T018 |
| YAML → model → YAML round-trip equal | T026 |
| Unknown keys rejected | T020, T022 |
| Unit tests cover each source type | T008, T019 (tests/test_job_sources.py) |
| `domain.py` imports without DB/network deps | T029 |

---

## Notes

- [P] = different files and no dependency on incomplete tasks
- Commit after each phase checkpoint
- Avoid: re-declaring models elsewhere (constitution I), importing `Settings` from `job.py`
  (provider sync is test-only, research R7)
