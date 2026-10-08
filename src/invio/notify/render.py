"""Markdown sanitizing, subject rendering and MIME message construction for digest mails."""

import datetime as dt
import email.policy
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr

import nh3
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from invio.markdown import MARKDOWN
from invio.notify.payload import NotificationPayload

__all__ = [
    "IndexEntry",
    "RenderedMail",
    "build_message",
    "markdown_to_safe_html",
    "render_archive_page",
    "render_global_index",
    "render_job_index",
    "render_mail",
    "render_subject",
]

# The only subject placeholders; any other brace text is left as written.
_PLACEHOLDER = re.compile(r"\{(job_name|date)\}")

# Shown in the footer for a number that is not known.
_MISSING = "–"  # noqa: RUF001 (en dash)

# Autoescaping is on for the HTML template only; the text template renders verbatim.
_ENV = Environment(
    loader=PackageLoader("invio.notify", "templates"),
    autoescape=select_autoescape(enabled_extensions=("html.j2",), default_for_string=False),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


# Sanitizer allowlist (contracts/email-message.md); everything else is removed.
_ALLOWED_TAGS = {
    "a", "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "b", "i", "u",
    "s", "del", "ul", "ol", "li", "blockquote", "code", "pre", "table", "thead", "tbody", "tr",
    "th", "td",
}  # fmt: skip
_ALLOWED_ATTRIBUTES = {"a": {"href", "title"}, "th": {"align"}, "td": {"align"}}
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}
# Dropped together with their content, not just the tags.
_CLEAN_CONTENT_TAGS = {"script", "style"}


@dataclass(frozen=True, slots=True)
class RenderedMail:
    """Subject plus both bodies; built once per digest, reused for every recipient."""

    subject: str
    text: str
    html: str


def markdown_to_safe_html(markdown: str) -> str:
    """Render ``markdown`` to HTML and strip everything outside the allowlist.

    Raw HTML in the digest is kept where the allowlist permits it, so the sanitizer, not the
    Markdown renderer, is the security boundary: every byte of the result passes through
    ``nh3``. Images are deliberately excluded (remote images in mail act as tracking beacons).
    """
    return nh3.clean(
        MARKDOWN.render(markdown),
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        url_relative="deny",  # a mail has no base URL: relative and ``//host`` links are dropped
        link_rel="noopener noreferrer",
        clean_content_tags=_CLEAN_CONTENT_TAGS,
        strip_comments=True,
    )


def render_subject(template: str, *, job_name: str, date: dt.date) -> str:
    """Replace ``{job_name}`` and ``{date}``; other braces stay; whitespace runs become one space.

    Collapsing every kind of whitespace (CR, LF, VT, FF, U+0085, U+2028, ...) keeps the subject
    a single header line.
    """
    values = {"job_name": job_name, "date": date.isoformat()}
    # One pass, so a ``{date}`` inside the job name is not substituted again.
    subject = _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)
    return " ".join(subject.split())


def _format_duration(seconds: float | None) -> str:
    """Return ``45s``, ``3m 12s`` or ``1h 02m``; a dash for an unknown duration."""
    if seconds is None:
        return _MISSING
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _stats_context(payload: NotificationPayload) -> dict[str, object]:
    """The footer figures shared by the mail and the archive page."""
    stats = payload.stats
    return {
        "items_found": _MISSING if stats.items_found is None else stats.items_found,
        "items_included": stats.items_included,
        "duration": _format_duration(stats.duration_seconds),
    }


def render_mail(
    payload: NotificationPayload, digest_markdown: str, *, archive_url: str | None = None
) -> RenderedMail:
    """Render both mail bodies from the stored payload and the digest's Markdown.

    ``archive_url`` adds the link to the archived page to both footers; ``None`` leaves the
    mail exactly as it was without an archive.
    """
    context = {
        "job_name": payload.job_name,
        "subject": payload.subject,
        "date": payload.digest_date.isoformat(),
        "is_empty": payload.is_empty,
        "stats": _stats_context(payload),
        "archive_url": archive_url,
    }
    html = _ENV.get_template("digest.html.j2").render(
        **context, body_html=Markup(markdown_to_safe_html(digest_markdown))
    )
    text = _ENV.get_template("digest.txt.j2").render(**context, body_text=digest_markdown)
    return RenderedMail(subject=payload.subject, text=text, html=html)


def format_utc_minute(moment: datetime) -> str:
    """``YYYY-MM-DD HH:MM UTC`` for an aware ``moment`` (a naive one is taken as UTC)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


def render_archive_page(
    payload: NotificationPayload,
    digest_markdown: str,
    *,
    run_started_at: datetime,
    index_href: str,
) -> str:
    """Render the standalone HTML page of an archived digest.

    The date is the run start in UTC (``payload.digest_date`` is in the job's time zone). The
    body goes through the same sanitizer as the mail HTML; nothing external is referenced.
    """
    return _ENV.get_template("archive_page.html.j2").render(
        job_name=payload.job_name,
        date_line=format_utc_minute(run_started_at),
        stats=_stats_context(payload),
        body_html=Markup(markdown_to_safe_html(digest_markdown)),
        index_href=index_href,
    )


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One line of an index page: relative link, visible label, extra text."""

    href: str
    label: str
    detail: str


def render_job_index(job_name: str, entries: Sequence[IndexEntry]) -> str:
    """Render a job's index page; ``entries`` are listed in the given order."""
    return _ENV.get_template("archive_index.html.j2").render(
        title=job_name,
        heading=job_name,
        entries=entries,
        back_href="../index.html",
        back_label="All jobs",
    )


def render_global_index(entries: Sequence[IndexEntry]) -> str:
    """Render the top-level index page listing every job."""
    return _ENV.get_template("archive_index.html.j2").render(
        title="Digest archive",
        heading="Digest archive",
        entries=entries,
        back_href=None,
        back_label=None,
    )


def build_message(
    mail: RenderedMail, *, sender: str, recipient: str, now: datetime
) -> EmailMessage:
    """Build the ``multipart/alternative`` message (plain text first) for one recipient."""
    message = EmailMessage(policy=email.policy.SMTP)
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = mail.subject
    message["Date"] = format_datetime(now)
    _, _, domain = parseaddr(sender)[1].partition("@")
    message["Message-ID"] = make_msgid(domain=domain or "localhost")
    message.set_content(mail.text, subtype="plain", charset="utf-8")
    message.add_alternative(mail.html, subtype="html", charset="utf-8")
    return message
