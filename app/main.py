import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request

from app.collector import BrowserSession, Collector
from app.config import Settings, get_settings
from app.db import Database
from app.domain import CalendarRecord, NewsRecord
from app.news.api import create_news_router
from app.news.backfill import NewsBackfill
from app.news.collector import NewsCollector
from app.news.compat import v1_news_detail, v1_news_list
from app.news.media import MediaWorker
from app.news.repository import NewsRepository
from app.news.snapshots import SnapshotStore
from app.news.source_document import SourceDocumentFetcher, SourceDocumentWorker
from app.repository import Repository
from app.runtime import BackgroundRuntime
from app.translation import KimiTranslator
from app.translation.worker import NewsTranslationWorker, TranslationWorker


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
        media_worker: MediaWorker | None = None
        source_client: httpx.AsyncClient | None = None
        runtime: BackgroundRuntime | None = None
        if repository is None:
            database = Database(configured.database_path)
            await database.open()
            await database.initialize()
            live_repository = Repository(database)
            app.state.repository = live_repository
            news_repository = NewsRepository(
                live_repository.db, live_repository.write_lock
            )
            app.state.news_repository = news_repository
            browser = BrowserSession(configured.cdp_url)
            calendar_collector = Collector(browser, live_repository)
            snapshot_store = SnapshotStore(configured.news_snapshot_dir, news_repository)
            news_collector = NewsCollector(
                browser,
                news_repository,
                ZoneInfo(configured.news_source_timezone),
                configured.news_detail_max_attempts,
                snapshot_store,
            )
            media_worker = MediaWorker(
                news_repository,
                configured.news_media_dir,
                configured.news_media_max_bytes,
            )
            source_client = httpx.AsyncClient(
                timeout=configured.news_source_timeout_seconds,
                follow_redirects=False,
            )
            source_worker = SourceDocumentWorker(
                news_repository,
                SourceDocumentFetcher(
                    source_client,
                    max_bytes=configured.news_source_max_bytes,
                    max_redirects=configured.news_source_max_redirects,
                ),
                max_attempts=configured.news_source_max_attempts,
                snapshot_store=snapshot_store,
            )
            kimi = KimiTranslator(configured)
            translator = TranslationWorker(live_repository, kimi)
            news_translator = NewsTranslationWorker(
                news_repository, kimi, configured.kimi_model
            )
            backfill = NewsBackfill(
                browser,
                news_repository,
                ZoneInfo(configured.news_source_timezone),
                configured.news_backfill_days,
            )
            runtime = BackgroundRuntime(
                [
                    lambda stop: calendar_collector.run_calendar(
                        stop, configured.collect_interval_seconds
                    ),
                    lambda stop: news_collector.run_listing(
                        stop, configured.collect_interval_seconds
                    ),
                    lambda stop: news_collector.run_details(
                        stop, configured.news_detail_interval_seconds
                    ),
                    media_worker.run,
                    lambda stop: source_worker.run(
                        stop, configured.news_source_interval_seconds
                    ),
                    translator.run,
                    news_translator.run,
                    lambda stop: snapshot_store.run_cleanup(
                        stop, configured.news_snapshot_retention_days
                    ),
                    backfill.run,
                ]
            )
            await runtime.start()
        yield
        if runtime:
            await runtime.stop()
        if media_worker:
            await media_worker.close()
        if source_client:
            await source_client.aclose()
        if browser:
            await browser.close()
        if database:
            await database.close()

    app = FastAPI(title="Forex Factory MVP", version="1.0.0", lifespan=lifespan)
    if repository is not None:
        app.state.repository = repository
        app.state.news_repository = NewsRepository(repository.db, repository.write_lock)

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
