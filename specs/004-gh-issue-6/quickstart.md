# Quickstart: Validate Schedule Calculation

**Feature**: [spec.md](spec.md) | **Contract**: [contracts/python-api.md](contracts/python-api.md)

## Prerequisites

```bash
uv sync --locked          # includes the dev group (pytest, hypothesis)
```

## 1. Automated tests

```bash
uv run pytest tests/test_next_run.py tests/test_next_run_properties.py -q
uv run pytest tests/test_job_service.py -q          # JobService still green with the real default
uv run pytest --cov                                 # coverage gate (fail_under = 95)
```

Expected:
- All tests pass.
- The parameterised cases cover daily, weekly and monthly runs, the day-31 clamp (Feb 28/29,
  Apr 30, back to Mar 31), the spring-forward gap, the fall-back overlap, an exact match and a
  naive input.
- The Hypothesis properties report no counterexamples.

## 2. Manual spot check (reference examples)

```bash
uv run python - <<'EOF'
from datetime import datetime, UTC
from invio.config.job import ScheduleConfig
from invio.scheduling.next_run import compute_next_run

s = ScheduleConfig(frequency="daily", time="02:30", timezone="Europe/Berlin")
print(compute_next_run(s, datetime(2026, 3, 28, 12, tzinfo=UTC)))   # 2026-03-29 01:30:00+00:00
print(compute_next_run(s, datetime(2026, 10, 24, 12, tzinfo=UTC)))  # 2026-10-25 00:30:00+00:00

m = ScheduleConfig(frequency="monthly", day_of_month=31, time="08:00", timezone="UTC")
print(compute_next_run(m, datetime(2028, 1, 31, 9, tzinfo=UTC)))    # 2028-02-29 08:00:00+00:00
EOF
```

Compare the output with the reference table in
[contracts/python-api.md](contracts/python-api.md#reference-examples-normative-mirrored-in-the-tests).

## 3. JobService integration

Create a job through `JobService` using its default `next_run`. `next_run_at` should be the
next configured local time in UTC, not "now". See the README section on the job service.

## 4. Quality gates (same as CI)

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```
