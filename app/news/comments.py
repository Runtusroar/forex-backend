from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from app.collector.browser import NewsCommentCapture
from app.news.detail import parse_news_detail_v2
from app.news.models import CommentCollectionObservation
from app.news.repository import NewsRepository


class CommentBrowserSource(Protocol):
    async def news_comments_html(
        self, url: str, expected_comment_count: int | None = None
    ) -> NewsCommentCapture: ...


class CommentCountMismatch(ValueError):
    pass


class CommentCollector:
    def __init__(
        self,
        browser: CommentBrowserSource,
        repository: NewsRepository,
        source_timezone: ZoneInfo,
        max_attempts: int = 8,
        audit_interval: timedelta = timedelta(hours=6),
        audit_window: timedelta = timedelta(days=30),
    ) -> None:
        self.browser = browser
        self.repository = repository
        self.source_timezone = source_timezone
        self.max_attempts = max_attempts
        self.audit_interval = audit_interval
        self.audit_window = audit_window

    async def run_cycle(self, now: datetime | None = None) -> int:
        observed_at = now or datetime.now(UTC)
        jobs = await self.repository.claim_comment_jobs(1, observed_at)
        if not jobs:
            await self.repository.enqueue_due_comment_audits(
                observed_at,
                audit_interval=self.audit_interval,
                recent_window=self.audit_window,
                limit=1,
            )
            jobs = await self.repository.claim_comment_jobs(1, observed_at)
            if not jobs:
                return 0
        job = jobs[0]
        try:
            capture = await self.browser.news_comments_html(job.ff_url, job.expected_count)
            detail = parse_news_detail_v2(
                capture.html, job.article_id, observed_at, self.source_timezone
            )
            comments = tuple(dict.fromkeys(comment.comment_id for comment in detail.comments))
            previous_count = await self.repository.comment_count(job.article_id)
            unverified_zero = (
                capture.declared_count == 0
                and not comments
                and previous_count > 0
                and not job.expected_count_observed
            )
            is_complete = (
                len(comments) == capture.declared_count
                and capture.collected_count == capture.declared_count
                and not unverified_zero
            )
            stored = await self.repository.replace_comments(
                CommentCollectionObservation(
                    article_id=job.article_id,
                    observed_at=observed_at,
                    expected_count=capture.declared_count,
                    comments=detail.comments,
                    is_complete=is_complete,
                ),
                claimed_expected_count=job.expected_count,
            )
            if not stored:
                return 0
            if not is_complete:
                raise CommentCountMismatch(
                    f"declared={capture.declared_count} collected={len(comments)}"
                )
        except Exception as error:
            await self.repository.fail_comment_job(
                job.article_id,
                error,
                observed_at,
                self.max_attempts,
                claimed_expected_count=job.expected_count,
            )
            await self.repository.set_runtime_state(
                "news_last_comment_error", type(error).__name__
            )
            return 0
        await self.repository.complete_comment_job(
            job.article_id,
            observed_at,
            claimed_expected_count=job.expected_count,
            collected_count=capture.declared_count,
        )
        await self.repository.set_runtime_state(
            "news_last_comment_success", observed_at.isoformat()
        )
        await self.repository.set_runtime_state("news_last_comment_error", "")
        return 1

    async def run(self, stop: asyncio.Event, interval: int) -> None:
        while not stop.is_set():
            with suppress(Exception):
                await self.run_cycle()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue
