# Contract: Digest e-mail message

## MIME structure

```text
multipart/alternative
├── text/plain; charset="utf-8"   (digest.txt.j2)
└── text/html;  charset="utf-8"   (digest.html.j2)
```

| Header | Value |
|--------|-------|
| `From` | `INVIO_SMTP_FROM` |
| `To` | the single recipient of this notification |
| `Subject` | the rendered subject from `NotificationPayload.subject` |
| `Date` | send time (RFC 5322) |
| `Message-ID` | generated per send; domain taken from the sender address |

Non-ASCII content is encoded per RFC 2047 (headers) and UTF-8 (bodies).

## Subject rendering

1. Start from `notification.subject` of the job config.
2. Replace every literal `{job_name}` with the job name and every `{date}` with the digest date
   (`YYYY-MM-DD`).
3. Leave every other `{…}`, `{` and `}` verbatim.
4. Replace each `\r` and `\n` with a space, then strip.

| Template | Job | Date | Result |
|----------|-----|------|--------|
| `invio: {job_name} – {date}` | `ai-news` | 2026-10-05 | `invio: ai-news – 2026-10-05` |
| `Weekly digest` | any | any | `Weekly digest` |
| `{job_name} {unknown} {` | `x` | any | `x {unknown} {` |

## Template context (both templates)

| Name | Type | HTML template |
|------|------|---------------|
| `job_name` | str | autoescaped |
| `subject` | str | autoescaped |
| `date` | str `YYYY-MM-DD` | autoescaped |
| `is_empty` | bool | if true, shows "No new items were found for this run." instead of the body |
| `body_html` | `Markup` (sanitized) | inserted as is; HTML template only |
| `body_text` | str (digest Markdown) | text template only |
| `stats.items_found` | int \| None | shows `–` when None |
| `stats.items_included` | int | |
| `stats.duration` | str, e.g. `3m 12s` | shows `–` when None |

The footer in both templates reads:
`Items found: {items_found} · Included: {items_included} · Run time: {duration}`.

## HTML sanitizer allowlist (applied after Markdown rendering)

| Kind | Allowed |
|------|---------|
| Tags | `a p br hr h1 h2 h3 h4 h5 h6 strong em b i u s del ul ol li blockquote code pre table thead tbody tr th td` |
| Attributes | `a`: `href`, `title`; `th`/`td`: `align` |
| URL schemes | `http`, `https`, `mailto` (anything else removes the `href`) |
| Added | `rel="noopener noreferrer"` on links |
| Removed with content | `script`, `style` |
| Removed (content kept as text) | all other tags (e.g. `iframe`, `form`, `img`, `div`, `span`) |
| Always removed | comments, `on*` attributes, `style`, `class`, `id` |

### Normative examples

| Markdown input | HTML must not contain | HTML must contain |
|----------------|-----------------------|-------------------|
| `<script>alert(1)</script>` | `<script`, `alert(1)` | — |
| `<a href="x" onclick="evil()">t</a>` | `onclick` | `>t</a>` |
| `[x](javascript:alert(1))` | `javascript:` | `x` |
| `<p style="color:red">t</p>` | `style=` | `t` |
| `<iframe src="https://e.x"></iframe>` | `<iframe` | — |
| `# H\n\n- **b** [l](https://e.x)` | — | `<h1>`, `<li>`, `<strong>`, `href="https://e.x"` |
| `<em>keep</em>` | — | `<em>keep</em>` |
