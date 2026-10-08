# Contract: `archive` section of the job file

```yaml
archive:                 # optional; omit for "no archive"
  enabled: true          # default false
  base_url: https://digests.example.org/invio   # optional; used for the link in the mail
```

| Input | Result |
|---|---|
| section omitted | `enabled=false`, `base_url=None` |
| `enabled: true`, no `base_url` | archive written, no link in mail |
| `base_url: https://h/x/` | stored as `https://h/x` |
| `base_url: ftp://h`, `h/x`, `https://`, `https://h?a=1`, `https://h#f` | rejected: error names `archive.base_url` |
| `enabled: "yes"` (non-bool) | rejected (strict bool), names `archive.enabled` |
| unknown key (`archive.dir`) | rejected, names `archive.dir` |

`docs/job.schema.json` is regenerated from `JobConfig`; `docs/job.example.yaml` shows the
section. Existing stored job configs without the section stay valid.

Settings: `INVIO_ARCHIVE_DIR` (default `/var/lib/invio/archive`) — the directory the service
user must be able to write.
