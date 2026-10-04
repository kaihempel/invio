# Contract: `invio db` command group

Module: `src/invio/cli/commands/db.py` (auto-discovered; exposes `app: typer.Typer`).

## `invio db upgrade [REVISION]`

Apply pending schema migrations to the database configured in `INVIO_DATABASE_URL`.

| Argument | Default | Meaning |
|---|---|---|
| `REVISION` | `head` | Target Alembic revision (developers only; operators use the default) |

### Behaviour

- Connects using `INVIO_DATABASE_URL` (secret setting). For `mysql*`/`mariadb*` URLs,
  `charset=utf8mb4` is enforced (R3).
- Runs Alembic `upgrade` to `REVISION`. Already at target → no changes, success.
- Leaves tables that invio does not own untouched.

### Output

| Stream | Content |
|---|---|
| stdout | Exactly one line on success: `database at revision <revision-id>` (e.g. `database at revision 0001`) |
| stderr | Structured JSON log lines (`migration started`, `migration finished`, per-step Alembic info) and, on failure, a single line `Error: <message>` |

No output on any stream contains the database password or the unredacted URL.

### Exit codes

| Code | When | Example stderr |
|---|---|---|
| 0 | Upgrade applied or already up to date | — |
| 2 | `INVIO_DATABASE_URL` not set or empty | `Configuration error: INVIO_DATABASE_URL is not set` |
| 2 | URL cannot be parsed / unknown dialect or driver | `Configuration error: invalid database URL: …` |
| 2 | Driver package (DBAPI) not installed | `Configuration error: database driver not installed: …` |
| 1 | Database unreachable, auth failure, migration error, unknown revision | `Error: OperationalError: (2003, "Can't connect to MySQL server on 'db' …")` |

## Developer access (not part of the CLI)

From the repository root (uses `alembic.ini`, `script_location = invio.db:migrations`):

```bash
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic revision --autogenerate -m "<message>"
```

These read `INVIO_DATABASE_URL` from the environment / `.env` like the CLI.
