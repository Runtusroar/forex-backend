from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.collector.browser import NewsCommentCapture
from app.db import Database
from app.news.collector import NewsCollector
from app.news.comments import CommentCollector
from app.news.repository import NewsRepository
from app.parsers import ChallengePageError

FIXTURES = Path(__file__).parents[1] / "fixtures"
LISTING = (FIXTURES / "news_v2/listing_all_sections.html").read_text()
DETAIL = (FIXTURES / "news_v2/detail_alloy.html").read_text()
PARTIAL_DETAIL = (FIXTURES / "news_v2/detail_partial_unrecognized_audit.html").read_text()
CHALLENGE = (FIXTURES / "challenge.html").read_text()
NOW = datetime(2026, 9, 3, tzinfo=UTC)


class FakeBrowser:
    def __init__(self, listing: str = LISTING, detail: str = DETAIL) -> None:
        self.listing = listing
        self.detail = detail
        self.detail_urls: list[str] = []

    async def news_html(self) -> str:
        return self.listing

    async def news_detail_html(self, url: str) -> str:
        self.detail_urls.append(url)
        return self.detail


class FailingDetailBrowser(FakeBrowser):
    async def news_detail_html(self, url: str) -> str:
        self.detail_urls.append(url)
        raise TimeoutError("private source detail omitted")


class CommentBrowser(FakeBrowser):
    def __init__(
        self,
        declared_count: int,
        collected_count: int,
        *,
        source_exhausted: bool = True,
    ) -> None:
        super().__init__(listing=LISTING.replace("16 comments", f"{declared_count} comments"))
        self.declared_count = declared_count
        self.collected_count = collected_count
        self.source_exhausted = source_exhausted

    async def news_comments_html(
        self, url: str, expected_comment_count: int | None = None
    ) -> NewsCommentCapture:
        self.detail_urls.append(url)
        return NewsCommentCapture(
            self.detail,
            self.declared_count,
            self.collected_count,
            source_exhausted=self.source_exhausted,
        )


@pytest.fixture
async def news_repository(tmp_path: Path):
    database = Database(tmp_path / "collector.sqlite3")
    await database.open()
    await database.initialize()
    assert database.connection is not None
    yield NewsRepository(database.connection)
    await database.close()


async def test_listing_and_detail_cycles_are_independent(news_repository: NewsRepository) -> None:
    browser = FakeBrowser()
    collector = NewsCollector(browser, news_repository, ZoneInfo("Asia/Shanghai"))

    result = await collector.run_listing_cycle(NOW)
    detail_count = await collector.run_detail_cycle(NOW)

    assert result.article_count == 6
    assert detail_count == 1
    assert browser.detail_urls == ["https://www.forexfactory.com/news/100-yen"]
    assert await news_repository.current_segment_keys("100")


async def test_challenge_listing_never_mutates_repository(news_repository: NewsRepository) -> None:
    collector = NewsCollector(
        FakeBrowser(listing=CHALLENGE), news_repository, ZoneInfo("Asia/Shanghai")
    )

    with pytest.raises(ChallengePageError):
        await collector.run_listing_cycle(NOW)

    assert await news_repository.count_articles() == 0


async def test_detail_failure_is_persisted_and_bounded(news_repository: NewsRepository) -> None:
    browser = FailingDetailBrowser()
    collector = NewsCollector(
        browser,
        news_repository,
        ZoneInfo("Asia/Shanghai"),
        detail_max_attempts=1,
    )
    await collector.run_listing_cycle(NOW)

    assert await collector.run_detail_cycle(NOW) == 0
    assert await news_repository.detail_job_state("100") == "failed"

    restarted = NewsCollector(
        browser,
        news_repository,
        ZoneInfo("Asia/Shanghai"),
        detail_max_attempts=1,
    )
    assert await restarted.run_detail_cycle(NOW) == 0
    assert browser.detail_urls.count("https://www.forexfactory.com/news/100-yen") == 1


async def test_partial_detail_is_saved_without_retiring_old_segments_and_retried(
    news_repository: NewsRepository,
) -> None:
    initial = NewsCollector(FakeBrowser(), news_repository, ZoneInfo("Asia/Shanghai"))
    await initial.run_listing_cycle(NOW)
    assert await initial.run_detail_cycle(NOW) == 1
    old_keys = set(await news_repository.current_segment_keys("100"))

    changed_listing = LISTING.replace("Yen moves", "Yen moves again")
    partial = NewsCollector(
        FakeBrowser(listing=changed_listing, detail=PARTIAL_DETAIL),
        news_repository,
        ZoneInfo("Asia/Shanghai"),
    )
    await partial.run_listing_cycle(NOW)

    assert await partial.run_detail_cycle(NOW) == 0
    assert await news_repository.detail_job_state("100") == "pending"
    assert old_keys.issubset(set(await news_repository.current_segment_keys("100")))


async def test_successful_cycles_capture_listing_and_detail_snapshots(
    news_repository: NewsRepository, tmp_path: Path
) -> None:
    from app.news.snapshots import SnapshotStore

    collector = NewsCollector(
        FakeBrowser(),
        news_repository,
        ZoneInfo("Asia/Shanghai"),
        snapshot_store=SnapshotStore(tmp_path / "snapshots", news_repository),
    )

    await collector.run_listing_cycle(NOW)
    await collector.run_listing_cycle(NOW)
    await collector.run_detail_cycle(NOW)

    assert await news_repository.snapshot_count() == 2


async def test_comment_collector_completes_only_when_source_count_matches(
    news_repository: NewsRepository,
) -> None:
    browser = CommentBrowser(declared_count=2, collected_count=2)
    listing = NewsCollector(browser, news_repository, ZoneInfo("Asia/Shanghai"))
    await listing.run_listing_cycle(NOW)
    collector = CommentCollector(browser, news_repository, ZoneInfo("Asia/Shanghai"))

    assert await collector.run_cycle(NOW) == 1
    assert await news_repository.comment_count("100") == 2
    assert await news_repository.comment_job_state("100") == "done"
    assert await news_repository.get_runtime_state("news_last_comment_success") == (
        NOW.isoformat()
    )


async def test_comment_collector_retries_nonexhausted_count_mismatch(
    news_repository: NewsRepository,
) -> None:
    browser = CommentBrowser(
        declared_count=3, collected_count=2, source_exhausted=False
    )
    listing = NewsCollector(browser, news_repository, ZoneInfo("Asia/Shanghai"))
    await listing.run_listing_cycle(NOW)
    collector = CommentCollector(browser, news_repository, ZoneInfo("Asia/Shanghai"))

    assert await collector.run_cycle(NOW) == 0
    assert await news_repository.comment_count("100") == 2
    assert await news_repository.comment_job_state("100") == "pending"
    assert await news_repository.get_runtime_state("news_last_comment_error") == (
        "CommentSourceIncomplete"
    )


async def test_zero_capture_does_not_remove_previously_collected_comments(
    news_repository: NewsRepository,
) -> None:
    initial_browser = CommentBrowser(declared_count=2, collected_count=2)
    listing = NewsCollector(initial_browser, news_repository, ZoneInfo("Asia/Shanghai"))
    await listing.run_listing_cycle(NOW)
    initial_collector = CommentCollector(
        initial_browser, news_repository, ZoneInfo("Asia/Shanghai")
    )
    assert await initial_collector.run_cycle(NOW) == 1

    zero_browser = CommentBrowser(declared_count=0, collected_count=0)
    zero_browser.detail = (
        '<article class="news__article"><div class="news__copy"><p>Story</p></div></article>'
    )
    await news_repository.db.execute(
        """UPDATE news_articles SET comment_count=0,comments_checked_at=?
           WHERE source_id='100'""",
        ((NOW.replace(year=2025)).isoformat().replace("+00:00", "Z"),),
    )
    await news_repository.db.commit()
    await news_repository.enqueue_due_comment_audits(
        NOW, audit_interval=NOW - NOW, recent_window=NOW - NOW.replace(year=2025)
    )

    result = await CommentCollector(
        zero_browser, news_repository, ZoneInfo("Asia/Shanghai")
    ).run_cycle(NOW)

    assert result == 0
    assert await news_repository.comment_count("100") == 2
    assert await news_repository.comment_job_state("100") == "pending"


async def test_explicit_listing_zero_can_confirm_all_comments_removed(
    news_repository: NewsRepository,
) -> None:
    initial_browser = CommentBrowser(declared_count=2, collected_count=2)
    await NewsCollector(
        initial_browser, news_repository, ZoneInfo("Asia/Shanghai")
    ).run_listing_cycle(NOW)
    assert (
        await CommentCollector(
            initial_browser, news_repository, ZoneInfo("Asia/Shanghai")
        ).run_cycle(NOW)
        == 1
    )
    zero_browser = CommentBrowser(declared_count=0, collected_count=0)
    zero_browser.detail = (
        '<article class="news__article"><div class="news__copy"><p>Story</p></div></article>'
    )
    await NewsCollector(
        zero_browser, news_repository, ZoneInfo("Asia/Shanghai")
    ).run_listing_cycle(NOW)

    result = await CommentCollector(
        zero_browser, news_repository, ZoneInfo("Asia/Shanghai")
    ).run_cycle(NOW)

    assert result == 1
    assert await news_repository.comment_count("100") == 0
    assert await news_repository.comment_job_state("100") == "done"
