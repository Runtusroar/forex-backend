from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.db import Database
from app.news.collector import NewsCollector
from app.news.repository import NewsRepository
from app.parsers import ChallengePageError

FIXTURES = Path(__file__).parents[1] / "fixtures"
LISTING = (FIXTURES / "news_v2/listing_all_sections.html").read_text()
DETAIL = (FIXTURES / "news_v2/detail_alloy.html").read_text()
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
