from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.collector import Collector
from app.db import Database
from app.domain import NewsObservation
from app.repository import Repository
from app.translation.worker import TranslationRunResult, TranslationWorker

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
async def repository(tmp_path: Path):
    database = Database(tmp_path / "workers.sqlite3")
    await database.open()
    await database.initialize()
    yield Repository(database)
    await database.close()


class FakeBrowser:
    async def calendar_html(self) -> str:
        return (FIXTURES / "calendar.html").read_text()

    async def news_html(self) -> str:
        return (FIXTURES / "news.html").read_text()

    async def news_detail_html(self, _url: str) -> str:
        return (FIXTURES / "news_article.html").read_text()


class FakeTranslator:
    async def translate(self, jobs):
        return {
            job.id: {"title_zh": "美元上涨", "summary_zh": "美元走强。", "body_zh": "美元上涨。"}
            for job in jobs
        }


class FailingTranslator:
    async def translate(self, _jobs):
        raise httpx.ReadTimeout("timed out")


async def test_collection_cycle_commits_calendar_and_news(repository: Repository) -> None:
    result = await Collector(FakeBrowser(), repository).run_cycle(
        datetime(2026, 9, 1, 12, tzinfo=UTC)
    )

    assert result.calendar_count == 2
    assert result.news_count == 1
    assert (await repository.get_news("9001")).body_en == "The dollar advanced.\n\nYields rose."


async def test_translation_worker_applies_pending_translation(repository: Repository) -> None:
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    await repository.upsert_news(
        [
            NewsObservation(
                "9001", "https://x", "Reuters", None, now, "Dollar rises", None, None, None
            )
        ]
    )

    result = await TranslationWorker(repository, FakeTranslator()).run_once()

    assert result.completed == 1
    assert (await repository.get_news("9001")).title_zh == "美元上涨"


async def test_translation_failure_is_delayed_without_changing_english(
    repository: Repository,
) -> None:
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    await repository.upsert_news(
        [NewsObservation("9002", "https://x", None, None, now, "Gold rises", None, None, None)]
    )
    worker = TranslationWorker(repository, FailingTranslator())

    first = await worker.run_once()
    immediate_retry = await worker.run_once()

    assert first.failed == 1
    assert immediate_retry == TranslationRunResult(0, 0)
    assert (await repository.get_news("9002")).title_en == "Gold rises"
    assert (await repository.get_news("9002")).title_zh is None
