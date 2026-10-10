"""End-to-end scenarios: YAML job, ``invio run-due``, digest, mail and archive.

Each scenario drives the real CLI over local fixtures (contract:
specs/022-gh-issue-26/contracts/e2e-harness.md). Selected with ``-m e2e``.
"""

import dataclasses
import re
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import select

from invio.db.models import Item
from invio.db.session import session_factory, session_scope
from tests.e2e_helpers import (
    RECIPIENTS,
    E2EArticle,
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
    no_external_network: list[str],
) -> None:
    with pytest.raises(ExternalNetworkError):
        socket.create_connection(("93.184.216.34", 80), timeout=1)
    with pytest.raises(ExternalNetworkError):
        socket.getaddrinfo("example.com", 80)
    with pytest.raises(ExternalNetworkError), socket.socket() as sock:
        sock.connect(("198.51.100.7", 80))
    server = LoopbackServer()
    server.routes["/"] = Route(body=b"ok")
    server.start()
    try:
        assert httpx2.get(server.base_url + "/").text == "ok"
    finally:
        server.stop()
    assert len(no_external_network) == 3
    no_external_network.clear()  # the attempts above were the point of this test


def test_e2e_first_run_delivers_digest_mail_and_archive(e2e_env: E2EEnv) -> None:
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


def test_e2e_rerun_without_new_articles_sends_nothing(e2e_env: E2EEnv) -> None:
    env = e2e_env
    run_scenario_a(env)
    before = env.snapshot()

    env.clock.advance(timedelta(days=1))
    env.make_due()
    rerun = env.run_due()

    assert rerun.exit_code == 0, rerun.output
    assert outcome_line(rerun.stdout, env.job_name, "succeeded"), rerun.stdout
    after = env.snapshot()
    assert [status for status, _errors in after.runs] == ["succeeded", "succeeded"]
    assert after.items == before.items
    assert after.digests == before.digests
    assert after.notifications == before.notifications
    assert after.mails == before.mails
    assert after.pages == before.pages
    assert after.relevance_calls == before.relevance_calls
    assert after.summarize_calls == before.summarize_calls
    assert after.locked_until is None
    assert after.next_run_at == env.expected_next_run()

    idle = env.run_due()  # not made due: nothing may run
    assert idle.exit_code == 0, idle.output
    assert "nothing due" in idle.stdout
    assert len(env.snapshot().runs) == 2


def test_e2e_new_article_is_the_only_item_processed(e2e_env: E2EEnv) -> None:
    env = e2e_env
    run_scenario_a(env)
    before = env.snapshot()
    a4 = E2EArticle("a4", "Delta Harbor Digest", env.clock() + timedelta(days=1, hours=-1))
    env.feed.add(a4)

    env.clock.advance(timedelta(days=1))
    env.make_due()
    result = env.run_due()

    assert result.exit_code == 0, result.output
    assert outcome_line(result.stdout, env.job_name, "succeeded"), result.stdout
    after = env.snapshot()
    assert [status for status, _errors in after.runs] == ["succeeded", "succeeded"]
    assert set(after.items) == set(before.items) | {a4.title}
    assert after.relevance_calls - before.relevance_calls == {a4.title: 1}
    assert after.summarize_calls - before.summarize_calls == {a4.title: 1}
    assert len(after.digests) == 2
    assert after.digests[1] == (after.items[a4.title][1],)

    for recipient in RECIPIENTS:
        old, new = mail_texts(env, recipient)
        assert a4.title in new
        assert not any(title in new for title in (A1, A2, A3))
        assert a4.title not in old
    assert after.mails == before.mails + len(RECIPIENTS)

    assert len(after.pages) == 2
    older, newer = (Path(page).name for page in after.pages)
    for index in (env.archive_dir / env.slug / "index.html",):
        html = index.read_text("utf-8")
        assert html.index(f'href="{newer}"') < html.index(f'href="{older}"')
    assert "2 pages" in (env.archive_dir / "index.html").read_text("utf-8")


def test_e2e_one_failing_item_gives_partial_run_and_still_mails(
    e2e_env: E2EEnv,
) -> None:
    env = e2e_env
    env.set_articles(
        [
            dataclasses.replace(article, fail_at="relevance") if article.title == A2 else article
            for article in env.feed.articles
        ]
    )
    result = run_scenario_a(env)

    assert outcome_line(result.stdout, env.job_name, "partial"), result.stdout
    seen = env.snapshot()
    ((status, errors),) = seen.runs
    assert status == "partial"
    (error,) = errors
    assert error["stage"] == "score_relevance"
    assert error["error_class"] == "RuntimeError"
    assert error["item_id"] == seen.items[A2][1]
    assert seen.items[A2][0] == "failed"
    (digest,) = seen.digests
    assert set(digest) == {seen.items[A1][1], seen.items[A3][1]}

    for recipient in RECIPIENTS:
        (text,) = mail_texts(env, recipient)
        assert A1 in text
        assert A3 in text
        assert A2 not in text
    assert len(seen.pages) == 1
    assert seen.locked_until is None
    # The regular slot, not the retry time: a partial run is not a failed run.
    assert seen.next_run_at == env.expected_next_run()

    assert "boom secret" not in repr(seen.runs)
    assert all(
        "boom secret" not in mail.raw.decode("utf-8", "replace")
        for mail in env.mail.handler.received
    )
    with session_scope(session_factory(env.assert_engine)) as session:
        last_error = session.scalars(select(Item.last_error).where(Item.title == A2)).one()
    assert last_error is not None
    assert last_error.startswith("RuntimeError")
    assert "boom secret" not in last_error
