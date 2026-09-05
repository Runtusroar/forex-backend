from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from app.news.detail import parse_news_detail_v2
from app.news.listing import parse_news_listing_v2
from app.news.models import ListingApplyResult
from app.news.repository import NewsRepository
from app.news.snapshots import SnapshotStore

logger = logging.getLogger(__name__)


class NewsBrowserSource(Protocol):
    async def news_html(self) -> str: ...

    async def news_detail_html(self, url: str) -> str: ...


class IncompleteDetailError(ValueError):
    pass


class NewsCollector:
    def __init__(
        self,
        browser: NewsBrowserSource,
        repository: NewsRepository,
        source_timezone: ZoneInfo,
        detail_max_attempts: int = 8,
        snapshot_store: SnapshotStore | None = None,
    ) -> None:
        self.browser = browser
        self.repository = repository
        self.source_timezone = source_timezone
        self.detail_max_attempts = detail_max_attempts
        self.snapshot_store = snapshot_store
        self.listing_lock = asyncio.Lock()

    async def run_listing_cycle(self, now: datetime | None = None) -> ListingApplyResult:
        observed_at = now or datetime.now(UTC)
        async with self.listing_lock:
            html: str | None = None
            try:
                html = await self.browser.news_html()
                batch = parse_news_listing_v2(html, observed_at, self.source_timezone)
                result = await self.repository.apply_listing(batch)
            except Exception as error:
                html = html or getattr(error, "source_html", None)
                if html is not None and self.snapshot_store:
                    with suppress(Exception):
                        await self.snapshot_store.capture(
                            "listing", "news", html, observed_at, error
                        )
                failures = await self._record_listing_failure(error, observed_at)
                logger.warning(
                    "News listing collection failed error_type=%s "
                    "consecutive_failures=%d message=%s",
                    type(error).__name__,
                    failures,
                    str(error),
                    exc_info=failures == 1 or failures % 10 == 0,
                )
                raise
            if self.snapshot_store:
                with suppress(Exception):
                    await self.snapshot_store.capture(
                        "listing", "news", html, observed_at
                    )
            previous_failures = await self._listing_failure_count()
            with suppress(Exception):
                await self.repository.set_runtime_state(
                    "news_last_listing_success", observed_at.isoformat()
                )
                await self.repository.set_runtime_state("news_last_listing_error", "")
                await self.repository.set_runtime_state("news_last_listing_error_at", "")
                await self.repository.set_runtime_state(
                    "news_listing_consecutive_failures", "0"
                )
            if previous_failures:
                logger.info(
                    "News listing collection recovered previous_consecutive_failures=%d",
                    previous_failures,
                )
            return result

    async def _listing_failure_count(self) -> int:
        with suppress(Exception):
            raw = await self.repository.get_runtime_state(
                "news_listing_consecutive_failures"
            )
            return max(0, int(raw or 0))
        return 0

    async def _record_listing_failure(self, error: Exception, observed_at: datetime) -> int:
        failures = await self._listing_failure_count() + 1
        with suppress(Exception):
            await self.repository.set_runtime_state(
                "news_last_listing_error", type(error).__name__
            )
            await self.repository.set_runtime_state(
                "news_last_listing_error_at", observed_at.isoformat()
            )
            await self.repository.set_runtime_state(
                "news_listing_consecutive_failures", str(failures)
            )
        return failures

    async def run_detail_cycle(self, now: datetime | None = None) -> int:
        observed_at = now or datetime.now(UTC)
        jobs = await self.repository.claim_detail_jobs(1, observed_at)
        if not jobs:
            await self.repository.enqueue_due_detail_audits(observed_at, limit=1)
            jobs = await self.repository.claim_detail_jobs(1, observed_at)
            if not jobs:
                return 0
        job = jobs[0]
        html: str | None = None
        try:
            html = await self.browser.news_detail_html(job.ff_url)
            detail = parse_news_detail_v2(
                html, job.article_id, observed_at, self.source_timezone
            )
            stored = await self.repository.replace_detail(
                job.article_id,
                detail,
                desired_source_hash=job.desired_source_hash,
            )
            if not stored:
                return 0
            if not detail.is_complete:
                raise IncompleteDetailError("news detail contains unrecognized article nodes")
            await self.repository.complete_detail_job(
                job.article_id, job.desired_source_hash
            )
        except Exception as error:
            if html is not None and self.snapshot_store:
                with suppress(Exception):
                    await self.snapshot_store.capture(
                        "detail", job.article_id, html, observed_at, error
                    )
            await self.repository.fail_detail_job(
                job.article_id,
                error,
                observed_at,
                self.detail_max_attempts,
                job.desired_source_hash,
            )
            with suppress(Exception):
                await self.repository.set_runtime_state(
                    "news_last_detail_error", type(error).__name__
                )
            return 0
        if self.snapshot_store:
            with suppress(Exception):
                await self.snapshot_store.capture(
                    "detail", job.article_id, html, observed_at
                )
        with suppress(Exception):
            await self.repository.set_runtime_state(
                "news_last_detail_success", observed_at.isoformat()
            )
            await self.repository.set_runtime_state("news_last_detail_error", "")
        return 1

    async def run_listing(self, stop: asyncio.Event, interval: int) -> None:
        while not stop.is_set():
            try:
                await self.run_listing_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                # run_listing_cycle records structured state and a throttled log entry.
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def run_details(self, stop: asyncio.Event, interval: int) -> None:
        while not stop.is_set():
            with suppress(Exception):
                await self.run_detail_cycle()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue
