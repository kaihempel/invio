# Quickstart: validating the static digest archive

## Prerequisites

`uv sync --locked`; a scratch archive directory; no network needed.

## 1. Automated checks (the gates from CI)

```sh
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest tests/test_archive_slug.py tests/test_archive_write.py tests/test_archive_delivery.py \
  tests/test_job_config.py tests/test_notify_payload.py tests/test_notify_render.py tests/test_settings.py
uv run pytest            # whole suite, incl. tests/test_notify_layering.py and the job schema test
```

## 2. Manual end-to-end (uses the fake LLM and a local SMTP sink, as in #20 quickstart)

1. Add to a job file:

   ```yaml
   archive:
     enabled: true
     base_url: https://digests.example.org/invio
   ```

2. Run with a scratch archive: `INVIO_ARCHIVE_DIR=$(mktemp -d) uv run invio job run <name>`.
3. Expected: `<archive_dir>/<slug>/<YYYY-MM-DD-HHMM>.html`, `<slug>/index.html`, `index.html`
   exist and are mode `0644`; no `*.tmp` files.
4. Open `index.html` in a browser with the network disabled: pages are styled, links work.
5. `grep -Ei '<script|<link|<img|src=|@import|url\(' -r <archive_dir>` finds nothing.
6. The received mail footer shows `https://digests.example.org/invio/<slug>/<name>.html`.
7. Serve with any static server (`python -m http.server -d <archive_dir>`) and open the mail link path.

## 3. Failure and edge checks

| Scenario | Expected |
|---|---|
| Job named `../../etc/x` or `日本語/ニュース` | directory is a safe slug inside `archive_dir` |
| Two jobs `A b` and `a-b` | two distinct directories (second gets `-<hash8>`) |
| Two runs in the same minute | `…-0930.html` and `…-0930-2.html` both exist |
| `archive_dir` not writable | run succeeds, mail sent **without** archive link, `archive.failed` in the log |
| `enabled: true`, no `base_url` | page written, mail has no archive link |
| Empty digest / failed run / dry run | no files created or changed |
| `invio notify retry` after a failed send | retried mail carries the link if the page still exists |

## 4. T036 run (2026-10-08): scripted, deviations

§2 and every §3 row are scripted in `tests/test_archive_quickstart.py` (`invio job run` and
`invio notify retry` through the CLI, `INVIO_*` settings, SQLite file, real `deliver_digest`,
in-process SMTP sink and a `http.server` on 127.0.0.1). Run-level gating (failed, partial, dry,
empty) is also covered in `tests/test_archive_pipeline.py`. All rows pass, with these deviations:

- §2 step 2: `uv run invio job run` fetches real sources and calls a real LLM, so the script
  swaps the pipeline ports for the fakes of `tests/pipeline_helpers.py`; there is no CLI switch
  to run the job against the fake LLM.
- §2 step 3: besides the three HTML files each job directory holds a `.job-name` marker (also
  `0644`); directories are `0755`.
- §2 step 4 (browser, network off) is replaced by the step-5 regex scan of every file plus the
  step-7 static server click-through (global index → job index → page, mail link path → 200).
- §3 `archive_dir` not writable: checked with a `0555` directory (not only a file in its place);
  `archive.failed` appears in the CLI output, exit code 0, the directory stays empty.
- §3 two jobs `A b` / `a-b`: holds, but a third job literally named `a-b-<sha256("A b")[:8]>`
  archived before `A b` makes `A b` publish into that job's directory (FR-009 gap in
  `job_directory`; pinned as strict xfail in `tests/test_archive_collision.py`).
