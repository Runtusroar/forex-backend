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


def mixed_listing(latest_id: str, fundamental_id: str) -> str:
    return f"""
    <div class="news-block"><h2>News / Latest Stories</h2>
      <div class="news-block__item"><div class="news-block__title">
        <a href="/news/{latest_id}-x">Story {latest_id}</a></div></div>
    </div>
    <div class="news-block"><h2>Fundamental Analysis / Latest Stories</h2>
      <div class="news-block__item"><div class="news-block__title">
        <a href="/news/{fundamental_id}-x">Story {fundamental_id}</a></div></div>
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


async def test_backfill_checkpoint_survives_new_instance_and_pauses_without_false_coverage() -> (
    None
):
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
    assert checkpoint["complete"] is False
    assert checkpoint["stop_reason"] == "no_progress"
    assert checkpoint["reached_cutoff"] is False
    await backfill.run_once(now=NOW)
    assert len(browser.calls) == 3
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


async def test_backfill_streak_only_counts_new_ids_in_current_section() -> None:
    page = mixed_listing("200", "100")
    browser = FakeContinuationBrowser([page])
    repository = FakeBackfillRepository()
    repository.ids.add("100")
    repository.state["news_backfill:fundamental"] = json.dumps(
        {
            "continuation_count": 0,
            "oldest_published_at": None,
            "last_run_at": NOW.isoformat(),
            "no_new_id_streak": 1,
            "complete": False,
        }
    )
    backfill = NewsBackfill(
        browser,
        repository,
        ZoneInfo("Asia/Shanghai"),
        days=30,
        sections=("fundamental",),
    )

    result = await backfill.run_once(now=NOW)

    checkpoint = json.loads(repository.state["news_backfill:fundamental"])
    assert result.completed_sections == 0
    assert checkpoint["no_new_id_streak"] == 2
    assert checkpoint["complete"] is False


async def test_existing_overlap_does_not_hide_unknown_third_page() -> None:
    browser = FakeContinuationBrowser(
        [listing(str(i), "Sep 2, 2026, 10:42pm") for i in range(1, 4)]
    )
    repository = FakeBackfillRepository()
    repository.ids.update({"1", "2"})
    backfill = NewsBackfill(browser, repository, ZoneInfo("UTC"), sections=("latest",))
    for _ in range(3):
        await backfill.run_once(now=NOW)
    assert "3" in repository.ids
    assert json.loads(repository.state["news_backfill:latest"])["reached_cutoff"] is False


async def test_completed_checkpoint_reopens_after_one_day() -> None:
    from datetime import timedelta

    browser = FakeContinuationBrowser([listing("1", "Jul 1, 2026, 10:42pm")])
    repository = FakeBackfillRepository()
    backfill = NewsBackfill(browser, repository, ZoneInfo("UTC"), sections=("latest",))
    await backfill.run_once(now=NOW)
    await backfill.run_once(now=NOW)
    await backfill.run_once(now=NOW + timedelta(days=1))
    assert browser.calls == [("latest", 1), ("latest", 1)]
    checkpoint = json.loads(repository.state["news_backfill:latest"])
    assert checkpoint["stop_reason"] == "cutoff"
    assert checkpoint["reached_cutoff"] is True
    assert checkpoint["target_cutoff"] is not None


async def test_legacy_completed_checkpoint_reopens() -> None:
    browser = FakeContinuationBrowser([listing("1", "Sep 2, 2026, 10:42pm")], terminal=True)
    repository = FakeBackfillRepository()
    repository.state["news_backfill:latest"] = json.dumps(
        {"continuation_count": 3, "complete": True}
    )
    backfill = NewsBackfill(browser, repository, ZoneInfo("UTC"), sections=("latest",))
    await backfill.run_once(now=NOW)
    assert browser.calls == [("latest", 1)]
    checkpoint = json.loads(repository.state["news_backfill:latest"])
    assert checkpoint["stop_reason"] == "source_exhausted"
    assert checkpoint["reached_cutoff"] is False


async def test_active_backfill_sections_make_fair_progress() -> None:
    browser = FakeContinuationBrowser([mixed_listing("1", "2")])
    repository = FakeBackfillRepository()
    for _ in range(4):
        await NewsBackfill(
            browser, repository, ZoneInfo("UTC"), sections=("latest", "fundamental")
        ).run_once(now=NOW)
    assert [section for section, _ in browser.calls] == [
        "latest",
        "fundamental",
        "latest",
        "fundamental",
    ]
