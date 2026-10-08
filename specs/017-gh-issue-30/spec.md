# Feature Specification: OpenAI Provider

**Feature Branch**: `gh-issue-30` (the constitution's naming; the issue's suggested `issue/30-add-openai-provider` is superseded)

**Created**: 2026-10-08

**Status**: Draft

**Input**: User description: "GitHub issue #30 (short name: gh-issue-30): Add OpenAI provider. OpenAI models become selectable per job via `llm.provider: openai`. Requirements: official OpenAI client; free-text completion that reports token usage; structured completion that asks the service for schema-conformant output (strict where supported) and reuses the shared repair helper; errors mapped to the common typed errors with the shared retry/backoff behaviour; registration in the provider factory and model registry; extension of the `llm test` command; tests using recorded HTTP fixtures. Acceptance criteria: both operations pass the shared provider contract tests; error mapping matches the Mistral provider's behaviour; `llm test openai` works with a valid key. Depends on #7 (provider abstraction)."

## User Scenarios & Testing *(mandatory)*

The "users" are the operator who writes job files and runs the `invio` CLI, and the pipeline
nodes that call the LLM layer. Today only Mistral models can be used; this feature lets the
operator pick OpenAI models for a job without any change to the pipeline.

### User Story 1 - Run a job on OpenAI models (Priority: P1)

An operator sets `llm.provider: openai` in a job file and names OpenAI models for the `fast`
and `smart` roles. When the job runs, the pipeline obtains free-text answers and structured
(validated) answers from OpenAI exactly as it would from Mistral, and every call reports its
token usage and cost.

**Why this priority**: This is the whole point of the feature; everything else supports it.

**Independent Test**: With recorded OpenAI responses standing in for the service, resolve the
`fast` and `smart` roles of a job configured for OpenAI, request one free-text and one
structured completion, and check the returned text, the validated structured value and the
reported token usage.

**Acceptance Scenarios**:

1. **Given** a job with `llm.provider: openai` and valid OpenAI model ids, **When** the LLM layer
   resolves a role, **Then** it returns the OpenAI provider and the configured model.
2. **Given** an OpenAI provider and a prompt, **When** a free-text completion is requested,
   **Then** the answer text is returned together with input and output token counts taken from
   the service's response.
3. **Given** an OpenAI provider, a prompt and an expected data shape, **When** a structured
   completion is requested, **Then** the service is asked to answer in that shape and the
   validated value is returned together with the token usage.
4. **Given** a structured answer that does not match the expected shape, **When** it is
   validated, **Then** exactly one repair attempt is made via the shared repair procedure, and
   if it still fails, the invalid-output error is raised carrying the summed usage of both calls.
5. **Given** only models that support strict schema-constrained output are registered, **When**
   the service rejects the requested schema as unsupported, **Then** the call fails with the
   "invalid request" error (no silent fallback to unconstrained output), and the shared repair
   procedure remains the safety net for answers that do not validate.

---

### User Story 2 - Failures behave like Mistral failures (Priority: P1)

When OpenAI rejects or fails a request, the operator and the pipeline see the same typed errors,
retries and messages they already know from the Mistral provider, so error handling needs no
provider-specific code.

**Why this priority**: Unattended runs depend on predictable failure behaviour; this is an
explicit acceptance criterion.

**Independent Test**: Replay recorded failure responses (bad key, rate limit with and without a
wait hint, server error, timeout, rejected request, malformed response) and compare the resulting
error types and retry behaviour with the Mistral provider's for the equivalent cases.

**Acceptance Scenarios**:

1. **Given** a rejected credential, **When** a call is made, **Then** the "auth" error is raised
   after one attempt, with no retry.
2. **Given** a rate-limit response with a wait hint, **When** a call is made, **Then** the
   "rate limit" error carries the hint and the shared retry/backoff logic honours it up to its
   configured maximum.
3. **Given** a server error, a network failure or a timeout, **When** a call is made, **Then**
   the "provider unavailable" error is raised immediately for a timeout (not retried, as with Mistral), or after the shared retry policy is exhausted for connection and 5xx failures.
4. **Given** a request the service rejects as invalid, **When** a call is made, **Then** the
   "invalid request" error is raised without retry.
5. **Given** any failure, **When** the error message is produced, **Then** it names provider and
   model and never contains the API key, prompt text or answer text.

---

### User Story 3 - Verify the setup with `llm test` (Priority: P2)

An operator who has just configured an OpenAI key runs `invio llm test openai` and gets a clear
pass/fail answer showing that the key, the network path and the chosen model work, including
token usage.

**Why this priority**: It is the operator's quick check before scheduling jobs, but the provider
is usable without it.

**Independent Test**: Run the command against recorded success and failure responses and check
output and exit code; with a real key, run it manually once.

**Acceptance Scenarios**:

1. **Given** a valid OpenAI key, **When** `invio llm test openai` runs, **Then** it reports
   success with the model used and token usage, writes results to stdout and exits 0.
2. **Given** a missing key, **When** the command runs, **Then** it exits with code 2 and a message
   naming the missing environment variable, and no network call is made.
3. **Given** an invalid key or an unavailable service, **When** the command runs, **Then** it
   exits with code 1 and the corresponding typed-error message on stderr.

---

### User Story 4 - Provider passes the shared contract (Priority: P2)

A developer runs the shared provider contract tests and the OpenAI provider passes them in the
same way the fake and Mistral providers do, so any pipeline test written against the contract is
valid for OpenAI.

**Why this priority**: It guards substitutability, but only matters once Story 1 exists.

**Independent Test**: Run the shared contract suite parametrized over the OpenAI provider backed
by recorded responses.

**Acceptance Scenarios**:

1. **Given** the shared contract suite, **When** it runs against the OpenAI provider, **Then**
   all free-text and structured cases pass.
2. **Given** the provider module and its model-registry file are added, **When** the factory
   loads, **Then** `openai` resolves without editing the factory or other providers.

---

### Edge Cases

- The service returns an empty answer, a refusal, or a truncated answer (length limit reached)
  → raises the "provider unavailable" error; never returned as valid.
- The response omits token usage → usage is reported as unknown/zero consistently with how other
  providers handle it, and the call does not crash.
- The expected data shape uses constructs the strict mode does not support → the shape is
  adapted for the service without changing what the shared validation accepts.
- A model id in the job is not in the registry or belongs to another provider → resolution fails
  with the existing registry error before any call is made.
- The API key is missing when the provider is requested → the "auth" error names the environment
  variable, not its value.
- Concurrent calls from several pipeline nodes share one provider instance safely.
- A call exceeds the application-wide call timeout → "provider unavailable" error.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST offer OpenAI as a selectable provider via `llm.provider: openai` in
  job files, resolvable for both the `fast` and `smart` roles.
- **FR-002**: The OpenAI provider MUST fulfil the shared provider contract: a free-text
  operation and a structured operation with the same inputs, outputs and usage reporting as other
  providers.
- **FR-003**: The free-text operation MUST return the answer text and the input/output token
  counts reported by the service.
- **FR-004**: The documentation of the provider MUST state which of the service's two request
  styles it uses and why.
- **FR-005**: The structured operation MUST ask the service to constrain its answer to the
  expected data shape in strict mode. Only models supporting this are registered; a schema the
  service rejects surfaces as the "invalid request" error rather than a silent non-strict retry.
- **FR-006**: The structured operation MUST use the shared repair procedure: validate, retry
  once with the validation error appended, then raise the invalid-output error; usage MUST be
  the sum over all calls made.
- **FR-007**: Service and transport failures MUST be mapped to the shared typed errors (auth,
  rate limit incl. wait hint, invalid request, unavailable, invalid output) with the same
  classification as the Mistral provider for equivalent situations. The one deliberate difference:
  a rate-limit response meaning exhausted quota/billing (not a transient limit) is reported as the
  "rate limit" error without a wait hint and is not retried.
- **FR-008**: Transient errors MUST be retried with the shared retry/backoff logic, not a
  provider-specific copy; permanent errors MUST NOT be retried.
- **FR-009**: Error messages, logs and representations MUST NOT contain the API key, prompt text
  or answer text, and MUST name provider and model.
- **FR-010**: Every call MUST be bounded by the application-wide call timeout setting.
- **FR-011**: The provider MUST self-register with the factory and ship its own model-registry
  file listing its models with price per 1M input/output tokens and context window; no shared
  file MUST need editing, and no model id MUST be hard-coded outside registry files and job
  configs.
- **FR-012**: Requesting the provider without a configured API key MUST raise the "auth" error
  naming the environment variable.
- **FR-013**: `invio llm test openai` MUST perform a minimal live check and report success
  (model, usage) or the typed failure, using stdout for results, stderr for diagnostics, and
  the existing exit codes (1 provider call failed, 2 configuration error). The command is
  already provider-generic; only the provider and its registry entries need to exist.
- **FR-014**: The existing structured per-call log line (provider, model, tokens, cost,
  duration, repair flag) MUST be emitted for OpenAI calls like for other providers.
- **FR-015**: Automated tests MUST cover all acceptance criteria and the failure paths using
  recorded HTTP fixtures, with no network access or real key required.

### Key Entities

- **OpenAI provider**: A registered implementation of the provider contract for the OpenAI
  service; holds credentials, timeout and retry policy.
- **Model registry entry**: Per-model record (id, provider, price per 1M input and output
  tokens, context window) for OpenAI models.
- **Usage**: Input and output token counts of a call.
- **Typed LLM errors**: Auth, rate limit, invalid request, unavailable, invalid output.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can switch an existing job from Mistral to OpenAI by changing only
  the job's LLM configuration, with zero pipeline or code changes.
- **SC-002**: 100% of the shared provider contract tests pass for the OpenAI provider.
- **SC-003**: For each failure category exercised against Mistral (auth, rate limit, server
  error, timeout, rejected request, malformed answer), OpenAI yields the same error type and
  retry behaviour — verified by an equivalent test for each category.
- **SC-004**: `invio llm test openai` gives an operator a pass/fail answer in a single command
  and under the call timeout, with a correct exit code in 100% of tested cases.
- **SC-005**: The full test suite for the provider runs offline, without a real key, and
  deterministically.
- **SC-006**: No test or log output contains a secret or prompt/answer text.

## Assumptions

- Issue #7 (provider contract, factory, registry, repair helper, typed errors, retry) is
  delivered and is the foundation; the Mistral provider is the behavioural reference.
- The API key comes from the same settings/environment mechanism as other providers' keys.
- The choice between the service's two request styles is a design decision for the plan,
  documented per FR-004; it does not change user-visible behaviour.
- Streaming, tool calling, image input and embeddings are out of scope.
- Initial model list in the registry covers a small set of current general-purpose OpenAI models;
  operators can extend the file.
- Azure-hosted or other OpenAI-compatible endpoints are out of scope.
- The job schema's fixed provider list already includes `openai` (per the #7 clarification);
  if not, a one-line addition is in scope.
