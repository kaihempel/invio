"""Item page extraction: fetch an item's page and store its text.

``extract_item`` is the body of the ``extract_text`` node of the ``process_item`` subgraph. The
page arrives through the injected :class:`~invio.graph.ports.PageFetcher` (unconditional GET, so
a page-mode web item, whose URL is the source URL, is never answered ``304``, research R6).

Extraction is CPU-bound, so it runs off the event loop; everything that touches the run's work
session stays on the loop thread.
"""

import asyncio
import logging

from invio.db.models import Item
from invio.db.repositories import ItemRepository
from invio.graph.ports import PageFetcher
from invio.retry import RetrySettings, Sleep, retrying
from invio.sources.errors import is_transient_fetch
from invio.sources.extract import ExtractionError, extract_text

__all__ = ["extract_item"]

logger = logging.getLogger("invio.graph")


async def extract_item(
    item: Item,
    *,
    fetch_page: PageFetcher,
    items: ItemRepository,
    retry: RetrySettings | None = None,
    sleep: Sleep = asyncio.sleep,
) -> bool:
    """Fetch ``item.url``, store the page text and return ``True``; ``False`` if too short.

    A transient fetch error is retried per ``retry`` (``None``: one attempt); a blocked,
    too large or unavailable-renderer error never is.

    A page with too little text is no failure: ``raw_content`` stays ``None`` and the item is
    rated from its title and teaser (``extract.too_short`` is logged with the item id). Every
    other error (``FetchError``, ``ExtractionError("too_large")``, a bug) propagates; the error
    boundary of ``process_item`` decides what it means for the item.
    """
    html = await retrying(
        lambda: fetch_page(item.url),
        policy=retry or RetrySettings(max_attempts=1),
        retry_on=is_transient_fetch,
        what="page",
        sleep=sleep,
    )
    try:
        result = await asyncio.to_thread(extract_text, html, item.url)
    except ExtractionError as err:
        if err.reason != "too_short":
            raise
        logger.info("extract.too_short", extra={"item_id": item.id})
        return False
    items.set_extracted(item, result.text)
    return True
