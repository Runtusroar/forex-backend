from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Protocol

from app.collector.snapshots import SourceSnapshots, capture_snapshot
from app.parsers import parse_calendar_detail
from app.repository import Repository


class CalendarDetailBrowserSource(Protocol):
    async def calendar_details_html(
        self, day: date, source_ids: Sequence[str]
    ) -> dict[str, str | None]: ...


class CalendarDetailCollector:
    def __init__(
        self,
        browser: CalendarDetailBrowserSource,
        repository: Repository,
        source_timezone: tzinfo = UTC,
        batch_size: int = 16,
        max_attempts: int = 8,
        refresh_interval: timedelta = timedelta(days=1),
        snapshot_store: SourceSnapshots | None = None,
    ) -> None:
        self.browser = browser
        self.repository = repository
        self.source_timezone = source_timezone
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.refresh_interval = refresh_interval
        self.snapshot_store = snapshot_store

    async def run_cycle(self, now: datetime | None = None) -> int:
        observed_at = now or datetime.now(UTC)
        jobs = await self.repository.claim_calendar_detail_jobs(self.batch_size, observed_at)
        if not jobs:
            await self.repository.enqueue_due_calendar_detail_refreshes(
                observed_at, self.refresh_interval, self.batch_size
            )
            jobs = await self.repository.claim_calendar_detail_jobs(self.batch_size, observed_at)
            if not jobs:
                return 0

        grouped = defaultdict(list)
        for job in jobs:
            day = job.source_date or job.event_at.astimezone(self.source_timezone).date()
            grouped[day].append(job)

        completed = 0
        had_failure = False
        for day, day_jobs in grouped.items():
            try:
                pages = await self.browser.calendar_details_html(
                    day, [job.source_id for job in day_jobs]
                )
            except Exception as error:
                await capture_snapshot(
                    self.snapshot_store,
                    "calendar-detail",
                    day.isoformat(),
                    getattr(error, "source_html", None),
                    observed_at,
                    error,
                )
                had_failure = True
                for job in day_jobs:
                    await self.repository.fail_calendar_detail_job(
                        job.source_id,
                        error,
                        observed_at,
                        self.max_attempts,
                        desired_source_hash=job.desired_source_hash,
                    )
                await self.repository.set_runtime_state(
                    "calendar_detail_last_error", type(error).__name__
                )
                continue
            batch_error = None
            for job in day_jobs:
                try:
                    html = pages[job.source_id]
                    if html is None:
                        await self.repository.complete_calendar_detail_job(
                            job.source_id,
                            job.desired_source_hash,
                            unavailable_reason="no_detail_control",
                            checked_at=observed_at,
                        )
                        completed += 1
                        continue
                    detail = parse_calendar_detail(html, job.source_id, observed_at)
                    stored = await self.repository.replace_calendar_detail(
                        detail, desired_source_hash=job.desired_source_hash
                    )
                    if not stored:
                        continue
                    await self.repository.complete_calendar_detail_job(
                        job.source_id,
                        job.desired_source_hash,
                        checked_at=observed_at,
                    )
                except Exception as error:
                    batch_error = error
                    had_failure = True
                    await self.repository.fail_calendar_detail_job(
                        job.source_id,
                        error,
                        observed_at,
                        self.max_attempts,
                        desired_source_hash=job.desired_source_hash,
                    )
                    await self.repository.set_runtime_state(
                        "calendar_detail_last_error", type(error).__name__
                    )
                else:
                    completed += 1
            source_pages = set(pages.values()) | {getattr(pages, "source_html", None)}
            for html in source_pages - {None}:
                await capture_snapshot(
                    self.snapshot_store,
                    "calendar-detail",
                    day.isoformat(),
                    html,
                    observed_at,
                    batch_error,
                )
        if completed:
            await self.repository.set_runtime_state(
                "calendar_detail_last_success", observed_at.isoformat()
            )
        if completed and not had_failure:
            await self.repository.set_runtime_state("calendar_detail_last_error", "")
        return completed

    async def run(self, stop: asyncio.Event, interval: int) -> None:
        while not stop.is_set():
            with suppress(Exception):
                await self.run_cycle()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue
