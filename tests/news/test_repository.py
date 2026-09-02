import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import Database
from app.news.models import (
    ArticleObservation,
    CategoryObservation,
    DetailObservation,
    FeedObservation,
    MediaObservation,
    NewsListingBatch,
    SegmentObservation,
)
from app.news.repository import NewsRepository

NOW = datetime(2026, 9, 3, 1, tzinfo=UTC)


@pytest.fixture
async def news_repository(tmp_path: Path):
    database = Database(tmp_path / "news.sqlite3")
    await database.open()
    await database.initialize()
    assert database.connection is not None
    yield NewsRepository(database.connection)
    await database.close()


def batch(
    *,
    rank: int | None = 0,
    categories: tuple[str, ...] = ("fundamental", "technical"),
    observed_at: datetime = NOW,
) -> NewsListingBatch:
    article = ArticleObservation(
        source_id="1416149",
        ff_url="https://www.forexfactory.com/news/1416149-yen",
        title_en="Japanese Yen Surges",
        observed_at=observed_at,
        published_at=NOW - timedelta(minutes=10),
        breaking_impact="high",
    )
    return NewsListingBatch(
        articles=(article,),
        categories=tuple(
            CategoryObservation(article.source_id, category, observed_at)  # type: ignore[arg-type]
            for category in categories
        ),
        feeds=(
            (FeedObservation(article.source_id, "latest", rank, observed_at),)
            if rank is not None
            else ()
        ),
        observed_at=observed_at,
        source_hash=f"page-{observed_at.timestamp()}",
        source_timezone="Asia/Shanghai",
        observed_sections=frozenset({"latest", *categories}),
    )


async def test_one_article_can_hold_two_categories(news_repository: NewsRepository) -> None:
    result = await news_repository.apply_listing(batch())
    article = await news_repository.get_article("1416149")

    assert result.new_article_ids == ("1416149",)
    assert article is not None
    assert article.categories == ("fundamental", "technical")
    assert await news_repository.count_articles() == 1


async def test_feed_history_records_enter_move_and_confirmed_leave(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(rank=3))
    await news_repository.apply_listing(batch(rank=1, observed_at=NOW + timedelta(seconds=30)))
    for offset in (60, 90):
        await news_repository.apply_listing(
            batch(rank=None, categories=(), observed_at=NOW + timedelta(seconds=offset))
        )
    assert await news_repository.current_feed_ids("latest") == ("1416149",)

    await news_repository.apply_listing(
        batch(rank=None, categories=(), observed_at=NOW + timedelta(seconds=120))
    )

    assert await news_repository.current_feed_ids("latest") == ()
    assert await news_repository.feed_event_types("1416149", "latest") == (
        "entered",
        "moved",
        "left",
    )


async def test_category_membership_survives_panel_absence(news_repository: NewsRepository) -> None:
    await news_repository.apply_listing(batch(categories=("fundamental",)))
    await news_repository.apply_listing(
        batch(categories=(), observed_at=NOW + timedelta(minutes=1))
    )

    article = await news_repository.get_article("1416149")
    assert article is not None
    assert article.categories == ("fundamental",)


async def test_detail_jobs_are_persistent_and_high_impact_first(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch())

    jobs = await news_repository.claim_detail_jobs(limit=1, now=NOW)

    assert len(jobs) == 1
    assert jobs[0].article_id == "1416149"
    assert jobs[0].priority == 100


async def test_complete_detail_reorders_without_duplicating_segments(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch())
    first = SegmentObservation("first", 0, "social", text_en="First")
    second = SegmentObservation("second", 1, "article", text_en="Second")
    await news_repository.replace_detail(
        "1416149",
        DetailObservation("1416149", NOW, "detail-1", (first, second)),
    )
    await news_repository.replace_detail(
        "1416149",
        DetailObservation(
            "1416149",
            NOW + timedelta(minutes=1),
            "detail-2",
            (
                SegmentObservation("second", 0, "article", text_en="Second"),
                SegmentObservation("first", 1, "social", text_en="First"),
            ),
        ),
    )

    assert await news_repository.current_segment_keys("1416149") == ("second", "first")
    assert await news_repository.segment_count("1416149") == 2


async def test_detail_persists_media_and_comments(news_repository: NewsRepository) -> None:
    from app.news.models import CommentObservation

    await news_repository.apply_listing(batch())
    segment = SegmentObservation("body", 0, "article", text_en="Body")
    detail = DetailObservation(
        "1416149",
        NOW,
        "detail-1",
        segments=(segment,),
        media=(
            MediaObservation(
                "chart-1",
                0,
                "chart",
                "https://assets.example/chart.png",
                segment_key="body",
            ),
        ),
        comments=(
            CommentObservation(
                "comment-1",
                "1416149",
                "Alice",
                "Useful",
                "https://www.forexfactory.com/comment/1",
                NOW,
            ),
        ),
    )

    await news_repository.replace_detail("1416149", detail)

    jobs = await news_repository.claim_media_jobs(2, NOW)
    assert [(job.article_id, job.original_url) for job in jobs] == [
        ("1416149", "https://assets.example/chart.png")
    ]
    assert await news_repository.comment_count("1416149") == 1


async def test_concurrent_listing_transactions_are_serialized(
    news_repository: NewsRepository,
) -> None:
    await asyncio.gather(
        news_repository.apply_listing(batch(observed_at=NOW)),
        news_repository.apply_listing(batch(observed_at=NOW + timedelta(seconds=1))),
    )

    assert await news_repository.count_articles() == 1


async def test_comment_count_change_requeues_completed_detail(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch())
    await news_repository.claim_detail_jobs(1, NOW)
    await news_repository.complete_detail_job("1416149")
    changed_batch = batch(observed_at=NOW + timedelta(minutes=1))
    changed_article = changed_batch.articles[0]
    changed_batch = NewsListingBatch(
        articles=(
            ArticleObservation(
                changed_article.source_id,
                changed_article.ff_url,
                changed_article.title_en,
                changed_article.observed_at,
                published_at=changed_article.published_at,
                breaking_impact="high",
                comment_count=2,
            ),
        ),
        categories=changed_batch.categories,
        feeds=changed_batch.feeds,
        observed_at=changed_batch.observed_at,
        source_hash=changed_batch.source_hash,
        source_timezone=changed_batch.source_timezone,
        observed_sections=changed_batch.observed_sections,
    )

    result = await news_repository.apply_listing(changed_batch)

    assert result.changed_article_ids == ("1416149",)
    assert await news_repository.detail_job_state("1416149") == "pending"
