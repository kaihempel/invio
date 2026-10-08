# Contract: Archive layout and page content

Consumers: web servers / browsers reading the archive; tests.

## Files

| Path (relative to `archive_dir`) | Written when | Mode |
|---|---|---|
| `<job-slug>/<YYYY-MM-DD-HHMM>[-n].html` | once per archived digest, never modified | `0644` |
| `<job-slug>/index.html` | rebuilt after each archived digest of the job | `0644` |
| `index.html` | rebuilt after each archived digest | `0644` |
| `<job-slug>/.job-name` | first archived digest of the job | `0644` |

Directories are `0755`. All writes are atomic (temp file in the same directory, then rename or
no-overwrite link); no `*.tmp` file remains after a call, also not after a failure.

## Page (`<name>.html`)

- Valid HTML5, `lang="en"`, `<meta charset="utf-8">`, `<meta name="viewport"
  content="width=device-width, initial-scale=1">`, `<meta name="robots" content="noindex">`,
  `<meta name="referrer" content="no-referrer">`.
- `<title>` and `<h1>`: job name (escaped); date line: `YYYY-MM-DD HH:MM UTC`.
- Body: the digest Markdown rendered and sanitized by the same allowlist as the mail HTML
  (`markdown_to_safe_html`); all other values autoescaped.
- Footer: `Items found: N · Included: N · Run time: …` (same figures as the mail), no archive link.
- Navigation: link to `index.html` (job index), relative.
- **No external references**: no `<script>`, `<link>`, `<img>`, `<iframe>`, `<form>`, no
  `src=`, `@import` or `url(`; one inline `<style>`.
- Layout fits 320 px width without horizontal page scroll (wide tables scroll inside their own
  container).

## Job index (`<job-slug>/index.html`)

Heading with the job name, a list of pages newest first: `YYYY-MM-DD HH:MM UTC` (and `#n` for
suffix pages) linking by relative file name; link to `../index.html`. Same head/inline-style
rules as pages. Empty list is never written (index is only built after a page exists).

## Global index (`index.html`)

List of jobs (original names from `.job-name`, sorted case-insensitively), each with the
number of pages, the newest page date and a relative link `<job-slug>/index.html`. A directory
without `.job-name` or without pages is not listed.

## URL for the mail

`<base_url>/<job-slug>/<page-name>.html`, where `base_url` has no trailing slash (normalised
by the job config). Only built when the archive is enabled, `base_url` is set and the page file
exists at send time.
