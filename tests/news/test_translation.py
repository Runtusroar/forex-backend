from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from app.config import Settings
from app.db import Database
from app.news.models import (
    ArticleObservation,
    CommentCollectionObservation,
    CommentObservation,
    DetailObservation,
    LocalizedTextJob,
    NewsListingBatch,
    SegmentObservation,
)
from app.news.repository import NewsRepository
from app.translation.kimi import KimiTranslator
from app.translation.worker import NewsTranslationWorker, TranslationRunResult

NOW = datetime(2026, 9, 3, tzinfo=UTC)


@pytest.fixture
async def news_repository(tmp_path: Path):
    database = Database(tmp_path / "translation.sqlite3")
    await database.open()
    await database.initialize()
    assert database.connection is not None
    repository = NewsRepository(database.connection)
    yield repository
    await database.close()


async def seed(repository: NewsRepository) -> None:
    article = ArticleObservation(
        "1", "https://www.forexfactory.com/news/1-x", "Dollar rises", NOW,
        teaser_en="Treasury yields advanced.",
        comment_count=1,
    )
    await repository.apply_listing(
        NewsListingBatch(
            (article,), NOW, "listing", "Asia/Shanghai", frozenset({"latest"})
        )
    )
    await repository.replace_detail(
        "1",
        DetailObservation(
            "1",
            NOW,
            "detail",
            segments=(SegmentObservation("body", 0, "article", text_en="Full story"),),
        ),
    )
    comment_job = (await repository.claim_comment_jobs(1, NOW))[0]
    await repository.replace_comments(
        CommentCollectionObservation(
            "1",
            NOW,
            1,
            (
                CommentObservation(
                    "10",
                    "1",
                    "Alice",
                    "Useful comment",
                    "https://www.forexfactory.com/comment/10",
                    NOW,
                ),
            ),
            True,
        ),
        claimed_expected_count=comment_job.expected_count,
    )
    await repository.complete_comment_job(
        "1", NOW, claimed_expected_count=comment_job.expected_count, collected_count=1
    )


class PartialTranslator:
    async def translate_fields(self, jobs):
        return {jobs[0].id: "美元上涨"}


class FailingTranslator:
    async def translate_fields(self, jobs):
        raise httpx.ReadTimeout("private failure")


async def test_news_translation_prioritizes_content_and_isolates_missing_items(
    news_repository: NewsRepository,
) -> None:
    await seed(news_repository)
    worker = NewsTranslationWorker(news_repository, PartialTranslator())

    result = await worker.run_once(limit=10)

    assert result.completed == 1
    assert result.failed == 3
    assert await news_repository.localized_text("article", "1", "title") == "美元上涨"
    assert await news_repository.localized_status("comment", "10", "text") == "pending"
    assert await worker.run_once(limit=10) == TranslationRunResult(0, 0)


async def test_translation_failure_does_not_change_english_source(
    news_repository: NewsRepository,
) -> None:
    await seed(news_repository)

    result = await NewsTranslationWorker(news_repository, FailingTranslator()).run_once()

    article = await news_repository.get_article("1")
    assert result == TranslationRunResult(completed=0, failed=4)
    assert article is not None and article.title_en == "Dollar rises"


async def test_late_translation_is_stale_after_source_changes(
    news_repository: NewsRepository,
) -> None:
    await seed(news_repository)
    jobs = await news_repository.claim_localized_jobs(1, NOW)
    assert jobs[0].field_name == "title"
    changed = ArticleObservation(
        "1", "https://www.forexfactory.com/news/1-x", "Dollar falls", NOW
    )
    await news_repository.apply_listing(
        NewsListingBatch(
            (changed,), NOW, "changed", "Asia/Shanghai", frozenset({"latest"})
        )
    )

    completed = await news_repository.complete_localized_job(
        jobs[0], "美元上涨", "k3-256k"
    )

    assert completed is False
    assert await news_repository.localized_status_by_id(jobs[0].id) == "stale"


async def test_removed_source_document_translation_is_marked_stale(
    news_repository: NewsRepository,
) -> None:
    await news_repository.db.execute(
        """INSERT INTO localized_texts
           (entity_type,entity_id,field_name,language,source_hash,status,attempts,
            created_at,updated_at)
           VALUES ('source_document','7','body','zh-Hans','old','pending',0,?,?)""",
        (NOW.isoformat(), NOW.isoformat()),
    )
    await news_repository.db.commit()

    jobs = await news_repository.claim_localized_jobs(10, NOW)

    assert jobs == []
    assert await news_repository.localized_status_by_id(1) == "stale"


@respx.mock
async def test_kimi_field_payload_accepts_partial_valid_results(tmp_path: Path) -> None:
    route = respx.post("https://api.kimi.com/coding/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "translations": [
                                        {"job_id": 1, "translated_text": "美元上涨"}
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )
    )
    configured = Settings(
        _env_file=None,
        database_path=tmp_path / "db.sqlite3",
        app_api_key="api-secret",
        moonshot_api_key="kimi-secret",
    )
    jobs = [
        LocalizedTextJob(1, "article", "1", "title", "Dollar rises", "a", 0),
        LocalizedTextJob(2, "comment", "2", "text", "Useful", "b", 0),
    ]

    result = await KimiTranslator(configured).translate_fields(jobs)
    payload = json.loads(route.calls[0].request.content)
    source_items = json.loads(payload["messages"][1]["content"])

    assert result == {1: "美元上涨"}
    assert source_items[0] == {
        "job_id": 1,
        "entity_type": "article",
        "field_name": "title",
        "source_text": "Dollar rises",
    }
