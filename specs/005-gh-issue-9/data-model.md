# Data Model: CLI Job Management with Interactive Creation Wizard

No database schema change. The feature reads and writes the existing `jobs` table through
`JobService` and reads `runs` for the last status. All new types are in-memory values.

## Existing entities (reused unchanged)

| Entity | Source | Used for |
|---|---|---|
| `JobConfig` (+ `ScheduleConfig`, `NotificationConfig`, `SourceConfig` union, `SearchConfig`, `KeywordsConfig`, `LLMConfig`, `LLMModels`, `LimitsConfig`) | `invio.config.job` | Built by the wizard and parsed from files or editor text; the single contract (constitution I) |
| `JobRecord` | `invio.services.jobs` | Result of create/update/enable/disable/import |
| `RunStatus` (`running`, `succeeded`, `partial`, `failed`) | `invio.domain` | Last status column |
| `ModelRegistry` / `ModelInfo` | `invio.llm.registry` | Model choices in the wizard |

## New service-level value: `JobSummary` (`invio.services.jobs`)

One row of `invio job list`. Unlike `JobRecord`, it can describe a job whose stored config no
longer validates.

| Field | Type | Rules |
|---|---|---|
| `name` | `str` | Unique job name |
| `enabled` | `bool` | |
| `next_run_at` | `datetime \| None` | UTC; `None` when disabled |
| `config` | `JobConfig \| None` | `None` exactly when `config_errors` is non-empty |
| `config_errors` | `list[str]` | `<dotted.loc>: <message>` lines; empty for valid jobs |
| `last_run_status` | `RunStatus \| None` | Status of the newest run (by `started_at`, then `id`); `None` = never run |

Derived display status (CLI only, highest precedence first):
`invalid config` → `last_run_status.value` → `never run`.

## New CLI-level values (`invio.cli.wizard` / `invio.cli.source_check`)

### `CheckResult`

Result of checking one source during the wizard (FR-007 to FR-009).

| Field | Type | Meaning |
|---|---|---|
| `reachable` | `bool` | Final status 2xx/3xx within the timeout |
| `status` | `int \| None` | Final HTTP status, if a response arrived |
| `reason` | `str \| None` | Human-readable problem (`timeout after 10s`, `HTTP 404`, `DNS lookup failed`, …) |
| `is_feed` | `bool \| None` | Only for `rss`: root element is RSS/RDF/Atom; `None` when not checked |

Decision rules in the wizard:
- URL syntax invalid → rejected inline (the `HttpUrl` rules of the job contract are reused).
- `reachable is False` → warning + "keep anyway?" (yes keeps the source, no asks for the URL
  again).
- `type == "rss"`, reachable, `is_feed is False` → warning + "keep anyway?".
- YouTube types → ask for `channel_id` or `playlist_id` (non-empty); no check.

### `WizardSession` (transient answers)

Collected step by step and never persisted until the operator confirms. Field order follows
FR-004.

| Step | Fields | Inline validation |
|---|---|---|
| 1 name | `name` | Non-empty, no leading or trailing whitespace, ≤ 200 chars (`MAX_NAME_LENGTH`), not an existing job name |
| 2 schedule | `frequency`, `weekday` (weekly), `day_of_month` (monthly, 1–31), `time` (`HH:MM`), `timezone` (exact IANA name) | Rules of `ScheduleConfig`; only the field that fits the frequency is asked |
| 3 notification | `to[]` (≥ 1, loop), `subject` (default `invio: <name>`) | `EmailStr` syntax; duplicates warned and skipped |
| 4 sources | `sources[]` (≥ 1, loop: type → URL or id → check → "add another?") | See `CheckResult` rules; duplicates (same type + locator) warned and skipped |
| 5 keywords | `any[]`, `all[]`, `exclude[]` | Comma-separated input; blank items dropped; may be empty |
| 6 search | `semantic_description` (multi-line) | Non-empty after stripping; line breaks kept |
| 7 llm | `provider`, `models.fast`, `models.smart` | Provider from `LLMProvider`; model via registry select or "Other…" with warning and confirmation |
| 8 limits | "keep defaults?" → otherwise the four `LimitsConfig` fields | Integer ≥ 1 |

After step 8 the answers are assembled into a mapping and passed through `validate_job`. A
`JobConfigError` there (which should not happen, since each step is validated) is shown and the
operator goes back to the step that owns the first failing field (FR-014). The valid `JobConfig`
is rendered with `dump_yaml` for the preview (FR-013).

## State transitions (job lifecycle as driven by the CLI)

```text
           create / create --from-file / import
 (none) ───────────────────────────────────────▶ enabled ──disable──▶ disabled
                                                    ▲                    │
                                                    └──────enable────────┘
 enabled|disabled ──edit (valid)──▶ same state, config replaced, next_run recalculated if enabled
 enabled|disabled ──delete (confirmed or --yes)──▶ (none)

 invalid config (stored, either state):
   list ✓ (shown)   show ✓ (errors, exit 2)   edit ✓ (repairs on valid save)
   disable ✓        delete ✓                  enable ✗ (exit 2)   export ✗ (exit 2)
```
