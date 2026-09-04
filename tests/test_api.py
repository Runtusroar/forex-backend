from datetime import UTC, datetime
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


async def make_client(tmp_path: Path) -> tuple[httpx.AsyncClient, Repository, Database]:
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
    transport = httpx.ASGITransport(app=create_app(settings, repository=repository))
    return httpx.AsyncClient(transport=transport, base_url="http://test"), repository, database


class FakeCalendarBrowser:
    def __init__(self, html: str) -> None:
        self.html = html
        self.calls: list[tuple[object, str]] = []

    async def calendar_detail_html(self, day: object, source_id: str) -> str:
        self.calls.append((day, source_id))
        return self.html


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

    async with client:
        calendar = await client.get(
            "/api/v1/calendar?from=2026-09-04T00:00:00Z&to=2026-09-05T00:00:00Z",
            headers={"X-API-Key": "api-secret"},
        )
        status = await client.get(
            "/api/v1/status", headers={"X-API-Key": "api-secret"}
        )
    await database.close()

    assert calendar.json()["generated_at"] == "2026-09-04T13:15:00Z"
    assert status.json()["calendar"] == {
        "last_success": "2026-09-04T13:15:00Z",
        "last_count": 14,
        "last_error": None,
    }


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
        response = await client.get(
            "/api/v1/calendar/1", headers={"X-API-Key": "api-secret"}
        )
    await database.close()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source_id"] == "1"
    assert payload["currency_name"] == "US dollar"
    assert payload["source_name"] == "ISM"
    assert payload["history"][0]["actual_state"] == "better"
    assert payload["related_stories"][0]["title_en"] == "Factories expanded"


async def test_calendar_detail_api_collects_and_caches_missing_detail(
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
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )

    async with client:
        response = await client.get(
            "/api/v1/calendar/149673", headers={"X-API-Key": "api-secret"}
        )
    cached = await repository.get_calendar_detail("149673")
    await database.close()

    assert response.status_code == 200, response.text
    assert fake_browser.calls == [(event_at.date(), "149673")]
    assert response.json()["source_name"] == "METI"
    assert cached is not None
    assert cached.ff_url == "https://www.forexfactory.com/calendar/225-jn-prelim-industrial-production-mm"
