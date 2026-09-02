import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request

from app.collector import BrowserSession, Collector
from app.config import Settings, get_settings
from app.db import Database
from app.domain import CalendarRecord, NewsRecord
from app.news.api import create_news_router
from app.news.compat import v1_news_detail, v1_news_list
from app.news.repository import NewsRepository
from app.repository import Repository
from app.runtime import BackgroundRuntime
from app.translation import KimiTranslator
from app.translation.worker import TranslationWorker


def _calendar_json(item: CalendarRecord) -> dict:
    return {
        "source_id": item.source_id,
        "event_at": item.event_at,
        "currency": item.currency,
        "impact": item.impact,
        "title_en": item.title_en,
        "title_zh": item.title_zh,
        "actual": item.actual,
        "forecast": item.forecast,
        "previous": item.previous,
        "updated_at": item.updated_at,
    }


def _news_json(item: NewsRecord) -> dict:
    return {
        "source_id": item.source_id,
        "url": item.url,
        "source": item.source,
        "published_at": item.published_at,
        "first_seen_at": item.first_seen_at,
        "title_en": item.title_en,
        "title_zh": item.title_zh,
        "summary_en": item.summary_en,
        "summary_zh": item.summary_zh,
        "body_en": item.body_en,
        "body_zh": item.body_zh,
        "image_url": item.image_url,
        "updated_at": item.updated_at,
    }


def create_app(settings: Settings | None = None, repository: Repository | None = None) -> FastAPI:
    configured = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database: Database | None = None
        browser: BrowserSession | None = None
        runtime: BackgroundRuntime | None = None
        if repository is None:
            database = Database(configured.database_path)
            await database.open()
            await database.initialize()
            live_repository = Repository(database)
            app.state.repository = live_repository
            app.state.news_repository = NewsRepository(live_repository.db)
            browser = BrowserSession(configured.cdp_url)
            collector = Collector(browser, live_repository)
            translator = TranslationWorker(live_repository, KimiTranslator(configured))
            runtime = BackgroundRuntime(
                [
                    lambda stop: collector.run(stop, configured.collect_interval_seconds),
                    translator.run,
                ]
            )
            await runtime.start()
        yield
        if runtime:
            await runtime.stop()
        if browser:
            await browser.close()
        if database:
            await database.close()

    app = FastAPI(title="Forex Factory MVP", version="1.0.0", lifespan=lifespan)
    if repository is not None:
        app.state.repository = repository
        app.state.news_repository = NewsRepository(repository.db)

    def repo(request: Request) -> Repository:
        return request.app.state.repository

    def authorize(x_api_key: Annotated[str | None, Header()] = None) -> None:
        expected = configured.app_api_key.get_secret_value()
        if x_api_key is None or not hmac.compare_digest(x_api_key, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/calendar", dependencies=[Depends(authorize)])
    async def calendar(
        repository: Annotated[Repository, Depends(repo)],
        start: Annotated[datetime | None, Query(alias="from")] = None,
        end: Annotated[datetime | None, Query(alias="to")] = None,
    ) -> dict:
        now = datetime.now(UTC)
        start = start or now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = end or start + timedelta(days=7)
        if end <= start or end - start > timedelta(days=31):
            raise HTTPException(status_code=422, detail="Invalid date range")
        items = await repository.list_calendar(start, end)
        return {"items": [_calendar_json(item) for item in items], "generated_at": now}

    @app.get("/api/v1/news", dependencies=[Depends(authorize)])
    async def news(
        repository: Annotated[Repository, Depends(repo)],
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        before: datetime | None = None,
    ) -> dict:
        v2_items = await v1_news_list(request.app.state.news_repository, limit, before)
        if v2_items:
            return {"items": v2_items, "generated_at": datetime.now(UTC)}
        items = await repository.list_news(limit, before)
        return {"items": [_news_json(item) for item in items], "generated_at": datetime.now(UTC)}

    @app.get("/api/v1/news/{source_id}", dependencies=[Depends(authorize)])
    async def news_detail(
        source_id: str,
        repository: Annotated[Repository, Depends(repo)],
        request: Request,
    ) -> dict:
        v2_item = await v1_news_detail(request.app.state.news_repository, source_id)
        if v2_item is not None:
            return v2_item
        item = await repository.get_news(source_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Not found")
        return _news_json(item)

    @app.get("/api/v1/status", dependencies=[Depends(authorize)])
    async def status() -> dict[str, str]:
        return {"status": "ok", "model": configured.kimi_model}

    app.include_router(create_news_router(configured, authorize))

    return app
