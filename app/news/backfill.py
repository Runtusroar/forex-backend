from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from app.collector.browser import NewsContinuationPage
from app.news.listing import parse_news_listing_v2
from app.news.models import ListingApplyResult, NewsListingBatch

DEFAULT_SECTIONS = (
    "latest",
    "fundamental",
    "technical",
    "industry",
    "entertainment",
    "educational",
)


class ContinuationSource(Protocol):
    async def news_more_html(
        self, section_slug: str, continuation_count: int
    ) -> NewsContinuationPage: ...


class BackfillRepository(Protocol):
    async def get_runtime_state(self, key: str) -> str | None: ...

    async def set_runtime_state(self, key: str, value: str) -> None: ...

    async def ready_detail_job_count(self) -> int: ...

    async def apply_listing(self, batch: NewsListingBatch) -> ListingApplyResult: ...


@dataclass(frozen=True, slots=True)
class BackfillResult:
    processed_pages: int
    completed_sections: int
    new_articles: int


class NewsBackfill:
    def __init__(
        self,
        browser: ContinuationSource,
        repository: BackfillRepository,
        source_timezone: ZoneInfo,
        days: int = 30,
        sections: tuple[str, ...] = DEFAULT_SECTIONS,
    ) -> None:
        self.browser = browser
        self.repository = repository
        self.source_timezone = source_timezone
        self.days = days
        self.sections = sections

    async def run_once(
        self,
        stop: asyncio.Event | None = None,
        now: datetime | None = None,
    ) -> BackfillResult:
        observed_at = now or datetime.now(UTC)
        if (stop and stop.is_set()) or await self.repository.ready_detail_job_count():
            return BackfillResult(0, 0, 0)
        for section in self.sections:
            key = f"news_backfill:{section}"
            raw = await self.repository.get_runtime_state(key)
            checkpoint = (
                json.loads(raw)
                if raw
                else {
                    "continuation_count": 0,
                    "oldest_published_at": None,
                    "no_new_id_streak": 0,
                    "complete": False,
                }
            )
            if checkpoint["complete"]:
                continue
            requested = int(checkpoint["continuation_count"]) + 1
            page = await self.browser.news_more_html(section, requested)
            if stop and stop.is_set():
                return BackfillResult(0, 0, 0)
            batch = parse_news_listing_v2(page.html, observed_at, self.source_timezone)
            result = await self.repository.apply_listing(batch)
            relevant_ids = self._relevant_ids(batch, section)
            timestamps = [
                article.published_at
                for article in batch.articles
                if article.source_id in relevant_ids and article.published_at is not None
            ]
            oldest = min(timestamps) if timestamps else None
            has_relevant_new_id = bool(set(result.new_article_ids) & relevant_ids)
            streak = (
                0
                if has_relevant_new_id
                else int(checkpoint["no_new_id_streak"]) + 1
            )
            beyond_cutoff = oldest is not None and oldest < observed_at - timedelta(days=self.days)
            complete = page.terminal or beyond_cutoff or streak >= 2
            updated = {
                "continuation_count": max(
                    int(checkpoint["continuation_count"]), page.continuation_count
                ),
                "oldest_published_at": oldest.isoformat() if oldest else None,
                "no_new_id_streak": streak,
                "complete": complete,
            }
            await self.repository.set_runtime_state(key, json.dumps(updated, separators=(",", ":")))
            return BackfillResult(1, int(complete), len(result.new_article_ids))
        return BackfillResult(0, 0, 0)

    @staticmethod
    def _relevant_ids(batch: NewsListingBatch, section: str) -> set[str]:
        if section == "latest":
            return {
                observation.article_id
                for observation in batch.feeds
                if observation.feed_type == "latest"
            }
        return {
            observation.article_id
            for observation in batch.categories
            if observation.category == section
        }

    async def run(self, stop: asyncio.Event, interval: int = 10) -> None:
        while not stop.is_set():
            with suppress(Exception):
                await self.run_once(stop)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue
