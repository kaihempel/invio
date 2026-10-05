# Feature Specification: Mistral LLM Provider

**Feature Branch**: `gh-issue-8` (created from `main`; follows the `gh-issue-6`/`gh-issue-7`
PR convention instead of the issue's suggested `issue/08-add-mistral-provider`)

**Created**: 2026-10-04

**Status**: Draft

**Input**: User description: "GitHub issue #8 — Add Mistral provider (short name: gh-issue-8). Depends on #7 (LLM provider abstraction, model registry, usage tracking — already merged in src/invio/llm: LLMProvider protocol, Usage, LLMAuthError/LLMRateLimitError/LLMUnavailableError/LLMInvalidOutputError, structured_with_repair, with_timeout, require_api_key, register_provider factory). Mistral is the first production LLM provider. It must support plain text completion and schema-constrained (structured) output, and map SDK/HTTP errors to the common error types. Requirements: (1) a Mistral provider module in the existing LLM package (src/invio/llm/mistral.py; the issue text says scout/llm but the package is invio.llm) using the official mistralai SDK; (2) `complete` uses chat completion and returns text plus correct token Usage read from the response; (3) `complete_structured` uses the JSON-schema response_format plus the structured_with_repair helper and returns a validated Pydantic object; (4) error mapping: HTTP 401/403 -> LLMAuthError, 429 -> LLMRateLimitError (honor Retry-After), 5xx and timeouts -> LLMUnavailableError; (5) configurable request timeout and retry with exponential backoff on 429/5xx (max 3 retries), after exhaustion raise the mapped error; (6) register the provider in the factory; add a `invio llm test mistral` CLI command (CLI skeleton from #9) performing a minimal live call; (7) tests with recorded HTTP fixtures (e.g. respx) that run fully offline, plus one optional live test skipped when no API key is set. Acceptance criteria: complete returns text and correct Usage; complete_structured returns a validated Pydantic object; 429 responses are retried with backoff then raised as LLMRateLimitError; invalid API key raises LLMAuthError; tests run offline."

## Clarifications

### Session 2026-10-04

- Q: Should the live Mistral test run only when explicitly requested, or whenever a Mistral API key happens to be configured? → A: Opt-in — it runs only when selected via a `live` test marker AND a key is configured; excluded from the default test run, skipped if selected without a key.
- Q: Should Mistral requests that time out or fail to connect be retried with the same backoff as 429/5xx, or fail immediately? → A: Retry connection failures (refused, DNS, reset before a response) with the 429/5xx backoff; timeouts fail immediately without retry.
- Q: Should the shipped Mistral model list use moving aliases (`*-latest`) or pinned version ids? → A: Pinned version ids only; updating to a newer model is a data-only edit of the registry file.
- Q: Which error should the pipeline receive when Mistral rejects a request as malformed (HTTP 400/404/422)? → A: A new shared "invalid request" error type (subclass of the common LLM error base in the LLM layer), never retried, naming HTTP status and Mistral's error message.
- Q: When Mistral's `Retry-After` asks for more than 60 seconds, should invio fail immediately or wait as requested? → A: Cap at 60 s (configurable at construction); a longer requested wait raises the rate-limit error immediately, carrying the requested wait in seconds.

## User Scenarios & Testing *(mandatory)*

The "users" of this feature are the research pipeline nodes that call a language model through
the provider-neutral LLM layer (#7), and the operator who configures a job with
`llm.provider: mistral`, runs it unattended and checks from the command line that the Mistral
credential works. Developers benefit from tests that pin Mistral's behaviour down without
network access.

### User Story 1 - Pipeline gets text and token usage from Mistral (Priority: P1)

A job configured with Mistral as its provider resolves a role (`fast` or `smart`) to the
Mistral provider and a Mistral model. A pipeline node sends a system and a user message with a
temperature and an output-token limit and receives the model's text answer together with the
exact input and output token counts that Mistral reported for the call.

**Why this priority**: Free-text completion is the most basic capability a production provider
must offer; without it no pipeline step can run against a real model.

**Independent Test**: Replay a recorded successful chat-completion response, request a
free-text completion and check that the returned text and the input/output token counts equal
the recorded values, and that the request sent to Mistral contained the given messages, model,
temperature and token limit.

**Acceptance Scenarios**:

1. **Given** a configured Mistral credential and a recorded successful response, **When** a
   free-text completion is requested, **Then** the answer text and a usage with the reported
   input and output token counts (one request) are returned.
2. **Given** a free-text completion request, **When** it is sent, **Then** the request carries
   the system message, the user message, the model id, the temperature and the output-token
   limit exactly as given.
3. **Given** a successful response that omits one or both token counts, **When** it is
   processed, **Then** the missing count is reported as zero and the call still succeeds.
4. **Given** a successful response with no answer text (empty choice list or empty content),
   **When** it is processed, **Then** the call fails with the provider-unavailable error naming
   provider and model, rather than returning an empty answer silently.

---

### User Story 2 - Pipeline gets validated structured data from Mistral (Priority: P1)

A pipeline node asks for an answer matching an expected result shape (for example a relevance
score with a reason). The Mistral provider asks Mistral to answer in that shape, validates the
answer, and — through the shared repair procedure from #7 — makes exactly one repair attempt if
the answer does not fit. The node receives a validated value and the combined token usage.

**Why this priority**: Most pipeline steps (filtering, scoring, summarising) consume structured
data; schema-constrained output is a core requirement of the first production provider.

**Independent Test**: Replay a recorded response whose content matches the expected shape and
check that a validated value is returned; replay an invalid response followed by a valid one
and check that exactly one repair request was made and usage is the sum of both.

**Acceptance Scenarios**:

1. **Given** an expected result shape and a recorded response whose content matches it,
   **When** a structured completion is requested, **Then** a validated value of that shape and
   the reported usage are returned.
2. **Given** a structured completion request, **When** it is sent, **Then** it asks Mistral for
   output constrained to the expected shape's schema (not only a prompt instruction).
3. **Given** a first response that violates the shape and a second response that matches it,
   **When** a structured completion is requested, **Then** exactly one repair request is made
   and the valid value is returned with the usage of both requests (two requests).
4. **Given** two responses that both violate the shape, **When** a structured completion is
   requested, **Then** the call fails with the invalid-output error carrying the final
   validation problem and the usage of both requests.

---

### User Story 3 - Mistral failures surface as the common typed errors, with retries for transient ones (Priority: P1)

When Mistral rejects the credential, rate limits, fails with a server error or does not answer
in time, the pipeline receives the same typed errors it would get from any provider. Transient
failures (rate limiting and server errors) are retried automatically a bounded number of times
with growing waits, honouring Mistral's requested wait when it gives one, before the error is
raised — so short hiccups do not fail an unattended run.

**Why this priority**: Unattended runs must survive brief rate limits and outages and must fail
clearly and uniformly otherwise; later fallback logic depends on these typed errors.

**Independent Test**: Replay recorded 401, 403, 429, 500/502/503 responses and a simulated
timeout; check which typed error is raised, how many requests were made and which waits were
applied (with the wait mechanism faked so tests run instantly).

**Acceptance Scenarios**:

1. **Given** Mistral answers 401 or 403, **When** a completion is requested, **Then** the
   authentication error is raised after exactly one request (no retry), naming the provider and
   the setting to check, without revealing the credential.
2. **Given** Mistral answers 429 to every request, **When** a completion is requested, **Then**
   it is retried 3 times (4 requests in total) with exponentially growing waits, and then the
   rate-limit error is raised.
3. **Given** Mistral answers 429 with a `Retry-After` value within the allowed maximum wait,
   **When** the request is retried, **Then** the wait before the retry is the requested value
   instead of the computed backoff.
4. **Given** Mistral answers 429 or 5xx once and then succeeds, **When** a completion is
   requested, **Then** the successful result is returned and its usage counts only the
   successful request.
5. **Given** Mistral answers 5xx to every request, **When** a completion is requested, **Then**
   it is retried 3 times and then the provider-unavailable error is raised, naming the provider,
   model and last status.
6. **Given** Mistral does not answer within the configured call timeout, **When** a completion
   is requested, **Then** the provider-unavailable error is raised naming provider, model and
   timeout, after exactly one attempt (timeouts are not retried).
7. **Given** Mistral is unreachable (connection refused, DNS failure) on every attempt,
   **When** a completion is requested, **Then** it is retried 3 times with the same backoff as
   5xx and then the provider-unavailable error is raised; **Given** the connection fails once
   and then succeeds, **Then** the successful result is returned.
8. **Given** Mistral answers 400, 404 or 422, **When** a completion is requested, **Then** the
   invalid-request error is raised after exactly one attempt, naming provider, model, HTTP
   status and Mistral's error message.
9. **Given** any of these errors, **When** it is raised or logged, **Then** no credential value
   and no prompt or answer text appears in it.

---

### User Story 4 - Operator checks the Mistral setup from the CLI (Priority: P2)

Before scheduling jobs, the operator runs `invio llm test mistral`. The command makes one
minimal, cheap call to Mistral and reports on stdout whether it worked, which model answered,
the token usage and the duration — or, on failure, a one-line reason (e.g. missing or rejected
credential, rate limit, unavailable) and a non-zero exit code.

**Why this priority**: Valuable for setup and troubleshooting, but pipeline calls (P1) work
without it.

**Independent Test**: Run the command against recorded responses (success, 401, missing
credential) and check stdout/stderr content and exit codes.

**Acceptance Scenarios**:

1. **Given** a valid credential, **When** `invio llm test mistral` is run, **Then** one minimal
   completion is made, stdout shows success, the model used, input/output tokens and duration,
   and the exit code is 0.
2. **Given** no Mistral credential is configured, **When** the command is run, **Then** it
   reports the missing setting by name without making a network request and exits with code 2
   (configuration error).
3. **Given** a rejected credential, a rate limit or an unavailable service, **When** the command
   is run, **Then** it prints a one-line reason naming the error kind and exits non-zero (1).
4. **Given** an explicit model option, **When** the command is run, **Then** that model is
   used; a model not listed for Mistral in the model registry is rejected before any request.
5. **Given** a provider name with no registered provider, **When** `invio llm test <name>` is
   run, **Then** it fails with exit code 2 listing the registered providers.

---

### User Story 5 - Mistral models are known to the registry and resolvable by configuration (Priority: P2)

The Mistral provider ships with a registry file listing the Mistral models invio supports,
with their prices and context windows, so job configurations can name them, roles resolve to
them and call costs are computed. The provider becomes available purely by its own module and
registry file, without editing shared files of the LLM layer.

**Why this priority**: Needed for cost tracking and for role resolution to succeed with real
model ids, but follows directly from the #7 mechanism.

**Independent Test**: Load the shipped registry and check that the Mistral models are present
with valid prices; resolve `fast` and `smart` roles from a job configuration naming Mistral
models; request the provider by name `mistral` and get a Mistral provider.

**Acceptance Scenarios**:

1. **Given** the shipped registry, **When** it is loaded, **Then** it contains at least one
   Mistral model suitable for the `fast` role and one for the `smart` role, each with prices
   and a context window, and every listed id is a pinned version (no `*-latest` alias).
2. **Given** a job configuration with `llm.provider: mistral` and Mistral model ids, **When**
   the roles are resolved, **Then** the Mistral provider and the configured models are
   returned.
3. **Given** the Mistral provider is added, **When** the change is inspected, **Then** no shared
   file of the LLM layer was edited to register it.
4. **Given** a job configuration naming a Mistral alias such as `mistral-small-latest`,
   **When** a role is resolved, **Then** resolution fails with the "model not in registry"
   error from #7.

---

### Edge Cases

- `Retry-After` given as an HTTP date instead of seconds: it is converted to a wait in seconds;
  an unparseable value falls back to the computed backoff.
- `Retry-After` larger than the maximum allowed wait (60 seconds): no further retry is made and
  the rate-limit error is raised immediately, carrying the requested wait so callers can see it.
- A 429 or 5xx on the repair request of a structured completion: that request is retried on its
  own budget; if retries are exhausted the typed provider error is raised, not the
  invalid-output error.
- Other client errors (400, 404 e.g. unknown or retired model, 422 e.g. unsupported schema):
  not retried; raised as the invalid-request error with the HTTP status and Mistral's error
  message (credential-free, no prompt text). Any remaining 4xx status not listed elsewhere is
  treated the same way.
- A response body that is not valid JSON or not shaped like a chat completion: raised as the
  provider-unavailable error, not retried.
- The call timeout applies to each individual request attempt; retries and their waits are not
  counted against it.
- The answer for a structured completion is wrapped in a code fence or has surrounding
  whitespace: handled by the shared repair procedure's parsing, not treated as invalid.
- Credential present but blank: treated as missing (authentication error when the provider is
  requested, per #7).
- Cancellation by the caller while waiting for a retry: the wait is aborted immediately and no
  further request is made.
- The same provider instance is built once and used from two successive event loops (e.g. two
  `asyncio.run` calls): the second call succeeds on fresh connection resources (FR-027).

## Requirements *(mandatory)*

### Functional Requirements

**Provider and registration**

- **FR-001**: The system MUST provide a Mistral provider that fulfils the LLM provider contract
  from #7 (free-text and structured completion), built on Mistral's official client library.
- **FR-002**: The Mistral provider MUST register itself under the name `mistral` from within its
  own module, so the LLM layer discovers it without edits to shared LLM files.
- **FR-003**: Requesting the Mistral provider MUST require the Mistral credential from the
  application settings; a missing or blank credential MUST raise the authentication error at
  request time (behaviour from #7).
- **FR-004**: The system MUST ship a Mistral model registry file listing the supported Mistral
  model ids with input price, output price (USD per 1M tokens) and context window, valid under
  the #7 registry format. Only pinned (dated/versioned) model ids MUST be listed; moving aliases
  such as `*-latest` MUST NOT appear, so the model and price used are reproducible across runs.

**Free-text completion**

- **FR-005**: Free-text completion MUST send one chat-completion request with the system
  message, user message, model, temperature and output-token limit, and return the answer text
  and a usage built from the input and output token counts reported in the response.
- **FR-006**: Missing token counts in a successful response MUST be reported as zero.
- **FR-007**: A successful response without answer text MUST raise the provider-unavailable
  error naming provider and model.

**Structured completion**

- **FR-008**: Structured completion MUST request schema-constrained output from Mistral using
  the expected result shape's JSON schema and MUST validate and repair the answer through the
  shared repair procedure from #7 (at most one repair request).
- **FR-009**: The usage returned by a structured completion (or carried by the invalid-output
  error) MUST be the sum over the successful requests made, including the repair request.

**Error mapping**

- **FR-010**: HTTP 401 and 403 responses MUST raise the authentication error and MUST NOT be
  retried.
- **FR-011**: HTTP 429 responses MUST raise the rate-limit error once retries are exhausted or
  not permitted.
- **FR-012**: HTTP 5xx responses and connection failures MUST raise the provider-unavailable
  error once retries are exhausted; request timeouts MUST raise it immediately (see FR-015).
- **FR-013**: The LLM layer MUST offer a shared invalid-request error type (alongside the
  existing typed errors, deriving from the common LLM error base). HTTP 400, 404, 422 and any
  other 4xx response not covered by FR-010/FR-011 MUST raise it without retry, including the
  HTTP status and Mistral's error message. Malformed response bodies MUST raise the
  provider-unavailable error without retry.
- **FR-014**: Every raised error MUST name the provider and model and MUST NOT contain the
  credential, prompt text or answer text.

**Timeout and retry**

- **FR-015**: Each request attempt MUST be bounded by the existing application call timeout
  setting (`llm_timeout_seconds`, default 60 s); exceeding it raises the provider-unavailable
  error and is not retried.
- **FR-016**: HTTP 429 and 5xx responses and connection failures (connection refused, DNS
  resolution failure, connection reset before a response) MUST be retried up to 3 times (at most 4 attempts per
  request) with exponential backoff (base 1 s, doubling per retry, with random jitter of up to
  ±25 %).
- **FR-017**: For a 429 response carrying `Retry-After` (seconds or HTTP date), the wait before
  the next attempt MUST be the requested wait when it is at most 60 s; if it exceeds 60 s, the
  rate-limit error MUST be raised immediately, carrying the requested wait. The rate-limit
  error MUST expose the provider's requested wait in seconds whenever one was given (absent
  otherwise), including when raised after retries are exhausted.
- **FR-018**: The retry count, backoff base and maximum wait MUST be configurable when the
  provider is constructed (defaults as above), and the waiting mechanism MUST be replaceable so
  tests run without real delays.
- **FR-019**: Each retry MUST be logged as one structured warning line with provider, model,
  attempt number, HTTP status (or failure kind) and the wait applied — metadata only.

**CLI check**

- **FR-020**: The CLI MUST offer `invio llm test <provider>` that, for `mistral`, performs one
  minimal free-text completion (tiny prompt, small output-token limit) and prints on stdout the
  outcome, model, input/output tokens and duration.
- **FR-021**: The command MUST accept an optional model; without it, it MUST use the cheapest
  Mistral model listed in the model registry (no model id hard-coded in code). A model not
  listed for the provider MUST be rejected before any request.
- **FR-022**: The command MUST exit 0 on success, 2 for configuration errors (missing
  credential, unknown provider, unknown model) and 1 for provider errors (authentication
  rejected, rate limited, unavailable, invalid request, invalid output), printing a one-line reason on stderr.
- **FR-023**: The command MUST be added as an auto-discovered command module, without editing
  other CLI files.
- **FR-026**: The command MUST bound each request attempt to the lower of the configured call
  timeout and 20 seconds, so a hanging endpoint cannot keep an interactive check waiting for
  the full pipeline timeout. Retry behaviour is the provider's normal behaviour (FR-016/FR-017).

**Testing**

- **FR-024**: All automated tests for the Mistral provider MUST run offline, using recorded or
  constructed HTTP responses intercepted at the HTTP layer, and MUST NOT require a credential.
- **FR-025**: One optional live test MUST make a real minimal call to Mistral. It MUST be
  opt-in: it carries a `live` test marker, is excluded from the default test run (so a plain
  test run never contacts Mistral, even when a credential is configured locally), runs only
  when the `live` marker is explicitly selected, and is skipped with a clear reason when
  selected without a Mistral credential.

**Lifecycle**

- **FR-027**: A Mistral provider instance MUST remain usable across separate event loops (for
  example successive `asyncio.run` calls by the CLI or the scheduler): its connection resources
  MUST be bound to the event loop that uses them and recreated when used from a new loop, never
  failing with a closed-loop error.

### Key Entities

- **Mistral provider**: The `mistral`-named provider implementing free-text and structured
  completion against Mistral's chat-completion service; holds the credential (as a secret),
  call timeout and retry policy.
- **Retry policy**: Maximum retries (3), backoff base (1 s), jitter, maximum wait honoured from
  `Retry-After` (60 s); applies to 429, 5xx and connection failures — not to timeouts or
  other client errors.
- **Mistral registry file**: The provider's model list (model id, prices per 1M input/output
  tokens, context window) in the #7 registry format.
- **Usage**: Input/output token counts and request count reported per call (from #7).
- **Invalid-request error**: New shared typed error meaning "the provider rejected the request
  itself" (malformed input, unknown/retired model, unsupported schema); carries provider, model
  and HTTP status; distinct from provider-unavailable so fallback logic never treats a bug as an
  outage.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For every recorded successful response in the test suite, the returned text and
  token counts match the recorded values exactly.
- **SC-002**: For every recorded valid structured response, a validated value of the expected
  shape is returned; for invalid-then-valid responses exactly one repair request is made.
- **SC-003**: A persistently rate-limited call makes exactly 4 attempts with growing waits
  before failing with the rate-limit error; a persistent server error behaves the same with the
  provider-unavailable error.
- **SC-004**: A rejected credential fails with the authentication error after exactly 1 attempt
  in 100 % of tested cases.
- **SC-005**: The default test run passes with network access disabled — with or without a
  credential configured — makes zero requests to Mistral, and the Mistral tests complete in
  under 10 seconds (no real waiting).
- **SC-006**: An operator can verify a Mistral credential with a single command that finishes
  in under 30 seconds whenever Mistral is reachable and not rate limiting (configuration errors
  in under 2 seconds, without any request), and whose exit code alone tells success,
  configuration error and provider error apart. When Mistral is slow, failing or rate limiting,
  the command still ends within a bounded time (per-attempt limit of 20 seconds, FR-026).
- **SC-007**: No tested error message or log line contains the credential, prompt text or
  answer text.

## Assumptions

- **Package location**: The issue refers to `scout/llm/mistral.py`; the project's package is
  `invio`, so the provider lives at `src/invio/llm/mistral.py` and its registry file at
  `src/invio/llm/models.d/mistral.yaml`.
- **Dependency on #7**: The provider contract, `Usage`, typed errors, `structured_with_repair`,
  `with_timeout`, `require_api_key`, `register_provider` and the model registry are already
  merged and are reused unchanged, except for two provider-neutral additions to the shared
  error set: the new invalid-request error type, and an optional "requested wait" (seconds) on
  the existing rate-limit error. Neither is Mistral-specific.
- **CLI skeleton (#9)**: The `invio` CLI with auto-discovered command modules
  (`src/invio/cli/commands/`) already exists; `invio llm test` is added as a new `llm` command
  module. Exit-code conventions follow the constitution (configuration errors → 2).
- **Credential and timeout settings**: The existing settings `INVIO_MISTRAL_API_KEY` and
  `INVIO_LLM_TIMEOUT_SECONDS` are used; no new environment variables are introduced. Retry
  parameters are constructor options with defaults, not operator settings.
- **Retry scope**: 429, 5xx and connection failures are retried (connection failures are
  cheap, fast and usually transient). Timeouts are not retried, to keep the worst-case duration
  of a call bounded (4 × the timeout plus waits would otherwise apply); the pipeline's later
  fallback logic handles them.
- **"Max 3"** means 3 retries after the first attempt (4 attempts in total).
- **Retry ownership**: Retries are performed by invio's provider code (so behaviour and logging
  are uniform and testable); any built-in retrying of the client library is disabled.
- **Fallback provider, token budgets and persisting usage records** remain out of scope
  (separate issues).
- **Registry content**: The shipped Mistral registry file lists pinned model versions only;
  prices reflect Mistral's public price list at implementation time. Moving to a newer version
  or updating prices is a data-only change. A retired pinned id surfaces as the non-retried
  invalid-request error naming the model.
- **New dependencies**: Mistral's official client library (runtime) and an HTTP mocking library
  for tests (dev) are added and justified in the PR description, per the constitution.
