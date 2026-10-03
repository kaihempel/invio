# Contract: Job File (YAML)

Consumers: operators (hand-written files), future `invio` job commands, scheduler, pipeline.
Machine-readable form: `docs/job.schema.json` (generated, see [python-api.md](./python-api.md)).
Field rules: [data-model.md](../data-model.md).

## Format

- UTF-8 YAML document whose top level is a mapping.
- Parsed with YAML-1.2-core scalar rules: only `true`/`false` are booleans, `null`/`~`/empty is
  null, plain decimal numbers are numbers; everything else is a string. So `time: 17:30`,
  keywords `on`/`yes`, and dates stay strings — no quoting needed.
- Unknown keys are errors at every level.
- Comments are allowed but are **not** preserved when a job is saved.

## Reference example (`docs/job.example.yaml`)

```yaml
schema_version: 1
schedule:
  frequency: weekly
  time: 07:30
  weekday: monday
  timezone: Europe/Berlin
notification:
  to:
    - research@example.com
  subject: "invio: AI agents digest – {date}"
  send_if_empty: false
sources:
  - type: rss
    url: https://example.com/feed.xml
    name: Example blog
  - type: web
    url: https://example.org/news
  - type: sitemap
    url: https://example.net/sitemap.xml
    enabled: false
  - type: youtube_channel
    channel_id: UCxxxxxxxxxxxxxxxxxxxxxx
  - type: youtube_playlist
    playlist_id: PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
search:
  keywords:
    any: [LangGraph, agent]
    all: []
    exclude: [crypto]
  semantic_description: >-
    New tools, papers and production experience reports about LLM agent frameworks.
  min_relevance: 0.6
llm:
  provider: openai
  models:
    fast: gpt-small
    smart: gpt-large
  fallback_provider: anthropic
limits:
  max_items_per_source: 20
  max_items_per_run: 100
  max_items_in_notification: 20
  max_llm_tokens_per_run: 200000
```

## Saved form

`dump_yaml` writes **every** field in the order of the example above, including defaults;
unset optional fields are written as `null` (e.g. `weekday: null` for a monthly job,
`name: null` for an unnamed source). Strings that YAML 1.1 tools would mis-read are quoted
(`time: '17:30'`). Non-ASCII text is written unescaped.

## Error contract

Any failure raises `JobConfigError`; its `str()` is:

```text
invalid job file <path>:
  schedule: weekday is required when frequency is 'weekly'
  notification.to[0]: value is not a valid email address: ...
  sources[2].url: Input should be a valid URL, ...
  search.min_relevance: Input should be less than or equal to 1
  llm.frequncy: Extra inputs are not permitted
```

- One line per problem, `<dotted.location>: <message>`; list positions in brackets; the
  discriminator tag (e.g. `rss`) is not part of the location.
- Unreadable file: `cannot read job file <path>: <reason>`.
- YAML syntax error: `invalid YAML in job file <path>: <parser message with line/column>`.
- Top level not a mapping / empty file: `job file <path> must contain a mapping at the top level`.
- Cross-field rules (`schedule` weekday/day_of_month, `llm` fallback) are reported at the
  section (`schedule: …`) and only once that section's own fields are valid, so fixing field
  errors may reveal them on the next attempt.
- `weekday` is accepted in any capitalisation although the JSON Schema lists only lowercase
  values (canonical saved form).
