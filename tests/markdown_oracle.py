"""Shared CommonMark oracle for the digest tests.

Built from the notifier's own renderer (raw HTML on, every destination accepted), so the tests
parse a digest exactly as the e-mail is rendered.
"""

from markdown_it.token import Token

from invio.notify.render import _MARKDOWN as ORACLE


def walk(markdown: str) -> list[Token]:
    """Return every token of ``markdown``, block and inline children included."""
    found: list[Token] = []
    stack: list[Token] = list(ORACLE.parse(markdown))
    while stack:
        token = stack.pop()
        found.append(token)
        stack.extend(token.children or ())
    return found


def rendered(markdown: str) -> tuple[list[str], int]:
    """Return the link destinations and the number of images and raw HTML tokens."""
    links: list[str] = []
    others = 0
    for token in walk(markdown):
        if token.type == "link_open":
            links.append(str(token.attrGet("href")))
        elif token.type in ("image", "html_inline", "html_block"):
            others += 1
    return links, others


def rendered_targets(text: str) -> list[str]:
    """Return every link/image destination and every raw HTML token markdown-it finds."""
    found: list[str] = []
    for token in walk(text):
        for name in ("href", "src"):
            value = token.attrGet(name)
            if value is not None:
                found.append(str(value))
        if token.type in ("html_inline", "html_block"):
            found.append(token.content)
    return found


def assert_renders_only_allowed(text: str, allowed: set[str]) -> None:
    targets = {ORACLE.normalizeLink(u) for u in allowed}
    for target in rendered_targets(text):
        assert target in targets, (target, text)


def token_signature(markdown: str, renderer: object = ORACLE) -> list[tuple[str, str]]:
    """Return (type, href/src/content) of every link, image and raw HTML token, sorted."""
    parse = renderer.parse  # type: ignore[attr-defined]
    signature: list[tuple[str, str]] = []
    stack: list[Token] = list(parse(markdown))
    while stack:
        token = stack.pop()
        if token.type in ("link_open", "image"):
            signature.append((token.type, str(token.attrGet("href") or token.attrGet("src"))))
        elif token.type in ("html_inline", "html_block"):
            signature.append((token.type, token.content))
        stack.extend(token.children or ())
    return sorted(signature)
