# Research: Validated Research Job Configuration

**Feature**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md) · **Date**: 2026-10-04

All Technical Context unknowns are resolved below. Each entry: Decision / Rationale / Alternatives.

## R1 — YAML library and YAML 1.1 pitfalls

**Decision**: Use **PyYAML** (`safe_load` / `safe_dump`) with a project loader `JobYamlLoader`
(subclass of `yaml.SafeLoader`) whose implicit resolvers are reduced to YAML-1.2-core behaviour:
only `null`/`~`/empty → None, `true`/`false` (any case) → bool, decimal ints, floats. Everything
else — including `17:30`, `yes`, `on`, `2026-10-04` — stays a string. Dump with
`yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)`.

**Rationale**: Verified in this repo (PyYAML 6.0.3): `safe_load("t: 17:30")` → `1050`
(sexagesimal int), `7:05` → `425`, `[yes, no, on]` → `[True, False, True]`, `2026-10-04` →
`date`. An operator writing `time: 17:30` or keyword `on` would get a baffling type error.
`safe_dump` already quotes strings that YAML 1.1 would mis-resolve (`'yes'`, `'17:30'`), so the
dumped file stays readable by both our loader and generic tools. `sort_keys=False` keeps
field-definition order (FR-024); `allow_unicode=True` keeps umlauts readable.
PyYAML is already in the lock file (transitively via pre-commit) and is the most common choice.

**Alternatives considered**:
- *ruamel.yaml* (YAML 1.2 by default, comment preservation): heavier, comments are explicitly not
  required (FR-026), and its round-trip types leak into model input.
- *Require quoting in job files*: shifts the burden to operators; violates "clear messages" goal.
- *Plain `yaml.safe_load`*: rejected because of the pitfalls above.

## R2 — Model framework and strictness

**Decision**: Pydantic v2 `BaseModel`s sharing a base `_StrictModel` with
`ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)`. Numeric and boolean
fields use `StrictInt` / `StrictFloat` / `StrictBool` (FR-002a); all other fields keep lax mode.
Verified: `StrictFloat` accepts YAML int `1` for `min_relevance` but rejects `true`; `StrictInt`
rejects `true` and `1.0`; `StrictBool` rejects `1`.

**Rationale**: Pydantic is already a dependency (pydantic-settings, mypy plugin configured).
`extra="forbid"` satisfies FR-002; `frozen=True` makes configs hashable/immutable contracts and
lets round-trip equality use `==`.

**Alternatives**: dataclasses + manual validation (much more code); `strict=True` globally
(also makes enums/`HttpUrl`/`EmailStr` reject plain YAML strings); plain lax types (verified:
`max_items_per_run: true` silently becomes `1`, `enabled: 1` becomes `True`).

## R3 — Source discriminated union

**Decision**: One model per kind (`RssSource`, `WebSource`, `SitemapSource`,
`YoutubeChannelSource`, `YoutubePlaylistSource`), each with `type: Literal[...]`, sharing
`_SourceBase(name: str | None = None, enabled: bool = True)`.
`SourceConfig = Annotated[RssSource | WebSource | ..., Field(discriminator="type")]`.
`url` fields use `pydantic.HttpUrl` (absolute, http/https only).
`channel_id` / `playlist_id` are non-empty strings.

**Rationale**: Native discriminated unions give per-kind validation (FR-014), reject foreign
fields via `extra="forbid"`, and produce a `union_tag_invalid` error listing allowed tags
(edge case "unsupported kind"). `HttpUrl` normalises (e.g. adds trailing `/` to bare hosts);
this is idempotent, so load → dump → load stays equal (FR-025).

**Alternatives**: single model with optional fields + validator (weak schema, poor errors);
plain `str` URLs (no http(s)/absolute check).

## R4 — Cross-field schedule rules and error locations

**Decision**: `ScheduleConfig` uses a `model_validator(mode="after")` that enforces
weekday ⇔ weekly and day_of_month ⇔ monthly, raising `ValueError` with plain-language messages
that name the field, e.g. `weekday is required when frequency is 'weekly'` and
`weekday is only allowed when frequency is 'weekly'`. Same pattern for
`LLMConfig` (`fallback_provider must differ from provider`).

**Rationale**: Model-level validators report loc `schedule`; including the field name in the
message keeps FR-009/FR-027 satisfied ("schedule: weekday is required …") without fragile
custom error construction.

**Alternatives**: `PydanticCustomError` with synthetic loc — not supported for model validators
without hacks; `field_validator` with `info.data` — order-dependent and skipped when the field
is absent.

## R5 — Time of day, weekday, timezone

**Decision**:
- `time`: `str` constrained by pattern `^([01]\d|2[0-3]):[0-5]\d$` (strict `HH:MM`, 24 h). Kept
  as string so the file value is reproduced exactly on dump.
- `weekday`: `Weekday` `StrEnum` (`monday`…`sunday`); a `field_validator(mode="before")`
  lower-cases string input (clarification Q5). JSON Schema lists the lowercase values.
- `timezone`: `str` validated with `zoneinfo.ZoneInfo(value)`; `ZoneInfoNotFoundError` /
  `ValueError` → `unknown timezone '<value>'`. Stored as the given key.

**Schema leniency (FR-028)**: the JSON Schema lists only the lowercase weekday values (the
canonical saved form), while the loader also accepts other capitalisations. This is documented
in the README "Job files" section and in the `Weekday` docstring; editors using the schema may
flag `Monday`, which is harmless because saving normalises it.

**Rationale**: `datetime.time` would dump as `17:30:00` and break exact round-trip of the file
text; string + regex is simplest. `zoneinfo` uses the system tz database, present on macOS and
the Ubuntu CI image.

**Alternatives**: `tzdata` package as fallback (not needed on target platforms; can be added
later); `pytz` (legacy).

## R6 — E-mail validation

**Decision**: `to: list[EmailStr]` with `min_length=1`; add runtime dependency
`email-validator` via `pydantic[email]`.

**Rationale**: `EmailStr` requires `email-validator`; Pydantic calls it with deliverability
(DNS) checks disabled, so validation is offline — compliant with constitution III (no network in
tests).

**Alternatives**: regex — notoriously wrong for edge cases.

## R7 — LLM provider list kept in sync with settings

**Decision**: `LLMProvider(StrEnum)`: `mistral`, `openai`, `anthropic`, `google`, `ollama`,
defined in `invio/config/job.py`. A unit test asserts that the set of providers equals
`{f[:-8] for f in Settings.model_fields if f.endswith("_api_key")} | {f[:-9] for f in
Settings.model_fields if f.endswith("_base_url")}`.

**Rationale**: Settings has no explicit provider list; deriving one from field names in a test
gives drift detection (clarification Q3) without coupling job models to settings at import time.

**Alternatives**: import provider list from settings at runtime (settings has none; would
invert intended layering); hardcode without test (drift undetected).

## R8 — Schema version

**Decision**: `schema_version: int = Field(default=1, ge=1)` plus a validator rejecting values
`> SUPPORTED_SCHEMA_VERSION` (= 1) with `schema_version N is not supported (max 1)`.

**Rationale**: Plain int keeps the schema readable and allows future versions; explicit message
satisfies the edge case.

**Alternatives**: `Literal[1]` — terse but the error message ("Input should be 1") is unclear.

## R9 — Load/dump API and error reporting

**Decision**: In `invio/config/job.py`:
- `load_yaml(path: str | Path) -> JobConfig`
- `dump_yaml(config: JobConfig) -> str` and `write_yaml(config, path) -> None`
- `JobConfigError(Exception)` with attributes `path` and `errors: list[str]`; raised for
  unreadable files (`OSError`), YAML syntax errors, non-mapping top level, and
  `ValidationError`. Each validation error is rendered `"<loc>: <msg>"` where loc is dotted
  with list indices in brackets (`sources[2].url`) and the discriminator tag segment removed.
  `path` is always stored as `Path(path)`. Cross-field errors carry the section as loc
  (`schedule: weekday is required …`); Pydantic runs a section's model validator only when its
  fields are valid, so such errors can appear on a second attempt (FR-027).

Dump uses `config.model_dump(mode="json")` — all fields, defaults included, unset optionals as
`null` (clarification Q4); key order = field definition order.

**Rationale**: One exception type lets the future CLI map it to exit code 2 (constitution II)
and print readable lines. `dump_yaml` returning text keeps it testable without files.

**Alternatives**: re-raising raw `ValidationError` (loc tuples include union tags like
`('sources', 2, 'rss', 'url')` — confusing for operators).

## R10 — JSON Schema export and staleness check

**Decision**: `job_json_schema() -> dict` returns `JobConfig.model_json_schema()`; running
`uv run python -m invio.config.job` writes it to `docs/job.schema.json` (indent 2, trailing
newline). A test regenerates the schema in memory and compares it with the committed file,
failing with "run `uv run python -m invio.config.job` to update" (FR-029). A second test
validates `docs/job.example.yaml` against the schema with `jsonschema` (dev dependency; SC-005).

**Rationale**: Generated from the model itself (FR-028); drift caught by CI.

**Alternatives**: new CLI command `invio config schema` — useful later, but this issue adds no
user-facing commands; generating in CI — leaves the committed doc stale.

## R11 — Shared domain records

**Decision**: `invio/domain.py`, stdlib only: `@dataclass(frozen=True, slots=True, kw_only=True)`
`Candidate` and `ProcessedItem`; `ItemType = Literal["article", "video"]`.

| Field | Type |
|---|---|
| Candidate.url / url_hash / title | `str` |
| Candidate.published_at | `datetime \| None` |
| Candidate.type | `ItemType` |
| Candidate.teaser / content_hash | `str \| None` |
| ProcessedItem.id | `int` |
| ProcessedItem.url / title / raw_content | `str` |
| ProcessedItem.type | `ItemType` |
| ProcessedItem.relevance | `float \| None = None` |
| ProcessedItem.summary / error | `str \| None = None` |

**Rationale**: Frozen records are safe to pass between pipeline nodes; updates use
`dataclasses.replace`. Test imports the module in a fresh interpreter and asserts no
`sqlalchemy`, `httpx`, `requests`, `pydantic`, `yaml`, `langgraph` module was loaded (FR-032).

**Alternatives**: Pydantic models (adds a dependency, violates "dependency-free"); mutable
dataclasses (accidental shared-state bugs in graph nodes).

## R12 — Typing support

**Decision**: Add dev dependency `types-PyYAML` (mypy strict) and `jsonschema` (+
`types-jsonschema`) for tests only.

**Rationale**: Constitution IV requires `mypy --strict` on `src/` to pass.
