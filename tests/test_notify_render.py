"""Tests for subject rendering, the Markdown to HTML pipeline and MIME message building."""

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
from jinja2 import Environment, PackageLoader

from invio.notify.payload import NotificationPayload
from invio.notify.render import (
    IndexEntry,
    RenderedMail,
    build_message,
    markdown_to_safe_html,
    render_archive_page,
    render_global_index,
    render_job_index,
    render_mail,
    render_subject,
)

DATE = date(2026, 10, 5)
NOW = datetime(2026, 10, 5, 7, 30, tzinfo=UTC)


def _payload(**overrides: Any) -> NotificationPayload:
    data: dict[str, Any] = {
        "job_name": "ai-news",
        "subject": "invio: ai-news – 2026-10-05",
        "digest_date": "2026-10-05",
        "is_empty": False,
        "stats": {"items_found": 7, "items_included": 2, "duration_seconds": 192.4},
    }
    data.update(overrides)
    return NotificationPayload.model_validate(data)


@pytest.mark.parametrize(
    ("template", "job", "expected"),
    [
        ("invio: {job_name} – {date}", "ai-news", "invio: ai-news – 2026-10-05"),
        ("Weekly digest", "any", "Weekly digest"),
        ("{job_name} {unknown} {", "x", "x {unknown} {"),
        ("{job_name}/{job_name} {date}", "a", "a/a 2026-10-05"),
    ],
)
def test_subject_placeholders(template: str, job: str, expected: str) -> None:
    assert render_subject(template, job_name=job, date=DATE) == expected


def test_subject_substitutes_in_a_single_pass() -> None:
    # A placeholder inside the job name is text, not a second substitution.
    subject = render_subject("{job_name} {date}", job_name="{date}-x", date=date(2026, 10, 4))

    assert subject == "{date}-x 2026-10-04"


def test_subject_placeholder_in_job_name_is_not_expanded_either_way() -> None:
    subject = render_subject("{date}: {job_name}", job_name="{job_name}", date=DATE)

    assert subject == "2026-10-05: {job_name}"


def test_subject_line_breaks_from_the_job_name_are_removed() -> None:
    subject = render_subject("[{job_name}]", job_name="x\r\nBcc: evil@example.org", date=DATE)

    assert "\r" not in subject
    assert "\n" not in subject
    assert subject == "[x Bcc: evil@example.org]"


def test_subject_strips_line_breaks() -> None:
    subject = render_subject("{job_name}\r\nBcc: x@example.org\n", job_name="a\nb", date=DATE)

    assert subject == "a b Bcc: x@example.org"


@pytest.mark.parametrize("separator", ["\v", "\f", "\x1c", "\x85", "\u2028", "\u2029"])
def test_subject_collapses_unicode_line_separators(separator: str) -> None:
    subject = render_subject("{job_name}", job_name=f"x{separator}Bcc: e@example.org", date=DATE)

    assert subject == "x Bcc: e@example.org"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(None, "–"), (0, "0s"), (45, "45s"), (192.4, "3m 12s"), (3720, "1h 02m")],
)
def test_footer_run_time_formats(seconds: float | None, expected: str) -> None:
    stats = {"items_found": 1, "items_included": 1, "duration_seconds": seconds}

    mail = render_mail(_payload(stats=stats), "body")

    assert f"Run time: {expected}" in mail.text
    assert f"Run time: {expected}" in mail.html


def test_text_part_has_job_date_body_and_footer() -> None:
    mail = render_mail(_payload(), "# News\n\n- item")

    assert isinstance(mail, RenderedMail)
    assert mail.subject == "invio: ai-news – 2026-10-05"
    assert "ai-news" in mail.text
    assert "2026-10-05" in mail.text
    assert "# News\n\n- item" in mail.text
    assert "Items found: 7 · Included: 2 · Run time: 3m 12s" in mail.text


def test_html_part_has_heading_job_date_and_footer() -> None:
    mail = render_mail(_payload(), "# News\n\n- item")

    assert "<h1>News</h1>" in mail.html
    assert "<li>item</li>" in mail.html
    assert "ai-news" in mail.html
    assert "2026-10-05" in mail.html
    assert "Items found: 7 · Included: 2 · Run time: 3m 12s" in mail.html
    assert "<title>invio: ai-news – 2026-10-05</title>" in mail.html


def test_missing_stats_render_as_dash() -> None:
    stats = {"items_found": None, "items_included": 0, "duration_seconds": None}

    mail = render_mail(_payload(stats=stats), "body")

    expected = "Items found: – · Included: 0 · Run time: –"
    assert expected in mail.text
    assert expected in mail.html


def test_empty_digest_shows_notice_instead_of_body() -> None:
    stats = {"items_found": 0, "items_included": 0, "duration_seconds": 1}

    mail = render_mail(_payload(is_empty=True, stats=stats), "SHOULD NOT APPEAR")

    assert "No new items were found for this run." in mail.text
    assert "No new items were found for this run." in mail.html
    assert "SHOULD NOT APPEAR" not in mail.text
    assert "SHOULD NOT APPEAR" not in mail.html
    assert "Included: 0" in mail.text


def test_build_message_is_multipart_alternative_with_headers() -> None:
    mail = render_mail(_payload(), "# News")

    message = build_message(
        mail, sender="Invio <invio@mail.example.org>", recipient="a@example.org", now=NOW
    )

    assert message.get_content_type() == "multipart/alternative"
    plain, html = message.iter_parts()
    assert (plain.get_content_type(), plain.get_content_charset()) == ("text/plain", "utf-8")
    assert (html.get_content_type(), html.get_content_charset()) == ("text/html", "utf-8")
    assert message["From"] == "Invio <invio@mail.example.org>"
    assert message["To"] == "a@example.org"
    assert message["Subject"] == mail.subject
    assert message["Date"] == "Mon, 05 Oct 2026 07:30:00 +0000"
    assert message["Message-ID"].endswith("@mail.example.org>")


def test_message_id_domain_falls_back_to_localhost() -> None:
    mail = render_mail(_payload(), "x")

    message = build_message(mail, sender="not-an-address", recipient="a@example.org", now=NOW)

    assert message["Message-ID"].endswith("@localhost>")


def test_message_ids_are_unique() -> None:
    mail = render_mail(_payload(), "x")

    ids = {
        build_message(mail, sender="i@x.org", recipient="a@example.org", now=NOW)["Message-ID"]
        for _ in range(3)
    }

    assert len(ids) == 3


def test_templates_load_from_the_package() -> None:
    env = Environment(loader=PackageLoader("invio.notify", "templates"))

    assert env.get_template("digest.html.j2") is not None
    assert env.get_template("digest.txt.j2") is not None


def test_markdown_renders_headings_and_lists() -> None:
    html = markdown_to_safe_html("# H\n\n- **b** [l](https://e.x)")

    assert "<h1>H</h1>" in html
    assert "<li>" in html
    assert "<strong>b</strong>" in html
    assert 'href="https://e.x"' in html


# (markdown, substrings the HTML must not contain, substrings it must contain)
SANITIZER_EXAMPLES: list[tuple[str, list[str], list[str]]] = [
    ("<script>alert(1)</script>", ["<script", "alert(1)"], []),
    ('<a href="x" onclick="evil()">t</a>', ["onclick"], [">t</a>"]),
    ("[x](javascript:alert(1))", ["javascript:"], ["x"]),
    ('<p style="color:red">t</p>', ["style="], ["t"]),
    ('<iframe src="https://e.x"></iframe>', ["<iframe"], []),
    ("# H\n\n- **b** [l](https://e.x)", [], ["<h1>", "<li>", "<strong>", 'href="https://e.x"']),
    ("<em>keep</em>", [], ["<em>keep</em>"]),
    ("[r](/p) [s](//e.x)", ["href="], ["r", "s"]),
]


@pytest.mark.parametrize(("markdown", "absent", "present"), SANITIZER_EXAMPLES)
def test_sanitizer_examples(markdown: str, absent: list[str], present: list[str]) -> None:
    html = markdown_to_safe_html(markdown)

    for needle in absent:
        assert needle not in html
    for needle in present:
        assert needle in html


@pytest.mark.parametrize(
    ("markdown", "absent"),
    [
        ('<b onerror="x()">t</b>', "onerror"),
        ('<img src="https://e.x/a.png" onerror="x()">', "onerror"),
        ("[d](data:text/html;base64,PHNjcmlwdD4=)", "data:"),
        ('<a href="vbscript:msgbox(1)">v</a>', "vbscript:"),
        ('<form action="https://e.x"><input name="a"></form>', "<form"),
        ("![alt](https://e.x/a.png)", "<img"),
        ('<img src="https://e.x/track.png">', "<img"),
        ("<!-- secret comment -->text", "secret comment"),
        ('<div class="c" id="i">t</div>', "class="),
        ("<style>p{color:red}</style>t", "color:red"),
    ],
)
def test_sanitizer_removes_unsafe_content(markdown: str, absent: str) -> None:
    assert absent not in markdown_to_safe_html(markdown)


def test_links_get_rel_noopener_noreferrer() -> None:
    html = markdown_to_safe_html("[l](https://e.x) <a href='mailto:a@example.org'>m</a>")

    assert html.count('rel="noopener noreferrer"') == 2


def test_sanitizer_keeps_table_structure_and_cell_alignment() -> None:
    html = markdown_to_safe_html(
        '| a |\n|---|\n| 1 |\n\n<table><tr><td align="right">r</td></tr></table>'
    )

    assert "<table>" in html
    assert "<th>a</th>" in html
    assert '<td align="right">r</td>' in html


def test_job_name_and_subject_are_escaped_in_html_but_not_in_text() -> None:
    payload = _payload(job_name="<b>&", subject='"<script>"')

    mail = render_mail(payload, "body")

    assert "&lt;b&gt;&amp;" in mail.html
    assert "<b>&" not in mail.html
    assert "<script>" not in mail.html
    assert "&lt;script&gt;" in mail.html
    assert "<b>&" in mail.text
    assert '"<script>"' not in mail.text  # the subject is a header, not part of the text body


def test_raw_html_in_digest_body_is_sanitized_in_the_mail() -> None:
    mail = render_mail(_payload(), "<script>alert(1)</script><em>ok</em>")

    assert "<script" not in mail.html
    assert "<em>ok</em>" in mail.html


# --- archive page --------------------------------------------------------------------------

STARTED = datetime(2026, 10, 8, 9, 30, 12, tzinfo=UTC)


def _page(markdown: str = "# News\n\n- item", **payload: Any) -> str:
    return render_archive_page(
        _payload(**payload), markdown, run_started_at=STARTED, index_href="index.html"
    )


def test_archive_page_has_job_name_date_body_and_footer_without_archive_link() -> None:
    html = _page()

    assert html.startswith("<!DOCTYPE html>")
    assert "<title>ai-news" in html
    assert "<h1>ai-news</h1>" in html
    assert "2026-10-08 09:30 UTC" in html
    assert "<li>item</li>" in html
    assert "Items found: 7 · Included: 2 · Run time: 3m 12s" in html
    assert "Archived version" not in html
    assert 'href="index.html"' in html


def test_archive_page_date_is_the_utc_run_start_not_the_job_local_digest_date() -> None:
    local = datetime(2026, 10, 9, 0, 45, tzinfo=timezone(timedelta(hours=2)))  # 22:45 UTC on 8th

    html = render_archive_page(_payload(), "x", run_started_at=local, index_href="index.html")

    assert "2026-10-08 22:45 UTC" in html
    assert "2026-10-05" not in html  # payload.digest_date is not used


def test_archive_page_escapes_the_job_name() -> None:
    html = _page(job_name="<b>x</b>")

    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


@pytest.mark.parametrize(("markdown", "absent", "_present"), SANITIZER_EXAMPLES)
def test_archive_page_sanitizes_the_digest(
    markdown: str, absent: list[str], _present: list[str]
) -> None:
    html = _page(markdown)
    body = html.split("<main", 1)[1].split("</main>", 1)[0]

    for needle in absent:
        assert needle not in body
    assert "onclick=" not in html
    assert "javascript:" not in html


def test_archive_page_drops_images() -> None:
    assert "<img" not in _page("![a](https://e.x/a.png) <img src='https://e.x/t.png'>")


def test_archive_page_missing_stats_render_as_dash() -> None:
    stats = {"items_found": None, "items_included": 1, "duration_seconds": None}

    assert "Items found: – · Included: 1 · Run time: –" in _page(stats=stats)


def test_job_index_lists_entries_in_given_order_and_escapes() -> None:
    entries = [
        IndexEntry("b.html", "2026-10-09 07:00 UTC", ""),
        IndexEntry("a.html", "2026-10-08 07:00 UTC #2", ""),
    ]

    html = render_job_index("<b>x</b>", entries)

    assert html.index('href="b.html"') < html.index('href="a.html"')
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert "<b>x</b>" not in html
    assert 'href="../index.html"' in html


def test_global_index_lists_jobs_with_detail() -> None:
    entries = [IndexEntry("news/index.html", "News <1>", "3 pages · latest 2026-10-08 09:30 UTC")]

    html = render_global_index(entries)

    assert 'href="news/index.html"' in html
    assert "News &lt;1&gt;" in html
    assert "3 pages · latest 2026-10-08 09:30 UTC" in html


# --- archive link in the mail --------------------------------------------------------------

URL = "https://h/x/s/2026-10-08-0930.html"


def test_mail_with_archive_url_links_it_in_both_footers() -> None:
    mail = render_mail(_payload(), "body", archive_url=URL)

    assert f'<a href="{URL}">' in mail.html
    assert mail.text.rstrip("\n").endswith(f"\nArchived version: {URL}")


def test_mail_without_archive_url_is_unchanged() -> None:
    mail = render_mail(_payload(), "body")

    assert mail == render_mail(_payload(), "body", archive_url=None)
    assert "Archived" not in mail.text
    assert "Archived" not in mail.html
    assert mail.text.endswith("Run time: 3m 12s\n")
    assert "Run time: 3m 12s\n</footer>" in mail.html


def test_archive_url_is_escaped_in_html() -> None:
    mail = render_mail(_payload(), "body", archive_url='https://h/a?x=1&y="2"')

    assert "&amp;y=&#34;2&#34;" in mail.html
    assert 'y="2"' not in mail.html
    assert 'https://h/a?x=1&y="2"' in mail.text
