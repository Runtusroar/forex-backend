from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.collector import Collector
from app.collector.calendar_details import CalendarDetailCollector
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
        return (FIXTURES / "calendar.html").read_text() + calendar_payload(
            date(2026, 9, 1), "1001", "1002"
        )

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
        return (FIXTURES / "calendar_current.html").read_text() + calendar_payload(
            date(2026, 8, 31), "149673"
        )


class CalendarDetailBatchBrowser(FakeBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.detail_batches: list[tuple[date, tuple[str, ...]]] = []

    async def calendar_details_html(self, day: date, source_ids: list[str]) -> dict[str, str]:
        self.detail_batches.append((day, tuple(source_ids)))
        html = (FIXTURES / "calendar_detail.html").read_text()
        return {source_id: html for source_id in source_ids}


class PartialCalendarDetailBatchBrowser(CalendarDetailBatchBrowser):
    async def calendar_details_html(self, day: date, source_ids: list[str]) -> dict[str, str]:
        self.detail_batches.append((day, tuple(source_ids)))
        return {"149673": (FIXTURES / "calendar_detail.html").read_text()}


class UnavailableCalendarDetailBatchBrowser(CalendarDetailBatchBrowser):
    async def calendar_details_html(
        self, day: date, source_ids: list[str]
    ) -> dict[str, str | None]:
        self.detail_batches.append((day, tuple(source_ids)))
        return {source_id: None for source_id in source_ids}


def calendar_payload(day: date, *source_ids: str) -> str:
    import json

    days = [{"date": f"{day:%b} {day.day}", "events": [{"id": value} for value in source_ids]}]
    return "<script>window.calendarComponentStates[1] = {days: " + json.dumps(days) + "};</script>"


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
    """ + calendar_payload(day, source_id)


async def test_collection_cycle_commits_calendar_and_news(repository: Repository) -> None:
    result = await Collector(FakeBrowser(), repository, horizon_days=1, lookback_days=0).run_cycle(
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
        lookback_days=0,
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
    collector = Collector(browser, repository, horizon_days=1, lookback_days=0)

    await collector.run_cycle(datetime(2026, 9, 1, 12, tzinfo=UTC))
    await collector.run_cycle(datetime(2026, 9, 1, 12, 1, tzinfo=UTC))

    assert browser.detail_calls == 1


async def test_detail_failure_does_not_block_news_listing_storage(
    repository: Repository,
) -> None:
    result = await Collector(
        FailingDetailBrowser(), repository, horizon_days=1, lookback_days=0
    ).run_cycle(datetime(2026, 9, 1, 12, tzinfo=UTC))

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


async def test_calendar_detail_worker_prefetches_queued_detail(
    repository: Repository,
) -> None:
    event_at = datetime(2026, 8, 31, 7, 50, tzinfo=UTC)
    await repository.upsert_calendar(
        [
            CalendarObservation(
                "149673",
                event_at,
                "JPY",
                "low",
                "Prelim Industrial Production m/m",
                "0.1%",
                "-0.7%",
                "1.9%",
            )
        ]
    )
    browser = CalendarDetailBatchBrowser()
    worker = CalendarDetailCollector(browser, repository, source_timezone=UTC)

    assert await worker.run_cycle() == 1
    assert browser.detail_batches == [(event_at.date(), ("149673",))]
    detail = await repository.get_calendar_detail("149673")
    assert detail is not None
    assert detail.source_name == "METI"


async def test_calendar_detail_worker_keeps_error_when_batch_is_partially_successful(
    repository: Repository,
) -> None:
    event_at = datetime(2026, 8, 31, 7, 50, tzinfo=UTC)
    await repository.upsert_calendar(
        [
            CalendarObservation(
                source_id,
                event_at,
                "JPY",
                "low",
                f"Event {source_id}",
                None,
                None,
                None,
            )
            for source_id in ("149673", "149674")
        ]
    )
    worker = CalendarDetailCollector(
        PartialCalendarDetailBatchBrowser(), repository, source_timezone=UTC
    )

    completed = await worker.run_cycle(datetime.now(UTC) + timedelta(seconds=1))

    assert completed == 1
    assert await repository.get_runtime_state("calendar_detail_last_error") == "KeyError"


async def test_calendar_detail_worker_completes_event_without_detail_link(
    repository: Repository,
) -> None:
    event_at = datetime(2026, 8, 31, 7, 50, tzinfo=UTC)
    await repository.upsert_calendar(
        [CalendarObservation("149673", event_at, "JPY", "low", "Holiday", None, None, None)]
    )
    worker = CalendarDetailCollector(
        UnavailableCalendarDetailBatchBrowser(), repository, source_timezone=UTC
    )

    completed = await worker.run_cycle(datetime.now(UTC) + timedelta(seconds=1))

    assert completed == 1
    assert await repository.get_calendar_detail("149673") is None
    assert (await repository.calendar_detail_job_counts()) == {"done": 1}


async def test_calendar_lookback_runs_once_per_source_day(repository: Repository) -> None:
    browser = FailingCalendarBrowser(date(2000, 1, 1))
    collector = Collector(
        browser, repository, horizon_days=2, lookback_days=2, schedule_interval=600
    )
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    await collector.run_calendar_cycle(now)
    assert browser.calendar_days == [date(2026, 9, i) for i in (1, 2, 3, 4)]
    browser.calendar_days.clear()
    # Restart should retain the daily lookback checkpoint.
    collector = Collector(
        browser, repository, horizon_days=2, lookback_days=2, schedule_interval=600
    )
    await collector.run_calendar_cycle(now + timedelta(minutes=20))
    assert browser.calendar_days == [date(2026, 9, 3), date(2026, 9, 4)]


async def test_calendar_failed_parse_captures_source_snapshot(repository: Repository) -> None:
    class ShellBrowser(FakeBrowser):
        async def calendar_html(self, day):
            return '<table><tr class="calendar__row"><td>Sep 1</td></tr></table>'

    class Snapshots:
        def __init__(self):
            self.calls = []

        async def capture(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    snapshots = Snapshots()
    collector = Collector(
        ShellBrowser(), repository, horizon_days=1, lookback_days=0, snapshot_store=snapshots
    )
    from app.parsers.errors import SourcePageError

    with pytest.raises(SourcePageError):
        await collector.run_calendar_cycle(datetime(2026, 9, 1, tzinfo=UTC))
    assert snapshots.calls[0][0][0:2] == ("calendar", "2026-09-01")
    assert snapshots.calls[0][1]["error"] is not None


async def test_calendar_detail_uses_source_day_over_utc_timestamp_day(
    repository: Repository,
) -> None:
    await repository.upsert_calendar(
        [
            CalendarObservation(
                "149673",
                datetime(2026, 8, 30, 23, 50, tzinfo=UTC),
                "JPY",
                "low",
                "Prelim Industrial Production m/m",
                None,
                None,
                None,
                source_date=date(2026, 8, 31),
            )
        ]
    )
    browser = CalendarDetailBatchBrowser()
    collector = CalendarDetailCollector(browser, repository, source_timezone=UTC)
    await collector.run_cycle()
    assert browser.detail_batches[0][0] == date(2026, 8, 31)


async def test_truncated_source_without_payload_cannot_replace_complete_calendar(
    repository: Repository,
) -> None:
    from selectolax.parser import HTMLParser

    from app.parsers.errors import SourcePageError

    complete = (FIXTURES / "calendar_source_2026-09-01.html").read_text()
    tree = HTMLParser(complete)
    for node in tree.css("script") + tree.css("tr.calendar__row[data-event-id]")[1:]:
        node.decompose()

    class SourceBrowser(FakeBrowser):
        html = complete

        async def calendar_html(self, day):
            return self.html

    browser = SourceBrowser()
    collector = Collector(browser, repository, horizon_days=1, lookback_days=0)
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    assert await collector.run_calendar_cycle(now) == 39
    browser.html = tree.html
    with pytest.raises(SourcePageError, match="source payload"):
        await collector.run_calendar_cycle(now + timedelta(minutes=1))
    retained = await repository.list_calendar(
        datetime(2026, 8, 30, tzinfo=UTC),
        datetime(2026, 9, 3, tzinfo=UTC),
    )
    assert len({row.source_id for row in retained}) == 39
