# Data Model: Static Digest Archive (HTML)

No database tables or migrations. The archive is files plus two small contract changes.

## ArchiveConfig (job file, `JobConfig.archive`)

| Field | Type | Default | Rules |
|-------|------|---------|-------|
| `enabled` | strict bool | `false` | switches the archive for this job |
| `base_url` | string or null | `null` | `http`/`https` URL with a host, no query/fragment; trailing `/` removed; errors name `archive.base_url` |

Unknown keys are rejected. `base_url` without `enabled` is allowed and has no effect.

## Settings

| Field | Type | Default |
|-------|------|---------|
| `archive_dir` (`INVIO_ARCHIVE_DIR`) | `Path` | `/var/lib/invio/archive` |

## NotificationPayload (schema_version stays 1)

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `archive_page` | string or null | `null` | relative path `<job-slug>/<page-name>.html` of the archived page; set only when the page was written |

Existing stored rows without the key load as `null`.

## Archive directory (entities from the spec)

```text
<archive_dir>/
├── index.html                      # Global Index: jobs, page count, newest page
├── <job-slug>/                     # Job Archive
│   ├── .job-name                   # original job name (UTF-8), identity + collision marker
│   ├── index.html                  # Job Index: pages newest first
│   ├── 2026-10-08-0930.html        # Digest Page
│   └── 2026-10-08-0930-2.html      # second digest of the same minute
```

### Job slug

`slugify(name)`: NFKD → ASCII → lower-case → runs of non-`[a-z0-9]` become `-` → trim `-` →
max 48 characters → `job` if empty. Collision (directory exists and `.job-name` differs):
`<slug>-<sha256(name)[:8]>`. Always matches `^[a-z0-9]+(-[a-z0-9]+)*$`.

### Page name

`YYYY-MM-DD-HHMM` of the run start in UTC, optional `-<n>` (n ≥ 2). Pattern
`^\d{4}-\d{2}-\d{2}-\d{4}(-\d+)?\.html$`; any other `*.html` in a job directory is ignored by
the index (except `index.html`, which is generated).

### State transitions

Per digest: `not archived` → (archive enabled, digest non-empty, run delivered) → `page written`
→ `indexes rebuilt`. Pages are never modified or deleted afterwards. A failure before the page
is published leaves `not archived` (temp files removed); a failure after publish leaves `page
written` with stale indexes that the next archived digest of any job heals (job index: next
digest of that job; global index: next digest of any job).
