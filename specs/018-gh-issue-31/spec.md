# Feature Specification: Anthropic (Claude) Provider

**Feature Branch**: `gh-issue-31` (the constitution's naming; the issue's suggested `issue/31-add-anthropic-claude-provider` is superseded)

**Created**: 2026-10-09

**Status**: Draft

**Input**: User description: "GitHub issue #31 (short name: gh-issue-31): Add Anthropic (Claude) provider. Claude models become selectable per job via `llm.provider: anthropic`. Requirements: official Anthropic client; free-text completion with a system instruction and an output-length limit that reports token usage; structured completion that forces the service to answer through a single schema-described tool call, validates the result and reuses the shared repair helper; errors (auth, rate limit, server/overloaded) mapped to the common typed errors; registration in the provider factory and model registry; extension of the `llm test` command. Acceptance criteria: provider passes the shared contract tests; structured output works for nested models and lists; overloaded/5xx responses are retried then raised as the 'provider unavailable' error. Depends on #7 (provider abstraction)."

## Clarifications

### Session 2026-10-09

- Q: When a structured answer is cut off by the output-length limit, fail as "provider unavailable" or attempt repair? → A: "Provider unavailable" for both free text and structured output, no repair (matches OpenAI).
- Q: Which output-length limit do structured calls use, since the shared structured operation passes none? → A: A per-model maximum output size stored in the Anthropic registry entries; structured calls use that model's maximum.
- Q: Should `invio llm test` also check structured output? → A: No. Free-text check only; the command stays unchanged apart from `anthropic` being selectable. The structured path is covered by the offline contract tests.
- Q: Which error does an out-of-credit rejection map to? → A: "Rate limit" without a wait hint, not retried (same as OpenAI's exhausted quota).
- Q (during planning): Which Claude models are registered, given that current models (Opus 5.5, Sonnet 5.5, Fable 5.1) reject forced tool choice and non-default temperature, while the pipeline always sends temperature 0? → A: Only previous-generation models that accept both: Claude Haiku 4.5 (fast) and Claude Sonnet 4.6 (smart).

## User Scenarios & Testing *(mandatory)*

The "users" are the operator who writes job files and runs the `invio` CLI, and the pipeline nodes that call the LLM layer. Today Mistral and OpenAI models can be used; this feature lets the operator pick Claude models for a job without any change to the pipeline.

### User Story 1 - Run a job on Claude models (Priority: P1)

An operator sets `llm.provider: anthropic` in a job file and names Claude models for the `fast` and `smart` roles. When the job runs, the pipeline obtains free-text answers and structured (validated) answers from Claude exactly as it would from other providers, and every call reports its token usage and cost.

**Why this priority**: This is the whole point of the feature; everything else supports it.

**Independent Test**: With recorded Claude responses standing in for the service, resolve the `fast` and `smart` roles of a job configured for Anthropic, request one free-text and one structured completion, and check the returned text, the validated structured value and the reported token usage.

**Acceptance Scenarios**:

1. **Given** a job with `llm.provider: anthropic` and valid Claude model ids, **When** the LLM layer resolves a role, **Then** it returns the Anthropic provider and the configured model.
2. **Given** an Anthropic provider, a prompt and an optional system instruction, **When** a free-text completion is requested, **Then** the answer text is returned together with input and output token counts taken from the service's response, and the answer length is bounded by a maximum output size.
3. **Given** an Anthropic provider, a prompt and an expected data shape, **When** a structured completion is requested, **Then** the service is made to answer in exactly that shape (it cannot reply with free text instead) and the validated value is returned together with the token usage.
4. **Given** an expected shape with nested records and lists of records, **When** a structured completion is requested, **Then** the complete nested value is returned validated, with no fields lost or flattened.
5. **Given** a structured answer that does not match the expected shape, **When** it is validated, **Then** exactly one repair attempt is made via the shared repair procedure, and if it still fails, the invalid-output error is raised carrying the summed usage of both calls.

---

### User Story 2 - Failures behave like other providers' failures (Priority: P1)

When Claude rejects or fails a request, the operator and the pipeline see the same typed errors, retries and messages they already know from the Mistral and OpenAI providers, so error handling needs no provider-specific code.

**Why this priority**: Unattended runs depend on predictable failure behaviour; "overloaded/5xx is retried then raised as unavailable" is an explicit acceptance criterion.

**Independent Test**: Replay recorded failure responses (bad key, rate limit with and without a wait hint, server error, "overloaded" response, timeout, rejected request, answer without the expected structured part) and compare the resulting error types and retry behaviour with the other providers' for the equivalent cases.

**Acceptance Scenarios**:

1. **Given** a rejected credential, **When** a call is made, **Then** the "auth" error is raised after one attempt, with no retry.
2. **Given** a rate-limit response with a wait hint, **When** a call is made, **Then** the "rate limit" error carries the hint and the shared retry/backoff logic honours it up to its configured maximum.
3. **Given** a server error (5xx) or an "overloaded" response, **When** a call is made, **Then** the call is retried under the shared retry policy and, once the policy is exhausted, the "provider unavailable" error is raised.
4. **Given** a connection failure, **When** a call is made, **Then** it is handled as in scenario 3; a timeout raises "provider unavailable" immediately (not retried), as with the other providers.
5. **Given** a request the service rejects as invalid, **When** a call is made, **Then** the "invalid request" error is raised without retry.
6. **Given** an account whose credit is exhausted, **When** a call is made, **Then** the "rate limit" error is raised without a wait hint and without retry.
7. **Given** any failure, **When** the error message is produced, **Then** it names provider and model and never contains the API key, prompt text or answer text.

---

### User Story 3 - Verify the setup with `llm test` (Priority: P2)

An operator who has just configured an Anthropic key runs `invio llm test anthropic` and gets a clear pass/fail answer showing that the key, the network path and the chosen model work, including token usage.

**Why this priority**: It is the operator's quick check before scheduling jobs, but the provider is usable without it.

**Independent Test**: Run the command against recorded success and failure responses and check output and exit code; with a real key, run it manually once.

**Acceptance Scenarios**:

1. **Given** a valid Anthropic key, **When** `invio llm test anthropic` runs, **Then** it reports success with the model used and token usage, writes results to stdout and exits 0.
2. **Given** a missing key, **When** the command runs, **Then** it exits with code 2 and a message naming the missing environment variable, and no network call is made.
3. **Given** an invalid key or an unavailable service, **When** the command runs, **Then** it exits with code 1 and the corresponding typed-error message on stderr.

---

### User Story 4 - Provider passes the shared contract (Priority: P2)

A developer runs the shared provider contract tests and the Anthropic provider passes them in the same way the fake, Mistral and OpenAI providers do, so any pipeline test written against the contract is valid for Anthropic.

**Why this priority**: It guards substitutability, but only matters once Story 1 exists.

**Independent Test**: Run the shared contract suite parametrized over the Anthropic provider backed by recorded responses.

**Acceptance Scenarios**:

1. **Given** the shared contract suite, **When** it runs against the Anthropic provider, **Then** all free-text and structured cases pass.
2. **Given** the provider module and its model-registry file are added, **When** the factory loads, **Then** `anthropic` resolves without editing other providers.

---

### Edge Cases

- The service answers with plain text (or several parts) instead of the forced structured part → treated as invalid output and sent through the single repair attempt; never returned as valid.
- The answer is cut off because the maximum output size was reached → raises the "provider unavailable" error for both free text and structured output, with no repair attempt (same as the OpenAI provider's handling of incomplete answers); never returned as valid.
- The service returns an empty answer or a refusal → raises the "provider unavailable" error.
- A free-text call asks for more output than the model allows → the service's rejection is reported as the "invalid request" error, with no retry.
- The response omits token usage → usage is reported as unknown/zero consistently with other providers, and the call does not crash.
- The expected data shape uses constructs (optional fields, unions, enums, recursive references) → the shape is passed to the service without changing what the shared validation accepts.
- A model id in the job is not in the registry or belongs to another provider → resolution fails with the existing registry error before any call is made.
- The API key is missing when the provider is requested → the "auth" error names the environment variable, not its value.
- Concurrent calls from several pipeline nodes share one provider instance safely.
- A call exceeds the application-wide call timeout → "provider unavailable" error.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST offer Anthropic as a selectable provider via `llm.provider: anthropic` in job files, resolvable for both the `fast` and `smart` roles.
- **FR-002**: The Anthropic provider MUST fulfil the shared provider contract: a free-text operation and a structured operation with the same inputs, outputs and usage reporting as other providers.
- **FR-003**: The free-text operation MUST pass the system instruction separately from the user prompt, bound the answer with a maximum output size, and return the answer text and the input/output token counts reported by the service.
- **FR-004**: The structured operation MUST force the service to answer through a single tool call whose input description is derived from the expected data shape, so that free-text replies are not possible, and MUST validate the tool input against that shape.
- **FR-005**: Structured output MUST work for nested records and lists of records.
- **FR-006**: The structured operation MUST use the shared repair procedure: validate, retry once with the validation error appended, then raise the invalid-output error; usage MUST be the sum over all calls made. An answer cut off by the output-length limit MUST NOT be repaired; it raises the "provider unavailable" error.
- **FR-007**: Service and transport failures MUST be mapped to the shared typed errors (auth, rate limit incl. wait hint, invalid request, unavailable, invalid output) with the same classification as the Mistral and OpenAI providers for equivalent situations; server errors and "overloaded" responses map to "provider unavailable". A rejection meaning the account's credit is exhausted (reported by the service as a billing error, or in its older form as a bad request about the credit balance) MUST be recognised and reported as the "rate limit" error without a wait hint, and MUST NOT be retried, matching the OpenAI provider's exhausted-quota handling.
- **FR-008**: Transient errors (rate limit, 5xx, overloaded, connection failures) MUST be retried with the shared retry/backoff logic, not a provider-specific copy, and raised as the mapped error once exhausted; permanent errors (auth, invalid request, exhausted credit) MUST NOT be retried.
- **FR-009**: Error messages, logs and representations MUST NOT contain the API key, prompt text or answer text, and MUST name provider and model.
- **FR-010**: Every call MUST be bounded by the application-wide call timeout setting.
- **FR-011**: The provider MUST self-register with the factory and ship its own model-registry entries listing its models with price per 1M input/output tokens, context window and maximum output size; no model id or model limit MUST be hard-coded outside registry files and job configs.
- **FR-011a**: The registry MUST accept an optional per-model maximum output size (a positive whole number of tokens). Existing registry files without it MUST stay valid and unchanged. Every Anthropic entry MUST define it.
- **FR-011b**: Structured calls MUST use the model's registered maximum output size as their output-length limit. Free-text calls MUST use the limit the caller passes. A missing maximum for an Anthropic model MUST be reported as a configuration error when the provider or model is resolved, before any call is made.
- **FR-012**: Requesting the provider without a configured API key MUST raise the "auth" error naming the environment variable.
- **FR-013**: `invio llm test anthropic` MUST perform a minimal live check and report success (model, usage) or the typed failure, using stdout for results, stderr for diagnostics, and the existing exit codes (1 provider call failed, 2 configuration error). The command is already provider-generic and checks free text only; it MUST NOT change beyond `anthropic` resolving as a provider.
- **FR-014**: The existing structured per-call log line (provider, model, tokens, cost, duration, repair flag) MUST be emitted for Anthropic calls like for other providers.
- **FR-015**: Automated tests MUST cover all acceptance criteria and the failure paths using recorded HTTP fixtures, with no network access or real key required.

### Key Entities

- **Anthropic provider**: A registered implementation of the provider contract for the Anthropic service; holds credentials, timeout and retry policy.
- **Model registry entry**: Per-model record (id, provider, price per 1M input and output tokens, context window, maximum output size) for Claude models; the maximum output size is optional for other providers.
- **Usage**: Input and output token counts of a call.
- **Typed LLM errors**: Auth, rate limit, invalid request, unavailable, invalid output.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can switch an existing job from another provider to Anthropic by changing only the job's LLM configuration, with zero pipeline or code changes.
- **SC-002**: 100% of the shared provider contract tests pass for the Anthropic provider.
- **SC-003**: Structured completions for a shape with nested records and lists return the full validated value in 100% of recorded-response test cases.
- **SC-004**: For each failure category exercised against the other providers (auth, rate limit, server error, overloaded, timeout, rejected request, malformed answer), Anthropic yields the same error type and retry behaviour — verified by an equivalent test for each category.
- **SC-005**: `invio llm test anthropic` gives an operator a pass/fail answer in a single command and under the call timeout, with a correct exit code in 100% of tested cases.
- **SC-006**: The full test suite for the provider runs offline, without a real key, and deterministically.
- **SC-007**: No test or log output contains a secret or prompt/answer text.

## Assumptions

- Issue #7 (provider contract, factory, registry, repair helper, typed errors, retry) is delivered and is the foundation; the Mistral and OpenAI providers are the behavioural reference.
- The API key comes from the same settings/environment mechanism as other providers' keys; the settings and the job schema already contain `anthropic`.
- The structured-output technique (forced single tool call) is specified by the issue; its details belong to the plan.
- The per-model maximum output size values come from Anthropic's published model limits at the time of implementation.
- Streaming, extended thinking, prompt caching, image input and multi-tool use are out of scope.
- The registry lists Claude Haiku 4.5 (fast) and Claude Sonnet 4.6 (smart). Both accept forced tool choice and temperature 0. Newer models that reject either are out of scope until the provider contract changes. Operators can extend the registry only with models that accept both.
- Cloud-hosted Claude endpoints (e.g. via other vendors) are out of scope.
