# Contract: SMTP settings

Environment variables (prefix `INVIO_`, also read from `.env` / `INVIO_ENV_FILE`).

| Variable | Type | Default | Notes |
|----------|------|---------|-------|
| `INVIO_SMTP_HOST` | str | — | required for delivery |
| `INVIO_SMTP_PORT` | int | `587` | not derived from the security mode; use `465` for `ssl` explicitly |
| `INVIO_SMTP_USER` | str | — | login only if user **and** password are set |
| `INVIO_SMTP_PASSWORD` | secret | — | never logged or stored in error texts |
| `INVIO_SMTP_FROM` | str | — | required for delivery; `From` header, e.g. `Invio <invio@example.com>` |
| `INVIO_SMTP_SECURITY` | `starttls` \| `ssl` \| `none` | `starttls` | **new**. `starttls` upgrades the plain connection and fails if the server does not offer STARTTLS; `ssl` uses implicit TLS from connect; `none` is unencrypted (local relays only) |
| `INVIO_SMTP_TIMEOUT_SECONDS` | float > 0 | `30` | **new**; per SMTP operation |
| ~~`INVIO_SMTP_STARTTLS`~~ | — | — | **removed**; a leftover value triggers the existing `unknown setting ignored` warning |

## Validation

- `INVIO_SMTP_SECURITY=tls` fails when settings are loaded. The error names `smtp_security`
  and lists `'starttls', 'ssl' or 'none'`.
- CLI commands report it as `Configuration error: …` with exit code 2.
- `INVIO_SMTP_TIMEOUT_SECONDS=0`, a negative value, `inf` or `nan` is rejected.
- Missing host or sender is not a load error, because settings stay optional at startup
  (constitution V). Delivery reports it per notification, and `invio notify retry` exits 2.

## `.env.example` change

```diff
-INVIO_SMTP_STARTTLS=true
+INVIO_SMTP_SECURITY=starttls
+INVIO_SMTP_TIMEOUT_SECONDS=30
```
