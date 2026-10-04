# Feature Specification: CLI Job Management with Interactive Creation Wizard

**Feature Branch**: `gh-issue-9`

**Created**: 2026-10-04

**Status**: Draft

**Input**: User description: "GitHub issue #9 (gh-issue-9): Add CLI job management with interactive creation wizard. Context: Jobs are created and maintained from the terminal. The wizard asks the four essentials (interval, mail address, URLs, search terms and semantic description) and validates inputs immediately. Depends on #5 (job config/schema) and #6 (next run time computation). Requirements: 1. Add Typer sub-app `scout job` with commands: create, list, show <name>, edit <name>, enable, disable, delete, export <name>, import <file>. 2. `create` uses questionary: name → frequency (+ weekday/day/time/timezone) → recipients → sources (loop: type + URL) → keywords any/all/exclude → semantic description (multi-line) → provider and models → limits. 3. Live validation: e-mail format, URL syntax and reachability (HEAD/GET with timeout, warn but allow override), feed detection for `rss` sources. 4. Show a final YAML preview and ask for confirmation before saving. 5. `create --from-file job.yaml` for non-interactive use. 6. `edit` opens the YAML in $EDITOR, re-validates on save and loops on errors. 7. `list` prints a table: name, enabled, frequency, next run, last status. 8. `delete` requires confirmation or `--yes`. Acceptance criteria: a job can be created interactively and appears in `scout job list`; invalid input is rejected inline without aborting the wizard; `create --from-file` works without a TTY; `edit` rejects invalid YAML and offers to retry or abort; CLI commands are covered by Typer CliRunner tests."

## Clarifications

### Session 2026-10-04

- Q: Should the job commands live under the project's existing `invio` CLI (`invio job …`), even though the issue says `scout job`? → A: Yes — `invio job …` only; "scout" is a legacy working name and no `scout` entry point is added.
- Q: When the wizard asks for the fast and smart LLM model names, should it only accept models listed in the project's model registry? → A: Offer the registry's models for the selected provider as choices; a model that isn't listed may be typed in after a warning and explicit confirmation.
- Q: Should the wizard ask for each of the four per-run limits one by one, or first ask "keep the default limits?"? → A: Ask "keep the default limits?" first; the four values are asked individually (pre-filled with defaults) only if the operator answers no.
- Q: How should the job commands treat a stored job whose configuration no longer validates? → A: `list` shows it with an "invalid config" status; `show` prints the stored errors; `edit` opens the stored content, shows the errors and allows repairing it; `disable`/`delete` always work; `enable` refuses until it is fixed. The job service is extended to report such jobs instead of silently skipping them.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Create a job interactively with the wizard (Priority: P1)

An operator wants to set up a new research job without writing a job file by hand. They start the job creation wizard, which asks step by step for: a job name; how often it runs (daily, weekly or monthly) with the weekday or day of month, the time of day and the time zone; one or more recipient e-mail addresses; one or more sources (choosing a source type and entering its address); keyword filters (any / all / exclude); a multi-line semantic description of what they are looking for; the LLM provider and its fast and smart models; and the per-run limits. Every answer is checked as soon as it is entered. At the end the wizard shows the complete job as it will be saved and asks for confirmation; only after confirmation is the job stored.

**Why this priority**: Creating jobs is the entry point to the whole product; without it nothing can be scheduled. The wizard is the main usability promise of the issue.

**Independent Test**: Run the wizard with scripted answers against an empty job store, confirm the preview, then list jobs and see the new job with its frequency and next run time.

**Acceptance Scenarios**:

1. **Given** an empty job store, **When** the operator completes all wizard steps with valid answers and confirms the preview, **Then** the job is saved, a success message names the job, and it appears in the job list with its next run time.
2. **Given** the wizard is asking for a recipient, **When** the operator enters `not-an-email`, **Then** an inline error explains the problem and the same question is asked again; the previously entered answers are kept and the wizard does not exit.
3. **Given** the wizard is asking for a job name, **When** the operator enters a name that already exists or violates the naming rules, **Then** the name is rejected inline with the reason and the question is repeated.
4. **Given** the operator chose frequency "weekly", **When** the schedule step continues, **Then** the wizard asks for a weekday (and not a day of month); for "monthly" it asks for a day of month 1–31 (and not a weekday); for "daily" it asks for neither.
5. **Given** the wizard is asking for a time or time zone, **When** the operator enters `25:00` or `Mars/Olympus`, **Then** the value is rejected inline and the question is repeated.
6. **Given** the operator reaches the preview, **When** they decline confirmation, **Then** nothing is saved and the command reports that the job was not created.
7. **Given** the operator presses Ctrl+C at any step, **When** the wizard aborts, **Then** nothing is saved and the command exits with a non-zero code without a stack trace.

---

### User Story 2 - Source checks during the wizard (Priority: P1)

While adding sources, the operator wants early feedback that a URL is well-formed, actually reachable, and — for RSS sources — really a feed, so that a typo does not silently produce empty digests later. Sources are added in a loop: the operator picks a type, enters the address, sees the result of the checks, and is asked whether to add another source.

**Why this priority**: Live validation is a core part of the issue's acceptance criteria ("invalid input is rejected inline"); unreachable or wrong sources are the most likely cause of failed unattended runs.

**Independent Test**: Run the wizard's source step with a malformed URL, an unreachable URL and a non-feed URL for an `rss` source (all served by local fakes), and observe rejection, warning-with-override and feed warning respectively.

**Acceptance Scenarios**:

1. **Given** the source step, **When** the operator enters a syntactically invalid URL (e.g. `htp:/example`), **Then** it is rejected inline and the question is repeated.
2. **Given** a syntactically valid URL that cannot be reached within the timeout or returns an error status, **When** the check finishes, **Then** the operator sees a warning naming the problem and is asked whether to keep the source anyway; answering yes keeps it, answering no re-asks the URL.
3. **Given** source type `rss` and a reachable URL whose content is not an RSS/Atom feed, **When** the check finishes, **Then** the operator is warned that no feed was detected and may keep the source anyway or re-enter it.
4. **Given** source type `youtube_channel` or `youtube_playlist`, **When** the operator is asked for the source, **Then** they are asked for a channel ID or playlist ID instead of a URL, and no reachability check is required.
5. **Given** at least one source has been added, **When** the operator answers "no" to "add another source?", **Then** the wizard moves on; the wizard does not allow finishing the source step with zero sources.

---

### User Story 3 - List and inspect jobs (Priority: P1)

The operator wants a quick overview of all jobs and the details of a single job.

**Why this priority**: Required to verify the main acceptance criterion ("appears in the job list") and the basic day-to-day operation.

**Independent Test**: Store two jobs via the job service, run the list command and the show command, and verify the output.

**Acceptance Scenarios**:

1. **Given** stored jobs, **When** the operator lists jobs, **Then** a table shows one row per job with name, enabled state, frequency, next run time and last run status, sorted by name.
2. **Given** a job that has never run, **When** it is listed, **Then** its last status is shown as a clear placeholder (e.g. "never run").
3. **Given** no stored jobs, **When** the operator lists jobs, **Then** a friendly message says there are no jobs and the command exits successfully.
4. **Given** a stored job, **When** the operator shows it by name, **Then** its full configuration is printed as YAML together with enabled state and next run time.
5. **Given** a stored job whose configuration no longer validates, **When** the operator lists jobs, **Then** the job appears with status "invalid config" instead of being hidden; showing it prints the validation errors and exits with code 2.
6. **Given** an unknown job name, **When** the operator shows, edits, enables, disables, deletes or exports it, **Then** an error names the missing job and the command exits non-zero.

---

### User Story 4 - Non-interactive creation from a file (Priority: P2)

An operator or a provisioning script wants to create a job from an existing YAML job file, without any prompts and without a terminal attached.

**Why this priority**: Required for automation and explicitly an acceptance criterion; builds on the existing job file contract.

**Independent Test**: Invoke creation with a file option in a non-TTY test runner and verify the job is stored; repeat with an invalid file and verify a non-zero exit with field-level errors.

**Acceptance Scenarios**:

1. **Given** a valid job file and no TTY, **When** the operator creates a job from the file, **Then** the job is stored without any prompt and the command exits 0.
2. **Given** an invalid job file, **When** the operator creates a job from it, **Then** every problem is reported with the offending field, nothing is stored, and the command exits with the configuration-error code (2).
3. **Given** a job with the same name already exists, **When** the operator creates from a file, **Then** the command fails with a clear "already exists" error and the existing job is untouched.
4. **Given** no TTY and no file option, **When** the operator runs the creation command, **Then** the command fails immediately with a message explaining that interactive creation needs a terminal and pointing to the file option, instead of hanging.

---

### User Story 5 - Edit a job in the operator's editor (Priority: P2)

The operator wants to change an existing job by editing its YAML in their preferred editor. After saving and closing the editor, the change is validated; if it is invalid, the errors are shown and the operator can reopen the editor with their changes preserved, or abort.

**Why this priority**: Maintaining jobs is the second most frequent task after creation; it is an explicit acceptance criterion.

**Independent Test**: Replace the editor with a scripted fake that writes valid or invalid YAML, then verify the stored job is updated, or that the retry/abort prompt appears and the stored job stays unchanged on abort.

**Acceptance Scenarios**:

1. **Given** a stored job, **When** the operator edits it and saves valid changes, **Then** the job is updated, its next run time is recalculated if the schedule changed, and a success message is shown.
2. **Given** the operator saves YAML that is malformed or fails validation, **When** the editor closes, **Then** the errors are listed and the operator is asked to retry (reopening the editor with their edited text, not the original) or abort.
3. **Given** the operator aborts after an invalid edit, **When** the command ends, **Then** the stored job is unchanged and the command exits non-zero.
4. **Given** the operator closes the editor without changes, **When** the command ends, **Then** the job is not modified and the operator is told nothing changed.
5. **Given** no editor is configured in the environment, **When** the operator edits a job, **Then** a sensible platform default editor is used.
6. **Given** a stored job whose configuration no longer validates, **When** the operator edits it, **Then** the current errors are shown, the editor opens with the stored content, and saving a valid version repairs the job.

---

### User Story 6 - Enable, disable and delete jobs (Priority: P2)

The operator wants to pause or resume a job, or remove it permanently, with protection against accidental deletion.

**Why this priority**: Basic lifecycle management; lower than create/list because jobs can still be managed via files in the meantime.

**Independent Test**: Toggle a stored job's enabled state and verify the list output; delete a job with and without confirmation.

**Acceptance Scenarios**:

1. **Given** an enabled job, **When** the operator disables it, **Then** it is shown as disabled in the list and will not be scheduled; enabling it again restores scheduling with a freshly computed next run time.
2. **Given** a job is already in the requested state, **When** the operator enables/disables it again, **Then** the command succeeds and reports that nothing changed.
3. **Given** a stored job, **When** the operator deletes it and confirms the prompt, **Then** the job is removed and no longer listed.
4. **Given** a stored job, **When** the operator deletes it and declines the prompt, **Then** the job remains and the command reports it was not deleted.
5. **Given** a stored job and no TTY, **When** the operator deletes it with the `--yes` flag, **Then** it is removed without a prompt; without `--yes` and without a TTY the command refuses and exits non-zero.

---

### User Story 7 - Export and import job files (Priority: P3)

The operator wants to back up a job as a YAML file or copy a job between installations.

**Why this priority**: Useful for backup and sharing, but not needed for the core create/run loop.

**Independent Test**: Export a stored job to a file, delete it, import the file, and verify the restored job is equivalent.

**Acceptance Scenarios**:

1. **Given** a stored job, **When** the operator exports it without a target path, **Then** the YAML is written to standard output; with a target path, it is written to that file.
2. **Given** a valid job file, **When** the operator imports it, **Then** the job is stored under the file's base name, or under an explicitly given name.
3. **Given** a job with that name already exists, **When** the operator imports without a replace option, **Then** the import fails with an "already exists" error; with the replace option the existing job is updated.
4. **Given** an invalid job file, **When** the operator imports it, **Then** field-level errors are shown, nothing is stored, and the command exits with code 2.

---

### Edge Cases

- Reachability check times out or the network is unavailable: treated as a warning with override, never as a hard failure, and never blocks longer than the configured timeout per source.
- URL redirects to another location: following redirects is allowed; the final status determines reachability.
- Server rejects HEAD requests (e.g. 405): the check falls back to a lightweight GET before warning.
- Monthly schedule on day 29–31: accepted; the wizard shows a note that months shorter than that run on their last day.
- `--name` given to the wizard but invalid or already taken: rejected before the first question, exit 1.
- Duplicate recipient addresses or duplicate sources entered in the wizard: the operator is warned and the duplicate is not added twice.
- Empty keyword lists: allowed (keywords are optional); empty semantic description is rejected.
- Multi-line semantic description: line breaks are preserved in the saved job and in the preview.
- Limits: accepting the defaults skips the individual limit questions; when customising, non-integer values or values below 1 are rejected inline.
- Model name not in the registry (e.g. a newly released model or a local Ollama model): warned, accepted only after explicit confirmation; declining re-asks the model.
- Wizard aborted with Ctrl+C / EOF at any prompt: nothing is saved, clean exit, non-zero code.
- Editor exits with a non-zero status: treated as abort, job unchanged.
- Job name taken by another job between the start of the wizard and saving: saving fails with "already exists" and the operator's answers are shown as YAML so they are not lost.
- A stored job whose configuration has become invalid (e.g. after a schema change): handled per FR-032–FR-035 — visible in `list` as "invalid config", repairable via `edit`, never crashes a command.

## Requirements *(mandatory)*

### Functional Requirements

**Command group**

- **FR-001**: The `invio` CLI MUST provide an `invio job` command group (no separate `scout` entry point) with the sub-commands `create`, `list`, `show <name>`, `edit <name>`, `enable <name>`, `disable <name>`, `delete <name>`, `export <name>` and `import <file>`.
- **FR-002**: All job commands MUST print results to standard output and diagnostics/errors to standard error, exit 0 on success, exit non-zero on failure, and exit with code 2 on job configuration errors.
- **FR-003**: All job commands MUST operate on the existing job store and job file contract; they MUST NOT introduce a second definition of the job configuration.

**Interactive creation wizard**

- **FR-004**: `create` without a file option MUST run an interactive wizard asking, in this order: name → frequency (plus weekday for weekly, day of month for monthly) → time of day → time zone → recipients → sources (loop) → keywords any/all/exclude → semantic description (multi-line) → LLM provider and fast/smart models → limits.
- **FR-005**: Each wizard answer MUST be validated immediately; an invalid answer MUST show an inline error message and re-ask the same question without aborting the wizard or discarding earlier answers.
- **FR-006**: The wizard MUST validate e-mail addresses for syntactic correctness and require at least one recipient.
- **FR-007**: The wizard MUST validate source URLs for syntax (absolute http/https URL) and reject malformed URLs inline.
- **FR-008**: For URL-based sources, the wizard MUST check reachability with a bounded timeout (a lightweight request first with a full request as fallback; when the content itself must be inspected, as for `rss` feed detection, a single full request); if the source is unreachable or returns an error status, the wizard MUST warn and let the operator keep the source anyway or re-enter it.
- **FR-009**: For `rss` sources, the wizard MUST check whether the fetched content is an RSS or Atom feed and warn (with override) when no feed is detected.
- **FR-010**: For YouTube channel and playlist sources, the wizard MUST ask for the channel or playlist identifier instead of a URL.
- **FR-011**: The wizard MUST require at least one source and a non-empty semantic description; keyword lists MAY be empty.
- **FR-012**: The wizard MUST offer the supported LLM providers as a choice. For the fast and smart models it MUST offer the models the model registry lists for the selected provider; the operator MAY type a model that isn't listed, in which case the wizard MUST warn that the model is not registered for that provider and that runs will fail until it is added to the model registry, and ask for confirmation before accepting it. If the registry lists no models for the provider, free text is asked with the same warning. For limits, the wizard MUST first ask whether to keep the default limits; only if the operator declines MUST it ask each of the four limits individually, pre-filled with its documented default.
- **FR-013**: Before saving, the wizard MUST display the complete job as YAML (exactly as it would be exported) and ask for confirmation; declining MUST save nothing.
- **FR-014**: The assembled job MUST pass full job validation before the preview is shown; any cross-field error MUST be reported and the operator returned to the relevant step rather than aborting.
- **FR-015**: Aborting the wizard (interrupt or end of input) MUST save nothing and exit non-zero without a stack trace.
- **FR-016**: When `create` is run without a file option and no interactive terminal is available, it MUST fail fast with a message pointing to the file option.

**Non-interactive creation**

- **FR-017**: `create --from-file <path>` MUST create a job from a YAML job file without any prompts and MUST work without a terminal.
- **FR-018**: The job name for `create --from-file` MUST default to the file's base name and MAY be overridden by an explicit name option.
- **FR-019**: `create --from-file` MUST NOT perform network reachability checks (to stay deterministic in automation) and MUST report all validation errors with field names.

**Listing and showing**

- **FR-020**: `list` MUST print a table with the columns name, enabled, frequency, next run and last status, one row per job, sorted by name.
- **FR-021**: Next run times in `list` and `show` MUST be shown in a human-readable form that states the time zone.
- **FR-022**: Last status MUST reflect the outcome of the most recent run of the job, or a "never run" placeholder.
- **FR-023**: `show <name>` MUST print the job's full configuration as YAML plus its enabled state and next run time.

**Editing**

- **FR-024**: `edit <name>` MUST open the job's YAML in the editor named by the operator's environment (with a platform default fallback) and wait for it to close.
- **FR-025**: After the editor closes, the content MUST be parsed and validated; on success the job MUST be updated (next run recalculated); on failure the errors MUST be shown and the operator offered retry (reopening their edited text) or abort.
- **FR-026**: Aborting an edit or saving unchanged content MUST leave the stored job unchanged.
- **FR-027**: `edit` MUST NOT allow the job to be renamed implicitly via file content; the job name remains the command argument.

**Enable / disable / delete**

- **FR-028**: `enable <name>` and `disable <name>` MUST change the job's enabled state and report the new state; repeating the current state MUST succeed and report no change.
- **FR-029**: `delete <name>` MUST ask for confirmation unless `--yes` is given; declining MUST keep the job; without a terminal and without `--yes` it MUST refuse and exit non-zero.

**Export / import**

- **FR-030**: `export <name>` MUST write the job's YAML to standard output, or to a file when an output path is given.
- **FR-031**: `import <file>` MUST store the job under the file's base name or an explicit name, MUST refuse to overwrite an existing job unless a replace option is given, and MUST report validation errors with field names.

**Jobs with an invalid stored configuration**

- **FR-032**: `list` MUST include jobs whose stored configuration no longer validates, showing "invalid config" as their status (frequency and next run shown as unavailable where they cannot be derived); the job store MUST report such jobs to the CLI instead of silently omitting them.
- **FR-033**: `show <name>` on such a job MUST print the stored content and the list of validation errors, and exit with code 2.
- **FR-034**: `edit <name>` on such a job MUST open the stored content in the editor, show the current validation errors before opening, and follow the normal validate/retry/abort loop (FR-025, FR-026); a successful save repairs the job.
- **FR-035**: `disable` and `delete` MUST work on such a job; `enable` and `export` MUST refuse with the validation errors and exit with code 2 until the job is repaired.

**Testing**

- **FR-036**: Every job command and every acceptance scenario above MUST be covered by automated CLI tests that run without a real terminal, network access or a real editor (prompts, network checks and the editor are replaced by fakes).

### Key Entities *(include if feature involves data)*

- **Job**: A named, stored research job with an enabled flag, a validated job configuration, a next run time and timestamps. Created, changed and removed by this feature via the existing job store.
- **Job Configuration**: The validated job definition (schedule, notification, sources, search, LLM, limits) defined by the existing job file contract; the wizard produces one, `edit`/`import`/`create --from-file` parse one.
- **Wizard Session**: The transient set of answers collected during one interactive creation; never persisted until the operator confirms the preview.
- **Source Check Result**: The outcome of checking a source during the wizard — valid syntax, reachable / unreachable (with reason), feed detected / not detected — and whether the operator chose to keep the source despite a warning.
- **Run Status**: The outcome of the most recent run of a job, shown as "last status" in the job list; "never run" when there is no run, and "invalid config" when the stored configuration no longer validates (this takes precedence).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can create a complete, valid job through the wizard in under 3 minutes without consulting documentation about the job file format.
- **SC-002**: 100% of invalid wizard answers covered by tests (bad e-mail, bad URL, bad time, unknown time zone, duplicate name, empty description, invalid limits) are rejected inline and the wizard continues to the same question.
- **SC-003**: A job created via the wizard or from a file appears in the job list immediately after the command finishes, with its frequency and next run time.
- **SC-004**: Creating a job from a file succeeds in a non-interactive environment (no terminal) in 100% of test runs, with no prompt ever shown.
- **SC-005**: Each source reachability check finishes (success or warning) within its timeout of at most 10 seconds, so a wizard with 5 sources never stalls for more than ~50 seconds in total on checks.
- **SC-006**: An invalid edit never alters the stored job: in 100% of tested invalid-edit-then-abort cases the stored configuration is byte-for-byte identical to before.
- **SC-007**: No job is ever deleted without either an explicit confirmation or the `--yes` flag.
- **SC-008**: Every job command and acceptance scenario is covered by an automated test that runs offline and deterministically.

## Assumptions

- The issue's `scout job` is realised as `invio job` (auto-discovered like the existing `db` commands); see Clarifications.
- Job storage, validation, YAML import/export, enable/disable and next-run calculation already exist from issues #3–#6 and are reused; this feature adds the terminal interface on top of them.
- "Last status" is derived from the most recent run recorded in the run history; until the pipeline records runs, every job shows "never run".
- The notification subject is required by the job contract but not listed among the wizard steps; the wizard asks for it together with the recipients and pre-fills a default derived from the job name. `send_if_empty`, `min_relevance` and `fallback_provider` keep their defaults in the wizard and can be changed via `edit`.
- Default reachability timeout is 10 seconds per source; redirects are followed.
- Reachability and feed checks are advisory only and run only in the interactive wizard, not in `create --from-file`, `import` or `edit`.
- The editor is taken from the standard editor environment variables, falling back to a platform default (e.g. `vi` on Unix-like systems).
- Times shown in `list`/`show` use the job's configured time zone.
- Comments in edited YAML are not preserved after saving (consistent with the existing job file contract).
- Single-operator use: no concurrent editing protection beyond failing cleanly if the job changed or disappeared during an edit.
