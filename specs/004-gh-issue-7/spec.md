# Feature Specification: LLM Provider Abstraction, Usage Tracking and Model Registry

**Feature Branch**: `gh-issue-7`

**Created**: 2026-10-04

**Status**: Draft

**Input**: User description: "GitHub issue #7 (short name: gh-issue-7): Define LLM provider protocol, factory, usage tracking and model registry. Context: Mistral comes first, but OpenAI, Claude, Google and Ollama must plug in without pipeline changes. Pipeline nodes use two roles, `fast` and `smart`, and need structured output validated by Pydantic. Depends on #2. Requirements: (1) `scout/llm/base.py` with `LLMProvider` protocol: `complete(system, user, *, model, temperature, max_tokens) -> (str, Usage)` and `complete_structured(system, user, schema, *, model, temperature) -> (T, Usage)`. (2) `Usage(input_tokens, output_tokens)` and typed errors `LLMRateLimitError`, `LLMAuthError`, `LLMInvalidOutputError`, `LLMUnavailableError`. (3) Shared `structured_with_repair()` helper: validate with Pydantic; on failure retry once with the validation error appended; then raise `LLMInvalidOutputError`. (4) `scout/llm/factory.py`: `get_provider(name, settings)` and `resolve(llm_config, role) -> (provider, model)`; providers self-register via `@register_provider("<name>")` decorator and the factory imports all modules in `scout/llm/` via `pkgutil`, so adding a provider never edits a shared file. (5) Model registry (`models.yaml` as logical name): loader merges all files in `scout/llm/models.d/*.yaml` (one file per provider; model id -> provider, price per 1M input/output tokens, context window); costs computed from it. (6) `FakeProvider` for tests returning scripted responses. Acceptance criteria: FakeProvider satisfies the protocol (checked via mypy); invalid structured output triggers exactly one repair attempt then raises LLMInvalidOutputError; requesting a provider without configured key raises LLMAuthError with a clear message; cost calculation from models.yaml is unit-tested; model ids are never hard-coded outside models.yaml and job configs; adding a provider module with @register_provider and its own models.d/<provider>.yaml makes it resolvable without editing factory.py."

## Clarifications

### Session 2026-10-04

- Q: Should a provider that registers itself also become selectable in job files automatically, or does the job schema's fixed provider list stay as it is? → A: Keep the fixed list; "no shared edits" applies within the LLM layer only — a provider beyond the five planned ones additionally needs a one-line addition to the job schema's provider list.
- Q: Should the LLM layer write one structured log line for every model call, or leave logging to the pipeline steps that call it? → A: The LLM layer logs one structured info line per call with metadata only (provider, model, input/output tokens, cost, duration, repair flag), plus a warning per repair attempt and per typed error; never prompt or answer text.
- Q: Should every model call have a time limit, after which it fails with the "provider unavailable" error, and if so, where should the limit come from? → A: One application setting for all providers (default 60 seconds); exceeding it raises the "provider unavailable" error.

## User Scenarios & Testing *(mandatory)*

The "users" of this feature are the parts of invio that talk to language models — mainly the
research pipeline nodes — and the developers who add support for new LLM providers. Through
them, the operator benefits: a job can switch provider or model purely by configuration, and
every LLM call reports how many tokens it used and what it cost.

### User Story 1 - Pipeline asks for a model by role and gets text or validated data back (Priority: P1)

A pipeline node knows only the job's LLM configuration and which role it needs (`fast` for
cheap bulk work such as relevance filtering, `smart` for demanding work such as summarizing).
It asks the LLM layer for the provider and model that fill that role, sends a system and a user
message, and receives either free text or a structured result that has already been checked
against the expected shape — together with the token usage of the call. The node never names a
concrete provider or model itself.

**Why this priority**: Every LLM-based pipeline step depends on this single, provider-neutral
way of calling a model. Without it no pipeline node can be built or tested.

**Independent Test**: With a scripted fake provider registered, resolve the `fast` and `smart`
roles from a job configuration, request a free-text completion and a structured completion,
and check that the returned text, the returned structured value and the reported token usage
match the script.

**Acceptance Scenarios**:

1. **Given** a job configuration that names a provider and a `fast` and a `smart` model,
   **When** the `fast` role is resolved, **Then** the configured provider and the `fast` model
   are returned; **When** the `smart` role is resolved, **Then** the same provider and the
   `smart` model are returned.
2. **Given** a resolved provider and model, **When** a free-text completion is requested with a
   system message, a user message, a temperature and an output-token limit, **Then** the
   response text and the number of input and output tokens used are returned.
3. **Given** a resolved provider and model and an expected result shape, **When** a structured
   completion is requested and the model answers with data matching that shape, **Then** a
   validated value of that shape and the token usage are returned.
4. **Given** a role other than `fast` or `smart`, **When** it is resolved, **Then** the request
   is rejected with an error naming the invalid role and the allowed roles.

---

### User Story 2 - Malformed structured answers are repaired once, then rejected (Priority: P1)

Models sometimes return structured data that does not match the expected shape. Instead of
every pipeline node handling this itself, the LLM layer validates the answer, and on failure
asks the model exactly once more, telling it what was wrong. If the second answer is still
invalid, the call fails with a dedicated "invalid output" error so the pipeline can handle it
consistently.

**Why this priority**: Structured output is how pipeline nodes get usable data (scores,
summaries, classifications). Uniform repair behaviour is needed before any node relies on it,
and it keeps every provider behaving the same way.

**Independent Test**: Script the fake provider to return an invalid answer followed by a valid
one, then two invalid answers; check that the first case succeeds after exactly one extra
request, the second case fails with the "invalid output" error after exactly two requests in
total, and that the repair request contains the validation problem.

**Acceptance Scenarios**:

1. **Given** a model whose first answer violates the expected shape and whose second answer is
   valid, **When** a structured completion is requested, **Then** exactly one repair request is
   made, it includes the validation problems of the first answer, and the valid value is
   returned with the combined token usage of both requests.
2. **Given** a model whose first and second answers both violate the expected shape, **When** a
   structured completion is requested, **Then** exactly one repair request is made and the call
   fails with the "invalid output" error, which describes the final validation problem and
   carries the combined token usage of both requests.
3. **Given** a model whose answer is not parseable as structured data at all (e.g. plain prose),
   **When** a structured completion is requested, **Then** this is treated like a validation
   failure and goes through the same single repair attempt.
4. **Given** a model whose first answer is valid, **When** a structured completion is requested,
   **Then** no repair request is made.

---

### User Story 3 - Provider failures surface as clear, typed errors (Priority: P1)

When a provider cannot be used — its credential is missing or rejected, it is rate limiting,
or it is unreachable — the pipeline receives one of a small set of typed errors that mean the
same thing for every provider. A missing credential is detected when the provider is
requested, and the message tells the operator exactly which setting to configure, without ever
revealing a credential value.

**Why this priority**: Unattended runs must fail in a way that is understandable from the logs
and that later retry/fallback logic can react to without knowing provider-specific errors.

**Independent Test**: Request a provider whose credential setting is empty and check that an
authentication error is raised naming the missing setting; script the fake provider to raise
each failure type and check that callers receive the matching typed error.

**Acceptance Scenarios**:

1. **Given** no credential is configured for a provider that needs one, **When** that provider
   is requested, **Then** an authentication error is raised whose message names the provider
   and the setting (environment variable) that must be set.
2. **Given** a provider that does not need a credential (a local model server), **When** it is
   requested without any credential, **Then** it is returned without an authentication error.
3. **Given** a provider that reports rate limiting, an invalid credential, or unavailability,
   **When** a completion is requested, **Then** the caller receives the rate-limit,
   authentication or unavailable error respectively, regardless of which provider it is.
4. **Given** any of these errors, **When** its message or representation is logged, **Then** no
   credential value appears in it.

---

### User Story 4 - Token cost is computed from a central model registry (Priority: P2)

Every model invio may use is described once in a model registry: which provider serves it,
what it costs per one million input and output tokens, and how large its context window is.
The registry is assembled from one file per provider. Given a model and the token usage of a
call, the LLM layer computes the cost of that call, so usage records and per-run budgets can be
reported in money and not only in tokens.

**Why this priority**: Cost tracking is a core promise for unattended runs, but pipeline calls
can already work (P1) before cost is reported.

**Independent Test**: Load a registry from test files with known prices and check computed
costs for several token counts, including zero tokens and very large counts; check that
loading fails clearly for a malformed or conflicting registry.

**Acceptance Scenarios**:

1. **Given** a registry entry for a model priced at P_in per 1M input tokens and P_out per 1M
   output tokens, **When** the cost of a call with I input and O output tokens is computed,
   **Then** the result equals I × P_in / 1,000,000 + O × P_out / 1,000,000, without
   floating-point rounding drift.
2. **Given** registry files for several providers, **When** the registry is loaded, **Then** it
   contains the models from all files.
3. **Given** two registry files that both define the same model id, **When** the registry is
   loaded, **Then** loading fails with an error naming the model and both files.
4. **Given** a registry file with a missing or invalid field (e.g. negative price, unknown key),
   **When** the registry is loaded, **Then** loading fails with an error naming the file, the
   model and the offending field.
5. **Given** a job configuration that names a model absent from the registry, or a model the
   registry assigns to a different provider, **When** a role is resolved, **Then** resolution
   fails with an error naming the model and the configured provider.

---

### User Story 5 - Developers add a provider without touching shared code (Priority: P2)

A developer adding support for a new provider (OpenAI, Claude, Google, Ollama after Mistral)
writes one new provider module that registers itself under its provider name, plus one
registry file listing that provider's models. Nothing else — no central list, no factory, no
pipeline code — needs to be edited for the provider to become resolvable.

**Why this priority**: This keeps the provider tracks independent and conflict-free, but it
only pays off once more than one provider exists.

**Independent Test**: In a test, add a new provider module that registers itself under a new
name together with a registry file for its models, and check that the provider can be
requested by name and that its models resolve and are priced — with no change to shared files.

**Acceptance Scenarios**:

1. **Given** a new provider module in the LLM package that registers itself under a name and a
   matching registry file, **When** the provider is requested by that name, **Then** it is
   returned, and its models resolve and have computable costs.
2. **Given** two provider modules registering under the same name, **When** providers are
   discovered, **Then** discovery fails with an error naming the duplicated provider name.
3. **Given** a provider name that no module registered, **When** it is requested, **Then** an
   error is raised naming the unknown provider and listing the registered ones.

---

### User Story 6 - Tests run against a scripted fake provider (Priority: P2)

Developers writing tests for pipeline nodes use a fake provider that returns pre-scripted
answers and token counts, records what it was asked, and can be scripted to raise any of the
typed errors. It behaves like a real provider from the caller's point of view, so tests need no
network and no real credentials.

**Why this priority**: Required by the project rule that unit tests never call real LLM
providers; it is the enabler for testing stories 1–3, but has no value on its own for the
operator.

**Independent Test**: Script the fake provider with a sequence of answers and errors, run calls
against it, and check that answers come back in order, that the recorded requests match what
was sent, and that the type checker accepts it wherever a provider is expected.

**Acceptance Scenarios**:

1. **Given** a fake provider scripted with answers A then B, **When** two completions are
   requested, **Then** A is returned first and B second, each with its scripted token usage.
2. **Given** a fake provider, **When** the project's static type check runs, **Then** it
   confirms the fake provider fulfils the provider contract.
3. **Given** a fake provider whose script is exhausted, **When** another completion is
   requested, **Then** it fails with a clear test-setup error instead of returning a default.
4. **Given** a fake provider, **When** completions are requested, **Then** each request's
   messages, model, temperature and token limit are recorded for later inspection.

---

### Edge Cases

- A job configuration names a provider that is valid in the configuration schema but has no
  provider module yet (e.g. `openai` before its issue is implemented): requesting it fails with
  the "unknown provider" error listing the registered providers.
- A credential setting is present but empty or whitespace only: treated as missing (auth error).
- A provider returns token counts that are missing: usage is reported as zero for the missing
  count rather than failing the call; the cost is computed from what is reported. This mapping
  happens inside each concrete provider (separate issues); this feature only requires that
  `Usage` accepts zero counts.
- Cost is requested for a model not in the registry (e.g. usage recorded for a since-removed
  model): the cost is reported as unknown (absent), not zero, so it is distinguishable from a
  free model.
- A model with price zero (e.g. a local model): cost is exactly zero.
- The registry directory contains no files or a non-registry file: an empty registry is valid
  but every role resolution then fails with the "model not in registry" error; only registry
  files (by extension) are read.
- A registry file declares a model for a provider other than the one the file is named after:
  loading fails, naming file and model.
- The repair request itself fails with a provider error (e.g. rate limit): that typed provider
  error is raised, not the "invalid output" error.
- The requested output-token limit or temperature is out of range (e.g. negative): the request
  is rejected before contacting the provider.
- A provider does not answer within the configured call timeout: the request fails with the
  provider-unavailable error; if this happens during the repair request, that error is raised
  instead of the invalid-output error.
- The call timeout setting is zero, negative or not a number: loading the settings fails with
  a message naming the setting.

## Requirements *(mandatory)*

### Functional Requirements

**Provider contract**

- **FR-001**: The LLM layer MUST define one provider contract with exactly two operations: a
  free-text completion (inputs: system message, user message, model, temperature, output-token
  limit; outputs: text and token usage) and a structured completion (inputs: system message,
  user message, expected result shape, model, temperature; outputs: validated value of that
  shape and token usage).
- **FR-002**: Token usage MUST be reported as a pair of non-negative counts: input tokens and
  output tokens.
- **FR-003**: The LLM layer MUST define four typed errors shared by all providers: rate limited,
  authentication failed/missing, invalid output, and provider unavailable. Providers MUST
  translate their own failures into these errors.
- **FR-004**: No error message, log line or representation produced by the LLM layer MUST
  contain a credential value.
- **FR-022**: Every request to a provider MUST be bounded by one call timeout taken from the
  application settings (default 60 seconds, must be a positive number, same for all providers);
  a request exceeding it MUST fail with the provider-unavailable error naming the provider,
  model and the timeout. Within a structured completion, the timeout applies to each request
  (the original and the repair request) separately.

**Structured output and repair**

- **FR-005**: The LLM layer MUST provide one shared repair procedure used by every provider's
  structured completion: validate the answer against the expected shape; on failure send
  exactly one follow-up request that includes the validation problems; if that answer is also
  invalid, raise the invalid-output error.
- **FR-006**: An answer that cannot be parsed as structured data MUST be treated as a
  validation failure.
- **FR-007**: The token usage returned (or carried by the invalid-output error) MUST be the sum
  over all requests made for that structured completion.

**Provider registration and resolution**

- **FR-008**: Providers MUST register themselves under a unique provider name from within their
  own module; the LLM layer MUST discover all provider modules in the LLM package
  automatically, so adding a provider requires no edit to any shared file of the LLM layer.
  The job configuration's fixed provider list is not changed by this feature.
- **FR-009**: Registering two providers under the same name MUST fail with an error naming the
  duplicated provider name.
- **FR-010**: The LLM layer MUST return a ready-to-use provider for a provider name and the
  application settings; an unknown name MUST raise an error naming it and listing the
  registered providers.
- **FR-011**: Requesting a provider that needs a credential when that credential is missing or
  blank MUST raise the authentication error naming the provider and the setting to configure.
  Credentials MUST be checked only when the provider is requested, not at application startup.
- **FR-012**: The LLM layer MUST resolve a job's LLM configuration and a role (`fast` or
  `smart`) to a provider and model id, using the provider and the role's model from the job
  configuration. Unknown roles MUST be rejected naming the allowed roles.
- **FR-013**: Resolution MUST verify that the model id exists in the model registry and belongs
  to the configured provider, and MUST fail otherwise with an error naming the model and the
  provider.

**Model registry and cost**

- **FR-014**: The model registry MUST be assembled by merging one registry file per provider;
  each entry describes a model id, its provider, its price per one million input tokens, its
  price per one million output tokens (both non-negative, in USD) and its context window
  (positive token count).
- **FR-015**: Registry loading MUST reject unknown keys, missing fields, invalid values,
  entries whose provider does not match the file's provider, and model ids defined more than
  once, with messages naming the file, model and problem.
- **FR-016**: The LLM layer MUST compute the cost of a call from its model and token usage as
  input tokens × input price / 1,000,000 + output tokens × output price / 1,000,000, using exact
  decimal arithmetic, with the result rounded half up to 6 decimal places (USD; the precision of
  the stored usage cost). For a model absent from the registry the cost MUST be reported as unknown.
- **FR-017**: Model ids MUST NOT be hard-coded anywhere in the application code; they appear
  only in registry files and job configurations (test fixtures excepted).

**Observability**

- **FR-019**: For every completed call (free-text or structured), the LLM layer MUST write one
  structured info log line containing provider, model, input tokens, output tokens, cost (or
  unknown), duration and whether a repair attempt was made.
- **FR-020**: The LLM layer MUST write one structured warning log line for each repair attempt
  (with the validation problem summary) and for each typed error raised (with error type,
  provider and model).
- **FR-021**: Log lines from the LLM layer MUST NOT contain prompt text, answer text or
  credential values.

**Test support**

- **FR-018**: The LLM layer MUST provide a fake provider that fulfils the provider contract
  (verified by the static type check), returns scripted answers and token usage in order, can be
  scripted to raise any typed error, records every request it receives, and fails with a clear
  error when its script is exhausted. Its structured completion MUST use the shared repair
  procedure so repair behaviour is testable through it.

### Key Entities

- **Provider**: A connection to one LLM service (e.g. Mistral, a local Ollama server),
  identified by a unique provider name; offers free-text and structured completion.
- **Role**: The kind of model a pipeline step needs — `fast` (cheap, high volume) or `smart`
  (capable, lower volume); mapped to a concrete model by the job configuration.
- **Usage**: Input and output token counts of one completion (including repair requests);
  the basis for cost and budget tracking and for the existing LLM usage records.
- **Model registry entry**: Model id, provider, price per 1M input tokens, price per 1M output
  tokens, context window; one registry file per provider, merged into one registry.
- **LLM errors**: Rate limited, authentication, invalid output (carries final validation
  problem and accumulated usage), unavailable.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A new provider becomes usable by adding exactly two new files (its module and its
  registry file) and changing zero existing files of the LLM layer — demonstrated by an
  automated test.
- **SC-002**: In 100% of tested cases, an invalid structured answer leads to exactly one repair
  request, and a second invalid answer leads to the invalid-output error.
- **SC-003**: For every provider requiring a credential, a missing credential produces an error
  that names the setting to configure, and in no tested case does a credential value appear in
  an error message or log line.
- **SC-004**: Computed costs match hand-calculated expected values exactly (to the smallest
  stored unit) for all tested combinations, including zero and very large token counts.
- **SC-005**: A search of the application code finds zero model ids outside registry files.
- **SC-006**: All tests for this feature run without network access or real credentials.
- **SC-007**: Every tested call produces exactly one info log line with the required metadata,
  and no tested log line contains prompt text, answer text or a credential value.

## Assumptions

- **Package location**: The issue refers to `scout/llm/`; the project's package is `invio`, so
  the LLM layer lives in the existing `invio.llm` package (`src/invio/llm/`), with registry files
  in a `models.d` directory inside it. "models.yaml" is only the logical name of the merged
  registry.
- **Job configuration provider list**: The job configuration schema from #3 restricts
  `llm.provider` to a fixed set (mistral, openai, anthropic, google, ollama), and this stays
  unchanged (the configuration layer must not depend on the LLM layer, and typos are caught when
  a job file is loaded). The "no shared edits" guarantee applies within the LLM layer and is
  tested there by provider name. A provider beyond the five planned ones additionally needs a
  one-line addition to the job schema's provider list before job files can select it.
- **Credentials**: Provider credentials come from the existing settings (`INVIO_<PROVIDER>_API_KEY`).
  Ollama needs no credential (it uses `ollama_base_url`); an unreachable server surfaces as the
  unavailable error when called.
- **Call timeout setting**: The call timeout is a new application setting alongside the
  existing provider settings (an `INVIO_`-prefixed environment variable); job files do not
  override it.
- **Scope**: This feature delivers the contract, errors, repair procedure, discovery/resolution,
  registry and cost calculation, and the fake provider. Concrete providers (Mistral first, then
  the others), retry/back-off on rate limits, fallback to `fallback_provider`, enforcing
  `max_llm_tokens_per_run`, and persisting usage records are separate issues; the outputs here
  (usage, cost) are designed to feed the existing LLM usage table.
- **Shipped registry content**: This feature ships the registry mechanism plus registry files
  only for providers that are implemented (none besides test fixtures is required); each
  provider issue adds its own registry file with current prices.
- **Currency and precision**: Prices and costs are in USD; cost precision matches the existing
  usage table (6 decimal places).
- **Structured shape**: The expected result shape is a validation model as used elsewhere in
  the project (Pydantic v2), per the constitution's "strict contracts at boundaries" rule.
- **Dependency**: Depends on #2 (settings), which is already merged.
