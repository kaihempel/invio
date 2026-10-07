# Contract: server env file

Research: [R6](../research.md#r6--env-file-format-location-and-permissions).

## Location and permissions

| Path | Owner:Group | Mode | Notes |
|---|---|---|---|
| `/etc/invio/` | `root:invio` | `0750` | Others can't list or traverse the directory |
| `/etc/invio/invio.env` | `invio:invio` | `0600` | Only root and `invio` can read it (FR-011, SC-006) |
| `/etc/invio/deployed-revision` | `root:root` | `0644` | Commit that was last migrated (R4); not secret |

## Format

- systemd `EnvironmentFile` syntax, one `KEY="value"` per line. Lines starting with `#` are
  comments.
- In values, `\` is written as `\\` and `"` as `\"`. Values must not contain newlines (the role
  rejects them in validation, FR-016).
- Only `INVIO_*` keys. Every key must be a known setting. Unknown keys are not fatal at runtime
  (invio logs "unknown setting ignored"), but the example file is checked in CI
  (`unknown_env_keys`, R12).
- `INVIO_ENV_FILE` is **not** set. Variables come from the process environment (spec Assumptions).

## Keys written by the role

| Key | Role variable | Required | Secret |
|---|---|---|---|
| `INVIO_DATABASE_URL` | built as `mysql+pymysql://{{ invio_db_user }}:{{ invio_db_password \| urlencode }}@{{ invio_db_host }}:{{ invio_db_port }}/{{ invio_db_name }}?charset=utf8mb4`; `invio_db_host=localhost` uses the Unix socket (`unix_socket=/run/mysqld/mysqld.sock` query parameter) | yes | yes |
| `INVIO_SMTP_HOST`, `INVIO_SMTP_PORT`, `INVIO_SMTP_SECURITY`, `INVIO_SMTP_FROM`, `INVIO_SMTP_USER` | `invio_smtp_*` | host and from: yes | no |
| `INVIO_SMTP_PASSWORD` | `invio_smtp_password` | no | yes |
| `INVIO_MISTRAL_API_KEY`, `INVIO_OPENAI_API_KEY`, `INVIO_ANTHROPIC_API_KEY`, `INVIO_GOOGLE_API_KEY` | `invio_llm_api_keys` (dict, provider → key) | at least one provider | yes |
| `INVIO_LOG_LEVEL` | `invio_log_level` (default `INFO`) | no | no |
| `INVIO_HTTP_CONTACT` | `invio_http_contact` | yes (sent to site owners) | no |
| `INVIO_ARCHIVE_DIR` | fixed `/var/lib/invio/archive` | — | no |
| `INVIO_HEALTHCHECK_URL` | `invio_healthcheck_url` (#23) | no | yes (the URL is a capability) |
| any other `INVIO_*` | `invio_env_extra` (dict, key → value; keys must start with `INVIO_`) | no | treated as secret (`no_log`) |

## Example file

`deploy/env/invio.env.example` lists every key above with a placeholder value and a comment. It
contains no real secret. CI checks that all its keys are known settings.

## Guarantees

- No secret from this file appears in Ansible output (`no_log`, FR-017), in the journal
  (constitution V, SC-007), or in `systemctl show` (`EnvironmentFile` is shown as a path, not
  its contents).
