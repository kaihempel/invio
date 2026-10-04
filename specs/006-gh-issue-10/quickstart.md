# Quickstart / Validation Guide: Safe Shared HTTP Client (gh-issue-10)

Proves the feature end-to-end. No internet access is needed; every test runs against loopback
servers or `httpx.MockTransport`. API details: [contracts/python-api.md](contracts/python-api.md);
entities and defaults: [data-model.md](data-model.md).

## Prerequisites

```bash
uv sync --locked          # httpx is added to [project.dependencies] by this feature
```

## 1. Quality gates (same as CI)

```bash
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
```

Expected: all green; coverage stays ≥ 95 % (`fail_under`).

## 2. Feature tests

```bash
uv run pytest tests/test_http_netguard.py tests/test_http_client.py \
              tests/test_http_robots.py tests/test_http_ratelimit.py -v
```

| Acceptance criterion (issue #10) | Test evidence |
|----------------------------------|---------------|
| `http://127.0.0.1`, `http://10.0.0.5`, `file:///etc/passwd` rejected | `BlockedError` with `non_public_address` / `unsupported_scheme`; `MockTransport` records zero requests |
| Redirect to a private IP rejected | loopback server returns `302 Location: http://10.0.0.5/` → `BlockedError`, no request to `10.0.0.5` |
| Response larger than limit → `TooLargeError` | routes with oversized `Content-Length`, chunked body without length, and gzip body that inflates past the limit |
| robots.txt-disallowed paths not fetched | server log contains `/robots.txt` once and never `/private/…` or `/feed.xml` when disallowed |
| Same-host requests spaced by interval | two concurrent `get`s with interval 0.2 s → server timestamps ≥ 0.2 s apart; second origin not delayed; `Crawl-delay: 1` raises spacing to ≥ 1 s |
| Local test server only | tests use `127.0.0.1` servers created with `allow_networks=[127.0.0.0/8]` |

Additional checks: 6-hop redirect chain → `too_many_redirects`; stalled server → `timeout`
within the total deadline; second `get` of a URL with `ETag` sends `If-None-Match` and returns
`NotModified`; robots.txt 404 → allowed, 503 → `blocked_by_robots`; DNS-rebinding fake resolver
→ request pinned to the first validated IP with original `Host` and `sni_hostname`.

## 3. Manual smoke check (optional, needs internet)

```bash
uv run python - <<'EOF'
import asyncio
from invio.sources.http import SafeHttpClient, BlockedError

async def main() -> None:
    async with SafeHttpClient() as client:
        result = await client.get("https://www.python.org/")
        print(result.status, len(result.content))
        try:
            await client.get("http://169.254.169.254/latest/meta-data/")
        except BlockedError as exc:
            print("blocked:", exc.reason)

asyncio.run(main())
EOF
```

Expected: `200 <bytes>` then `blocked: non_public_address`.

## 4. Configuration check

```bash
INVIO_HTTP_HOST_INTERVAL_SECONDS=0 uv run python -c \
  "from invio.config.settings import Settings; Settings()"
```

Expected: validation error naming `http_host_interval_seconds` (FR-004).
