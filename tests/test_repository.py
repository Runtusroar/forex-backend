import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import Database
from app.domain import (
    CalendarDetailObservation,
    CalendarHistoryObservation,
    CalendarObservation,
    CalendarRelatedStoryObservation,
    NewsObservation,
)
from app.repository import Repository


@pytest.fixture
async def repository(tmp_path: Path):
    database = Database(tmp_path / "test.sqlite3")
    await database.open()
    await database.initialize()
    yield Repository(database)
    await database.close()


def calendar_item() -> CalendarObservation:
    return CalendarObservation(
        source_id="142001",
        event_at=datetime(2026, 9, 1, 12, 30, tzinfo=UTC),
        currency="USD",
        impact="high",
        title_en="ISM Manufacturing PMI",
        actual="51.2",
        forecast="50.5",
        previous="49.8",
    )


def calendar_detail(source_id: str, title: str) -> CalendarDetailObservation:
    return CalendarDetailObservation(
        source_id=source_id,
        title_en=title,
        currency="USD",
        currency_name=None,
        impact="high",
        actual=None,
        forecast=None,
        previous=None,
        actual_state=None,
        previous_state=None,
        previous_revised_from=None,
        ff_url=None,
        source_name=None,
        source_url=None,
        latest_release_url=None,
        measures=None,
        usual_effect=None,
        frequency=None,
        next_release_text=None,
        next_release_url=None,
        ff_notes=None,
        why_traders_care=None,
    )


def news_item() -> NewsObservation:
    return NewsObservation(
        source_id="9001",
        url="https://www.forexfactory.com/news/9001-dollar-rises",
        source="Reuters",
        published_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        first_seen_at=datetime(2026, 9, 1, 12, 1, tzinfo=UTC),
        title_en="Dollar rises",
        summary_en="The dollar gained.",
        body_en="The dollar advanced against major peers.",
        image_url=None,
    )


async def test_upsert_commits_english_and_enqueues_translation(repository: Repository) -> None:
    item = calendar_item()
    await repository.upsert_calendar([item])

    stored = (
        await repository.list_calendar(
            item.event_at - timedelta(seconds=1), item.event_at + timedelta(seconds=1)
        )
    )[0]
    jobs = await repository.claim_translation_jobs(limit=10)

    assert stored.title_en == "ISM Manufacturing PMI"
    assert stored.title_zh is None
    assert jobs[0].source_hash == stored.source_hash


async def test_unchanged_content_does_not_duplicate_job(repository: Repository) -> None:
    item = calendar_item()
    await repository.upsert_calendar([item])
    await repository.upsert_calendar([item])

    assert await repository.translation_job_count() == 1


async def test_replace_calendar_window_removes_only_stale_rows_inside_window(
    repository: Repository,
) -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    stale = replace(calendar_item(), source_id="stale", event_at=start + timedelta(hours=1))
    outside = replace(calendar_item(), source_id="outside", event_at=end + timedelta(hours=1))
    fresh = replace(calendar_item(), source_id="fresh", event_at=start + timedelta(hours=2))
    await repository.upsert_calendar([stale, outside])

    await repository.replace_calendar_window([fresh], start, end)

    rows = await repository.list_calendar(start, end + timedelta(days=1))
    assert [row.source_id for row in rows] == ["fresh", "outside"]


async def test_list_calendar_excludes_event_at_end_boundary(
    repository: Repository,
) -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    inside = replace(calendar_item(), source_id="inside", event_at=end - timedelta(seconds=1))
    boundary = replace(calendar_item(), source_id="boundary", event_at=end)
    await repository.upsert_calendar([inside, boundary])

    rows = await repository.list_calendar(start, end)

    assert [row.source_id for row in rows] == ["inside"]


async def test_calendar_round_trips_source_time_text_and_position(
    repository: Repository,
) -> None:
    item = replace(
        calendar_item(),
        source_time_text="Tentative",
        source_position=7,
    )

    await repository.upsert_calendar([item])

    stored = (
        await repository.list_calendar(
            item.event_at - timedelta(seconds=1),
            item.event_at + timedelta(seconds=1),
        )
    )[0]
    assert stored.source_time_text == "Tentative"
    assert stored.source_position == 7


async def test_calendar_detail_round_trips_specs_history_and_related_stories(
    repository: Repository,
) -> None:
    item = calendar_item()
    await repository.upsert_calendar([item])
    await repository.replace_calendar_detail(
        CalendarDetailObservation(
            source_id=item.source_id,
            title_en=item.title_en,
            currency=item.currency,
            currency_name="US dollar",
            impact=item.impact,
            actual=item.actual,
            forecast=item.forecast,
            previous=item.previous,
            actual_state="better",
            previous_state="worse",
            previous_revised_from="49.5",
            ff_url="https://www.forexfactory.com/calendar/1-us-ism-manufacturing-pmi",
            source_name="ISM",
            source_url="https://www.ismworld.org/",
            latest_release_url="https://www.ismworld.org/latest/",
            measures="Level of a diffusion index;",
            usual_effect="'Actual' greater than 'Forecast' is good for currency;",
            frequency="Released monthly;",
            next_release_text="Oct 1, 2026",
            next_release_url="https://www.forexfactory.com/calendar?day=oct1.2026#detail=2",
            ff_notes="Survey notes;",
            why_traders_care="It is a leading indicator;",
            history=(
                CalendarHistoryObservation(
                    "Sep 1, 2026",
                    "https://www.forexfactory.com/calendar?day=sep1.2026#detail=1",
                    "51.2",
                    "50.5",
                    "49.8",
                    actual_state="better",
                    previous_state="worse",
                    previous_revised_from="49.5",
                ),
            ),
            related_stories=(
                CalendarRelatedStoryObservation(
                    "US factory activity expanded",
                    "https://www.forexfactory.com/news/1-us-factory-activity-expanded",
                    "ismworld.org",
                    "https://www.forexfactory.com/news/1-us-factory-activity-expanded/hit",
                    "Sep 1, 2026",
                    "tables",
                ),
            ),
        )
    )

    detail = await repository.get_calendar_detail(item.source_id)

    assert detail is not None
    assert detail.source_name == "ISM"
    assert detail.actual_state == "better"
    assert detail.previous_revised_from == "49.5"
    assert detail.history[0].release_date_text == "Sep 1, 2026"
    assert detail.history[0].previous_state == "worse"
    assert detail.related_stories[0].title_en == "US factory activity expanded"


async def test_calendar_detail_replace_rolls_back_on_child_insert_failure(
    repository: Repository,
) -> None:
    item = calendar_item()
    await repository.upsert_calendar([item])
    old_detail = replace(
        calendar_detail(item.source_id, "Old detail"),
        history=(CalendarHistoryObservation("Old release", None, None, None, None),),
    )
    await repository.replace_calendar_detail(old_detail)
    invalid_detail = replace(
        calendar_detail(item.source_id, "Incomplete replacement"),
        history=(
            CalendarHistoryObservation(None, None, None, None, None),  # type: ignore[arg-type]
        ),
    )

    with pytest.raises(sqlite3.IntegrityError):
        await repository.replace_calendar_detail(invalid_detail)

    stored = await repository.get_calendar_detail(item.source_id)
    assert stored is not None
    assert stored.title_en == "Old detail"
    assert [row.release_date_text for row in stored.history] == ["Old release"]


async def test_calendar_change_enqueues_durable_detail_refresh(
    repository: Repository,
) -> None:
    item = calendar_item()
    await repository.upsert_calendar([item])
    first = (await repository.claim_calendar_detail_jobs(1))[0]
    await repository.complete_calendar_detail_job(first.source_id, first.desired_source_hash)

    changed = replace(item, actual="52.0")
    await repository.upsert_calendar([changed])
    jobs = await repository.claim_calendar_detail_jobs(1)

    assert len(jobs) == 1
    assert jobs[0].source_id == item.source_id
    assert jobs[0].event_at == item.event_at
    assert jobs[0].priority == 100


async def test_stale_calendar_detail_worker_cannot_mutate_newer_job(
    repository: Repository,
) -> None:
    item = calendar_item()
    await repository.upsert_calendar([item])
    stale_job = (await repository.claim_calendar_detail_jobs(1))[0]

    await repository.upsert_calendar([replace(item, actual="52.0")])
    stored = await repository.replace_calendar_detail(
        calendar_detail(item.source_id, "Stale detail"),
        desired_source_hash=stale_job.desired_source_hash,
    )
    failed = await repository.fail_calendar_detail_job(
        stale_job.source_id,
        ValueError("stale page"),
        desired_source_hash=stale_job.desired_source_hash,
    )

    assert stored is False
    assert failed is False
    assert await repository.get_calendar_detail(item.source_id) is None
    current_job = (await repository.claim_calendar_detail_jobs(1))[0]
    assert current_job.desired_source_hash != stale_job.desired_source_hash
    assert current_job.attempts == 0


async def test_due_calendar_detail_refresh_requeues_future_event(
    repository: Repository,
) -> None:
    current = datetime.now(UTC)
    item = replace(calendar_item(), event_at=current + timedelta(days=1))
    await repository.upsert_calendar([item])
    job = (await repository.claim_calendar_detail_jobs(1))[0]
    await repository.complete_calendar_detail_job(job.source_id, job.desired_source_hash)

    queued = await repository.enqueue_due_calendar_detail_refreshes(
        current + timedelta(days=1, minutes=1),
        refresh_interval=timedelta(days=1),
    )

    assert queued == 1


async def test_calendar_detail_refreshes_near_release_more_frequently(
    repository: Repository,
) -> None:
    now = datetime.now(UTC)
    near = replace(calendar_item(), source_id="near", event_at=now + timedelta(hours=2))
    future = replace(calendar_item(), source_id="future", event_at=now + timedelta(days=1))
    await repository.upsert_calendar([near, future])
    observed_at = now + timedelta(seconds=1)
    jobs = await repository.claim_calendar_detail_jobs(2, observed_at)
    for job in jobs:
        await repository.replace_calendar_detail(
            calendar_detail(job.source_id, "Cached detail"),
            desired_source_hash=job.desired_source_hash,
        )
        await repository.complete_calendar_detail_job(job.source_id, job.desired_source_hash)
    await repository.db.execute(
        "UPDATE calendar_event_details SET last_success_at=?",
        ((observed_at - timedelta(minutes=20)).isoformat().replace("+00:00", "Z"),),
    )
    await repository.db.commit()

    queued = await repository.enqueue_due_calendar_detail_refreshes(
        observed_at, refresh_interval=timedelta(days=1), limit=2
    )
    refreshed = await repository.claim_calendar_detail_jobs(2, observed_at)

    assert queued == 1
    assert [job.source_id for job in refreshed] == ["near"]


async def test_stale_translation_cannot_overwrite_changed_source(
    repository: Repository,
) -> None:
    item = news_item()
    await repository.upsert_news([item])
    old_job = (await repository.claim_translation_jobs(1))[0]
    await repository.upsert_news([replace(item, title_en="Updated title")])

    applied = await repository.complete_translation(
        old_job,
        {"title_zh": "旧标题", "summary_zh": None, "body_zh": None},
    )

    assert applied is False
    assert (await repository.get_news(item.source_id)).title_zh is None


async def test_reads_do_not_see_uncommitted_rows(repository: Repository) -> None:
    await repository.db.execute(
        "INSERT INTO runtime_state(key,value) VALUES ('uncommitted','dirty')"
    )
    try:
        assert await repository.get_runtime_state("uncommitted") is None
    finally:
        await repository.db.rollback()


async def test_cancelled_write_is_rolled_back_before_next_commit(repository, monkeypatch):
    import asyncio

    started = asyncio.Event()

    async def interrupted(_items, _now):
        await repository.db.execute(
            "INSERT INTO runtime_state(key,value) VALUES ('cancelled','dirty')"
        )
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(repository, "_upsert_calendar", interrupted)
    task = asyncio.create_task(repository.upsert_calendar([]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await repository.set_runtime_state("unrelated", "committed")
    assert await repository.get_runtime_state("cancelled") is None


async def test_translation_claim_has_a_lease(repository):
    await repository.upsert_calendar([calendar_item()])
    assert len(await repository.claim_translation_jobs(1)) == 1
    assert await repository.claim_translation_jobs(1) == []


async def test_unavailable_calendar_detail_waits_for_refresh(repository):
    item = calendar_item()
    await repository.upsert_calendar([item])
    job = (await repository.claim_calendar_detail_jobs(1))[0]
    checked = item.event_at
    await repository.complete_calendar_detail_job(
        item.source_id,
        job.desired_source_hash,
        unavailable_reason="no_detail_control",
        checked_at=checked,
    )
    assert await repository.enqueue_due_calendar_detail_refreshes(now=checked) == 0
    assert (
        await repository.enqueue_due_calendar_detail_refreshes(now=checked + timedelta(days=1)) == 1
    )


async def test_read_connection_keeps_snapshot_and_is_read_only(repository):
    await repository.set_runtime_state("version", "old")
    async with repository.database.read_connection() as reader:
        scoped = Repository(repository.database, reader=reader)
        assert await scoped.get_runtime_state("version") == "old"
        await repository.set_runtime_state("version", "new")
        assert await scoped.get_runtime_state("version") == "old"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await reader.execute("UPDATE runtime_state SET value='bad'")
        # A supplied reader never redirects a write method to the read-only connection.
        await scoped.set_runtime_state("other", "writer")
        assert await scoped.get_runtime_state("other") is None
    assert await repository.get_runtime_state("version") == "new"
    assert await repository.get_runtime_state("other") == "writer"


async def test_repeated_cancellation_holds_writer_lock_until_rollback(repository, monkeypatch):
    import asyncio

    inserted = asyncio.Event()
    rolling_back = asyncio.Event()
    release_rollback = asyncio.Event()
    original_rollback = repository.db.rollback

    async def paused_insert(_items, _now):
        await repository.db.execute(
            "INSERT INTO runtime_state(key,value) VALUES ('cancelled','dirty')"
        )
        inserted.set()
        await asyncio.Event().wait()

    async def paused_rollback():
        rolling_back.set()
        await release_rollback.wait()
        await original_rollback()

    monkeypatch.setattr(repository, "_upsert_calendar", paused_insert)
    monkeypatch.setattr(repository.db, "rollback", paused_rollback)
    task = asyncio.create_task(repository.upsert_calendar([]))
    await inserted.wait()
    task.cancel()
    await rolling_back.wait()
    task.cancel()
    other = asyncio.create_task(repository.set_runtime_state("other", "committed"))
    await asyncio.sleep(0)
    assert not other.done()
    release_rollback.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await other
    assert await repository.get_runtime_state("cancelled") is None


async def test_translation_lease_expires_and_calendar_job_keeps_source_date(
    repository, monkeypatch
):
    import app.repository as module

    now = datetime(2026, 9, 5, tzinfo=UTC)
    monkeypatch.setattr(module, "_now", lambda: now)
    item = replace(calendar_item(), source_date=now.date())
    await repository.upsert_calendar([item])
    first = await repository.claim_translation_jobs(1)
    assert first
    assert not await repository.claim_translation_jobs(1)
    now += timedelta(minutes=6)
    assert (await repository.claim_translation_jobs(1))[0].id == first[0].id
    assert (await repository.claim_calendar_detail_jobs(1))[0].source_date == item.source_date
