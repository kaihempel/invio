# Contract: Job File `language` Setting

User-facing addition to the job file format (schema version stays `1`).

## Field

| Key | Type | Required | Default | Meaning |
|-----|------|----------|---------|---------|
| `language` | string | no | `en` | Language summaries are written in, as a lower-case ISO 639-1 code |

Top-level key, written by `dump_yaml` directly after `schema_version`:

```yaml
schema_version: 1
language: de
schedule:
  ...
```

## Validation

- Must be two lower-case letters and a known ISO 639-1 code.
- Errors follow the existing job-file format, one line per problem:

```text
invalid job file job.yaml:
  language: unknown ISO 639-1 language code 'xx'
```

- `DE`, `deu`, `german` and `""` are rejected; a missing key yields `en`.

## JSON Schema (`docs/job.schema.json`)

`"language": {"type": "string", "pattern": "^[a-z]{2}$", "default": "en", ...}`; the
membership check is enforced by the loader only. The committed schema is regenerated with
`uv run python -m invio.config.job`.

## Compatibility

Existing job files without `language` stay valid and behave as `language: en`. Saving such a
job writes `language: en` (every field is written on save).
