# Quickstart: Validating the Job Configuration Feature

References: [job file contract](./contracts/job-file.md) ·
[Python API](./contracts/python-api.md) · [data model](./data-model.md)

## Prerequisites

```bash
uv sync          # installs new deps: pyyaml, email-validator; dev: types-PyYAML, jsonschema
```

## 1. All quality gates (constitution IV)

```bash
uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest
```

Expected: all green. Feature tests live in `tests/test_job_config.py`, `tests/test_job_schedule.py`,
`tests/test_job_sections.py`, `tests/test_job_sources.py`, `tests/test_job_providers.py`,
`tests/test_job_yaml.py`, `tests/test_job_schema.py`, `tests/test_domain.py`.

## 2. Reference example loads (SC-001)

```bash
uv run python -c "from invio.config.job import load_yaml; print(load_yaml('docs/job.example.yaml').schedule)"
```

Expected: prints `frequency=<Frequency.WEEKLY: 'weekly'> time='07:30' weekday=<Weekday.MONDAY: 'monday'> …`, no error.

## 3. Invalid file is rejected with field-level messages (SC-002, SC-007)

```bash
cat > /tmp/bad-job.yaml <<'EOF'
schedule: {frequency: weekly, time: "25:00", timezone: Europe/Atlantis}
notification: {to: [not-an-email], subject: x}
sources: []
search: {semantic_description: " ", min_relevance: 1.5}
llm: {provider: openai, models: {fast: a, smart: b}, fallback_provider: openai, frequncy: 1}
EOF
uv run python -c "from invio.config.job import load_yaml; load_yaml('/tmp/bad-job.yaml')"
```

Expected: `JobConfigError` listing one line each for `schedule.time`, `schedule.timezone`,
`notification.to[0]`, `sources`, `search.semantic_description`, `search.min_relevance` and
`llm.frequncy`. There are no `schedule:` (weekday required) or `llm:` (fallback must differ)
lines: Pydantic skips a section's cross-field validators while any of that section's fields
fail, so those lines appear only after the field errors are fixed.

## 4. Round-trip (SC-003)

```bash
uv run python - <<'EOF'
from invio.config.job import dump_yaml, load_yaml, write_yaml
a = load_yaml("docs/job.example.yaml")
write_yaml(a, "/tmp/job-roundtrip.yaml")
assert load_yaml("/tmp/job-roundtrip.yaml") == a
print(dump_yaml(a))
EOF
```

Expected: assertion passes; output shows every field incl. defaults, keys in contract order.

## 5. Schema is current and validates the example (SC-005, FR-029)

```bash
uv run python -m invio.config.job && git diff --exit-code docs/job.schema.json
```

Expected: no diff. `tests/test_job_schema.py` also validates `docs/job.example.yaml` with
`jsonschema`.

## 6. Domain records are dependency-free (SC-006)

```bash
uv run python -X importtime -c "import invio.domain" 2>&1 | grep -E "pydantic|yaml|sqlalchemy|httpx" || echo "clean"
```

Expected: `clean`.
