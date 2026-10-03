# Data Model: Validated Research Job Configuration

**Feature**: [spec.md](./spec.md) · **Research**: [research.md](./research.md)

All configuration models live in `src/invio/config/job.py`, share a base with
`extra="forbid"`, `frozen=True`, `str_strip_whitespace=True`, and declare fields in the order
shown (this is also the key order of saved YAML). Every `int`, `float` and `bool` field below is
declared as `StrictInt`, `StrictFloat` or `StrictBool` (FR-002a): no `true`→`1`, `1`→`true` or
`1.0`→`1` conversions; `StrictFloat` still accepts whole numbers. Domain records live in `src/invio/domain.py`.

## Enumerations

| Name | Values | Notes |
|---|---|---|
| `Frequency` | `daily`, `weekly`, `monthly` | exact, lowercase |
| `Weekday` | `monday` … `sunday` | input case-insensitive, stored lowercase (FR-006); JSON Schema lists lowercase only (documented leniency, FR-028) |
| `LLMProvider` | `mistral`, `openai`, `anthropic`, `google`, `ollama` | kept in sync with `Settings` by test (FR-019) |
| `ItemType` (domain) | `article`, `video` | `Literal`, stdlib only |

## JobConfig (root)

| # | Field | Type | Required | Default | Rules |
|---|---|---|---|---|---|
| 1 | `schema_version` | int | no | `1` | `1 ≤ v ≤ SUPPORTED_SCHEMA_VERSION (1)` (FR-003) |
| 2 | `schedule` | ScheduleConfig | yes | — | |
| 3 | `notification` | NotificationConfig | yes | — | |
| 4 | `sources` | list[SourceConfig] | yes | — | `min_length=1` (FR-015) |
| 5 | `search` | SearchConfig | yes | — | |
| 6 | `llm` | LLMConfig | yes | — | |
| 7 | `limits` | LimitsConfig | no | `LimitsConfig()` | whole section optional (FR-022) |

## ScheduleConfig

| Field | Type | Required | Default | Rules |
|---|---|---|---|---|
| `frequency` | Frequency | yes | — | FR-004 |
| `time` | str | yes | — | `^([01][0-9]\|2[0-3]):[0-5][0-9]$` (ASCII digits only) (FR-005) |
| `weekday` | Weekday \| None | iff weekly | `None` | FR-006 |
| `day_of_month` | int \| None | iff monthly | `None` | `1..31` (FR-007) |
| `timezone` | str | yes | — | resolvable by `zoneinfo.ZoneInfo` (FR-008) |

Cross-field (model validator, messages per FR-009):

| Condition | Message |
|---|---|
| weekly, weekday missing | `weekday is required when frequency is 'weekly'` |
| monthly, day_of_month missing | `day_of_month is required when frequency is 'monthly'` |
| weekday set, frequency ≠ weekly | `weekday is only allowed when frequency is 'weekly'` |
| day_of_month set, frequency ≠ monthly | `day_of_month is only allowed when frequency is 'monthly'` |

## NotificationConfig

| Field | Type | Required | Default | Rules |
|---|---|---|---|---|
| `to` | list[EmailStr] | yes | — | `min_length=1`; duplicates allowed (FR-010) |
| `subject` | str | yes | — | non-empty after strip; placeholders not validated (FR-011) |
| `send_if_empty` | bool | no | `false` | FR-012 |

## SourceConfig (discriminated union on `type`)

Common fields on every variant (declared after `type`):

| Field | Type | Default |
|---|---|---|
| `name` | str \| None | `None` |
| `enabled` | bool | `true` |

| Variant | `type` | Locator field | Locator rules |
|---|---|---|---|
| `RssSource` | `rss` | `url: HttpUrl` | absolute http(s) |
| `WebSource` | `web` | `url: HttpUrl` | absolute http(s) |
| `SitemapSource` | `sitemap` | `url: HttpUrl` | absolute http(s) |
| `YoutubeChannelSource` | `youtube_channel` | `channel_id: str` | non-empty |
| `YoutubePlaylistSource` | `youtube_playlist` | `playlist_id: str` | non-empty |

Field order per variant: `type`, locator, `name`, `enabled`. Fields of other variants are
rejected (`extra="forbid"`, FR-014a).

## SearchConfig

| Field | Type | Required | Default | Rules |
|---|---|---|---|---|
| `keywords` | KeywordsConfig | no | `KeywordsConfig()` | |
| `semantic_description` | str | yes | — | non-empty after strip (FR-017) |
| `min_relevance` | float | no | `0.6` | `0 ≤ v ≤ 1` (FR-018) |

### KeywordsConfig

| Field | Type | Default | Rules |
|---|---|---|---|
| `any` | list[str] | `[]` | each item non-empty after strip |
| `all` | list[str] | `[]` | each item non-empty after strip |
| `exclude` | list[str] | `[]` | each item non-empty after strip |

## LLMConfig

| Field | Type | Required | Default | Rules |
|---|---|---|---|---|
| `provider` | LLMProvider | yes | — | exact lowercase value |
| `models` | LLMModels | yes | — | |
| `fallback_provider` | LLMProvider \| None | no | `None` | ≠ `provider` → `fallback_provider must differ from provider` (FR-021) |

### LLMModels

| Field | Type | Rules |
|---|---|---|
| `fast` | str | non-empty |
| `smart` | str | non-empty |

## LimitsConfig

| Field | Type | Default | Rules |
|---|---|---|---|
| `max_items_per_source` | int | `20` | `≥ 1` |
| `max_items_per_run` | int | `100` | `≥ 1` |
| `max_items_in_notification` | int | `20` | `≥ 1` |
| `max_llm_tokens_per_run` | int | `200000` | `≥ 1` |

No cross-limit rules (overlapping caps are allowed; smaller effective cap wins at run time).

## Domain records (`src/invio/domain.py`)

`@dataclass(frozen=True, slots=True, kw_only=True)`, stdlib imports only.

### Candidate

| Field | Type |
|---|---|
| `url` | str |
| `url_hash` | str |
| `title` | str |
| `published_at` | datetime \| None |
| `type` | ItemType |
| `teaser` | str \| None |
| `content_hash` | str \| None |

### ProcessedItem

| Field | Type | Default |
|---|---|---|
| `id` | int | — |
| `url` | str | — |
| `type` | ItemType | — |
| `title` | str | — |
| `raw_content` | str | — |
| `relevance` | float \| None | `None` |
| `summary` | str \| None | `None` |
| `error` | str \| None | `None` |

## Relationships

```text
JobConfig 1 ─┬─ 1 ScheduleConfig
             ├─ 1 NotificationConfig
             ├─ 1..* SourceConfig (Rss | Web | Sitemap | YoutubeChannel | YoutubePlaylist)
             ├─ 1 SearchConfig ── 1 KeywordsConfig
             ├─ 1 LLMConfig ── 1 LLMModels
             └─ 1 LimitsConfig

SourceConfig ──(produces at run time)──▶ Candidate ──(processed into)──▶ ProcessedItem
```

No state transitions: configuration is immutable once loaded.
