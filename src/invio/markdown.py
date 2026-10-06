"""The one CommonMark parser invio renders digests with.

The notifier renders the digest mail with it and the synthesis node checks the model's answer
with it, so both see exactly the same links, images and raw HTML.
"""

from typing import Final

from markdown_it import MarkdownIt

__all__ = ["MARKDOWN"]


class _AcceptAllLinks(MarkdownIt):
    """Markdown-it that accepts every link target.

    The stock validator prints links with unsafe schemes as literal text, which would keep
    ``javascript:...`` in the output. Accepting them makes them real links, so the mail
    sanitizer drops the ``href`` and only the link text stays (contracts/email-message.md), and
    the synthesis post-check sees every destination as a link token.
    """

    def validateLink(self, url: str) -> bool:
        return True


# Raw HTML on, no linkify, tables enabled.
MARKDOWN: Final = _AcceptAllLinks("commonmark", {"html": True, "linkify": False}).enable("table")
