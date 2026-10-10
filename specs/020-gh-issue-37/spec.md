# Feature Specification: Job Search Suggestion Assistant

**Feature Branch**: `gh-issue-37`

**Created**: 2026-10-09

**Status**: Draft

**Input**: User description: "GitHub issue #37: Add `scout job suggest` LLM assistant for keywords and semantic description. Writing good keywords and a precise semantic description is the hardest part of creating a job. An assistant drafts them from a short free-text topic, and the user reviews and edits them. Requirements: (1) Define `SearchSuggestion(keywords_any, keywords_all, keywords_exclude, semantic_description, suggested_sources_hint)`. (2) Add `scout job suggest \"<topic>\" [--language de] [--provider NAME]` using the `smart` model and structured output. (3) Prompt requires: include synonyms and English/German variants, specific exclusions for typical noise, and a semantic description that states what is relevant and what is not. (4) Show the suggestion in an editable preview; on confirmation either write a new job (hands over to the wizard from #9 with prefilled search fields) or print YAML. (5) Allow iterative refinement: \"make it narrower\" re-calls the model with the previous suggestion and the user's remark. Acceptance criteria: suggestion contains all four search fields and validates against `SearchConfig`; refinement round uses the previous suggestion as context (verified with `FakeProvider`); the wizard accepts a prefilled suggestion and the user can edit every field; no job is saved without explicit confirmation. Depends on #8 and #9. Labels: feature, m6-comfort, cli, llm, track:cli-sched, round-3. Effort M. Branch: issue/37-add-scout-job-suggest-llm."

## Clarifications

### Session 2026-10-09

- Q: When `invio job suggest` runs before any job exists, how are the provider and its capable ("smart") model picked? → A: Provider: `--provider`, else the first registered provider (alphabetical) with credentials configured. Model: `--model`, else the most expensive registered model of that provider (input + output price per million tokens; ties by model id).
- Q: What happens with the suggestion's sources hint when a job is created from it? → A: It stays free text: shown in the preview and again as a note when the wizard reaches its sources step; no sources are prefilled.
- Q: How is the job's language set when a job is created from a suggestion, given that the wizard had no language step? → A: The wizard gains a language step (ISO 639-1, validated) for all job creation; `--language` prefills it, otherwise it defaults to `en`.
- Q: How does the operator edit a suggestion in the preview? → A: Per-field prompts: pick a field (any / all / exclude keywords or description) and edit its current value in a text prompt (multi-line for the description), validated immediately; no external editor.

## User Scenarios & Testing *(mandatory)*

The user of this feature is the operator who maintains research jobs from the terminal. Creating a job is easy except for its search part: the operator knows the topic ("heat pumps for apartment buildings") but struggles to write a good set of keywords, the exclusions that keep out noise, and a precise description of what is and is not relevant. Following the project decision taken for #9, the command lives under the project's `invio` CLI (`invio job suggest "<topic>"`); "scout" is a legacy working name.

### User Story 1 - Get a drafted search definition for a topic (Priority: P1)

The operator runs `invio job suggest "heat pumps for apartment buildings"`. The assistant asks the configured language model (capable tier) for a search definition and shows a readable preview containing: keywords of which any must match, keywords of which all must match, keywords to exclude, a semantic description, and a hint about which kinds of sources are likely to cover the topic. The keyword lists contain synonyms and both English and German variants; the exclusions name concrete, typical noise for the topic; the semantic description states explicitly what is relevant and what is not.

**Why this priority**: Producing a good, valid draft is the core value of the feature; every other story builds on it.

**Independent Test**: With a fake language model returning a prepared answer, run the command non-interactively for a topic and verify that the printed result contains all four search fields, is accepted by the job's search configuration rules, and that the request sent to the model contains the topic and the content requirements (synonyms, English/German variants, noise exclusions, relevant/not-relevant description).

**Acceptance Scenarios**:

1. **Given** a configured language model provider, **When** the operator runs `invio job suggest "<topic>"`, **Then** a suggestion is shown with `keywords.any`, `keywords.all`, `keywords.exclude`, `semantic_description` and a sources hint.
2. **Given** a suggestion returned by the model, **When** it is shown, **Then** its keyword lists and semantic description are accepted by the job's search configuration rules without modification (e.g. no empty keywords, non-empty description).
3. **Given** the model returns an answer that does not match the expected structure or fails the search configuration rules, **When** the assistant processes it, **Then** it attempts one repair round with the model and, if that still fails, exits non-zero with an error that names what was invalid; nothing is written.
4. **Given** `--language de`, **When** the suggestion is requested, **Then** the semantic description is written in German, the keyword lists still contain English and German variants, and a job created from it is prefilled with language `de`.
5. **Given** `--provider NAME` (optionally with `--model ID`), **When** the suggestion is requested, **Then** the named provider is used with the given model or, without `--model`, with that provider's most expensive registered model; an unknown provider name, a provider without credentials or a model not registered for the provider results in a configuration error (exit code 2) before any model call.
6. **Given** no `--provider` and credentials configured for several providers, **When** the suggestion is requested, **Then** the alphabetically first of those providers that has at least one registered model is used with its most expensive registered model, and the preview names the provider and model used.
7. **Given** an empty or whitespace-only topic, **When** the command is run, **Then** it fails with a usage error and no model call is made.

---

### User Story 2 - Refine the suggestion iteratively (Priority: P2)

After seeing the preview, the operator is not yet happy ("make it narrower", "drop everything about new buildings"). They choose to refine and type a remark. The assistant asks the model again, sending the original topic, the previous suggestion (including any edits the operator made to it) and the remark, and shows the new suggestion. This can be repeated until the operator is satisfied.

**Why this priority**: First drafts are rarely perfect; refinement turns the assistant from a one-shot generator into a usable tool, and it is an explicit acceptance criterion.

**Independent Test**: With a fake language model scripted for two answers, run a session that refines once with the remark "make it narrower" and verify that the second request contains the topic, the complete first suggestion and the remark, and that the second answer is the one shown afterwards.

**Acceptance Scenarios**:

1. **Given** a shown suggestion, **When** the operator chooses "refine" and enters "make it narrower", **Then** the model is called again with the topic, the full previous suggestion and the remark, and the new suggestion replaces the old one in the preview.
2. **Given** the operator edited a field of the suggestion before refining, **When** they refine, **Then** the edited version (not the original model answer) is sent as the previous suggestion.
3. **Given** several refinement rounds, **When** the operator refines again, **Then** the most recent suggestion is used as context.
4. **Given** a refinement call that fails (provider error or invalid answer after repair), **When** the failure occurs, **Then** an error is shown and the previous suggestion remains available so the operator can retry, edit or accept it.
5. **Given** an empty refinement remark, **When** the operator submits it, **Then** no model call is made and the operator is asked again.

---

### User Story 3 - Edit the suggestion and turn it into a job (Priority: P2)

From the preview the operator can edit any field of the suggestion directly (each keyword list and the semantic description). When satisfied, they choose to create a job: the job creation wizard from #9 starts with the search fields (any/all/exclude keywords and semantic description) and the language prefilled from the suggestion. The operator walks through all wizard steps as usual, can change every prefilled field, and the job is only saved after they confirm the wizard's final preview.

**Why this priority**: Saving the suggestion as a job is the main end goal, but it reuses the existing wizard; the acceptance criteria require that prefilled values stay editable and that nothing is saved without confirmation.

**Independent Test**: With a scripted prompter and a fake language model, accept a suggestion, choose "create job", change one prefilled keyword list and the description in the wizard, and confirm; verify the saved job contains the operator's edited values. Repeat but decline the wizard's final confirmation (and separately abort with Ctrl+C) and verify no job is saved.

**Acceptance Scenarios**:

1. **Given** a shown suggestion, **When** the operator edits the "any" keywords, "all" keywords, "exclude" keywords or the semantic description in the preview, **Then** the preview shows the edited value, and edits that would violate the search configuration rules (e.g. empty description) are rejected with a message and the field is asked again.
2. **Given** an accepted suggestion, **When** the operator chooses "create job", **Then** the wizard starts with the four search fields and the language prefilled as defaults, and asks for all remaining job fields (name, language, schedule, notification, sources, model settings, limits) as it normally does; the language question defaults to the `--language` value.
3. **Given** the wizard reaches its sources step after "create job", **When** the step starts, **Then** the suggestion's sources hint is shown as a note and the source list starts empty.
4. **Given** the wizard runs with prefilled values, **When** the operator reaches a prefilled field, **Then** they can keep it, change it, or clear it by deleting the prefilled text (subject to the usual validation: keyword lists may become empty, the description may not).
5. **Given** the wizard reaches its final preview, **When** the operator declines saving or aborts (Ctrl+C / end of input) at any point before it, **Then** no job is created and the command reports that nothing was saved.
6. **Given** the operator chooses "create job", **When** the wizard asks for the job's model settings, **Then** the provider and model used for the suggestion are offered as the defaults for the provider and the smart model.
7. **Given** the operator chooses "discard" in the preview, **When** the command ends, **Then** nothing is saved and nothing is printed besides a short notice.

---

### User Story 4 - Print the suggestion as YAML (Priority: P3)

Instead of creating a job, the operator chooses "print YAML" in the preview, or runs the command non-interactively (e.g. in a pipe or with `--yaml`). The assistant prints the search section in the job file format so it can be pasted into an existing or new job file. The sources hint is printed as a YAML comment, because it is not part of a job's search configuration.

**Why this priority**: Useful for operators who edit job files by hand or want to update an existing job, but not required to get value from the feature.

**Independent Test**: Run the command with a fake model and output redirected (no terminal); verify that standard output contains only a YAML `search:` block that loads and validates as a job search configuration, with the sources hint as a comment.

**Acceptance Scenarios**:

1. **Given** a shown suggestion, **When** the operator chooses "print YAML", **Then** a `search:` block with `keywords.any`, `keywords.all`, `keywords.exclude` and `semantic_description` is printed to standard output and the sources hint follows as comment lines.
2. **Given** no interactive terminal (or `--yaml`), **When** the command runs, **Then** it requests a single suggestion, prints the YAML and exits with code 0 without asking any questions and without saving anything.
3. **Given** the printed YAML, **When** it is pasted under a job's root, **Then** the job validates (assuming the rest of the job is valid).

---

### Edge Cases

- The model returns duplicate keywords, keywords differing only by case or surrounding whitespace, or a keyword that appears both in an include list and in `exclude`: duplicates are removed (first occurrence kept), whitespace is trimmed, and include/exclude conflicts are dropped from `exclude` with a warning in the preview.
- The model returns an empty `keywords.all` list: this is valid (no mandatory terms) and shown as "(none)".
- The model returns an empty `keywords.any` list or empty `exclude` list: allowed by the job rules but shown with a warning, because the prompt requires synonyms and noise exclusions.
- Very long answers: each keyword list is capped at 30 entries and the semantic description at 2,000 characters; longer content is treated as an invalid answer and goes through the repair round.
- The provider is unreachable, rate-limited or times out: the existing retry behaviour of the provider layer applies; on final failure the command exits non-zero with a message of the form `Error: <provider>/<model>: <error type>: <message>`, without showing credentials.
- The topic is very long (> 500 characters): rejected with a usage error to keep prompts bounded.
- The topic or remark contains text that tries to instruct the model ("ignore previous instructions"): it is passed to the model only as user data inside the prompt; the answer is still validated like any other answer, so it cannot cause anything other than an invalid or odd suggestion.
- `--language` with an unknown code: rejected with a usage error listing that a valid ISO 639-1 code is required.
- The operator aborts with Ctrl+C or end of input in the preview, in a refinement prompt or in the wizard: the command stops, reports that nothing was saved and exits non-zero (as an aborted wizard does today).
- A job name chosen in the wizard already exists: handled by the wizard's existing name validation.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST define a search suggestion record with exactly these parts: keywords of which any must match, keywords of which all must match, keywords to exclude, a semantic description, and a free-text hint on suggested source kinds.
- **FR-002**: The search parts of a suggestion (the three keyword lists and the semantic description) MUST be convertible into the job's search configuration and MUST be validated against the job's search configuration rules before they are shown.
- **FR-003**: The system MUST provide the command `invio job suggest "<topic>"` with options `--language CODE` (ISO 639-1, default `en`), `--provider NAME` and `--model ID`.
- **FR-003a**: Without `--provider`, the command MUST use the alphabetically first registered provider that has credentials configured and at least one registered model; if none has credentials, it MUST exit with a configuration error (code 2) naming the missing settings. Without `--model`, it MUST use the selected provider's most expensive registered model (input + output price per million tokens; ties broken by model id) as the capable ("smart") model. The provider and model used MUST be shown in the preview.
- **FR-004**: The command MUST request the suggestion from the capable ("smart") model selected per FR-003a and MUST request a structured answer that is parsed into the suggestion record; an answer that cannot be parsed or validated MUST trigger at most one repair attempt and then fail with a clear error.
- **FR-005**: The instructions sent to the model MUST require: synonyms and English and German variants of the key terms in the keyword lists; specific exclusions for noise typical of the topic; a semantic description that states both what is relevant and what is not; the semantic description written in the requested language.
- **FR-006**: The topic and refinement remarks MUST be passed to the model as user data, clearly separated from the instructions.
- **FR-007**: In an interactive terminal, the command MUST show the suggestion in a preview and offer the actions: edit a field, refine, create job, print YAML, discard.
- **FR-008**: Editing MUST be possible for each of the four search fields individually: the operator picks a field and edits its current value in a text prompt (comma-separated for keyword lists, multi-line for the description); no external editor is opened. Edited values MUST be re-validated against the search configuration rules, and invalid input MUST be rejected with a message and asked again.
- **FR-009**: Refinement MUST call the model again with the topic, the complete current suggestion (including the operator's edits) and the operator's remark, and MUST replace the shown suggestion with the new answer; refinement MUST be repeatable without a fixed limit.
- **FR-010**: A failed model call during refinement MUST NOT discard the current suggestion.
- **FR-011**: "Create job" MUST start the existing job creation wizard with the any/all/exclude keywords, the semantic description and the language prefilled as editable defaults, and with the suggestion's provider and model as the defaults for the job's provider and smart model; every wizard field MUST remain editable. The sources hint MUST be shown as a note at the start of the wizard's sources step and MUST NOT prefill any source.
- **FR-011a**: The job creation wizard MUST ask for the job's language (ISO 639-1 code, validated against the same rules as the job file) in every job creation, defaulting to the prefilled value when started from a suggestion and to `en` otherwise; an invalid code MUST be rejected with a message and asked again.
- **FR-012**: A job MUST only be saved when the operator explicitly confirms the wizard's final preview; declining, discarding or aborting at any point MUST leave the job store unchanged.
- **FR-013**: "Print YAML" and non-interactive use (no terminal, or `--yaml`) MUST print a `search:` block in the job file format to standard output, with the sources hint as YAML comments, and MUST NOT save anything.
- **FR-014**: Keyword lists in a suggestion MUST be normalised before display: whitespace trimmed, empty entries removed, case-insensitive duplicates removed, and entries present in both an include list and `exclude` removed from `exclude` with a warning.
- **FR-015**: Usage errors (empty or over-long topic, unknown language code) MUST be reported before any model call; provider configuration errors (unknown provider, missing credentials) MUST exit with code 2; other failures MUST exit non-zero.
- **FR-016**: Each model call MUST be logged like other model calls in the system (provider, model, usage), and credentials MUST NOT appear in output or logs.
- **FR-017**: The command MUST be covered by automated tests that use the fake language model provider and a scripted prompter, with no network access.
- **FR-018**: The user-facing documentation of the CLI MUST describe the new command, its options and the interactive actions.

### Key Entities

- **Search Suggestion**: The assistant's draft for a job's search part — any-keywords, all-keywords, exclude-keywords, semantic description, and a sources hint. The first four map one-to-one onto the job's search configuration; the sources hint is informational only.
- **Suggestion Request**: The topic, the target language and, for refinement rounds, the previous suggestion and the operator's remark.
- **Job Search Configuration** (existing): The validated search part of a job (keyword lists, semantic description, minimum relevance). A suggestion never sets the minimum relevance; the job's default applies.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of suggestions shown to the operator contain all four search fields and pass the job's search configuration rules.
- **SC-002**: An operator can go from a one-line topic to a saved, valid job in a single command session, without opening or editing a job file by hand.
- **SC-003**: In every refinement round, the request to the model contains the topic, the full current suggestion and the operator's remark (verified by automated tests for one and for several rounds).
- **SC-004**: In 100% of tested exit paths that do not end with an explicit confirmation (discard, decline in wizard, abort in preview/refinement/wizard, non-interactive run, model failure), the job store is unchanged.
- **SC-005**: Every prefilled field in the wizard can be changed by the operator, verified by a test that changes each of the four search fields and the language.
- **SC-006**: A suggestion (excluding model response time) is displayed within 1 second after the model answers.

## Assumptions

- The command is named `invio job suggest`; "scout" in the issue is the project's legacy working name (same decision as for #9 and #36).
- The registry has no per-provider "default capable model"; price is used as the capability proxy (see Clarifications and FR-003a). When "create job" is chosen, the suggestion's provider and model are offered as the wizard's defaults for the provider and the smart model.
- `--language` defaults to `en`. It controls the language of the semantic description and is prefilled in the wizard's new language step (FR-011a), where it can be changed; keyword lists always contain English and German variants regardless of it.
- The sources hint is free text shown in the preview, as a note in the wizard's sources step and as a YAML comment; it is never turned into job sources. Finding concrete feeds is left to the operator (e.g. with `invio source discover`, #36).
- The minimum relevance threshold is not suggested; the job default is used and stays editable through the usual job editing paths.
- The existing wizard's prompts (with defaults) are the edit mechanism for prefilled fields, and the preview's per-field editing uses the same kind of prompts (see FR-008).
- The repair round and retry behaviour for model answers reuse the existing provider layer behaviour (#8).
- Model cost of a suggestion is small (a few calls per session) and is not counted against any job's token budget, since no job exists yet.
