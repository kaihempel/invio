"""The contract every source adapter implements.

An adapter turns one configured source (feed, page, sitemap, channel, ...) into
:class:`~invio.domain.Candidate` records. It reaches the network only through
:class:`~invio.sources.http.SafeHttpClient`, never stores anything and never fetches the
candidates' content: deduplication against stored items and extraction come later in the
pipeline.
"""

from typing import Protocol, runtime_checkable

from invio.domain import Candidate

__all__ = ["Source"]


@runtime_checkable
class Source[ConfigT](Protocol):
    """A source adapter for configs of type ``ConfigT`` (e.g. ``Source[RssSource]``).

    ``fetch`` returns the candidates of one source in source order, URLs canonicalized and
    without duplicates. An unchanged source (HTTP 304) yields ``[]``; a failed fetch or an
    unusable response raises :class:`~invio.sources.errors.FetchError`.
    """

    async def fetch(self, config: ConfigT, /) -> list[Candidate]:
        """Fetch the source described by ``config`` and return its candidates."""
        ...
