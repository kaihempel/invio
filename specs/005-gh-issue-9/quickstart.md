# Quickstart: Validate `invio job`

**Feature**: [spec.md](./spec.md) · **CLI contract**: [contracts/cli-job.md](./contracts/cli-job.md)
· **API**: [contracts/python-api.md](./contracts/python-api.md)

## Prerequisites

```bash
uv sync --locked
```

The automated tests need no network, no terminal, no editor and no database server. They use
SQLite with fakes for the prompter, source checker and editor.

## 1. Automated checks (same as CI)

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest tests/test_cli_job*.py tests/test_source_check.py tests/test_wizard.py -q
uv run pytest            # full suite incl. coverage gate (fail_under = 95)
```

Expected: everything passes. Every acceptance scenario in spec.md has at least one test
(FR-036).

## 2. Manual walkthrough (real terminal, SQLite file)

```bash
export INVIO_DATABASE_URL=sqlite:///scratch.db
uv run invio db upgrade
```

| # | Command | Expected |
|---|---|---|
| 1 | `uv run invio job list` | `no jobs yet — …`, exit 0 |
| 2 | `uv run invio job create` and enter `7:30` as the time, then `me@example` as the recipient | Inline error after each; the same question is asked again; the wizard continues |
| 3 | Same wizard: `rss` source `https://example.com/` | Warning "no RSS/Atom feed detected"; answering *No* asks for the URL again |
| 4 | Finish the wizard, confirm the preview | `created job '…' (next run: …)` |
| 5 | `uv run invio job list` | Row with `yes`, frequency, next run in the job's zone, `never run` |
| 6 | `uv run invio job create`, press Ctrl+C at any step | No job saved, exit 1, no traceback |
| 7 | `uv run invio job export ai-news -o ai.yaml` then `uv run invio job import ai.yaml` | Import fails (`already exists`, exit 1); with `--name ai-copy` it succeeds |
| 8 | `uv run invio job create --from-file docs/job.example.yaml < /dev/null \| cat` | Created without prompts (no TTY), exit 0 |
| 9 | `uv run invio job create < /dev/null \| cat` | Exit 2, message points to `--from-file` |
| 10 | `EDITOR=nano uv run invio job edit ai-news`, set `time: "25:00"`, save | Errors listed; `Re-open the editor?` → *n* → `edit aborted; job unchanged`, exit 1 |
| 11 | `uv run invio job edit ai-news`, fix to a valid time, save | `updated job …`, next run recalculated |
| 12 | `uv run invio job disable ai-news`, then `uv run invio job list` | `no` and `—` for next run; `enable` restores both |
| 13 | `uv run invio job delete ai-news` → *n* | `job not deleted`, exit 1 |
| 14 | `uv run invio job delete ai-news < /dev/null` | Exit 2, asks for `--yes` |
| 15 | `uv run invio job delete ai-news --yes` | `deleted job 'ai-news'` |

### Invalid stored configuration (clarification Q4)

```bash
sqlite3 scratch.db "UPDATE jobs SET config = json_set(config, '$.schedule.timezone', 'Europe/Atlantis') WHERE name = 'ai-copy';"
uv run invio job list            # row shows "invalid config"
uv run invio job show ai-copy    # stored YAML + errors, exit 2
uv run invio job enable ai-copy  # exit 2
uv run invio job edit ai-copy    # errors shown first; fix the zone → "updated job 'ai-copy'"
```

## 3. Clean up

```bash
rm -f scratch.db ai.yaml
```
