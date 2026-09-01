import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from app.parsers import parse_calendar, parse_news_detail, parse_news_listing
from app.repository import Repository


class BrowserSource(Protocol):
    async def calendar_html(self) -> str: ...
    async def news_html(self) -> str: ...
    async def news_detail_html(self, url: str) -> str: ...


@dataclass(frozen=True, slots=True)
class CollectionResult:
    calendar_count: int
    news_count: int


class Collector:
    def __init__(self, browser: BrowserSource, repository: Repository) -> None:
        self.browser = browser
        self.repository = repository
        self.lock = asyncio.Lock()

    async def run_cycle(self, now: datetime | None = None) -> CollectionResult:
        async with self.lock:
            observed_at = now or datetime.now(UTC)
            calendar = parse_calendar(await self.browser.calendar_html(), observed_at)
            await self.repository.upsert_calendar(calendar)
            news = parse_news_listing(await self.browser.news_html(), observed_at)
            enriched = []
            for item in news:
                if item.body_en is None:
                    detail = parse_news_detail(await self.browser.news_detail_html(item.url))
                    item = replace(
                        item,
                        body_en=detail.body_en,
                        image_url=detail.image_url or item.image_url,
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
