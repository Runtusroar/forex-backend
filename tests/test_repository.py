from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import Database
from app.domain import CalendarObservation, NewsObservation
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
