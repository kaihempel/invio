"""The extract node and the video path: page text for an item, too short pages, hard limits."""

import pytest
from sqlalchemy.orm import Session

from invio.db.models import Item
from invio.db.repositories import ItemRepository
from invio.domain import ItemStatus
from invio.graph.nodes.extract import extract_item, video_path
from invio.sources.errors import FetchError, TooLargeError
from invio.sources.extract import MAX_INPUT_CHARS, ExtractionError
from tests.db_helpers import make_item, make_job
from tests.pipeline_helpers import article_html, fixture_article

URL = "https://example.com/post-1"


class _Pages:
    """A ``PageFetcher`` returning one HTML string or raising one error."""

    def __init__(self, outcome: str | BaseException) -> None:
        self.outcome = outcome
        self.urls: list[str] = []

    async def __call__(self, url: str) -> str:
        self.urls.append(url)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _item(session: Session) -> Item:
    return make_item(session, make_job(session), url=URL, teaser="a teaser")


async def test_an_article_page_is_stored_as_extracted_text(db_session: Session) -> None:
    item = _Pages(article_html("Agents"))
    stored = _item(db_session)

    extracted = await extract_item(stored, fetch_page=item, items=ItemRepository(db_session))

    assert extracted is True
    assert item.urls == [URL]
    assert stored.status == ItemStatus.EXTRACTED
    assert stored.raw_content is not None
    assert "Agents paragraph 1" in stored.raw_content


async def test_the_news_article_fixture_is_extracted(db_session: Session) -> None:
    stored = _item(db_session)

    assert await extract_item(
        stored, fetch_page=_Pages(fixture_article()), items=ItemRepository(db_session)
    )
    assert stored.raw_content


async def test_a_too_short_page_leaves_the_item_unchanged_and_continues(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    stored = _item(db_session)
    before = stored.status

    with caplog.at_level("INFO", logger="invio.graph"):
        extracted = await extract_item(
            stored,
            fetch_page=_Pages("<html><body><p>tiny</p></body></html>"),
            items=ItemRepository(db_session),
        )

    assert extracted is False
    assert stored.raw_content is None
    assert stored.status == before
    (record,) = [r for r in caplog.records if r.getMessage() == "extract.too_short"]
    assert record.item_id == stored.id  # type: ignore[attr-defined]


async def test_a_too_large_page_propagates(db_session: Session) -> None:
    stored = _item(db_session)
    huge = "<p>" + "x" * (MAX_INPUT_CHARS + 1) + "</p>"

    with pytest.raises(ExtractionError) as raised:
        await extract_item(stored, fetch_page=_Pages(huge), items=ItemRepository(db_session))

    assert raised.value.reason == "too_large"
    assert stored.raw_content is None


@pytest.mark.parametrize(
    "error",
    [TooLargeError(url=URL, limit=1), FetchError("timeout", url=URL), RuntimeError("bug")],
    ids=["too-large-fetch", "fetch-error", "bug"],
)
async def test_fetch_errors_propagate_unchanged(db_session: Session, error: Exception) -> None:
    stored = _item(db_session)

    with pytest.raises(type(error)) as raised:
        await extract_item(stored, fetch_page=_Pages(error), items=ItemRepository(db_session))

    assert raised.value is error


async def test_video_path_passes_the_item_state_through() -> None:
    state = {"item_id": 4, "kind": "video"}

    assert await video_path(state) == {"stage": "extract_text"}  # type: ignore[arg-type]
