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
        if not self.sections:
            return BackfillResult(0, 0, 0)
        cursor = await self.repository.get_runtime_state("news_backfill:last_section")
        offset = (self.sections.index(cursor) + 1) if cursor in self.sections else 0
        sections = self.sections[offset:] + self.sections[:offset]
        for section in sections:
            key = f"news_backfill:{section}"
            raw = await self.repository.get_runtime_state(key)
            checkpoint = json.loads(raw) if raw else {}
            last_run = checkpoint.get("last_run_at")
            stale = last_run is None or observed_at - datetime.fromisoformat(last_run) >= timedelta(
                days=1
            )
            cutoff = observed_at - timedelta(days=self.days)
            # Old checkpoints cannot prove coverage. Periodically restart even completed
            # sections, since the rolling target and relative More offsets move each day.
            if stale:
                checkpoint = {}
            elif checkpoint.get("complete"):
                continue
            retry_at = checkpoint.get("retry_at")
            if retry_at and observed_at < datetime.fromisoformat(retry_at):
                continue
            requested = int(checkpoint.get("continuation_count", 0)) + 1
            await self.repository.set_runtime_state("news_backfill:last_section", section)
            try:
                page = await self.browser.news_more_html(section, requested)
                if stop and stop.is_set():
                    return BackfillResult(0, 0, 0)
                batch = parse_news_listing_v2(page.html, observed_at, self.source_timezone)
                result = await self.repository.apply_listing(batch)
            except Exception:
                checkpoint.update(
                    complete=False,
                    reached_cutoff=False,
                    stop_reason="source_error",
                    last_run_at=observed_at.isoformat(),
                    target_cutoff=cutoff.isoformat(),
                    retry_at=(observed_at + timedelta(minutes=10)).isoformat(),
                )
                await self.repository.set_runtime_state(key, json.dumps(checkpoint))
                raise
            relevant_ids = self._relevant_ids(batch, section)
            timestamps = [
                article.published_at
                for article in batch.articles
                if article.source_id in relevant_ids and article.published_at is not None
            ]
            oldest = min(timestamps) if timestamps else None
            has_relevant_new_id = bool(set(result.new_article_ids) & relevant_ids)
            streak = 0 if has_relevant_new_id else int(checkpoint.get("no_new_id_streak", 0)) + 1
            seen_ids = set(checkpoint.get("source_ids", []))
            current_ids = set(page.source_ids) or relevant_ids
            no_progress = (
                int(checkpoint.get("no_progress_streak", 0)) + 1
                if seen_ids and not current_ids - seen_ids
                else 0
            )
            reached_cutoff = oldest is not None and oldest <= cutoff
            stop_reason = (
                "cutoff"
                if reached_cutoff
                else "source_exhausted"
                if page.terminal
                else "no_progress"
                if no_progress >= 2
                else "page_limit"
                if requested >= 100
                else None
            )
            complete = reached_cutoff or page.terminal
            updated = {
                "continuation_count": max(
                    int(checkpoint.get("continuation_count", 0)), page.continuation_count
                ),
                "oldest_published_at": oldest.isoformat() if oldest else None,
                "no_new_id_streak": streak,
                "no_progress_streak": no_progress,
                "source_ids": sorted(seen_ids | current_ids),
                "complete": complete,
                "reached_cutoff": reached_cutoff,
                "target_cutoff": cutoff.isoformat(),
                "stop_reason": stop_reason,
                "last_run_at": observed_at.isoformat(),
                "retry_at": (
                    (observed_at + timedelta(days=1)).isoformat()
                    if stop_reason and not complete
                    else None
                ),
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
