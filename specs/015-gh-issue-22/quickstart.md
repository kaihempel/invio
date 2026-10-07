# Quickstart: validating `invio job run` and run history (#22)

This guide lists the scenarios that prove the feature works. Command details are in
[contracts/cli-commands.md](contracts/cli-commands.md), and the stored data is described in
[data-model.md](data-model.md).

## Prerequisites

```bash
uv sync --locked
export INVIO_DATABASE_URL="sqlite:///$(mktemp -d)/invio.db"
uv run invio db upgrade
uv run invio job import docs/job.example.yaml --name demo   # or: invio job create demo
```

A live dry run against the example job calls its real sources and LLM provider, so it needs the
provider key (for example `INVIO_MISTRAL_API_KEY`). The automated tests need neither a network
nor a key.

## 1. Automated validation (the CI gates)

```bash
uv run ruff check && uv run ruff format --check && uv run mypy src
uv run pytest tests/test_cli_job_run.py tests/test_cli_run.py \
              tests/test_run_errors.py tests/test_run_progress.py tests/test_run_service.py -q
uv run pytest -q          # full suite stays green
```

Expected: all pass. The CLI tests run the real pipeline with `FakeProvider` (routed per
purpose), fixture sources and articles, a recording notifier and a fixed clock.

## 2. Scenario checklist

| # | Scenario | Command | Expected |
|---|---|---|---|
| 1 | Dry run prints the digest and stats and sends nothing | `invio job run demo --dry-run` | stdout: `# <title>` + digest body, stats table, `Tokens: …` line. No notification row, `next_run_at` unchanged (`invio job show demo`). Exit 0 |
| 2 | Nothing relevant | dry run of a job whose keywords match nothing | `No digest: nothing relevant was found.` + stats; exit 0 |
| 3 | Partial | fixture: one item's relevance reply invalid | exit 2; stderr `run <id> finished partial: 1 error(s); see 'invio run show <id>'` |
| 4 | Failed | fixture: every source raises `FetchError` | exit 1; stderr `run <id> failed: all sources failed` |
| 5 | Could not start | unknown name / disabled job / invalid stored config / lock held | one-line error, exit 1, **no new run row** (the busy case creates none either) |
| 6 | Item cap | `invio job run demo --dry-run --max-items 2` | at most 2 items processed (`Processed` ≤ 2) |
| 7 | Cap above limit | `--max-items 1000` (limit 100) | stderr note `using 100`; run proceeds; stored config unchanged |
| 8 | Invalid cap | `--max-items 0` | `Error: --max-items must be at least 1`, exit 1, no run row |
| 9 | Real run | `invio job run demo` | notification delivered; stats include `Notifications sent 1` |
| 10 | Verbose | `invio job run demo --dry-run -v 2>err.txt` | `err.txt` has one `item:` line per processed item |
| 11 | Clean stdout | `invio job run demo --dry-run > out.md 2>/dev/null` | `out.md` contains only the digest, table and usage line, with no progress lines |
| 12 | History list | `invio run list`, `invio run list --job demo` | newest first; dry runs marked `(dry)`; an unknown job exits 1; no runs gives `No runs.`, exit 0 |
| 13 | History detail | `invio run show <id of #3>` | `Errors (1)` with stage, message, title and URL of the failed item |
| 14 | History stable after retry | run #3's job again so the item succeeds, then `invio run show <id of #3>` | the earlier run still lists its item error |
| 15 | Dry-run errors kept | `invio run show <id of a dry run with a failed item>` | the error is listed even though the items were rolled back |
| 16 | Legacy run | a run whose stats lack `errors` | `Errors: item errors are not available for this run` |
| 17 | Cost display | runs with all, some or no priced calls | `$0.0103` / `≥ $0.0050 (2 calls without price)` / `unknown`; no LLM calls gives `$0.00` |
| 18 | Ctrl-C | interrupt a live run | `interrupted; run recorded as failed`, exit 1; `invio run show` shows `failed`, and the job lock is released |

## 3. Manual spot check of the live display

Run `invio job run demo --dry-run` in a real terminal. The live panel updates found, then new,
then relevant, and disappears when the run ends. The final output is then printed once.
