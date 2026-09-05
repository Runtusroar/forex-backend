from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.collector.browser import NewsCommentCapture
from app.db import Database
from app.news.collector import NewsCollector
from app.news.comments import CommentCollector
from app.news.models import CommentCollectionObservation, CommentObservation
from app.news.repository import NewsRepository
from app.news.snapshots import SnapshotStore

FIXTURES = Path(__file__).parents[1] / "fixtures"
LISTING = (FIXTURES / "news_v2/listing_all_sections.html").read_text()
DETAIL = (FIXTURES / "news_v2/detail_alloy.html").read_text()
UNSUPPORTED_WITH_COMMENT = (
    FIXTURES / "news_v2/comments_unsupported_audit.html"
).read_text()
HIDDEN_COVERAGE = (FIXTURES / "news_v2/comments_hidden_coverage_audit.html").read_text()
UNACCOUNTED_NODE = (FIXTURES / "news_v2/comments_unaccounted_node_audit.html").read_text()
NOW = datetime(2026, 9, 3, tzinfo=UTC)


class FixtureBrowser:
    def __init__(
        self,
        comment_html: str,
        declared_count: int,
        collected_count: int,
        *,
        source_exhausted: bool,
    ) -> None:
        self.comment_html = comment_html
        self.declared_count = declared_count
        self.collected_count = collected_count
        self.source_exhausted = source_exhausted

    async def news_html(self) -> str:
        return LISTING

    async def news_detail_html(self, url: str) -> str:
        return DETAIL

    async def news_comments_html(
        self, url: str, expected_comment_count: int | None = None
    ) -> NewsCommentCapture:
        return NewsCommentCapture(
            html=self.comment_html,
            declared_count=self.declared_count,
            collected_count=self.collected_count,
            source_exhausted=self.source_exhausted,
        )


@pytest.fixture
async def news_repository(tmp_path: Path):
    database = Database(tmp_path / "comments.sqlite3")
    await database.open()
    await database.initialize()
    assert database.connection is not None
    yield NewsRepository(database.connection)
    await database.close()


async def test_comments_are_saved_when_story_body_is_unsupported(
    news_repository: NewsRepository,
) -> None:
    browser = FixtureBrowser(
        UNSUPPORTED_WITH_COMMENT, 1, 1, source_exhausted=True
    )
    await NewsCollector(browser, news_repository, ZoneInfo("Asia/Shanghai")).run_listing_cycle(
        NOW
    )

    result = await CommentCollector(
        browser, news_repository, ZoneInfo("Asia/Shanghai")
    ).run_cycle(NOW)
    comments, _ = await news_repository.list_comments("100", 10)

    assert result == 1
    assert [(row["comment_id"], row["text_en"]) for row in comments] == [
        ("700", "Comment survives an unsupported story body.")
    ]


async def test_source_exhausted_mismatch_preserves_rows_and_finishes_job(
    news_repository: NewsRepository, tmp_path: Path
) -> None:
    complete_browser = FixtureBrowser(DETAIL, 2, 2, source_exhausted=True)
    await NewsCollector(
        complete_browser, news_repository, ZoneInfo("Asia/Shanghai")
    ).run_listing_cycle(NOW)
    assert (
        await CommentCollector(
            complete_browser, news_repository, ZoneInfo("Asia/Shanghai")
        ).run_cycle(NOW)
        == 1
    )
    await news_repository.db.execute(
        "UPDATE news_articles SET comments_checked_at=? WHERE source_id='100'",
        ((NOW - timedelta(hours=7)).isoformat().replace("+00:00", "Z"),),
    )
    await news_repository.db.commit()
    assert await news_repository.enqueue_due_comment_audits(NOW, limit=1) == 1

    mismatch_browser = FixtureBrowser(
        UNSUPPORTED_WITH_COMMENT, 2, 1, source_exhausted=True
    )
    snapshot_store = SnapshotStore(tmp_path / "snapshots", news_repository)
    result = await CommentCollector(
        mismatch_browser,
        news_repository,
        ZoneInfo("Asia/Shanghai"),
        snapshot_store=snapshot_store,
    ).run_cycle(NOW)
    comments, _ = await news_repository.list_comments("100", 10)
    article_rows = await news_repository.db.execute_fetchall(
        """SELECT comment_count,comments_state,comments_source_complete,
                  comments_visible_count
           FROM news_articles WHERE source_id='100'"""
    )
    snapshot_rows = await news_repository.db.execute_fetchall(
        "SELECT page_type,parse_status,error_type FROM source_snapshots"
    )

    assert result == 1
    assert await news_repository.comment_job_state("100") == "done"
    assert {row["comment_id"] for row in comments} == {"700", "701"}
    assert dict(article_rows[0]) == {
        "comment_count": 2,
        "comments_state": "partial",
        "comments_source_complete": 1,
        "comments_visible_count": 1,
    }
    assert [tuple(row) for row in snapshot_rows] == [
        ("comments", "failed", "CommentCountMismatch")
    ]


async def test_equal_counts_without_source_exhaustion_preserve_prior_comments_and_retry(
    news_repository: NewsRepository,
) -> None:
    complete_browser = FixtureBrowser(DETAIL, 2, 2, source_exhausted=True)
    await NewsCollector(
        complete_browser, news_repository, ZoneInfo("Asia/Shanghai")
    ).run_listing_cycle(NOW)
    assert (
        await CommentCollector(
            complete_browser, news_repository, ZoneInfo("Asia/Shanghai")
        ).run_cycle(NOW)
        == 1
    )
    await news_repository.db.execute(
        "UPDATE news_articles SET comments_checked_at=? WHERE source_id='100'",
        ((NOW - timedelta(hours=7)).isoformat().replace("+00:00", "Z"),),
    )
    await news_repository.db.commit()
    assert await news_repository.enqueue_due_comment_audits(NOW, limit=1) == 1

    timed_out_browser = FixtureBrowser(
        UNSUPPORTED_WITH_COMMENT, 1, 1, source_exhausted=False
    )
    result = await CommentCollector(
        timed_out_browser, news_repository, ZoneInfo("Asia/Shanghai")
    ).run_cycle(NOW)
    comments, _ = await news_repository.list_comments("100", 10)

    assert result == 0
    assert await news_repository.comment_job_state("100") == "pending"
    assert {row["comment_id"] for row in comments} == {"700", "701"}
    assert await news_repository.get_runtime_state("news_last_comment_error") == (
        "CommentSourceIncomplete"
    )


async def test_exhausted_hidden_comments_finish_partial_and_preserve_old_body(
    news_repository: NewsRepository, tmp_path: Path
) -> None:
    browser = FixtureBrowser(HIDDEN_COVERAGE, 15, 15, source_exhausted=True)
    await NewsCollector(browser, news_repository, ZoneInfo("Asia/Shanghai")).run_listing_cycle(
        NOW
    )
    hidden_old_body = CommentObservation(
        "2012",
        "100",
        "Previously Visible",
        "Older body retained after source moderation.",
        "https://www.forexfactory.com/news/coverage/comment/2012#post2012",
        NOW - timedelta(days=1),
    )
    await news_repository.replace_comments(
        CommentCollectionObservation(
            "100", NOW - timedelta(days=1), 15, (hidden_old_body,), False
        )
    )
    snapshot_store = SnapshotStore(tmp_path / "hidden-snapshots", news_repository)

    result = await CommentCollector(
        browser,
        news_repository,
        ZoneInfo("Asia/Shanghai"),
        snapshot_store=snapshot_store,
    ).run_cycle(NOW)
    comments, _ = await news_repository.list_comments("100", 20)
    article_rows = await news_repository.db.execute_fetchall(
        """SELECT comment_count,comments_state,comments_source_complete,
                  comments_visible_count
           FROM news_articles WHERE source_id='100'"""
    )
    snapshot_rows = await news_repository.db.execute_fetchall(
        "SELECT parse_status,error_type FROM source_snapshots"
    )

    assert result == 1
    assert await news_repository.comment_job_state("100") == "done"
    assert len(comments) == 12
    assert next(row for row in comments if row["comment_id"] == "2012")["text_en"] == (
        "Older body retained after source moderation."
    )
    assert dict(article_rows[0]) == {
        "comment_count": 15,
        "comments_state": "partial",
        "comments_source_complete": 1,
        "comments_visible_count": 15,
    }
    assert [tuple(row) for row in snapshot_rows] == [
        ("failed", "SourceCommentsUnavailable")
    ]
    assert await news_repository.get_runtime_state("news_last_comment_warning") == (
        "SourceCommentsUnavailable"
    )


async def test_source_exhausted_unaccounted_comment_node_stays_retryable(
    news_repository: NewsRepository,
) -> None:
    browser = FixtureBrowser(UNACCOUNTED_NODE, 2, 2, source_exhausted=True)
    await NewsCollector(browser, news_repository, ZoneInfo("Asia/Shanghai")).run_listing_cycle(
        NOW
    )

    result = await CommentCollector(
        browser, news_repository, ZoneInfo("Asia/Shanghai")
    ).run_cycle(NOW)

    assert result == 0
    assert await news_repository.comment_job_state("100") == "pending"
    assert await news_repository.get_runtime_state("news_last_comment_error") == (
        "CommentCountMismatch"
    )
