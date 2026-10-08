# Quickstart: validating the YouTube source

**Feature**: [spec.md](spec.md) · **Contracts**: [contracts/](contracts/) · **Data model**: [data-model.md](data-model.md)

## Prerequisites

```bash
uv sync --locked          # installs yt-dlp together with the other dependencies
```

No network or YouTube account is needed for the automated checks.

## 1. Automated checks (mirror CI)

```bash
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest tests/test_source_youtube.py tests/test_job_sources.py \
  tests/test_settings.py tests/test_pipeline_deps.py tests/test_pipeline_run.py
```

Expected: all green. `tests/test_source_youtube.py` covers, with a fake extractor:

| Scenario (spec) | Expected |
|---|---|
| channel id / URL / `@handle` (US1) | one `type="video"` candidate per entry, canonical watch URL |
| playlist (US2) | same; entry without id/title skipped |
| `max_age_days` + undated videos (US3) | old dropped, undated kept after dated |
| `max_items` default 20 / explicit 5 (US3) | never more than the limit, newest first |
| timeout, block (429), not found (404), connection error (US4) | `FetchError` with reasons `timeout`, `http_status`, `http_status`, `connection_failed` |
| cookies / proxy configured (US4) | passed to the extractor options; absent when unset; missing cookies file → `cookies_unavailable` |
| hostile locators (`file://…`, `http://169.254.169.254`, other hosts) | rejected at config load |

## 2. End-to-end with a fake extractor

`tests/test_pipeline_run.py` runs a job with a `youtube_channel` source and one RSS source whose
fetch fails: the YouTube candidates reach the pipeline as `video` items, the failing source is
recorded in `runs.stats["errors"]`, and the run completes.

## 3. Manual smoke test (optional, needs network; also the only check of SC-002, listing ≤20 videos in under 10 s)

```bash
cat > /tmp/yt-job.yaml <<'EOF'
# a valid job file with one source:
#   - type: youtube_channel
#     channel_id: "@YouTube"
#     max_items: 5
EOF
uv run invio job run <name> --dry-run
```

Expected: up to 5 video candidates in the run output, no media files created. From a blocked
server IP the run reports a `fetch` error for that source and still finishes; setting
`INVIO_YOUTUBE_COOKIES_FILE` or `INVIO_YOUTUBE_PROXY` is the remedy documented in the README.
