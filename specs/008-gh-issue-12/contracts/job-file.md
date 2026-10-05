# Contract: `web` source in the job file (delta to specs/001-gh-issue-3/contracts/job-file.md)

Only the `web` source entry changes. All other sections, and `schema_version: 1`, stay as
they are: the new keys are optional and have defaults, so existing files remain valid.

## Keys

```yaml
sources:
  - type: web                      # required
    url: https://example.org/news  # required, http(s)
    name: Example news             # optional
    enabled: true                  # optional, default true
    selector: "main .post-list"    # optional CSS selector; default: whole <body>
    mode: page                     # optional: page (default) | links
    url_pattern: "/news/\\d{4}/"   # optional regex, only with mode: links
    render: static                 # optional: static (default) | js
    wait_for: ".post-list li"      # optional CSS selector, only with render: js
```

| Key | Meaning |
|-----|---------|
| `selector` | Only the text (page mode) or links (links mode) inside the matching elements count. All matches are used, in page order. No match on a fetched page = fetch error `selector_not_found`. |
| `mode: page` | One item for the page itself. It is reported as new when the normalized text of the region changes. |
| `mode: links` | One item per linked URL in the region. Without `url_pattern`, only links on the page's own host. With it, links on any host whose absolute URL the regex matches (anywhere in the URL, use `^…$` to anchor). |
| `render: js` | Load the page in a headless browser first (needs the optional extra: `uv sync --extra render` and `uv run playwright install chromium`). Waits until the network is quiet for 0.5 s, plus `wait_for` if set, for at most 30 s. |

## Load-time errors (exit code 2, one line per problem)

| Input | Message (`<loc>: <message>`) |
|-------|------------------------------|
| `selector: "div["` | `sources[0].selector: invalid CSS selector` |
| `url_pattern: "("` | `sources[0].url_pattern: invalid regular expression: <re error>` |
| `url_pattern` with `mode: page` | `sources[0].url_pattern: only allowed with mode: links` |
| `wait_for` with `render: static` | `sources[0].wait_for: only allowed with render: js` |
| `mode: feed` / `render: browser` | Pydantic literal error naming the allowed values |
| unknown key | existing "extra inputs are not permitted" error |

## Saved form

`job create/edit` writes every key, defaults included (`selector: null`, `mode: page`,
`url_pattern: null`, `render: static`, `wait_for: null`), in the order above.
`docs/job.schema.json` is regenerated, and `docs/job.example.yaml` gets a `selector` on its
`web` entry.
