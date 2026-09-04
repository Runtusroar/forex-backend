from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.collector import Collector
from app.db import Database
from app.domain import CalendarObservation, NewsObservation
from app.repository import Repository
from app.translation.worker import TranslationRunResult, TranslationWorker

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
async def repository(tmp_path: Path):
    database = Database(tmp_path / "workers.sqlite3")
    await database.open()
    await database.initialize()
    yield Repository(database)
    await database.close()


class FakeBrowser:
    def __init__(self) -> None:
        self.detail_calls = 0
        self.calendar_days: list[date] = []

    async def calendar_html(self, day: date) -> str:
        self.calendar_days.append(day)
        return (FIXTURES / "calendar.html").read_text()

    async def news_html(self) -> str:
        return (FIXTURES / "news.html").read_text()

    async def news_detail_html(self, _url: str) -> str:
        self.detail_calls += 1
        return (FIXTURES / "news_article.html").read_text()


class FakeTranslator:
    async def translate(self, jobs):
        return {
            job.id: {"title_zh": "美元上涨", "summary_zh": "美元走强。", "body_zh": "美元上涨。"}
            for job in jobs
        }


class FailingTranslator:
    async def translate(self, _jobs):
        raise httpx.ReadTimeout("timed out")


class FailingDetailBrowser(FakeBrowser):
    async def news_detail_html(self, _url: str) -> str:
        self.detail_calls += 1
        raise TimeoutError("detail timed out")


class FailingCalendarBrowser(FakeBrowser):
    def __init__(self, fail_on: date) -> None:
        super().__init__()
        self.fail_on = fail_on

    async def calendar_html(self, day: date) -> str:
        self.calendar_days.append(day)
        if day == self.fail_on:
            raise TimeoutError("calendar timed out")
        return daily_calendar_html(day, str(day.day))


class CurrentCalendarBrowser(FakeBrowser):
    async def calendar_html(self, day: date) -> str:
        self.calendar_days.append(day)
        return (FIXTURES / "calendar_current.html").read_text()


def daily_calendar_html(day: date, source_id: str) -> str:
    return f"""
    <table>
      <tr class="calendar__row calendar__row--day-breaker"><td>{day:%a %b} {day.day}</td></tr>
      <tr class="calendar__row" data-event-id="{source_id}">
        <td class="calendar__date">{day:%a %b} {day.day}</td>
        <td class="calendar__time">4:00pm</td>
        <td class="calendar__currency">USD</td>
        <td class="calendar__impact"><span class="icon--ff-impact-red"></span></td>
        <td class="calendar__event">Daily event {source_id}</td>
        <td class="calendar__actual"></td>
        <td class="calendar__forecast">50</td>
        <td class="calendar__previous">49</td>
      </tr>
    </table>
    """


async def test_collection_cycle_commits_calendar_and_news(repository: Repository) -> None:
    result = await Collector(FakeBrowser(), repository, horizon_days=1).run_cycle(
        datetime(2026, 9, 1, 12, tzinfo=UTC)
    )

    assert result.calendar_count == 2
    assert result.news_count == 1
    assert (await repository.get_news("9001")).body_en == "The dollar advanced.\n\nYields rose."


async def test_calendar_collection_requests_daily_page_and_uses_source_timezone(
    repository: Repository,
) -> None:
    browser = CurrentCalendarBrowser()
    collector = Collector(
        browser,
        repository,
        source_timezone=timezone(timedelta(hours=8)),
        horizon_days=1,
    )

    await collector.run_calendar_cycle(datetime(2026, 8, 31, 12, tzinfo=UTC))

    rows = await repository.list_calendar(
        datetime(2026, 8, 30, 23, tzinfo=UTC),
        datetime(2026, 8, 31, 1, tzinfo=UTC),
    )
    assert browser.calendar_days == [date(2026, 8, 31)]
    assert rows[0].event_at == datetime(2026, 8, 30, 23, 50, tzinfo=UTC)


async def test_failed_horizon_fetch_keeps_previous_snapshot_and_records_error(
    repository: Repository,
) -> None:
    day = date(2026, 9, 1)
    old = CalendarObservation(
        "old",
        datetime(2026, 9, 1, 4, tzinfo=UTC),
        "USD",
        "high",
        "Last complete event",
        None,
        None,
        None,
    )
    await repository.upsert_calendar([old])
    collector = Collector(
        FailingCalendarBrowser(day + timedelta(days=1)),
        repository,
        source_timezone=timezone(timedelta(hours=8)),
        horizon_days=2,
    )

    with pytest.raises(TimeoutError, match="calendar timed out"):
        await collector.run_calendar_cycle(datetime(2026, 9, 1, tzinfo=UTC))

    rows = await repository.list_calendar(
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 9, 2, tzinfo=UTC),
    )
    assert [row.source_id for row in rows] == ["old"]
    assert "TimeoutError" in (await repository.get_runtime_state("calendar_last_error"))


async def test_collection_reuses_stored_news_detail_on_later_cycles(
    repository: Repository,
) -> None:
    browser = FakeBrowser()
    collector = Collector(browser, repository, horizon_days=1)

    await collector.run_cycle(datetime(2026, 9, 1, 12, tzinfo=UTC))
    await collector.run_cycle(datetime(2026, 9, 1, 12, 1, tzinfo=UTC))

    assert browser.detail_calls == 1


async def test_detail_failure_does_not_block_news_listing_storage(
    repository: Repository,
) -> None:
    result = await Collector(FailingDetailBrowser(), repository, horizon_days=1).run_cycle(
        datetime(2026, 9, 1, 12, tzinfo=UTC)
    )

    stored = await repository.get_news("9001")
    assert result.news_count == 1
    assert stored is not None
    assert stored.title_en == "Dollar rises"
    assert stored.body_en is None


async def test_translation_worker_applies_pending_translation(repository: Repository) -> None:
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    await repository.upsert_news(
        [
            NewsObservation(
                "9001", "https://x", "Reuters", None, now, "Dollar rises", None, None, None
            )
        ]
    )

    result = await TranslationWorker(repository, FakeTranslator()).run_once()

    assert result.completed == 1
    assert (await repository.get_news("9001")).title_zh == "美元上涨"


async def test_translation_failure_is_delayed_without_changing_english(
    repository: Repository,
) -> None:
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    await repository.upsert_news(
        [NewsObservation("9002", "https://x", None, None, now, "Gold rises", None, None, None)]
    )
    worker = TranslationWorker(repository, FailingTranslator())

    first = await worker.run_once()
    immediate_retry = await worker.run_once()

    assert first.failed == 1
    assert immediate_retry == TranslationRunResult(0, 0)
    assert (await repository.get_news("9002")).title_en == "Gold rises"
    assert (await repository.get_news("9002")).title_zh is None
