from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.collector.browser import NewsContinuationPage
from app.news.backfill import NewsBackfill
from app.news.models import ListingApplyResult

NOW = datetime(2026, 9, 3, tzinfo=UTC)


def listing(article_id: str, source_time: str) -> str:
    return f"""
    <div class="news-block"><h2>News / Latest Stories</h2>
      <div class="news-block__item"><div class="news-block__title">
        <a href="/news/{article_id}-x">Story {article_id}</a></div>
        <div class="news-block__details"><span class="nowrap" title="{source_time}">
        ago</span></div></div>
    </div>
    """


class FakeContinuationBrowser:
    def __init__(self, pages: list[str], terminal: bool = False) -> None:
        self.pages = pages
        self.terminal = terminal
        self.calls: list[tuple[str, int]] = []

    async def news_more_html(self, section: str, count: int) -> NewsContinuationPage:
        self.calls.append((section, count))
        html = self.pages[min(count - 1, len(self.pages) - 1)]
        return NewsContinuationPage(html, count, frozenset(), self.terminal)


class FakeBackfillRepository:
    def __init__(self) -> None:
        self.state: dict[str, str] = {}
        self.ids: set[str] = set()

    async def get_runtime_state(self, key: str) -> str | None:
        return self.state.get(key)

    async def set_runtime_state(self, key: str, value: str) -> None:
        self.state[key] = value

    async def ready_detail_job_count(self) -> int:
        return 0

    async def apply_listing(self, batch) -> ListingApplyResult:
        observed = {article.source_id for article in batch.articles}
        new = tuple(sorted(observed - self.ids))
        self.ids.update(observed)
        return ListingApplyResult(len(observed), new)


async def test_backfill_checkpoint_survives_new_instance_and_stops_after_no_new_ids() -> None:
    page = listing("1", "Sep 2, 2026, 10:42pm")
    browser = FakeContinuationBrowser([page])
    repository = FakeBackfillRepository()

    for _ in range(3):
        backfill = NewsBackfill(
            browser,
            repository,
            ZoneInfo("Asia/Shanghai"),
            days=30,
            sections=("latest",),
        )
        await backfill.run_once(now=NOW)

    checkpoint = json.loads(repository.state["news_backfill:latest"])
    assert browser.calls == [("latest", 1), ("latest", 2), ("latest", 3)]
    assert checkpoint["no_new_id_streak"] == 2
    assert checkpoint["complete"] is True
    assert repository.ids == {"1"}


async def test_backfill_stops_at_cutoff_or_terminal_page() -> None:
    old = listing("2", "Jul 1, 2026, 10:42pm")
    browser = FakeContinuationBrowser([old])
    repository = FakeBackfillRepository()
    backfill = NewsBackfill(
        browser,
        repository,
        ZoneInfo("Asia/Shanghai"),
        days=30,
        sections=("latest",),
    )

    result = await backfill.run_once(now=NOW)

    checkpoint = json.loads(repository.state["news_backfill:latest"])
    assert result.completed_sections == 1
    assert checkpoint["complete"] is True
