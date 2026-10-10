"""End-to-end scenarios: YAML job, ``invio run-due``, digest, mail and archive.

Each scenario drives the real CLI over local fixtures (contract:
specs/022-gh-issue-26/contracts/e2e-harness.md). Selected with ``-m e2e``.
"""

import re
import socket
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from tests.e2e_helpers import (
    RECIPIENTS,
    E2EEnv,
    ExternalNetworkError,
    archive_pages,
    assert_no_external_resources,
    e2e_env,  # noqa: F401
    initial_articles,
    mail_texts,
    no_external_network,  # noqa: F401
    run_scenario_a,
)
from tests.http_helpers import LoopbackServer, Route

pytestmark = pytest.mark.e2e

A1, A2, A3, X1 = (article.title for article in initial_articles(datetime(2026, 1, 1, tzinfo=UTC)))


def outcome_line(stdout: str, job_name: str, status: str) -> re.Match[str] | None:
    """The ``ran <job> run <id> <status>`` line of ``run-due`` (the summary line also names the
    statuses, so a plain substring check proves nothing)."""
    pattern = rf"^ran\s+{re.escape(job_name)}\s+run \d+\s+{status}\b"
    return re.search(pattern, stdout, re.MULTILINE)


def test_e2e_network_guard_blocks_external_hosts(
    no_external_network: list[str],  # noqa: F811
) -> None:
    with pytest.raises(ExternalNetworkError):
        socket.create_connection(("93.184.216.34", 80), timeout=1)
    with pytest.raises(ExternalNetworkError):
        socket.getaddrinfo("example.com", 80)
    with pytest.raises(ExternalNetworkError):
        socket.socket().connect(("::2", 80))
    server = LoopbackServer()
    server.routes["/"] = Route(body=b"ok")
    server.start()
    try:
        assert httpx2.get(server.base_url + "/").text == "ok"
    finally:
        server.stop()
    assert len(no_external_network) == 3
    no_external_network.clear()  # the attempts above were the point of this test


def test_e2e_first_run_delivers_digest_mail_and_archive(e2e_env: E2EEnv) -> None:  # noqa: F811
    env = e2e_env
    result = run_scenario_a(env)

    assert outcome_line(result.stdout, env.job_name, "succeeded"), result.stdout
    seen = env.snapshot()
    assert [status for status, _errors in seen.runs] == ["succeeded"]
    assert seen.runs[0][1] == ()
    assert set(seen.items) == {A1, A2, A3, X1}
    ids = {title: seen.items[title][1] for title in (A1, A2, A3)}
    assert len(seen.digests) == 1
    assert set(seen.digests[0]) == set(ids.values())
    assert seen.items[X1][1] not in seen.digests[0]

    link = re.compile(
        rf"https://archive\.example\.test/digests/{re.escape(env.slug)}/[\w.-]+\.html"
    )
    for recipient in RECIPIENTS:
        (text,) = mail_texts(env, recipient)
        assert all(title in text for title in (A1, A2, A3))
        assert X1 not in text
        assert link.search(text), text
    assert len(env.mail.messages) == len(RECIPIENTS)

    (page,) = archive_pages(env)
    for index in (env.archive_dir / env.slug / "index.html", env.archive_dir / "index.html"):
        assert index.is_file()
    assert f'href="{page.name}"' in (env.archive_dir / env.slug / "index.html").read_text("utf-8")
    assert f'href="{env.slug}/index.html"' in (env.archive_dir / "index.html").read_text("utf-8")
    for path in env.archive_dir.rglob("*.html"):
        assert_no_external_resources(path)

    next_run_at, locked_until = env.job_schedule()
    assert locked_until is None
    assert next_run_at == env.expected_next_run()
    assert next_run_at == datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
    assert env.clock() + timedelta(hours=1) != next_run_at  # not the retry slot
