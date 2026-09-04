import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Protocol

from app.domain import CalendarObservation
from app.parsers import parse_calendar, parse_news_detail, parse_news_listing
from app.repository import Repository

logger = logging.getLogger(__name__)


class BrowserSource(Protocol):
    async def calendar_html(self, day: date) -> str: ...
    async def calendar_detail_html(self, day: date, source_id: str) -> str: ...
    async def news_html(self) -> str: ...
    async def news_detail_html(self, url: str) -> str: ...


@dataclass(frozen=True, slots=True)
class CollectionResult:
    calendar_count: int
    news_count: int


class Collector:
    def __init__(
        self,
        browser: BrowserSource,
        repository: Repository,
        source_timezone: tzinfo = UTC,
        horizon_days: int = 8,
        schedule_interval: int = 600,
    ) -> None:
        if horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        if schedule_interval <= 0:
            raise ValueError("schedule_interval must be positive")
        self.browser = browser
        self.repository = repository
        self.source_timezone = source_timezone
        self.horizon_days = horizon_days
        self.schedule_interval = schedule_interval
        self.lock = asyncio.Lock()
        self._next_schedule_refresh_at: datetime | None = None

    async def _collect_calendar(
        self, observed_at: datetime
    ) -> list[CalendarObservation]:
        local_day = observed_at.astimezone(self.source_timezone).date()
        full_refresh = (
            self._next_schedule_refresh_at is None
            or observed_at >= self._next_schedule_refresh_at
        )
        requested_days = (
            [local_day + timedelta(days=offset) for offset in range(self.horizon_days)]
            if full_refresh
            else [local_day]
        )
        observations: list[CalendarObservation] = []
        try:
            for day in requested_days:
                html = await self.browser.calendar_html(day)
                observations.extend(
                    parse_calendar(
                        html,
                        observed_at,
                        source_timezone=self.source_timezone,
                        expected_date=day,
                    )
                )
            unique = list({item.source_id: item for item in observations}.values())
            window_start = datetime.combine(
                local_day, time.min, tzinfo=self.source_timezone
            ).astimezone(UTC)
            window_days = self.horizon_days if full_refresh else 1
            window_end = window_start + timedelta(days=window_days)
            await self.repository.replace_calendar_window(
                unique, window_start, window_end
            )
        except Exception as error:
            await self.repository.set_runtime_state(
                "calendar_last_error", f"{type(error).__name__}: {error}"
            )
            raise

        success_at = observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        await self.repository.set_runtime_state("calendar_last_success", success_at)
        await self.repository.set_runtime_state("calendar_last_count", str(len(unique)))
        await self.repository.set_runtime_state("calendar_last_error", "")
        if full_refresh:
            self._next_schedule_refresh_at = observed_at + timedelta(
                seconds=self.schedule_interval
            )
        return unique

    async def run_calendar_cycle(self, now: datetime | None = None) -> int:
        async with self.lock:
            observed_at = now or datetime.now(UTC)
            calendar = await self._collect_calendar(observed_at)
            return len(calendar)

    async def run_cycle(self, now: datetime | None = None) -> CollectionResult:
        async with self.lock:
            observed_at = now or datetime.now(UTC)
            calendar = await self._collect_calendar(observed_at)
            news = parse_news_listing(await self.browser.news_html(), observed_at)
            enriched = []
            for item in news:
                if item.body_en is None:
                    stored = await self.repository.get_news(item.source_id)
                    if (
                        stored
                        and stored.body_en
                        and stored.title_en == item.title_en
                        and stored.summary_en == item.summary_en
                    ):
                        item = replace(
                            item,
                            body_en=stored.body_en,
                            image_url=item.image_url or stored.image_url,
                        )
                    else:
                        try:
                            detail = parse_news_detail(
                                await self.browser.news_detail_html(item.url)
                            )
                            item = replace(
                                item,
                                body_en=detail.body_en,
                                image_url=detail.image_url or item.image_url,
                            )
                        except Exception as error:
                            logger.warning(
                                "news detail unavailable for %s: %s",
                                item.source_id,
                                type(error).__name__,
                            )
                enriched.append(item)
            await self.repository.upsert_news(enriched)
            return CollectionResult(len(calendar), len(enriched))

    async def run(self, stop: asyncio.Event, interval: int) -> None:
        while not stop.is_set():
            with suppress(Exception):
                await self.run_cycle()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def run_calendar(self, stop: asyncio.Event, interval: int) -> None:
        while not stop.is_set():
            try:
                await self.run_calendar_cycle()
            except Exception:
                logger.exception("calendar collection failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue
