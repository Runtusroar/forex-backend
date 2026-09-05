import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request

from app.binance import BinanceFuturesContract, BinanceFuturesMarket, BinanceMarketError
from app.collector import BrowserSession, Collector
from app.collector.calendar_details import CalendarDetailCollector
from app.config import Settings, get_settings
from app.db import Database
from app.domain import CalendarDetailRecord, CalendarRecord, NewsRecord
from app.news.api import create_news_router
from app.news.backfill import NewsBackfill
from app.news.collector import NewsCollector
from app.news.comments import CommentCollector
from app.news.compat import v1_news_detail, v1_news_list
from app.news.media import MediaWorker
from app.news.repository import NewsRepository
from app.news.snapshots import SnapshotStore
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
        "source_time_text": item.source_time_text,
        "source_position": item.source_position,
        "updated_at": item.updated_at,
    }


def _calendar_detail_json(item: CalendarDetailRecord) -> dict:
    return {
        "source_id": item.source_id,
        "title_en": item.title_en,
        "currency": item.currency,
        "currency_name": item.currency_name,
        "impact": item.impact,
        "actual": item.actual,
        "forecast": item.forecast,
        "previous": item.previous,
        "actual_state": item.actual_state,
        "previous_state": item.previous_state,
        "previous_revised_from": item.previous_revised_from,
        "ff_url": item.ff_url,
        "source_name": item.source_name,
        "source_url": item.source_url,
        "latest_release_url": item.latest_release_url,
        "measures": item.measures,
        "usual_effect": item.usual_effect,
        "frequency": item.frequency,
        "next_release_text": item.next_release_text,
        "next_release_url": item.next_release_url,
        "ff_notes": item.ff_notes,
        "why_traders_care": item.why_traders_care,
        "history": [
            {
                "release_date_text": row.release_date_text,
                "event_url": row.event_url,
                "actual": row.actual,
                "forecast": row.forecast,
                "previous": row.previous,
                "actual_state": row.actual_state,
                "previous_state": row.previous_state,
                "previous_revised_from": row.previous_revised_from,
            }
            for row in item.history
        ],
        "related_stories": [
            {
                "title_en": story.title_en,
                "ff_url": story.ff_url,
                "source_name": story.source_name,
                "source_url": story.source_url,
                "published_at_source_text": story.published_at_source_text,
                "preview": story.preview,
            }
            for story in item.related_stories
        ],
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


def _binance_contract_json(item: BinanceFuturesContract) -> dict:
    return {
        "symbol": item.symbol,
        "pair": item.pair,
        "contract_type": item.contract_type,
        "market_type": item.market_type,
        "underlying_type": item.underlying_type,
        "underlying_subtypes": list(item.underlying_subtypes),
        "status": item.status,
        "base_asset": item.base_asset,
        "quote_asset": item.quote_asset,
        "margin_asset": item.margin_asset,
        "last_price": item.last_price,
        "weighted_avg_price": item.weighted_avg_price,
        "price_change": item.price_change,
        "price_change_percent": item.price_change_percent,
        "high_price": item.high_price,
        "low_price": item.low_price,
        "open_price": item.open_price,
        "volume": item.volume,
        "quote_volume": item.quote_volume,
        "count": item.count,
        "volatility_percent": item.volatility_percent,
        "updated_at": _iso_z(item.updated_at),
    }


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _calendar_default_range(
    now: datetime, source_timezone: tzinfo, horizon_days: int
) -> tuple[datetime, datetime]:
    local_day = now.astimezone(source_timezone).date()
    start = datetime.combine(local_day, time.min, tzinfo=source_timezone).astimezone(UTC)
    return start, start + timedelta(days=horizon_days)


def create_app(
    settings: Settings | None = None,
    repository: Repository | None = None,
    binance_market: BinanceFuturesMarket | None = None,
) -> FastAPI:
    configured = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database: Database | None = None
        browser: BrowserSession | None = None
        media_worker: MediaWorker | None = None
        runtime: BackgroundRuntime | None = None
        if repository is None:
            database = Database(configured.database_path)
            await database.open()
            await database.initialize()
            live_repository = Repository(database)
            app.state.repository = live_repository
            news_repository = NewsRepository(live_repository.db, live_repository.write_lock)
            app.state.news_repository = news_repository
            snapshot_store = SnapshotStore(configured.news_snapshot_dir, news_repository)
            browser = BrowserSession(configured.cdp_url)
            app.state.calendar_browser = browser
            calendar_collector = Collector(
                browser,
                live_repository,
                ZoneInfo(configured.calendar_source_timezone),
                configured.calendar_horizon_days,
                configured.calendar_schedule_interval_seconds,
                lookback_days=configured.calendar_lookback_days,
                snapshot_store=snapshot_store,
            )
            calendar_detail_collector = CalendarDetailCollector(
                browser,
                live_repository,
                ZoneInfo(configured.calendar_source_timezone),
                configured.calendar_detail_batch_size,
                configured.news_detail_max_attempts,
                timedelta(seconds=configured.calendar_detail_refresh_interval_seconds),
                snapshot_store=snapshot_store,
            )
            news_collector = NewsCollector(
                browser,
                news_repository,
                ZoneInfo(configured.news_source_timezone),
                configured.news_detail_max_attempts,
                snapshot_store,
            )
            comment_collector = CommentCollector(
                browser,
                news_repository,
                ZoneInfo(configured.news_source_timezone),
                configured.news_detail_max_attempts,
                timedelta(seconds=configured.news_comment_audit_interval_seconds),
                timedelta(days=configured.news_comment_audit_days),
                snapshot_store=snapshot_store,
            )
            media_worker = MediaWorker(
                news_repository,
                configured.news_media_dir,
                configured.news_media_max_bytes,
            )
            kimi = KimiTranslator(configured)
            translator = TranslationWorker(live_repository, kimi)
            news_translator = NewsTranslationWorker(news_repository, kimi, configured.kimi_model)
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
                    lambda stop: calendar_detail_collector.run(
                        stop, configured.calendar_detail_interval_seconds
                    ),
                    lambda stop: news_collector.run_listing(
                        stop, configured.collect_interval_seconds
                    ),
                    lambda stop: news_collector.run_details(
                        stop, configured.news_detail_interval_seconds
                    ),
                    lambda stop: comment_collector.run(
                        stop, configured.news_comment_interval_seconds
                    ),
                    media_worker.run,
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
        if browser:
            await browser.close()
        if database:
            await database.close()

    app = FastAPI(title="Forex Factory MVP", version="1.0.0", lifespan=lifespan)
    if repository is not None:
        app.state.repository = repository
        app.state.news_repository = NewsRepository(repository.db, repository.write_lock)
    app.state.binance_market = binance_market or BinanceFuturesMarket(
        base_url=configured.binance_base_url,
        timeout_seconds=configured.binance_timeout_seconds,
        cache_ttl_seconds=configured.binance_cache_ttl_seconds,
    )

    async def repo(request: Request) -> AsyncIterator[Repository]:
        live = request.app.state.repository
        async with live.database.read_connection() as reader:
            yield Repository(live.database, reader=reader)

    def market(request: Request) -> BinanceFuturesMarket:
        return request.app.state.binance_market

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
        default_start, default_end = _calendar_default_range(
            now,
            ZoneInfo(configured.calendar_source_timezone),
            configured.calendar_horizon_days,
        )
        start = start or default_start
        end = end or (default_end if start == default_start else start + timedelta(days=7))
        if end <= start or end - start > timedelta(days=31):
            raise HTTPException(status_code=422, detail="Invalid date range")
        items = await repository.list_calendar(start, end)
        generated_at = await repository.get_runtime_state("calendar_last_success") or now
        return {"items": [_calendar_json(item) for item in items], "generated_at": generated_at}

    @app.get("/api/v1/calendar/{source_id}", dependencies=[Depends(authorize)])
    async def calendar_detail(
        source_id: str,
        repository: Annotated[Repository, Depends(repo)],
    ) -> dict:
        detail = await repository.get_calendar_detail(source_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Not found")
        return _calendar_detail_json(detail)

    @app.get("/api/v1/news", dependencies=[Depends(authorize)])
    async def news(
        repository: Annotated[Repository, Depends(repo)],
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        before: datetime | None = None,
    ) -> dict:
        news_repository = NewsRepository(
            repository.db, repository.write_lock, reader=repository.reader
        )
        v2_items = await v1_news_list(news_repository, limit, before)
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
        news_repository = NewsRepository(
            repository.db, repository.write_lock, reader=repository.reader
        )
        v2_item = await v1_news_detail(news_repository, source_id)
        if v2_item is not None:
            return v2_item
        item = await repository.get_news(source_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Not found")
        return _news_json(item)

    @app.get("/api/v1/binance/futures/top-contracts", dependencies=[Depends(authorize)])
    async def binance_top_contracts(
        binance: Annotated[BinanceFuturesMarket, Depends(market)],
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        market_type: Annotated[str, Query(pattern="^(all|crypto|traditional)$")] = "all",
    ) -> dict:
        try:
            items = await binance.top_contracts(
                limit, None if market_type == "all" else market_type
            )
        except BinanceMarketError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "items": [_binance_contract_json(item) for item in items],
            "generated_at": _iso_z(datetime.now(UTC)),
        }

    @app.get("/api/v1/status", dependencies=[Depends(authorize)])
    async def status(repository: Annotated[Repository, Depends(repo)]) -> dict:
        last_success = await repository.get_runtime_state("calendar_last_success")
        last_count = await repository.get_runtime_state("calendar_last_count")
        last_error = await repository.get_runtime_state("calendar_last_error")
        detail_last_success = await repository.get_runtime_state(
            "calendar_detail_last_success"
        )
        detail_last_error = await repository.get_runtime_state("calendar_detail_last_error")
        detail_jobs = await repository.calendar_detail_job_counts()
        issues = []
        if last_error:
            issues.append("calendar_error")
        if detail_last_error or detail_jobs.get("failed", 0):
            issues.append("calendar_detail_error")
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(last_success)).total_seconds()
        except (TypeError, ValueError):
            age = None
        if age is None or age > max(300, configured.collect_interval_seconds * 5):
            issues.append("calendar_stale")
        return {
            "status": "degraded" if issues else "ok",
            "issues": issues,
            "collection_age_seconds": age,
            "model": configured.kimi_model,
            "calendar": {
                "last_success": last_success,
                "last_count": int(last_count) if last_count is not None else None,
                "last_error": last_error or None,
                "detail_last_success": detail_last_success,
                "detail_last_error": detail_last_error or None,
                "detail_jobs": detail_jobs,
            },
        }

    app.include_router(create_news_router(configured, authorize))

    return app
