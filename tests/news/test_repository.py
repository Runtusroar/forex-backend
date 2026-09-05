import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import Database
from app.news.models import (
    ArticleObservation,
    CategoryObservation,
    CommentCollectionObservation,
    CommentObservation,
    DetailObservation,
    FeedObservation,
    MediaObservation,
    NewsListingBatch,
    SegmentLinkObservation,
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
    comment_count: int = 0,
    comments: tuple[CommentObservation, ...] = (),
) -> NewsListingBatch:
    article = ArticleObservation(
        source_id="1416149",
        ff_url="https://www.forexfactory.com/news/1416149-yen",
        title_en="Japanese Yen Surges",
        observed_at=observed_at,
        published_at=NOW - timedelta(minutes=10),
        breaking_impact="high",
        comment_count=comment_count,
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
        comments=comments,
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


async def test_article_detail_persists_media_without_owning_comments(
    news_repository: NewsRepository,
) -> None:
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
    assert await news_repository.comment_count("1416149") == 0


async def test_detail_stores_presentation_and_link_without_publisher_document(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch())
    segment = SegmentObservation(
        "body",
        0,
        "article",
        text_en="Forex Factory excerpt...",
        is_excerpt=True,
        display_mode="clamped",
        max_lines=10,
        external_action_label="Show More",
    )
    link = SegmentLinkObservation(
        "full-story",
        "body",
        0,
        "full_story",
        "full story",
        "https://publisher.example/story",
    )
    await news_repository.replace_detail(
        "1416149",
        DetailObservation("1416149", NOW, "detail-1", segments=(segment,), links=(link,)),
    )

    detail = await news_repository.detail_data("1416149")
    assert detail is not None
    assert detail["segments"][0]["text_en"] == "Forex Factory excerpt..."
    assert detail["segments"][0]["display_mode"] == "clamped"
    assert detail["segments"][0]["max_lines"] == 10
    assert detail["segments"][0]["external_action_label"] == "Show More"
    assert detail["segments"][0]["links"][0]["original_url"] == ("https://publisher.example/story")


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


async def test_delayed_detail_retry_does_not_block_low_priority_backfill(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch())
    job = (await news_repository.claim_detail_jobs(1, NOW))[0]
    await news_repository.fail_detail_job(job.article_id, ValueError("bad detail"), NOW)

    assert await news_repository.ready_detail_job_count(NOW) == 0
    assert await news_repository.ready_detail_job_count(NOW + timedelta(minutes=1)) == 1


async def test_listing_persists_source_comment_count_decrease(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(comment_count=5))
    await news_repository.apply_listing(
        batch(comment_count=3, observed_at=NOW + timedelta(minutes=1))
    )

    article = await news_repository.get_article("1416149")

    assert article is not None
    assert article.comment_count == 3


async def test_listing_comment_does_not_downgrade_detail_comment(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(comment_count=1))
    detail_comment = CommentObservation(
        "comment-1",
        "1416149",
        "Alice",
        "Full comment",
        "https://www.forexfactory.com/comment/1",
        NOW,
        published_at=NOW - timedelta(minutes=2),
        observation_quality="detail",
    )
    await news_repository.replace_comments(
        CommentCollectionObservation("1416149", NOW, 1, (detail_comment,), is_complete=True)
    )
    listing_comment = CommentObservation(
        "comment-1",
        "1416149",
        "Unknown",
        "",
        "https://www.forexfactory.com/comment/1",
        NOW + timedelta(minutes=1),
        feed_rank=0,
        observation_quality="listing",
    )

    await news_repository.apply_listing(
        batch(
            comment_count=1,
            observed_at=NOW + timedelta(minutes=1),
            comments=(listing_comment,),
        )
    )
    rows, _ = await news_repository.list_comments("1416149", 10)

    assert [(row["author_name"], row["text_en"]) for row in rows] == [("Alice", "Full comment")]


async def test_only_complete_comment_collection_deactivates_missing_rows(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(comment_count=2))
    first = CommentObservation("comment-1", "1416149", "Alice", "One", "https://ff.test/1", NOW)
    second = CommentObservation(
        "comment-2",
        "1416149",
        "Bob",
        "Two",
        "https://ff.test/2",
        NOW,
        position=1,
    )
    await news_repository.replace_comments(
        CommentCollectionObservation("1416149", NOW, 2, (first, second), True)
    )
    await news_repository.replace_comments(
        CommentCollectionObservation("1416149", NOW + timedelta(minutes=1), 2, (first,), False)
    )
    assert await news_repository.comment_count("1416149") == 2

    await news_repository.replace_comments(
        CommentCollectionObservation("1416149", NOW + timedelta(minutes=2), 1, (first,), True)
    )

    assert await news_repository.comment_count("1416149") == 1


async def test_comment_parent_is_kept_only_when_parent_row_is_materialized(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(comment_count=2))
    parent = CommentObservation(
        "parent", "1416149", "Parent", "Older visible body", "https://ff.test/parent", NOW
    )
    child = CommentObservation(
        "child",
        "1416149",
        "Child",
        "Child body",
        "https://ff.test/child",
        NOW,
        parent_comment_id="parent",
        position=1,
        depth=1,
    )
    await news_repository.replace_comments(
        CommentCollectionObservation("1416149", NOW, 2, (parent, child), True)
    )

    refreshed_child = CommentObservation(
        "child",
        "1416149",
        "Child",
        "Updated child body",
        "https://ff.test/child",
        NOW + timedelta(minutes=1),
        parent_comment_id="parent",
        position=1,
        depth=1,
    )
    await news_repository.replace_comments(
        CommentCollectionObservation(
            "1416149", NOW + timedelta(minutes=1), 2, (refreshed_child,), False
        )
    )
    rows, _ = await news_repository.list_comments("1416149", 10)
    by_id = {row["comment_id"]: row for row in rows}

    assert by_id["parent"]["text_en"] == "Older visible body"
    assert by_id["child"]["parent_comment_id"] == "parent"


async def test_unknown_and_self_comment_parents_are_cleared_but_depth_is_preserved(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(comment_count=2))
    unknown_parent = CommentObservation(
        "child",
        "1416149",
        "Child",
        "Child body",
        "https://ff.test/child",
        NOW,
        parent_comment_id="hidden-parent",
        depth=1,
    )
    self_parent = CommentObservation(
        "self",
        "1416149",
        "Self",
        "Self body",
        "https://ff.test/self",
        NOW,
        parent_comment_id="self",
        depth=2,
    )

    await news_repository.replace_comments(
        CommentCollectionObservation(
            "1416149", NOW, 2, (unknown_parent, self_parent), False
        )
    )
    rows, _ = await news_repository.list_comments("1416149", 10)

    assert {(row["comment_id"], row["parent_comment_id"], row["depth"]) for row in rows} == {
        ("child", None, 1),
        ("self", None, 2),
    }
    self_rows = await news_repository.db.execute_fetchall(
        "SELECT count(*) AS count FROM news_comments WHERE parent_comment_id=comment_id"
    )
    assert self_rows[0]["count"] == 0


async def test_comment_jobs_track_latest_expected_count_and_priority(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(comment_count=2))
    first = (await news_repository.claim_comment_jobs(1, NOW))[0]
    await news_repository.replace_comments(
        CommentCollectionObservation(
            "1416149",
            NOW,
            2,
            (
                CommentObservation("comment-1", "1416149", "A", "One", "https://ff.test/1", NOW),
                CommentObservation(
                    "comment-2",
                    "1416149",
                    "B",
                    "Two",
                    "https://ff.test/2",
                    NOW,
                    position=1,
                ),
            ),
            True,
        )
    )
    await news_repository.complete_comment_job(first.article_id, NOW)

    await news_repository.apply_listing(
        batch(comment_count=3, observed_at=NOW + timedelta(minutes=1))
    )
    jobs = await news_repository.claim_comment_jobs(1, NOW + timedelta(minutes=1))

    assert len(jobs) == 1
    assert jobs[0].article_id == "1416149"
    assert jobs[0].expected_count == 3
    assert jobs[0].priority == 100
    assert await news_repository.comment_collection_state("1416149") == "pending"


async def test_identical_latest_comment_does_not_reset_processing_job(
    news_repository: NewsRepository,
) -> None:
    preview = CommentObservation(
        "comment-2",
        "1416149",
        "B",
        "Preview",
        "https://ff.test/2",
        NOW,
        feed_rank=0,
        observation_quality="listing",
    )
    await news_repository.apply_listing(batch(comment_count=2, comments=(preview,)))
    await news_repository.claim_comment_jobs(1, NOW)

    repeated = CommentObservation(
        "comment-2",
        "1416149",
        "B",
        "Preview",
        "https://ff.test/2",
        NOW + timedelta(seconds=30),
        feed_rank=0,
        observation_quality="listing",
    )
    await news_repository.apply_listing(
        batch(
            comment_count=2,
            comments=(repeated,),
            observed_at=NOW + timedelta(seconds=30),
        )
    )

    assert await news_repository.comment_job_state("1416149") == "processing"
    assert await news_repository.claim_comment_jobs(1, NOW + timedelta(seconds=30)) == []


async def test_identical_latest_comment_does_not_reset_failed_job_backoff(
    news_repository: NewsRepository,
) -> None:
    preview = CommentObservation(
        "comment-2",
        "1416149",
        "B",
        "Preview",
        "https://ff.test/2",
        NOW,
        feed_rank=0,
        observation_quality="listing",
    )
    await news_repository.apply_listing(batch(comment_count=2, comments=(preview,)))
    job = (await news_repository.claim_comment_jobs(1, NOW))[0]
    await news_repository.fail_comment_job(
        job.article_id,
        ValueError("bad page"),
        NOW,
        max_attempts=1,
        claimed_expected_count=job.expected_count,
    )

    repeated = CommentObservation(
        "comment-2",
        "1416149",
        "B",
        "Preview",
        "https://ff.test/2",
        NOW + timedelta(seconds=30),
        feed_rank=0,
        observation_quality="listing",
    )
    await news_repository.apply_listing(
        batch(
            comment_count=2,
            comments=(repeated,),
            observed_at=NOW + timedelta(seconds=30),
        )
    )

    assert await news_repository.comment_job_state("1416149") == "failed"
    assert await news_repository.claim_comment_jobs(1, NOW + timedelta(seconds=30)) == []


async def test_zero_comment_article_is_queued_for_detail_verification(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(comment_count=0))

    jobs = await news_repository.claim_comment_jobs(1, NOW)
    assert [job.expected_count for job in jobs] == [0]
    assert await news_repository.comment_collection_state("1416149") == "pending"


async def test_listing_zero_does_not_hide_comments_before_detail_verification(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(comment_count=1))
    job = (await news_repository.claim_comment_jobs(1, NOW))[0]
    comment = CommentObservation("comment-1", "1416149", "A", "One", "https://ff.test/1", NOW)
    await news_repository.replace_comments(
        CommentCollectionObservation("1416149", NOW, 1, (comment,), True),
        claimed_expected_count=job.expected_count,
    )
    await news_repository.complete_comment_job(
        job.article_id, NOW, claimed_expected_count=job.expected_count, collected_count=1
    )

    await news_repository.apply_listing(
        batch(comment_count=0, observed_at=NOW + timedelta(minutes=1))
    )

    assert await news_repository.comment_count("1416149") == 1
    assert await news_repository.comment_collection_state("1416149") == "pending"


async def test_latest_comment_preview_cannot_reactivate_removed_detail_comment(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(comment_count=1))
    first_job = (await news_repository.claim_comment_jobs(1, NOW))[0]
    comment = CommentObservation("comment-1", "1416149", "A", "One", "https://ff.test/1", NOW)
    await news_repository.replace_comments(
        CommentCollectionObservation("1416149", NOW, 1, (comment,), True),
        claimed_expected_count=first_job.expected_count,
    )
    await news_repository.complete_comment_job(
        first_job.article_id,
        NOW,
        claimed_expected_count=first_job.expected_count,
        collected_count=1,
    )
    await news_repository.apply_listing(
        batch(comment_count=0, observed_at=NOW + timedelta(minutes=1))
    )
    removal_job = (await news_repository.claim_comment_jobs(1, NOW + timedelta(minutes=1)))[0]
    await news_repository.replace_comments(
        CommentCollectionObservation("1416149", NOW + timedelta(minutes=1), 0, (), True),
        claimed_expected_count=removal_job.expected_count,
    )
    await news_repository.complete_comment_job(
        removal_job.article_id,
        NOW + timedelta(minutes=1),
        claimed_expected_count=removal_job.expected_count,
        collected_count=0,
    )
    stale_preview = CommentObservation(
        "comment-1",
        "1416149",
        "A",
        "One",
        "https://ff.test/1",
        NOW + timedelta(minutes=2),
        feed_rank=0,
        observation_quality="listing",
    )

    await news_repository.apply_listing(
        batch(
            comment_count=0,
            observed_at=NOW + timedelta(minutes=2),
            comments=(stale_preview,),
        )
    )

    assert await news_repository.comment_count("1416149") == 0


async def test_stale_comment_worker_cannot_complete_newer_job(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(comment_count=2))
    stale_job = (await news_repository.claim_comment_jobs(1, NOW))[0]

    await news_repository.apply_listing(
        batch(comment_count=3, observed_at=NOW + timedelta(minutes=1))
    )
    stored = await news_repository.replace_comments(
        CommentCollectionObservation(
            "1416149",
            NOW + timedelta(minutes=1),
            2,
            (
                CommentObservation("comment-1", "1416149", "A", "One", "https://ff.test/1", NOW),
                CommentObservation("comment-2", "1416149", "B", "Two", "https://ff.test/2", NOW),
            ),
            True,
        ),
        claimed_expected_count=stale_job.expected_count,
    )
    completed = await news_repository.complete_comment_job(
        stale_job.article_id,
        NOW + timedelta(minutes=1),
        claimed_expected_count=stale_job.expected_count,
        collected_count=2,
    )

    article = await news_repository.get_article("1416149")
    assert stored is False
    assert completed is False
    assert article is not None
    assert article.comment_count == 3
    assert await news_repository.comment_count("1416149") == 0
    assert await news_repository.comment_job_state("1416149") == "pending"
    current_job = (await news_repository.claim_comment_jobs(1, NOW + timedelta(minutes=1)))[0]
    assert current_job.expected_count == 3


async def test_stale_comment_worker_cannot_fail_newer_job(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(comment_count=2))
    stale_job = (await news_repository.claim_comment_jobs(1, NOW))[0]

    await news_repository.apply_listing(
        batch(comment_count=3, observed_at=NOW + timedelta(minutes=1))
    )
    failed = await news_repository.fail_comment_job(
        stale_job.article_id,
        ValueError("stale page"),
        NOW + timedelta(minutes=1),
        claimed_expected_count=stale_job.expected_count,
    )

    assert failed is False
    current_job = (await news_repository.claim_comment_jobs(1, NOW + timedelta(minutes=1)))[0]
    assert current_job.expected_count == 3
    assert current_job.attempts == 0


async def test_comment_job_failure_rolls_back_partial_state_update(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(comment_count=1))
    job = (await news_repository.claim_comment_jobs(1, NOW))[0]
    await news_repository.db.executescript(
        """
        CREATE TRIGGER reject_comment_state_update
        BEFORE UPDATE OF comments_state ON news_articles
        BEGIN
          SELECT RAISE(ABORT, 'forced state failure');
        END;
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced state failure"):
        await news_repository.fail_comment_job(
            job.article_id,
            ValueError("bad page"),
            NOW,
            claimed_expected_count=job.expected_count,
        )

    assert await news_repository.comment_job_state(job.article_id) == "processing"
    await news_repository.db.execute("DROP TRIGGER reject_comment_state_update")
    await news_repository.db.commit()
    assert await news_repository.complete_comment_job(
        job.article_id,
        NOW,
        claimed_expected_count=job.expected_count,
        collected_count=1,
    )


async def test_due_comment_audit_requeues_recent_completed_article(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(comment_count=1))
    job = (await news_repository.claim_comment_jobs(1, NOW))[0]
    comment = CommentObservation("comment-1", "1416149", "Alice", "One", "https://ff.test/1", NOW)
    await news_repository.replace_comments(
        CommentCollectionObservation("1416149", NOW, 1, (comment,), True)
    )
    await news_repository.complete_comment_job(job.article_id, NOW)

    queued = await news_repository.enqueue_due_comment_audits(
        NOW + timedelta(hours=7), audit_interval=timedelta(hours=6)
    )
    jobs = await news_repository.claim_comment_jobs(1, NOW + timedelta(hours=7))

    assert queued == 1
    assert [item.article_id for item in jobs] == ["1416149"]


async def test_comment_audit_respects_failed_job_retry_time(
    news_repository: NewsRepository,
) -> None:
    await news_repository.apply_listing(batch(comment_count=1))
    job = (await news_repository.claim_comment_jobs(1, NOW))[0]
    await news_repository.fail_comment_job(
        job.article_id, ValueError("bad comments"), NOW, max_attempts=1
    )

    queued = await news_repository.enqueue_due_comment_audits(NOW, audit_interval=timedelta(0))

    assert queued == 0


async def test_obsolete_detail_cannot_overwrite_current_listing(news_repository):
    await news_repository.apply_listing(batch())
    job = (await news_repository.claim_detail_jobs(1, now=NOW))[0]
    await news_repository.apply_listing(
        batch(comment_count=1, observed_at=NOW + timedelta(minutes=1))
    )
    assert not await news_repository.replace_detail(
        job.article_id,
        DetailObservation(job.article_id, NOW, "old", segments=()),
        desired_source_hash=job.desired_source_hash,
    )
    assert (await news_repository.get_article(job.article_id)).detail_state == "pending"


async def test_exhausted_details_are_requeued_with_a_daily_bound(news_repository):
    await news_repository.apply_listing(batch())
    job = (await news_repository.claim_detail_jobs(1, now=NOW))[0]
    await news_repository.fail_detail_job(job.article_id, RuntimeError(), now=NOW, max_attempts=1)
    assert await news_repository.enqueue_due_detail_audits(now=NOW + timedelta(hours=1)) == 0
    assert await news_repository.enqueue_due_detail_audits(now=NOW + timedelta(days=1)) == 1
    assert await news_repository.enqueue_due_detail_audits(now=NOW + timedelta(days=1)) == 0


async def test_recent_zero_comment_article_is_audited(news_repository):
    await news_repository.apply_listing(batch())
    await news_repository.claim_comment_jobs(1, now=NOW)
    await news_repository.complete_comment_job("1416149", completed_at=NOW)
    assert await news_repository.enqueue_due_comment_audits(now=NOW + timedelta(hours=7)) == 1


async def test_cancelled_news_transaction_cannot_leak_into_next_commit(
    news_repository, monkeypatch
):
    entered = asyncio.Event()
    original = news_repository._upsert_article

    async def paused(article, source_hash):
        await original(article, source_hash)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(news_repository, "_upsert_article", paused)
    task = asyncio.create_task(news_repository.apply_listing(batch()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await news_repository.set_runtime_state("other", "committed")
    assert await news_repository.count_articles() == 0


async def test_news_read_scope_holds_one_committed_snapshot(tmp_path):
    database = Database(tmp_path / "snap.sqlite3")
    await database.open()
    await database.initialize()
    try:
        writer = NewsRepository(database.connection, database.write_lock)
        await writer.apply_listing(batch())
        async with database.read_connection() as reader:
            scoped = NewsRepository(database.connection, database.write_lock, reader=reader)
            assert (await scoped.get_article("1416149")).comment_count == 0
            await writer.apply_listing(batch(comment_count=1))
            assert (await scoped.detail_data("1416149"))["article"]["comment_count"] == 0
        assert (await writer.get_article("1416149")).comment_count == 1
    finally:
        await database.close()


async def test_mismatch_metadata_preserves_declared_count_and_comments(news_repository):
    await news_repository.apply_listing(batch(comment_count=2))
    old = CommentObservation("old", "1416149", "Alice", "Keep", "https://ff.test/old", NOW)
    await news_repository.replace_comments(
        CommentCollectionObservation("1416149", NOW, 1, (old,), is_complete=True)
    )
    fresh = CommentObservation("fresh", "1416149", "Bob", "New", "https://ff.test/new", NOW)
    await news_repository.replace_comments(
        CommentCollectionObservation(
            "1416149",
            NOW,
            2,
            (fresh,),
            is_complete=False,
            source_complete=True,
            visible_count=1,
        )
    )
    detail = await news_repository.detail_data("1416149")
    assert detail["article"]["comment_count"] == 2
    assert detail["article"]["comments_visible_count"] == 1
    assert detail["article"]["comments_source_complete"] == 1
    assert detail["article"]["comments_state"] == "partial"
    assert await news_repository.comment_count("1416149") == 2
    status = await news_repository.status_counts()
    assert status["comments_count_mismatch"] == 1
    assert status["comment_states"] == {"partial": 1}


async def test_status_excludes_obsolete_failed_media(news_repository):
    await news_repository.apply_listing(batch())
    await news_repository.db.execute(
        """INSERT INTO news_media(article_id,stable_key,position,media_type,original_url,
           download_state,is_current) VALUES ('1416149','old',0,'image','https://ff.test/old',
           'failed',0)"""
    )
    await news_repository.db.commit()
    assert (await news_repository.status_counts())["media_jobs"] == {}
