# Quickstart: Validating `invio source discover` (gh-issue-36)

How to prove the feature works. Behaviour details: [spec.md](spec.md),
[contracts/cli-source-discover.md](contracts/cli-source-discover.md),
[contracts/python-api.md](contracts/python-api.md).

## Prerequisites

```bash
uv sync --locked
```

Unit tests need no network and no external database (SQLite is used for `--add-to`).

## 1. Automated checks (the gate)

```bash
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest tests/test_discover.py tests/test_cli_source.py tests/test_job_service.py \
  tests/test_http_robots.py tests/test_source_rss.py tests/test_source_sitemap.py \
  tests/test_source_youtube.py tests/test_cli_layering.py
uv run pytest            # full suite before the PR
```

## 2. Acceptance criteria → tests

| Issue acceptance criterion | Spec | Test (expected name) |
|---|---|---|
| Fixture page with an RSS `<link>` yields a validated RSS source | US1-1 | `test_discover_announced_rss_link_is_offered` |
| Probing finds `/sitemap.xml` on a fixture site | US2-1 | `test_discover_probes_sitemap_xml_at_root` |
| `--add-to` appends the chosen source and re-validates | US3-1, US3-8 | `test_cli_add_to_appends_and_revalidates`, `test_cli_add_to_invalid_result_is_not_saved` |
| Unreachable or invalid candidates are not offered | US1-4, US2-3 | `test_discover_drops_unreachable_and_invalid_findings` |

Further scenarios each get one test: folder probing (US2-5/6), `robots.txt` sitemaps and cap
(US2-7/8), comment feed ordering (US1-6), direct feed/sitemap/YouTube input (US1-5, US4-5),
YouTube channel/playlist recognition and de-duplication (US4-1..4), `--pick` and the
non-interactive single-candidate rule (US3-3..5), missing job before network (US3-6, asserted
by a transport that fails on any request), duplicate source (US3-7), private-address start page
(edge case), every printed snippet validating through `validate_job` (SC-003).

## 3. Manual smoke test (needs internet)

The SSRF guard refuses loopback and private addresses, so a local `http.server` cannot be used
from the CLI; the automated tests cover local fixtures. Against a public site:

```bash
uv run invio source discover https://blog.python.org
```

Expected: at least one `[n] rss …` entry with a title, a newest-entry date and a YAML snippet;
exit code 0 (`echo $?`). A nonsense host (`uv run invio source discover nothing.invalid`) prints
"No source found …" on stderr and exits 1; `uv run invio source discover ftp://x` exits 2
without network access.

## 4. `--add-to` round trip

```bash
uv run invio job list                                   # pick an existing job, e.g. news
uv run invio source discover https://blog.python.org --add-to news --pick 1
uv run invio job show news                              # new source is the last entry
uv run invio source discover https://blog.python.org --add-to news --pick 1
# → "Job 'news' already contains this source; nothing changed." exit 0
```
