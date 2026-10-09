# CLI Contract: `invio source discover`

Module: `src/invio/cli/commands/source.py` (auto-discovered Typer `app`, group `source`).
The group uses the root callback's runtime setup (configuration errors exit 2).

## Synopsis

```text
invio source discover URL [--add-to JOB] [--pick N]
```

| Argument / option | Type | Description |
|---|---|---|
| `URL` | str, required | Website, page, feed, sitemap or YouTube address. A scheme-less host (`example.com`) means `https://example.com`. Only `http`/`https`. |
| `--add-to JOB` | str, optional | Append one discovered source to the existing job `JOB` and save it after re-validation. Needs `INVIO_DATABASE_URL`. |
| `--pick N` | int ≥ 1, optional | Choose candidate `N` of the printed list without a prompt. Only valid with `--add-to`. |

## Processing order

1. Validate `URL` and options (usage errors, no network).
2. With `--add-to`: load the job; missing → exit 1; stored config invalid → exit 2 (no network).
3. Discover: start page, announced feeds, probe locations (root and page folder),
   `robots.txt` sitemaps, YouTube links; validate everything; order and number the results.
4. Print the results (stdout) and diagnostics (stderr).
5. With `--add-to`: select (`--pick`, prompt, or single-candidate rule) and append.

## Standard output

For each validated source, numbered from 1 in the order defined by FR-010:

```text
[1] rss  https://example.com/feed.xml
    title: Example Blog · newest entry: 2026-10-01 · found: announced
    - type: rss
      url: https://example.com/feed.xml

[2] sitemap  https://example.com/sitemap.xml
    sitemap: 128 URLs · newest entry: 2026-09-30 · found: probed
    - type: sitemap
      url: https://example.com/sitemap.xml

[3] youtube_channel  @examplecreator
    title: Example Creator · newest entry: 2026-10-05 · found: linked
    - type: youtube_channel
      channel_id: '@examplecreator'

[4] rss (comments)  https://example.com/comments/feed
    title: Comments for Example Blog · newest entry: 2026-10-02 · found: announced
    - type: rss
      url: https://example.com/comments/feed
```

- Dates are `YYYY-MM-DD` (UTC). A missing date prints `unknown`; an empty feed prints
  `no entries`. A sitemap index prints `sitemap index: N sitemaps`.
- The YAML lines after the summary are a valid entry for the `sources:` list of a job file
  (only required fields; defaults omitted).
- With `--add-to`, a final line confirms the outcome:
  `Added [2] sitemap https://example.com/sitemap.xml to job 'news'.`,
  `Job 'news' already contains this source; nothing changed.` or
  `Nothing added to job 'news'.` (cancelled).

## Standard error

One line each, never secrets, URLs redacted (no credentials, no query string):

- `Start page not usable: <reason>: <url>` when the page failed or was neither HTML, a feed nor
  a sitemap.
- `Not offered: <url>: <reason>[ (HTTP <status>)]` for each rejected finding.
- `Skipped: <n> more announced feeds / YouTube links / robots.txt sitemaps (limit <m>)`.
- `No source found for <url> (checked: page links, common locations, robots.txt sitemaps,
  YouTube links).`
- Validation errors of the job, one per line, naming the field (as `invio job` prints them).

## Interactive selection

Only with `--add-to`, without `--pick`, when stdin and stdout are terminals and at least one
source was found. One `select` prompt: the numbered entries plus `Cancel`. Ctrl-C or `Cancel` →
nothing changes, exit 0 with `Nothing added to job '<job>'.`

## Exit codes

| Code | When |
|---|---|
| 0 | At least one source found and (without `--add-to`) printed; or (with `--add-to`) the source was added, was already present, or the operator cancelled |
| 1 | No source found; `--add-to` job does not exist; `--pick` out of range (message names `1–<n>`); non-interactive `--add-to` with several sources and no `--pick`; database error |
| 2 | Usage error (invalid `URL`, `--pick` without `--add-to`, `--pick` < 1); configuration error (settings, missing database URL with `--add-to`); stored job config invalid; job config invalid after appending (nothing saved) |

## Non-goals

No JSON output; no adding several sources in one run; no editing of optional source fields
(use `invio job edit`); `web` sources are never suggested.
