# Quickstart: Validating the Persistent Data Store

**Feature**: [spec.md](./spec.md) · Contracts: [cli-db.md](./contracts/cli-db.md),
[python-api.md](./contracts/python-api.md) · Schema: [data-model.md](./data-model.md)

## Prerequisites

- `uv sync` done (pulls SQLAlchemy, Alembic, PyMySQL).
- Optional: Docker, for the MariaDB scenarios.

## 1. Default test suite (no database server)

```bash
uv run pytest
uv run pytest -m db          # only the persistence tests
```

Expected: all pass on in-memory/temp-file SQLite, no network access; the `db` tests add
< 10 s (SC-006) — check with `uv run pytest -m db --durations=10` (total wall time in the summary). Covers upgrade/downgrade, duplicate `(job_id, url_hash)` → `IntegrityError`,
job cascade, run/digest `SET NULL`, status vocabularies, relevance precision, umlaut/emoji
round-trip, due-job lookup, model ↔ migration drift.

## 2. Migrations on SQLite via the CLI

```bash
export INVIO_DATABASE_URL="sqlite:///$PWD/invio-dev.sqlite"
uv run invio db upgrade            # stdout: database at revision 0001 ; exit 0
uv run invio db upgrade            # again: same output, no changes ; exit 0
sqlite3 invio-dev.sqlite ".tables" # alembic_version digests items jobs llm_usage notifications runs
uv run alembic downgrade base      # developer-only
sqlite3 invio-dev.sqlite ".tables" # alembic_version
rm invio-dev.sqlite
```

## 3. Migrations on a fresh MariaDB

```bash
docker run -d --name invio-mariadb -p 127.0.0.1:3306:3306 \
  -e MARIADB_ALLOW_EMPTY_ROOT_PASSWORD=1 -e MARIADB_DATABASE=invio_test mariadb:11.4
export INVIO_DATABASE_URL="mysql+pymysql://root@127.0.0.1:3306/invio_test"
uv run invio db upgrade            # charset=utf8mb4 is added automatically ; exit 0
docker exec invio-mariadb mariadb -uroot invio_test \
  -e "SELECT table_name, engine, table_collation FROM information_schema.tables WHERE table_schema='invio_test';"
# 6 invio tables + alembic_version, ENGINE=InnoDB, utf8mb4_unicode_ci
uv run alembic downgrade base      # only alembic_version remains
```

## 4. Persistence tests against MariaDB (as the optional CI job does)

```bash
INVIO_TEST_DATABASE_URL="mysql+pymysql://root@127.0.0.1:3306/invio_test?charset=utf8mb4" \
  uv run pytest -m db
docker rm -f invio-mariadb
```

Expected: the same `db` tests pass on MariaDB.

## 5. Error paths of `invio db upgrade`

```bash
unset INVIO_DATABASE_URL; uv run invio db upgrade; echo $?
# stderr: Configuration error: INVIO_DATABASE_URL is not set → 2
INVIO_DATABASE_URL="mysql+pymysql://invio:s3cret@127.0.0.1:1/x" uv run invio db upgrade; echo $?
# stderr: Error: OperationalError: … (no "s3cret" anywhere) → 1
```

## 6. Quality gates

```bash
uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest --cov
```

Expected: all green, coverage ≥ 95 %.

## Acceptance criteria → scenario

| Issue acceptance criterion | Scenario |
|---|---|
| `alembic upgrade head` creates all tables on fresh MariaDB and SQLite | 2, 3, tests |
| `alembic downgrade base` removes them | 2, 3, tests |
| Duplicate `(job_id, url_hash)` → integrity error | 1, 4 |
| Deleting a job cascades | 1, 4 |
| Umlauts/emoji round-trip | 1, 4 |
| Repository tests pass on SQLite and MariaDB | 1, 4 |
