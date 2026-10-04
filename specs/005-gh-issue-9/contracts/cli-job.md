# Contract: `invio job` command group

Module: `src/invio/cli/commands/job.py` (auto-discovered as `invio job`). Results go to stdout;
errors, warnings and logs go to stderr. Every command needs `INVIO_DATABASE_URL`. If it is
missing, the command prints `Configuration error: …` and exits 2.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success, including no-op enable/disable and an unchanged edit |
| 1 | Not found, already exists, invalid name, aborted (Ctrl+C / EOF), preview or delete declined, edit aborted |
| 2 | Configuration or usage error: invalid job file or stored config, missing setting, interactive command without a TTY |

Error message shapes (stderr, single header line, then indented details):

```text
Error: job 'ai-news' not found
Error: job 'ai-news' already exists
Error: invalid job name '': must not be empty
invalid job file job.yaml:
  schedule.time: String should match pattern '^([01][0-9]|2[0-3]):[0-5][0-9]$'
invalid stored job 'ai-news':
  schedule.timezone: unknown timezone 'Europe/Atlantis'
Error: interactive creation needs a terminal; use 'invio job create --from-file <job.yaml>'
```

## Commands

### `invio job create [--from-file PATH] [--name NAME]`

- Without `--from-file`, the command runs the wizard (spec FR-004 to FR-016). If there is no
  TTY it exits 2 with the message above. `--name` replaces step 1. It is validated before the
  first question: an invalid or existing name exits 1 without starting the wizard.
- Ctrl+C at any point, including during a reachability check, prints `aborted; nothing saved`
  and exits 1 with no traceback.
- With `--from-file`, there are no prompts and no network calls. The name is `--name` or the
  file stem. An existing name gives exit 1. On success it prints
  `created job 'NAME' (next run: 2026-10-05 07:00 CEST)`.
- Wizard success prints the same line after the YAML preview has been confirmed. A declined
  preview prints `job not created` and exits 1.

### `invio job list`

A table on stdout with the columns `NAME | ENABLED | FREQUENCY | NEXT RUN | LAST STATUS`,
sorted by name.

- `ENABLED`: `yes` or `no`.
- `FREQUENCY`: `daily 07:00`, `weekly mon 07:30`, `monthly 31 06:00`, plus the zone, e.g.
  `weekly mon 07:30 Europe/Berlin`. `—` for an invalid config.
- `NEXT RUN`: `YYYY-MM-DD HH:MM <TZ abbr>` in the job's zone. `—` when disabled or invalid.
- `LAST STATUS`: `invalid config` | `running` | `succeeded` | `partial` | `failed` |
  `never run`.
- With no jobs it prints `no jobs yet — create one with 'invio job create'` and exits 0.

### `invio job show NAME`

- Prints `name:`, `enabled:` and `next run:` header lines, a blank line, then the full YAML
  (`dump_yaml`).
- For an invalid stored config it prints the stored content as YAML, writes the error block to
  stderr, and exits 2.

### `invio job edit NAME`

1. The command loads the YAML: `dump_yaml(config)` for a valid job, or `yaml.safe_dump(stored)`
   plus the error block shown first for an invalid one.
2. It opens the editor (`$VISUAL`/`$EDITOR`, falling back to `vi` or `notepad`).
3. If the file is not saved or the text is unchanged, it prints `no changes` and exits 0.
4. It parses the text with `loads_yaml`. If that fails, it prints the errors and asks
   `Re-open the editor? [Y/n]`. Yes reopens the editor with the *edited* text; no prints
   `edit aborted; job unchanged` and exits 1.
5. If the text is valid, it calls `JobService.update` and prints
   `updated job 'NAME' (next run: …)`.

An editor that exits non-zero is treated as abort (exit 1, job unchanged). The job name never
comes from the file content.

### `invio job enable NAME` / `invio job disable NAME`

- `enable` prints `enabled job 'NAME' (next run: …)`, or `job 'NAME' is already enabled`.
  Enabling an invalid stored config gives the error block and exit 2.
- `disable` prints `disabled job 'NAME'`, or `job 'NAME' is already disabled`. Disabling an
  invalid stored config succeeds (exit 0) and additionally warns on stderr that the stored
  configuration is invalid.

### `invio job delete NAME [--yes/-y]`

- Without `--yes`, it asks `Delete job 'NAME' and its run history? [y/N]`. Declining prints
  `job not deleted` and exits 1.
- Without `--yes` and without a TTY it exits 2 with
  `Error: refusing to delete without confirmation; pass --yes`.
- Success prints `deleted job 'NAME'`. This works for invalid stored configs too.

### `invio job export NAME [--output/-o PATH]`

- Writes the YAML to stdout, or atomically to `PATH` (`write_yaml`), then prints
  `exported job 'NAME' to PATH`.
- An invalid stored config gives the error block and exit 2.

### `invio job import FILE [--name NAME] [--replace]`

- Stores `FILE` under `--name`, or under the file stem when `--name` is absent. There are no
  prompts and no network calls.
- An existing name without `--replace` gives exit 1. With `--replace` the job is updated.
- An invalid file gives exit 2 with the error block.
- Prints `imported job 'NAME'` or `replaced job 'NAME'`.

## Wizard transcript (illustrative)

```text
? Job name: ai-news
? How often should it run? weekly
? On which weekday? monday
? At what time (HH:MM)? 7:30
  ✗ time must be HH:MM (00:00–23:59)
? At what time (HH:MM)? 07:30
? Time zone: Europe/Berlin
? Recipient e-mail: me@example
  ✗ not a valid e-mail address
? Recipient e-mail: me@example.com
? Add another recipient? No
? E-mail subject: invio: ai-news
? Source type: rss
? Feed URL: https://example.com/blog
  ! reachable, but no RSS/Atom feed detected
? Keep this source anyway? No
? Feed URL: https://example.com/feed.xml
  ✓ feed detected
? Add another source? No
? Keywords — any of (comma-separated): agents, LangGraph
? Keywords — all of: 
? Keywords — exclude: crypto
? Describe what you are looking for (finish with Esc+Enter):
  New open-source agent frameworks and notable releases.
? LLM provider: openai
? Fast model: gpt-small
? Smart model: Other…
? Smart model id: gpt-huge
  ! model 'gpt-huge' is not registered for provider 'openai'; runs will fail until it is added to models.d/openai.yaml
? Use it anyway? Yes
? Keep the default limits? Yes

schema_version: 1
schedule:
  frequency: weekly
  ...

? Save this job? Yes
created job 'ai-news' (next run: 2026-10-05 07:30 CEST)
```
