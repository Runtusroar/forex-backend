from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.config import Settings
from app.db import Database
from app.domain import CalendarObservation, NewsObservation
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
