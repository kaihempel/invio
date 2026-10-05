# Data Model: Web Page Source with Change Detection (gh-issue-12)

No database changes. `Candidate` (in `src/invio/domain.py`) is unchanged; this feature defines
how a `web` source fills it.

## WebSource (configuration, `src/invio/config/job.py`) — extended

Field order is fixed: `type` and the locator come first, as in every source model.

| Field | Type | Default | Rules |
|-------|------|---------|-------|
| `type` | `Literal["web"]` | — | discriminator |
| `url` | `HttpUrl` | — | the tracked page |
| `name` | `str \| None` | `None` | min length 1 |
| `enabled` | `StrictBool` | `True` | |
| `selector` | `str \| None` | `None` | min length 1; must be a valid CSS selector (FR-002) |
| `mode` | `Literal["page", "links"]` | `"page"` | |
| `url_pattern` | `str \| None` | `None` | min length 1; must compile as a Python regular expression; only with `mode: links` |
| `render` | `Literal["static", "js"]` | `"static"` | |
| `wait_for` | `str \| None` | `None` | min length 1; valid CSS selector; only with `render: js` |

Cross-field rules (model validator, error located at the offending field):

- `url_pattern` set and `mode != "links"` → `url_pattern: only allowed with mode: links`
- `wait_for` set and `render != "js"` → `wait_for: only allowed with render: js`

Old files without the new keys load unchanged (FR-003). `dump_yaml` writes all fields,
defaults included, like every other model.

## Candidate mapping

### Page mode (`mode: page`) — exactly one candidate (FR-013)

| Candidate field | Value |
|-----------------|-------|
| `url` | `canonical_url(config.url)`: the configured URL, not the redirect target |
| `url_hash` | `url_hash(url)` |
| `title` | collapsed `<title>` text; fallback `url` |
| `published_at` | `None` |
| `type` | `"article"` |
| `teaser` | `teaser(region_text)`: at most `TEASER_MAX_CHARS` (500) including `…`, cut at a word boundary; `None` when the text is empty |
| `content_hash` | `sha256(region_text).hexdigest()` |

### Links mode (`mode: links`) — zero or more candidates, page order (FR-014–FR-016)

| Candidate field | Value |
|-----------------|-------|
| `url` | canonical absolute link URL |
| `url_hash` | `url_hash(url)` |
| `title` | collapsed link text; fallback `url` |
| `published_at` | `None` |
| `type` | `"article"` |
| `teaser` | `None` |
| `content_hash` | `None` |

Link filter pipeline: `a[href]` in the regions → resolve against `<base href>` or the final URL
→ canonicalize → http(s) with a host only → drop the page itself → `url_pattern` matches
(`re.search`, any host) **or**, without a pattern, the host equals the final page URL's host
(case-insensitive, exact) → first occurrence wins.

## Derived values (internal, `src/invio/sources/web.py` / `text.py`)

- **Document**: decoded HTML (encoding precedence: BOM → header charset → `<meta>` → UTF-8),
  plus the final URL (after redirects or browser navigation).
- **Region**: `body`, or every node matching `selector` in document order; zero matches →
  `FetchError("selector_not_found")` (in both modes).
- **Region text**: the regions with `script`/`style`/`noscript`/`template` removed, then
  extracted (comments dropped, block elements separate words), joined by a space, NFC, collapsed
  and trimmed.

## Fetch outcomes

| Situation | Result |
|-----------|--------|
| 2xx HTML, page mode | `[page_candidate]` |
| 2xx HTML, links mode | `[link_candidate, …]` (may be `[]`) |
| 304 (static only) | `[]` |
| Not HTML | `FetchError("not_html")` |
| Selector matches nothing | `FetchError("selector_not_found")` |
| Engine or browser missing (`render: js`) | `FetchError("render_unavailable")` |
| Render exceeds 30 s / `wait_for` never appears | `FetchError("timeout")` |
| Rendered main response ≥ 400 | `FetchError("http_status", status=…)` |
| Navigation failed, no response | `FetchError("render_failed")` |
| Rendered HTML larger than `max_response_bytes` | `TooLargeError` |
| Guard, robots.txt, size or timeout failure (static) | the client's existing `FetchError` / `BlockedError` / `TooLargeError` |

A browser sub-request refused by the guard does not fail the fetch. It is aborted, and the page
renders without it.
