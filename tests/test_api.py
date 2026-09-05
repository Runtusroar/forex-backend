from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

import app.main as main
from app.config import Settings
from app.db import Database
from app.domain import (
    CalendarDetailObservation,
    CalendarHistoryObservation,
    CalendarObservation,
    CalendarRelatedStoryObservation,
    NewsObservation,
)
from app.main import create_app
from app.repository import Repository


async def make_client(
    tmp_path: Path, calendar_browser: object | None = None
) -> tuple[httpx.AsyncClient, Repository, Database]:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "api.sqlite3",
        app_api_key="api-secret",
        moonshot_api_key="kimi-secret",
    )
    database = Database(settings.database_path)
    await database.open()
    await database.initialize()
    repository = Repository(database)
    app = create_app(settings, repository=repository)
    if calendar_browser is not None:
        app.state.calendar_browser = calendar_browser
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), repository, database


class FakeCalendarBrowser:
    def __init__(self, html: str) -> None:
        self.html = html
        self.calls: list[tuple[object, str]] = []

    async def calendar_detail_html(self, day: object, source_id: str) -> str:
        self.calls.append((day, source_id))
        return self.html


class FailingCalendarBrowser:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    async def calendar_detail_html(self, day: object, source_id: str) -> str:
        self.calls.append((day, source_id))
        raise RuntimeError("browser disconnected")


def minimal_calendar_detail(source_id: str, source_name: str) -> CalendarDetailObservation:
    return CalendarDetailObservation(
        source_id=source_id,
        title_en="Cached title",
        currency="USD",
        currency_name="US dollar",
        impact="high",
        actual=None,
        forecast=None,
        previous=None,
        actual_state=None,
        previous_state=None,
        previous_revised_from=None,
        ff_url=None,
        source_name=source_name,
        source_url=None,
        latest_release_url=None,
        measures=None,
        usual_effect=None,
        frequency=None,
        next_release_text=None,
        next_release_url=None,
        ff_notes=None,
        why_traders_care=None,
    )


async def test_calendar_requires_api_key(tmp_path: Path) -> None:
    client, _, database = await make_client(tmp_path)
    async with client:
        response = await client.get("/api/v1/calendar")
    await database.close()
    assert response.status_code == 401


async def test_api_returns_english_while_translation_is_pending(tmp_path: Path) -> None:
    client, repository, database = await make_client(tmp_path)
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    await repository.upsert_calendar(
        [CalendarObservation("1", now, "USD", "high", "ISM PMI", None, "50", "49")]
    )
    await repository.upsert_news(
        [
            NewsObservation(
                "9",
                "https://www.forexfactory.com/news/9-x",
                "Reuters",
                None,
                now,
                "Dollar rises",
                "Dollar gained",
                None,
                None,
            )
        ]
    )
    async with client:
        calendar = await client.get(
            "/api/v1/calendar?from=2026-09-01T00:00:00Z&to=2026-09-02T00:00:00Z",
            headers={"X-API-Key": "api-secret"},
        )
        news = await client.get("/api/v1/news", headers={"X-API-Key": "api-secret"})
    await database.close()

    assert calendar.status_code == 200, calendar.text
    assert calendar.json()["items"][0]["title_en"] == "ISM PMI"
    assert calendar.json()["items"][0]["title_zh"] is None
    assert news.json()["items"][0]["title_en"] == "Dollar rises"


def test_calendar_default_range_uses_source_timezone_midnight() -> None:
    start, end = main._calendar_default_range(
        datetime(2026, 9, 4, 18, 30, tzinfo=UTC),
        ZoneInfo("Asia/Singapore"),
        horizon_days=8,
    )

    assert start == datetime(2026, 9, 4, 16, tzinfo=UTC)
    assert end == datetime(2026, 9, 12, 16, tzinfo=UTC)


async def test_calendar_api_reports_last_successful_snapshot_time(tmp_path: Path) -> None:
    client, repository, database = await make_client(tmp_path)
    await repository.set_runtime_state("calendar_last_success", "2026-09-04T13:15:00Z")
    await repository.set_runtime_state("calendar_last_count", "14")
    await repository.set_runtime_state("calendar_last_error", "")
    await repository.set_runtime_state("calendar_detail_last_success", "2026-09-04T13:16:00Z")
    await repository.set_runtime_state("calendar_detail_last_error", "")

    async with client:
        calendar = await client.get(
            "/api/v1/calendar?from=2026-09-04T00:00:00Z&to=2026-09-05T00:00:00Z",
            headers={"X-API-Key": "api-secret"},
        )
        status = await client.get("/api/v1/status", headers={"X-API-Key": "api-secret"})
    await database.close()

    assert calendar.json()["generated_at"] == "2026-09-04T13:15:00Z"
    assert status.json()["calendar"] == {
        "last_success": "2026-09-04T13:15:00Z",
        "last_count": 14,
        "last_error": None,
        "detail_last_success": "2026-09-04T13:16:00Z",
        "detail_last_error": None,
        "detail_jobs": {},
    }


async def test_status_is_degraded_when_calendar_detail_collection_failed(
    tmp_path: Path,
) -> None:
    client, repository, database = await make_client(tmp_path)
    await repository.set_runtime_state("calendar_last_error", "")
    await repository.set_runtime_state("calendar_detail_last_error", "TimeoutError")
    item = CalendarObservation(
        "1", datetime.now(UTC), "USD", "high", "Event", None, None, None
    )
    await repository.upsert_calendar([item])
    claimed_at = datetime.now(UTC) + timedelta(seconds=1)
    job = (await repository.claim_calendar_detail_jobs(1, claimed_at))[0]
    await repository.fail_calendar_detail_job(
        job.source_id,
        TimeoutError("source timeout"),
        claimed_at,
        max_attempts=1,
        desired_source_hash=job.desired_source_hash,
    )

    async with client:
        response = await client.get("/api/v1/status", headers={"X-API-Key": "api-secret"})
    await database.close()

    assert response.json()["status"] == "degraded"

    assert response.json()["calendar"]["detail_last_error"] == "TimeoutError"
    assert response.json()["calendar"]["detail_jobs"]["failed"] == 1


async def test_calendar_api_cannot_read_an_uncommitted_worker_write(tmp_path: Path) -> None:
    client, repository, database = await make_client(tmp_path)
    at = datetime(2026, 9, 5, 12, tzinfo=UTC)
    await repository.upsert_calendar([
        CalendarObservation("1", at, "USD", "high", "Committed", None, None, None)
    ])
    await database.connection.execute(
        "UPDATE calendar_events SET title_en='Uncommitted' WHERE source_id='1'"
    )
    try:
        async with client:
            response = await client.get(
                "/api/v1/calendar?from=2026-09-05T00:00:00Z&to=2026-09-06T00:00:00Z",
                headers={"X-API-Key": "api-secret"},
            )
        assert response.json()["items"][0]["title_en"] == "Committed"
    finally:
        await database.connection.rollback()
        await database.close()


async def test_calendar_status_reports_stale_collection(tmp_path: Path) -> None:
    client, repository, database = await make_client(tmp_path)
    await repository.set_runtime_state(
        "calendar_last_success", (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    )
    async with client:
        response = await client.get("/api/v1/status", headers={"X-API-Key": "api-secret"})
    await database.close()
    assert response.json()["status"] == "degraded"
    assert "calendar_stale" in response.json()["issues"]


async def test_calendar_api_exposes_forex_factory_time_label_and_order(
    tmp_path: Path,
) -> None:
    client, repository, database = await make_client(tmp_path)
    event_at = datetime(2026, 9, 8, 16, tzinfo=UTC)
    await repository.upsert_calendar(
        [
            CalendarObservation(
                "151045",
                event_at,
                "USD",
                "low",
                "ADP Weekly Employment Change",
                None,
                None,
                "11.8K",
                source_time_text="Aug 23rd",
                source_position=9,
            )
        ]
    )

    async with client:
        response = await client.get(
            "/api/v1/calendar?from=2026-09-08T00:00:00Z&to=2026-09-09T00:00:00Z",
            headers={"X-API-Key": "api-secret"},
        )
    await database.close()

    item = response.json()["items"][0]
    assert item["source_time_text"] == "Aug 23rd"
    assert item["source_position"] == 9


async def test_calendar_detail_api_returns_cached_specs_history_and_related_stories(
    tmp_path: Path,
) -> None:
    client, repository, database = await make_client(tmp_path)
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    await repository.upsert_calendar(
        [CalendarObservation("1", now, "USD", "high", "ISM PMI", "51.2", "50", "49")]
    )
    await repository.replace_calendar_detail(
        CalendarDetailObservation(
            source_id="1",
            title_en="ISM PMI",
            currency="USD",
            currency_name="US dollar",
            impact="high",
            actual="51.2",
            forecast="50",
            previous="49",
            actual_state="better",
            previous_state=None,
            previous_revised_from=None,
            ff_url="https://www.forexfactory.com/calendar/1-us-ism-pmi",
            source_name="ISM",
            source_url="https://www.ismworld.org/",
            latest_release_url=None,
            measures="Level of a diffusion index;",
            usual_effect="'Actual' greater than 'Forecast' is good for currency;",
            frequency="Released monthly;",
            next_release_text="Oct 1, 2026",
            next_release_url="https://www.forexfactory.com/calendar?day=oct1.2026#detail=2",
            ff_notes=None,
            why_traders_care="It is a leading indicator;",
            history=(
                CalendarHistoryObservation(
                    "Sep 1, 2026",
                    "https://www.forexfactory.com/calendar?day=sep1.2026#detail=1",
                    "51.2",
                    "50",
                    "49",
                    actual_state="better",
                ),
            ),
            related_stories=(
                CalendarRelatedStoryObservation(
                    "Factories expanded",
                    "https://www.forexfactory.com/news/1",
                    "ismworld.org",
                    "https://www.forexfactory.com/news/1/hit",
                    "Sep 1, 2026",
                    "tables",
                ),
            ),
        )
    )

    async with client:
        response = await client.get("/api/v1/calendar/1", headers={"X-API-Key": "api-secret"})
    await database.close()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source_id"] == "1"
    assert payload["currency_name"] == "US dollar"
    assert payload["source_name"] == "ISM"
    assert payload["history"][0]["actual_state"] == "better"
    assert payload["related_stories"][0]["title_en"] == "Factories expanded"


async def test_calendar_detail_api_returns_not_found_while_background_prefetch_is_pending(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "api.sqlite3",
        app_api_key="api-secret",
        moonshot_api_key="kimi-secret",
        calendar_source_timezone="America/New_York",
    )
    database = Database(settings.database_path)
    await database.open()
    await database.initialize()
    repository = Repository(database)
    event_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
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
    app = create_app(settings, repository=repository)
    fake_browser = FakeCalendarBrowser(
        (Path(__file__).parent / "fixtures/calendar_detail.html").read_text()
    )
    app.state.calendar_browser = fake_browser
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async with client:
        response = await client.get("/api/v1/calendar/149673", headers={"X-API-Key": "api-secret"})
    await database.close()

    assert response.status_code == 404
    assert fake_browser.calls == []


async def test_calendar_detail_api_returns_existing_cache_without_source_request(
    tmp_path: Path,
) -> None:
    browser = FakeCalendarBrowser(
        (Path(__file__).parent / "fixtures/calendar_detail.html").read_text()
    )
    client, repository, database = await make_client(tmp_path, browser)
    event_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    await repository.upsert_calendar(
        [CalendarObservation("149673", event_at, "JPY", "low", "Event", None, None, None)]
    )
    await repository.replace_calendar_detail(minimal_calendar_detail("149673", "Stale source"))

    async with client:
        response = await client.get("/api/v1/calendar/149673", headers={"X-API-Key": "api-secret"})
    await database.close()

    assert response.status_code == 200, response.text
    assert browser.calls == []
    assert response.json()["source_name"] == "Stale source"


async def test_calendar_detail_api_returns_cached_detail_when_refresh_fails(
    tmp_path: Path,
) -> None:
    browser = FailingCalendarBrowser()
    client, repository, database = await make_client(tmp_path, browser)
    event_at = datetime(2026, 9, 1, 12, tzinfo=UTC)
    await repository.upsert_calendar(
        [CalendarObservation("1", event_at, "USD", "high", "Event", None, None, None)]
    )
    await repository.replace_calendar_detail(minimal_calendar_detail("1", "Cached source"))

    async with client:
        response = await client.get("/api/v1/calendar/1", headers={"X-API-Key": "api-secret"})
    await database.close()

    assert response.status_code == 200, response.text
    assert browser.calls == []
    assert response.json()["source_name"] == "Cached source"


async def test_calendar_detail_api_does_not_call_browser_when_cache_is_missing(
    tmp_path: Path,
) -> None:
    browser = FailingCalendarBrowser()
    client, repository, database = await make_client(tmp_path, browser)
    event_at = datetime(2026, 9, 1, 12, tzinfo=UTC)
    await repository.upsert_calendar(
        [CalendarObservation("1", event_at, "USD", "high", "Event", None, None, None)]
    )

    async with client:
        response = await client.get("/api/v1/calendar/1", headers={"X-API-Key": "api-secret"})
    await database.close()

    assert response.status_code == 404
    assert browser.calls == []
