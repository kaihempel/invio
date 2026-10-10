# CLI Contract: `invio job suggest`

## Synopsis

```text
invio job suggest TOPIC [--language CODE] [--provider NAME] [--model ID] [--yaml]
```

| Argument / option | Default | Rules |
|---|---|---|
| `TOPIC` | required | 1–500 characters after trimming whitespace |
| `--language CODE` | `en` | ISO 639-1 code (case-insensitive input, stored lower-case) |
| `--provider NAME` | first registered provider (alphabetical) with credentials and ≥ 1 registered model | must be a registered provider with its API key set |
| `--model ID` | most expensive registered model of the provider (input + output price per Mtok; ties: lowest id) | must be registered for the provider |
| `--yaml` | off | print YAML once and exit; no questions |

Usage and configuration checks happen in this order, all **before** any model call:
topic → language → provider (registered, credentials) → model (registered) → registry readable.

## Modes

**Non-interactive** (`--yaml`, or stdin/stdout not a terminal): one model call, then stdout:

```yaml
# Suggested sources: <hint line 1>
# <hint line 2 …>
search:
  keywords:
    any:
    - Wärmepumpe
    - heat pump
    all: []
    exclude:
    - Stellenangebot
  semantic_description: |-
    Relevant: …
    Not relevant: …
```

Nothing is saved; no question is asked; no database access.

**Interactive**: stderr line `Asking <provider>/<model> …`; then the preview on stdout:

```text
Suggestion (anthropic / claude-sonnet-4-6, language: de)

  Keywords — any of:   Wärmepumpe, heat pump, Luft-Wasser-Wärmepumpe, …
  Keywords — all of:   (none)
  Keywords — exclude:  Stellenangebot, job, Gebrauchtmarkt, …
  Description:
    Relevant: …
    Not relevant: …
  Suggested sources:   Fachportale der Wohnungswirtschaft, …

  ! 'heat pump' removed from exclude: it is also an include keyword
```

then `? What next?` with the choices, in this order:

| Choice | Effect |
|---|---|
| `Create job` | Starts the job wizard with keywords, description, language, provider and smart model prefilled; the sources hint is shown at the sources step. The job is saved only after the wizard's `Save this job?` is confirmed. |
| `Refine` | `? How should it change?` (non-empty) → new model call with topic, current suggestion (incl. edits) and remark → new preview. On failure: `Error: <provider>/<model>: <ErrorClass>: <message>` on stderr, previous preview again. |
| `Edit a field` | `? Which field?` (`Keywords — any of`, `Keywords — all of`, `Keywords — exclude`, `Description`, `Suggested sources`) → text prompt with the current value as default (lists comma-separated, description and sources hint multi-line); deleting the text clears a keyword list; invalid input (e.g. an empty description) is rejected inline and asked again → new preview. |
| `Print YAML` | Prints the YAML block of the non-interactive mode to stdout, exit 0. |
| `Discard` | Prints `Nothing saved.` to stderr, exit 0. |

After the session, stderr: `suggestions used N requests, X in / Y out tokens, ~$Z`
(cost omitted if unknown).

## Exit codes

| Code | When |
|---|---|
| 0 | YAML printed; job created; discarded |
| 1 | Model call failed (provider error, invalid answer after one repair; stderr `Error: <provider>/<model>: <ErrorClass>: <message>`) in non-interactive mode or on the first call; aborted (Ctrl+C / EOF → `aborted; nothing saved`); wizard save declined (`job not created`); job name exists (wizard handles re-ask) |
| 2 | Empty / over-long topic; unknown language code; unknown provider; provider without credentials; no provider with credentials; model not registered; no models for provider; model registry unreadable; missing database setting when creating a job |

## Streams

- stdout: preview, YAML, `created job '<name>' (next run: …)`.
- stderr: prompts (questionary), warnings, errors, `llm.call` JSON log lines, usage summary.
- Never printed: API keys, the system prompt.

## Changes to `invio job create` (wizard)

A new question after the description: `? Summary language (ISO 639-1 code) [en]`. Invalid codes
are rejected inline. All other questions and their order are unchanged.
