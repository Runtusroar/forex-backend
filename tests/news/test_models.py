from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from app.news.models import ArticleObservation, NewsListingBatch


def test_listing_batch_contains_typed_immutable_articles() -> None:
    observed_at = datetime(2026, 9, 3, tzinfo=UTC)
    article = ArticleObservation(
        source_id="1416152",
        ff_url="https://www.forexfactory.com/news/1416152-x",
        title_en="BoC update",
        observed_at=observed_at,
    )
    batch = NewsListingBatch(
        articles=(article,),
        observed_at=observed_at,
        source_hash="listing-hash",
        source_timezone="Asia/Shanghai",
        observed_sections=frozenset({"latest"}),
    )

    assert batch.articles == (article,)
    assert batch.categories == ()
    assert article.comment_count == 0
    with pytest.raises(FrozenInstanceError):
        article.title_en = "changed"  # type: ignore[misc]
