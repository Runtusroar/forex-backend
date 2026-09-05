import os
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config import Settings
from app.db import Database
from app.domain import CalendarObservation
from app.main import _calendar_default_range, create_app
from app.news.repository import _iso as news_iso
from app.parsers.calendar import parse_calendar
from app.repository import Repository
from app.repository import _iso as calendar_iso


@pytest.mark.parametrize(("month", "day"), [(3, 8), (11, 1)])
def test_default_calendar_window_ends_at_local_midnight_across_dst(month, day):
    zone = ZoneInfo("America/New_York")
    start, end = _calendar_default_range(datetime(2026, month, day, 12, tzinfo=UTC), zone, 1)
    assert start == datetime(2026, month, day, tzinfo=zone)
    assert end == datetime(2026, month, day + 1, tzinfo=zone)


@pytest.mark.parametrize("serializer", [calendar_iso, news_iso])
def test_storage_rejects_a_datetime_without_timezone(serializer):
    with pytest.raises(ValueError, match="timezone"):
        serializer(datetime(2026, 9, 5, 20, 30))


def test_standalone_calendar_parser_does_not_depend_on_host_timezone():
    html = (Path(__file__).parent / "fixtures/calendar_current.html").read_text()
    original = os.environ.get("TZ")
    parsed = []
    try:
        for zone in ("UTC", "America/Los_Angeles", "Asia/Shanghai"):
            os.environ["TZ"] = zone
            time.tzset()
            parsed.append(parse_calendar(html, datetime(2026, 9, 1, tzinfo=UTC))[0].event_at)
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()
    assert len(set(parsed)) == 1


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-09-05T00:00:00", "2026-09-06T00:00:00"),
        ("2026-09-05T00:00:00", "2026-09-06T00:00:00Z"),
    ],
)
async def test_calendar_api_rejects_missing_timezone(tmp_path, start, end):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "time.sqlite3",
        app_api_key="test",
        moonshot_api_key="test",
    )
    db = Database(settings.database_path)
    await db.open()
    await db.initialize()
    try:
        app = create_app(settings, repository=Repository(db))
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/calendar", params={"from": start, "to": end}, headers={"X-API-Key": "test"}
            )
        assert response.status_code == 422
    finally:
        await db.close()


async def test_calendar_offset_query_and_storage_represent_the_same_instant(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "roundtrip.sqlite3",
        app_api_key="test",
        moonshot_api_key="test",
    )
    db = Database(settings.database_path)
    await db.open()
    await db.initialize()
    try:
        repo = Repository(db)
        event_time = datetime(2026, 9, 5, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        await repo.upsert_calendar(
            [CalendarObservation("time1", event_time, "USD", "high", "Time test", None, None, None)]
        )
        transport = httpx.ASGITransport(app=create_app(settings, repository=repo))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            payloads = []
            for start, end in (
                ("2026-09-05T00:00:00+08:00", "2026-09-06T00:00:00+08:00"),
                ("2026-09-04T16:00:00Z", "2026-09-05T16:00:00Z"),
            ):
                response = await client.get(
                    "/api/v1/calendar",
                    params={"from": start, "to": end},
                    headers={"X-API-Key": "test"},
                )
                assert response.status_code == 200
                payloads.append(response.json()["items"])
        assert payloads[0] == payloads[1]
        assert payloads[0][0]["event_at"] == "2026-09-04T16:30:00Z"
    finally:
        await db.close()


async def test_news_api_rejects_missing_timezone(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "before.sqlite3",
        app_api_key="test",
        moonshot_api_key="test",
    )
    db = Database(settings.database_path)
    await db.open()
    await db.initialize()
    try:
        transport = httpx.ASGITransport(
            app=create_app(settings, repository=Repository(db)), raise_app_exceptions=False
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/news",
                params={"before": "2026-09-05T00:00:00"},
                headers={"X-API-Key": "test"},
            )
        assert response.status_code == 422
    finally:
        await db.close()


async def test_calendar_accepts_31_local_days_across_fall_dst(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "dst.sqlite3",
        app_api_key="test",
        moonshot_api_key="test",
        calendar_source_timezone="America/New_York",
    )
    db = Database(settings.database_path)
    await db.open()
    await db.initialize()
    try:
        transport = httpx.ASGITransport(app=create_app(settings, repository=Repository(db)))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/calendar",
                params={"from": "2026-10-15T04:00:00Z", "to": "2026-11-15T05:00:00Z"},
                headers={"X-API-Key": "test"},
            )
        assert response.status_code == 200
    finally:
        await db.close()
