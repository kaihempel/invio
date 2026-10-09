# Feature Specification: Google (Gemini) Provider

**Feature Branch**: `gh-issue-32` (the constitution's naming; the issue's suggested `issue/32-add-google-gemini-provider` is superseded)

**Created**: 2026-10-09

**Status**: Draft

**Input**: User description: "GitHub issue #32: Add Google (Gemini) LLM provider. Google models become selectable via `llm.provider: google`. Requirements: official Google Gen AI client; free-text completion with a system instruction and generation settings that reports token usage; structured completion that asks the service for JSON matching a described schema, validates the result and reuses the shared repair helper (some schema constructs may need flattening first); errors and safety blocks mapped to the common typed errors (blocked responses → invalid-output error with the block reason); registration in the provider factory and model registry; extension of the `llm test` command. Acceptance criteria: provider passes the shared contract tests; the nested schemas used by the pipeline (relevance result, item summary) work; safety-blocked responses produce a descriptive error, not a crash. Depends on #7 (provider abstraction)."

## Clarifications

### Session 2026-10-09

- Q: How should the provider handle Gemini's internal "thinking", which counts against the output-length limit and could leave no room for the answer? → A: Set thinking to the lowest level each model allows (off where possible), stored per model in the registry; the caller's output limit then applies to the visible answer only.
- Q: The pipeline always sends temperature 0, but Google advises against lowering temperature on its newest Gemini generation. Which models are registered and how is temperature handled? → A: Register current-generation fast and smart models; a per-model registry flag forces the model's default temperature (ignoring the caller's value) for models that degrade at low temperature; other models use the caller's temperature.

## User Scenarios & Testing *(mandatory)*

The "users" are the operator who writes job files and runs the `invio` CLI, and the pipeline nodes that call the LLM layer. Today Mistral, OpenAI and Anthropic models can be used; this feature lets the operator pick Gemini models for a job without any change to the pipeline. The job schema and settings already accept `google` and a Google API key; only the provider itself is missing.

### User Story 1 - Run a job on Gemini models (Priority: P1)

An operator sets `llm.provider: google` in a job file and names Gemini models for the `fast` and `smart` roles. When the job runs, the pipeline obtains free-text answers and structured (validated) answers from Gemini exactly as it would from other providers, and every call reports its token usage and cost.

**Why this priority**: This is the whole point of the feature; everything else supports it.

**Independent Test**: With recorded Gemini responses standing in for the service, resolve the `fast` and `smart` roles of a job configured for Google, request one free-text and one structured completion, and check the returned text, the validated structured value and the reported token usage.

**Acceptance Scenarios**:

1. **Given** a job with `llm.provider: google` and valid Gemini model ids, **When** the LLM layer resolves a role, **Then** it returns the Google provider and the configured model.
2. **Given** a Google provider, a prompt and an optional system instruction, **When** a free-text completion is requested, **Then** the answer text is returned together with input and output token counts taken from the service's usage report, and the caller's output-length limit is applied; the caller's temperature is applied unless the model's registry entry says to keep the model's default temperature.
3. **Given** a Google provider, a prompt and an expected data shape, **When** a structured completion is requested, **Then** the service is asked to answer as JSON in that shape and the validated value is returned together with the token usage.
4. **Given** the pipeline's relevance result (bounded score, reason, list of key points) and item summary (headline, 3–6 bullets, why-relevant) shapes, **When** a structured completion is requested for each, **Then** the complete value is returned validated, with no fields lost, and all constraints the service cannot express (value ranges, list lengths, no extra fields, single-line headline) are still enforced by validation.
5. **Given** an expected shape with nested records, lists of records and shared sub-definitions, **When** a structured completion is requested, **Then** the shape is rewritten into a form the service accepts (references inlined, unsupported keywords dropped) and the complete nested value is returned validated.
6. **Given** a structured answer that does not match the expected shape, **When** it is validated, **Then** exactly one repair attempt is made via the shared repair procedure, and if it still fails, the invalid-output error is raised carrying the summed usage of both calls.

---

### User Story 2 - Safety blocks and failures behave predictably (Priority: P1)

When Gemini blocks a prompt or an answer on safety (or similar policy) grounds, or rejects or fails a request, the operator and the pipeline see a descriptive typed error — never a crash — and the same retries and messages they already know from the other providers, so error handling needs no provider-specific code.

**Why this priority**: "Safety-blocked responses produce a descriptive error, not a crash" is an explicit acceptance criterion, and unattended runs depend on predictable failure behaviour.

**Independent Test**: Replay recorded responses for a blocked prompt, a blocked answer, a bad key, a rate limit, an exhausted quota, a server error, a timeout, a rejected request, a cut-off answer and an empty answer, and compare the resulting error types and retry behaviour with the other providers' for the equivalent cases.

**Acceptance Scenarios**:

1. **Given** the service blocks the prompt itself, **When** a free-text or structured call is made, **Then** the invalid-output error is raised naming the block reason (e.g. safety, prohibited content, blocklist) and, where reported, the offending category; no retry and no repair attempt is made; the usage of the call is attached.
2. **Given** the service stops the answer for safety, recitation or another policy reason, **When** a free-text or structured call is made, **Then** the invalid-output error is raised naming that stop reason, without retry or repair, and partial answer text is never returned as valid.
3. **Given** a rejected credential, **When** a call is made, **Then** the "auth" error is raised after one attempt, with no retry.
4. **Given** a rate-limit response, **When** a call is made, **Then** the "rate limit" error is raised, carries the service's wait hint when one is provided, and the shared retry/backoff logic honours it up to its configured maximum.
5. **Given** a response saying the project's quota is exhausted (not merely a per-minute limit), **When** a call is made, **Then** the quota-exhausted variant of the "rate limit" error is raised without retry, as for the other providers.
6. **Given** a server error (5xx), service-overloaded or connection failure, **When** a call is made, **Then** the call is retried under the shared retry policy and, once exhausted, the "provider unavailable" error is raised; a timeout raises "provider unavailable" immediately.
7. **Given** a request the service rejects as invalid (including an unknown model), **When** a call is made, **Then** the "invalid request" error is raised without retry.
8. **Given** any failure, **When** the error message is produced, **Then** it names provider and model and never contains the API key, prompt text or answer text.

---

### User Story 3 - Verify the setup with `llm test` (Priority: P2)

An operator who has just configured a Google API key runs `invio llm test google` and gets a clear pass/fail answer showing that the key, the network path and the chosen model work, including token usage.

**Why this priority**: It is the operator's quick check before scheduling jobs, but the provider is usable without it.

**Independent Test**: Run the command against recorded success and failure responses and check output and exit code; with a real key, run it manually once.

**Acceptance Scenarios**:

1. **Given** a valid Google key, **When** `invio llm test google` runs, **Then** it reports success with the model used and token usage, writes results to stdout and exits 0.
2. **Given** a missing key, **When** the command runs, **Then** it exits with code 2 and a message naming the missing environment variable, and no network call is made.
3. **Given** an invalid key, an unavailable service or a safety block, **When** the command runs, **Then** it exits with code 1 and the corresponding typed-error message on stderr.

---

### User Story 4 - Provider passes the shared contract (Priority: P2)

A developer runs the shared provider contract tests and the Google provider passes them in the same way the fake, Mistral, OpenAI and Anthropic providers do, so any pipeline test written against the contract is valid for Google.

**Why this priority**: It guards substitutability, but only matters once Story 1 exists.

**Independent Test**: Run the shared contract suite parametrized over the Google provider backed by recorded responses.

**Acceptance Scenarios**:

1. **Given** the shared contract suite, **When** it runs against the Google provider, **Then** all free-text, structured and error cases pass.
2. **Given** the provider module and its model-registry file are added, **When** the factory loads, **Then** `google` resolves without editing other providers.

---

### Edge Cases

- The answer is cut off because the output-length limit was reached → "provider unavailable" for both free text and structured output, no repair attempt (same as the OpenAI and Anthropic providers); never returned as valid.
- The service returns no candidate, an empty answer or only non-text parts → "provider unavailable" for free text; for structured output an empty or non-JSON answer goes through the single repair attempt like any other invalid output.
- The structured answer is wrapped in a code fence or has surrounding whitespace → handled by the shared validation (fence stripping) like for other providers.
- Expected shapes with references to shared sub-definitions, optional fields, enums or unions → rewritten into a form the service accepts; a shape that cannot be expressed at all (e.g. a recursive reference) is rejected as a configuration error before any call is made, not sent to the service.
- Constraints the service does not support (string length, patterns, constants, defaults, exclusive bounds, custom validators) → dropped from the shape sent to the service but still enforced by local validation; violations go through the repair path. Constraints the service documents (numeric ranges, list length bounds, "no extra fields", enums) are sent unchanged.
- The response omits usage metadata or some of its counts → missing counts are reported as zero, consistently with other providers, and the call does not crash.
- The service reports additional token kinds (e.g. internal "thinking" tokens) → these are counted as output tokens, so cost reporting is not understated.
- A model flagged to keep its default temperature receives a call with temperature 0 → the caller's value is ignored and no temperature is sent; answers may vary slightly between runs, which the pipeline tolerates because every structured answer is validated.
- A model still thinks a little at its lowest thinking level (whether or not it can switch thinking off) → the free-text call's output-length limit is raised by that model's registered thinking allowance so the visible answer keeps the caller's full limit; `invio llm test google` (5-token answer limit) passes on every registered model.
- A blocked call still consumed input tokens → the usage reported by the service is attached to the error so cost tracking stays complete.
- The model id is unknown to the registry → configuration error raised by the shared registry before any call.
- The API key is missing → "auth" error naming the environment variable, raised when the provider is requested.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST offer a provider named `google`, selectable via `llm.provider: google`, that fulfils the existing provider contract (free-text completion, structured completion, close).
- **FR-002**: The provider MUST use Google's official Gen AI client library, with the API key taken from the existing settings (`INVIO_GOOGLE_API_KEY`), and MUST close its client when closed.
- **FR-003**: Free-text completion MUST send the system instruction (when given) separately from the user content, apply the temperature rule of FR-013c and the caller's output-length limit, and return the answer text with input and output token counts taken from the service's usage report.
- **FR-004**: Structured completion MUST request a JSON answer described by the expected shape, validate it with the shared validation, and on failure make exactly one repair attempt via the shared repair procedure; usage of both calls MUST be summed.
- **FR-005**: Before a structured request, the expected shape MUST be converted into a form the service accepts: references to shared sub-definitions inlined, keywords the service rejects removed. Validation MUST still use the full original shape, so no constraint is lost. Shapes that cannot be converted MUST raise a configuration error before any call is made.
- **FR-006**: The relevance-result and item-summary shapes used by the pipeline MUST work end to end with structured completion.
- **FR-007**: A prompt blocked by the service, or an answer stopped for safety, recitation, prohibited content, blocklist, sensitive personal data or another policy reason, MUST raise the invalid-output error whose message names the block or stop reason (and the safety category when reported). Such blocks MUST NOT be retried and MUST NOT go through the repair attempt. The call's reported usage MUST be attached.
- **FR-008**: An answer cut off at the output-length limit MUST raise "provider unavailable", without repair, for free text and structured output.
- **FR-009**: Service errors MUST map to the common typed errors: rejected credential/permission → "auth"; rate limit → "rate limit" with the wait hint when provided; exhausted quota → the quota-exhausted "rate limit" variant; invalid request or unknown model → "invalid request"; server error, overload, connection failure or timeout → "provider unavailable".
- **FR-010**: Transient errors (rate limit, 5xx, overload, connection failures) MUST be retried with the shared retry/backoff logic, not a provider-specific copy; permanent errors (auth, invalid request, exhausted quota, safety block) MUST NOT be retried. The client library's own retries MUST be disabled so retries are not multiplied.
- **FR-011**: Error messages, logs and representations MUST NOT contain the API key, prompt text or answer text, and MUST name provider and model.
- **FR-012**: Every call MUST be bounded by the application-wide call timeout setting.
- **FR-013**: The provider MUST self-register with the factory and ship its own model-registry file listing Gemini models (one fast, one smart) with price per 1M input/output tokens and context window; no model id or price MUST be hard-coded outside registry files and job configs.
- **FR-013a**: Every Google registry entry MUST define the model's thinking setting: the lowest thinking level the model allows (off where the model permits it) and a thinking token allowance (a positive whole number), because even the lowest level may think a little on some models. The setting is optional in the registry schema, so existing registry files stay valid and unchanged; a Google model without it MUST be reported as a configuration error when the provider or model is resolved, before any call.
- **FR-013b**: Every call MUST request the model's registered thinking level. The visible answer MUST keep the caller's full output-length limit (free text) or the model's default limit (structured): any thinking allowance is added on top of it, never taken from it. Thinking tokens reported by the service MUST be counted as output tokens in usage and cost.
- **FR-013c**: A Google registry entry MAY carry a flag saying the model's default temperature must be kept. For a flagged model, free-text and structured calls MUST send no temperature (the service default applies) whatever the caller passes; for other models the caller's temperature MUST be sent unchanged. The flag defaults to "off" and is ignored by other providers, so existing registry files stay valid. Whether a model is flagged follows Google's published guidance for that model.
- **FR-014**: `invio llm test google` MUST perform a minimal live check and report success (model, usage) or the typed failure, using stdout for results, stderr for diagnostics, and the existing exit codes (1 provider call failed, 2 configuration error). The command is already provider-generic; it MUST NOT change beyond `google` resolving as a provider.
- **FR-015**: The existing structured per-call log line (provider, model, tokens, cost, duration, repair flag) MUST be emitted for Google calls like for other providers.
- **FR-016**: Automated tests MUST cover all acceptance criteria and the failure paths, including both kinds of safety block, using recorded responses with no network access or real key required. An optional live test, skipped without a key, MAY exist like for the other providers.

### Key Entities

- **Google provider**: A registered implementation of the provider contract for the Gemini service; holds credentials, timeout and retry policy.
- **Model registry entry**: Per-model record (id, provider, price per 1M input and output tokens, context window) plus, for Gemini models, the thinking setting (lowest thinking level and thinking token allowance) and the optional keep-default-temperature flag.
- **Service-compatible shape**: The expected data shape rewritten for the service (references inlined, unsupported keywords removed); used only for the request, never for validation.
- **Block reason**: The service's explanation for refusing a prompt or stopping an answer (reason and, where reported, safety category); carried in the invalid-output error message.
- **Usage**: Input and output token counts of a call.
- **Typed LLM errors**: Auth, rate limit (incl. quota exhausted), invalid request, unavailable, invalid output.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can switch an existing job from another provider to Google by changing only the job's LLM configuration, with zero pipeline or code changes.
- **SC-002**: 100% of the shared provider contract tests pass for the Google provider.
- **SC-003**: Structured completions for the relevance-result and item-summary shapes, and for a test shape with nested records, lists and shared sub-definitions, return the full validated value in 100% of recorded-response test cases.
- **SC-004**: 100% of tested safety-blocked responses (blocked prompt and blocked answer) end in the invalid-output error naming the reason; none ends in an unhandled exception.
- **SC-005**: For each failure category exercised against the other providers (auth, rate limit, exhausted quota, server error, timeout, rejected request, malformed answer, cut-off answer), Google yields the same error type and retry behaviour — verified by an equivalent test per category.
- **SC-006**: `invio llm test google` gives an operator a pass/fail answer in a single command and under the call timeout, with a correct exit code in 100% of tested cases.
- **SC-007**: The full test suite for the provider runs offline, without a real key, and deterministically.
- **SC-008**: No test or log output contains a secret or prompt/answer text.

## Assumptions

- Issue #7 (provider contract, factory, registry, repair helper, typed errors, retry) is delivered; the Mistral, OpenAI and Anthropic providers are the behavioural reference, and the shared error classification and test harness from #31 are reused.
- The issue's `scout/llm/google.py` path refers to the project's former name; the provider lives next to the existing providers in the `invio` LLM package.
- The API key comes from the existing settings mechanism; settings and the job schema already contain `google`.
- Only the Gemini Developer API with an API key is in scope; Vertex AI, service-account credentials and regional endpoints are out of scope.
- An "exhausted quota" is a rate-limit response whose quota details name a daily quota (a quota id containing `PerDay`) or a quota of zero for the account's tier; it is not retried. Every other rate-limit response, including those without quota details, is treated as a temporary limit and retried with the shared backoff, honouring the service's suggested delay.
- Safety thresholds are left at the service defaults; the provider does not loosen or tighten them.
- Treating blocks as invalid output without repair follows the issue; a repair attempt would resend the same blocked content.
- The registered models are the current-generation fast and smart Gemini models (generally available where possible) that support JSON structured output and system instructions; exact ids and prices are taken from Google's published model and pricing pages at implementation time and recorded with a verification date in the registry file.
- Google bills internal "thinking" tokens at the output price; the lowest-thinking setting (Clarifications) keeps them minimal. The exact level names and allowances come from Google's model documentation at implementation time.
- Streaming, tool/function calling, multimodal input, context caching and grounding are out of scope.
