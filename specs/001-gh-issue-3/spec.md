# Feature Specification: Validated Research Job Configuration

**Feature Branch**: `gh-issue-3`

**Created**: 2026-10-04

**Status**: Draft

**Input**: User description: "GitHub issue #3: [FEAT] Add JobConfig Pydantic models with YAML import/export. A research job is described by schedule, notification, sources, search criteria, LLM selection and limits. The config is the central contract between CLI, scheduler and pipeline, so it must be strictly validated."

## Clarifications

### Session 2026-10-04

- Q: What fields should each source type accept in a job file? → A: Locator per type (`rss`/`web`/`sitemap` → `url`; `youtube_channel` → `channel_id`; `youtube_playlist` → `playlist_id`) plus optional common `name` and `enabled` (default true) on every type.
- Q: Which limits should a job file let you set for a single run, and what should their defaults be? → A: `max_items_per_source` (20), `max_items_per_run` (100), `max_items_in_notification` (20), `max_llm_tokens_per_run` (200000); all positive integers.
- Q: Which LLM providers should a job file accept for `provider` and `fallback_provider`? → A: Exactly the providers configurable in the existing settings module: `mistral`, `openai`, `anthropic`, `google`, `ollama`; kept in sync with settings.
- Q: When a job is saved to a file, should values that just repeat a default be written out or left out? → A: Write all fields, including defaults (complete, explicit file).
- Q: How should the weekday of a weekly schedule be written in a job file? → A: Full English names in any case (`Monday`, `MONDAY`), always saved as lowercase.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Load a valid research job definition (Priority: P1)

An operator writes a research job definition file describing when the job runs, who receives the results, which sources to scan, what to search for, which language models to use and what limits apply. The system reads the file and produces a single, fully validated job description that the CLI, scheduler and research pipeline can all rely on.

**Why this priority**: Every other component (scheduling, fetching, filtering, notification) consumes this job description. Without a trustworthy loaded configuration nothing else can run.

**Independent Test**: Load the reference example job file `docs/job.example.yaml` and confirm it is accepted with all values (including defaults) available to callers.

**Acceptance Scenarios**:

1. **Given** the reference example job file, **When** it is loaded, **Then** it is accepted without errors and every section (schedule, notification, sources, search, LLM, limits) is available.
2. **Given** a job file that omits optional values (e.g. `send_if_empty`, `min_relevance`, `schema_version`), **When** it is loaded, **Then** documented defaults are applied (`false`, `0.6`, `1` respectively).
3. **Given** a job file listing one source of each supported kind (RSS feed, web page, YouTube channel, YouTube playlist, sitemap), **When** it is loaded, **Then** each source is recognised as its specific kind with the fields appropriate to that kind.

---

### User Story 2 - Reject invalid job definitions with clear messages (Priority: P1)

An operator makes a mistake in a job file — a typo in a key, a weekly schedule without a weekday, an unknown timezone, a malformed e-mail address, or a relevance threshold outside the allowed range. The system refuses the file and tells the operator exactly which field is wrong and why, before any job is scheduled or executed.

**Why this priority**: The configuration is the contract between components; silently accepting wrong data would cause failed or misdirected runs (e.g. reports never sent, jobs running at the wrong time). Strict validation is equally critical as loading.

**Independent Test**: Feed a set of deliberately broken job files and verify each is rejected with a message naming the offending field and the rule violated.

**Acceptance Scenarios**:

1. **Given** a schedule with frequency `weekly` and no weekday, **When** loaded, **Then** it is rejected with a message stating that a weekday is required for weekly schedules.
2. **Given** a schedule with frequency `monthly` and no day of month, **When** loaded, **Then** it is rejected with a message stating that a day of month is required for monthly schedules.
3. **Given** a schedule with a timezone name that does not exist (e.g. `Europe/Atlantis`), **When** loaded, **Then** it is rejected naming the timezone field.
4. **Given** a notification recipient that is not a valid e-mail address, **When** loaded, **Then** it is rejected naming the recipient field.
5. **Given** a notification with an empty recipient list, **When** loaded, **Then** it is rejected because at least one recipient is required.
6. **Given** a minimum relevance of `1.5` or `-0.1`, **When** loaded, **Then** it is rejected because the value must lie between 0 and 1 inclusive.
7. **Given** any section containing an unknown key (e.g. a misspelt `frequncy`), **When** loaded, **Then** it is rejected naming the unknown key.
8. **Given** a source whose kind is not one of the supported kinds, **When** loaded, **Then** it is rejected listing the supported kinds.
9. **Given** a time of day not in `HH:MM` 24-hour form (e.g. `25:00`, `7:5`), **When** loaded, **Then** it is rejected naming the time field.
10. **Given** an empty semantic description, **When** loaded, **Then** it is rejected because a non-empty description is required.

---

### User Story 3 - Save a job definition back to a file (Priority: P2)

A tool (e.g. a future CLI command that creates or edits jobs) holds a job description in memory and writes it back to a human-readable job file. Reading that file again yields exactly the same job description.

**Why this priority**: Needed for job creation/editing commands, but loading and validation deliver value on their own first.

**Independent Test**: Load a job file, save it, load the saved file again and compare the two job descriptions for equality.

**Acceptance Scenarios**:

1. **Given** a valid loaded job description, **When** it is saved and re-loaded, **Then** the re-loaded description is equal to the original.
2. **Given** a job description, **When** it is saved, **Then** keys appear in the same order as the fields are defined in the job structure, so diffs stay readable.
3. **Given** a job file containing comments, **When** it is loaded and saved, **Then** comments are not preserved (documented, expected behaviour).
4. **Given** a job file that omits defaulted values (e.g. `send_if_empty`, `limits`), **When** it is loaded and saved, **Then** the saved file contains those fields with their default values written out explicitly.

---

### User Story 4 - Publish a machine-readable schema for job files (Priority: P3)

An operator editing job files in an editor, or another tool producing them, can use a published machine-readable schema describing every allowed field, its type, constraints and defaults, to get autocompletion and early validation.

**Why this priority**: Improves authoring experience and documents the contract, but is not required for jobs to run.

**Independent Test**: Generate the schema document and validate the reference example job file against it with an independent schema validator.

**Acceptance Scenarios**:

1. **Given** the job structure, **When** the schema is exported, **Then** a schema document is written to `docs/job.schema.json`.
2. **Given** the exported schema, **When** the reference example job file is checked against it, **Then** it passes.
3. **Given** the job structure changes, **When** the schema is regenerated, **Then** the committed schema document can be verified as up to date (a stale schema is detectable).

---

### User Story 5 - Shared research item records (Priority: P2)

Developers working on parallel tracks (source fetching, relevance filtering, summarisation, notification) need a common, lightweight description of a discovered candidate item and of a processed item, so their components exchange data in the same shape without depending on the database or network layers.

**Why this priority**: Unblocks parallel work on other tracks; small in scope but a hard dependency for them.

**Independent Test**: Import the shared record definitions in an environment with no database or network access and construct one of each record.

**Acceptance Scenarios**:

1. **Given** the shared records module, **When** it is imported, **Then** it loads without pulling in database, network or language-model dependencies.
2. **Given** the fields URL, URL hash, title, publication time, item type, teaser and content hash, **When** a candidate record is created, **Then** all fields are accessible.
3. **Given** the fields id, URL, item type, title, raw content, relevance, summary and error, **When** a processed item record is created, **Then** all fields are accessible, and relevance, summary and error may be absent.

---

### Edge Cases

- `daily` schedule that also specifies a weekday or day of month: rejected as contradictory, so operators are not misled into thinking it will be honoured.
- `weekly` schedule with a day of month, or `monthly` schedule with a weekday: rejected for the same reason.
- `monthly` schedule with day of month 29–31: accepted; behaviour in shorter months is a scheduler concern (out of scope here).
- Day of month `0` or `32`: rejected.
- Any limit set to `0`, a negative number, or a non-integer: rejected naming the limit.
- Boolean where a number is expected (`max_items_per_run: true`, `min_relevance: false`) or number where a boolean is expected (`enabled: 1`, `send_if_empty: 0`): rejected naming the field (FR-002a).
- `max_items_in_notification` larger than `max_items_per_run`, or `max_items_per_source` larger than `max_items_per_run`: accepted (the smaller effective cap wins at run time; not a configuration error).
- Weekday given in an unsupported form (e.g. `Funday`, abbreviations like `mon`, or numbers like `1`): rejected listing allowed values.
- Weekday written as `Monday` or `MONDAY`: accepted and saved as `monday` (a round-trip of the normalised value is exact).
- Duplicate recipients in the notification list: accepted as written (no deduplication at configuration level).
- Empty source list: rejected — a job without sources cannot do anything.
- Sources all set to `enabled: false`: accepted at configuration level (temporarily disabling a job's sources is legitimate); the run simply finds nothing.
- `rss`/`web`/`sitemap` source with a non-http(s) or relative `url`: rejected naming the source's position (e.g. `sources[2].url`).
- YouTube source given a `url` instead of `channel_id`/`playlist_id`: rejected as an unknown key for that kind.
- Keyword lists (`any`, `all`, `exclude`) all empty: accepted; relevance is then driven by the semantic description alone.
- Fallback LLM provider identical to the primary provider: rejected as a misconfiguration.
- Provider name in a different case (e.g. `OpenAI`) or unknown (e.g. `cohere`): rejected listing the allowed providers.
- Provider whose API key is not set in the environment: accepted at load time; credentials are checked only when the provider is used (consistent with settings behaviour).
- Job file that is empty, not a mapping at the top level, or not syntactically valid: rejected with a message identifying the file.
- Job file path that does not exist or cannot be read: rejected with a message naming the path.
- `schema_version` higher than the version supported by the running software: rejected with a message stating the supported version.
- Non-ASCII text (e.g. German umlauts in subject or keywords): loaded and saved without corruption.

## Requirements *(mandatory)*

### Functional Requirements

**Job structure**

- **FR-001**: The system MUST describe a research job as one structure composed of: schedule, notification, sources, search criteria, LLM selection, limits, and a schema version.
- **FR-002**: Every section of the job structure MUST reject unknown keys, naming the unknown key in the error.
- **FR-002a**: Numeric and boolean fields MUST NOT silently convert values of another type: `true`/`false` is rejected for numbers, `1`/`0` is rejected for booleans, and a fractional number is rejected for integers. Whole numbers are accepted for decimal fields (e.g. `min_relevance: 1`).
- **FR-003**: The job structure MUST carry a `schema_version` integer defaulting to `1`; versions newer than the supported version MUST be rejected.

**Schedule**

- **FR-004**: Schedule MUST support frequencies `daily`, `weekly` and `monthly`.
- **FR-005**: Schedule MUST require a time of day in 24-hour `HH:MM` format (00:00–23:59).
- **FR-006**: Schedule MUST require a weekday if and only if the frequency is `weekly`. The weekday is a full English day name (`monday` … `sunday`), accepted case-insensitively and normalised to lowercase; it is always saved in lowercase.
- **FR-007**: Schedule MUST require a day of month between 1 and 31 if and only if the frequency is `monthly`.
- **FR-008**: Schedule MUST require a timezone that is a valid IANA timezone name known to the running system.
- **FR-009**: Violations of FR-006 and FR-007 MUST produce messages that state the rule in plain language (e.g. "weekday is required when frequency is 'weekly'").

**Notification**

- **FR-010**: Notification MUST require at least one recipient, and each recipient MUST be a syntactically valid e-mail address.
- **FR-011**: Notification MUST hold a subject template (text that may contain placeholders filled at send time).
- **FR-012**: Notification MUST hold a `send_if_empty` flag defaulting to `false`.

**Sources**

- **FR-013**: The system MUST support exactly these source kinds, distinguished by a `type` field: `rss`, `web`, `youtube_channel`, `youtube_playlist`, `sitemap`.
- **FR-014**: Each source kind MUST validate only the fields relevant to that kind and reject fields belonging to other kinds.
- **FR-014a**: Source fields per kind: `rss`, `web`, `sitemap` require `url` (absolute http/https URL); `youtube_channel` requires `channel_id`; `youtube_playlist` requires `playlist_id`. Every kind additionally accepts optional `name` (display label) and `enabled` (boolean, default `true`). No other fields are allowed.
- **FR-015**: A job MUST contain at least one source.

**Search**

- **FR-016**: Search criteria MUST support keyword lists `any`, `all` and `exclude`, each defaulting to empty.
- **FR-017**: Search criteria MUST require a non-empty (non-whitespace) semantic description of what the operator is looking for.
- **FR-018**: Search criteria MUST hold a minimum relevance between 0 and 1 inclusive, defaulting to 0.6.

**LLM selection**

- **FR-019**: LLM selection MUST require a provider chosen from the providers configurable in application settings: `mistral`, `openai`, `anthropic`, `google`, `ollama`. The list MUST stay in sync with settings: adding or removing a provider in one place without the other MUST be caught by an automated test.
- **FR-020**: LLM selection MUST require a "fast" model name and a "smart" model name.
- **FR-021**: LLM selection MAY name a fallback provider from the same supported list, which MUST differ from the primary provider.

**Limits**

- **FR-022**: Limits MUST hold these caps for a single run, each a positive integer (≥ 1) with the given default: `max_items_per_source` (20), `max_items_per_run` (100), `max_items_in_notification` (20), `max_llm_tokens_per_run` (200000). The whole limits section is optional; when omitted all defaults apply.

**File import/export**

- **FR-023**: The system MUST load a job structure from a YAML job file given its path, applying all validation rules above.
- **FR-024**: The system MUST save a job structure to YAML, emitting keys in field-definition order and preserving non-ASCII text. Every field MUST be written, including those equal to their default and optional fields that are unset (written as null), so the saved file is complete and later default changes do not alter saved jobs.
- **FR-025**: Loading a saved job MUST yield a job structure equal to the one that was saved (lossless round-trip).
- **FR-026**: Comments in job files are NOT preserved on save; this MUST be documented.
- **FR-027**: Load errors MUST identify the file and, for validation failures, each offending field by its path within the file (e.g. `sources[2].url`). Errors that concern a combination of fields (cross-field rules such as FR-006/FR-007/FR-021) are reported at the enclosing section and name the field in the message (e.g. `schedule: weekday is required when frequency is 'weekly'`). All field-level problems are reported together in one pass; cross-field rules of a section are checked once that section's individual fields are valid, so they may surface on a second load attempt.

**Schema**

- **FR-028**: The system MUST export a machine-readable JSON Schema of the job structure to `docs/job.schema.json`, generated from the job structure definition itself. The schema describes the canonical saved form; where the loader is more lenient than the schema (weekday capitalisation, FR-006) this MUST be documented alongside the schema.
- **FR-029**: It MUST be possible to detect automatically that the committed schema document is out of date.

**Shared records**

- **FR-030**: The system MUST provide a shared, dependency-free definition of a **Candidate** item with: URL, URL hash, title, publication time, item type, teaser, content hash.
- **FR-031**: The system MUST provide a shared, dependency-free definition of a **ProcessedItem** with: id, URL, item type, title, raw content, relevance, summary, error.
- **FR-032**: Importing the shared records MUST NOT require database, network or language-model components.

**Testing**

- **FR-033**: Automated tests MUST cover each acceptance criterion of issue #3, including at least one valid and one invalid case per source kind.

### Key Entities

- **JobConfig**: The complete definition of one research job; owns all sections below plus `schema_version`.
- **ScheduleConfig**: When the job runs — frequency, time of day, weekday (weekly), day of month (monthly), timezone.
- **NotificationConfig**: Who receives results and how — recipients, subject template, whether to send when nothing relevant was found.
- **SourceConfig**: One place to scan; a variant per kind (RSS feed, web page, YouTube channel, YouTube playlist, sitemap), identified by `type`, with one locator (`url`, `channel_id` or `playlist_id`) plus optional `name` and `enabled`.
- **SearchConfig**: What counts as relevant — keyword lists (any/all/exclude), semantic description, minimum relevance score.
- **LLMConfig**: Which language-model provider and models to use — provider, fast model, smart model, optional fallback provider.
- **LimitsConfig**: Caps on the work a single run may perform — items per source, items per run, items per notification, LLM tokens per run.
- **Candidate**: A discovered item not yet processed, identified by URL/URL hash, with title, publication time, type, teaser and content hash.
- **ProcessedItem**: An item after processing, with raw content, relevance score, summary, and an error description if processing failed.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The reference example job file `docs/job.example.yaml` loads successfully on first attempt.
- **SC-002**: 100% of the invalid-configuration cases listed in User Story 2 and Edge Cases are rejected, and each message names the offending field.
- **SC-003**: For every valid job file in the test suite, load → save → load produces an equal job description (100% round-trip fidelity).
- **SC-004**: All five source kinds have at least one passing "valid" and one passing "rejected" test.
- **SC-005**: The reference example job file validates against the exported schema using an independent schema validator.
- **SC-006**: The shared records can be imported in an isolated environment with no database or network available, with zero errors.
- **SC-007**: An operator can identify and fix a single configuration mistake from the error message alone, without reading source code.

## Assumptions

- The code base package is `invio` (the issue's `scout/` paths map to `src/invio/config/job.py` and `src/invio/domain.py`).
- Issue #1 (project skeleton) is complete; YAML and e-mail-validation libraries may be added as dependencies.
- The "architecture doc" with the reference example job is not in the repository; the reference example is authored as `docs/job.example.yaml` (all sections, all five source kinds) and is used by the tests and documentation.
- The subject template's placeholder syntax is not validated beyond being non-empty text; rendering is the notifier's concern.
- Only `schema_version: 1` exists; migration between versions is out of scope.
- Scheduling execution, fetching, notification sending and job storage are out of scope; this feature only defines and validates the contract.
- Shared records are plain data holders without behaviour or validation.
