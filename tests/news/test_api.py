from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.news.models import (
    ArticleObservation,
    CategoryObservation,
    CommentCollectionObservation,
    CommentObservation,
    DetailObservation,
    FeedObservation,
    MediaObservation,
    NewsListingBatch,
    SegmentLinkObservation,
    SegmentObservation,
)
from app.news.repository import NewsRepository
from app.repository import Repository

NOW = datetime(2026, 9, 3, tzinfo=UTC)


@pytest.fixture
async def api(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "api.sqlite3",
        news_media_dir=tmp_path / "media",
        app_api_key="api-secret",
        moonshot_api_key="kimi-secret",
    )
    database = Database(settings.database_path)
    await database.open()
    await database.initialize()
    legacy = Repository(database)
    news = NewsRepository(legacy.db)
    article = ArticleObservation(
        "100",
        "https://www.forexfactory.com/news/100-yen",
        "Yen rises",
        NOW,
        teaser_en="BOJ watched markets.",
        published_at=NOW,
        breaking_impact="high",
        comment_count=1,
        listing_thumbnail_url="https://assets.example/yen.png",
    )
    await news.apply_listing(
        NewsListingBatch(
            (article,),
            NOW,
            "listing",
            "Asia/Shanghai",
            frozenset({"latest", "fundamental"}),
            categories=(CategoryObservation("100", "fundamental", NOW),),
            feeds=(FeedObservation("100", "latest", 0, NOW),),
        )
    )
    await news.set_runtime_state("news_last_listing_success", NOW.isoformat())
    await news.replace_detail(
        "100",
        DetailObservation(
            "100",
            NOW,
            "detail",
            segments=(
                SegmentObservation("alert", 0, "social", text_en="First alert"),
                SegmentObservation(
                    "body",
                    1,
                    "article",
                    text_en="Forex Factory excerpt...",
                    is_excerpt=True,
                    display_mode="full",
                ),
            ),
            links=(
                SegmentLinkObservation(
                    "source-link",
                    "body",
                    0,
                    "full_story",
                    "full story",
                    "https://publisher.example/story",
                ),
            ),
            media=(
                MediaObservation(
                    "chart",
                    0,
                    "chart",
                    "https://assets.example/chart.png",
                    segment_key="body",
                ),
            ),
            comments=(
                CommentObservation(
                    "700",
                    "100",
                    "Alice",
                    "Useful",
                    "https://www.forexfactory.com/comment/700",
                    NOW,
                ),
            ),
        ),
    )
    await news.replace_comments(
        CommentCollectionObservation(
            "100",
            NOW,
            1,
            (
                CommentObservation(
                    "700",
                    "100",
                    "Alice",
                    "Useful",
                    "https://www.forexfactory.com/comment/700",
                    NOW,
                    position=0,
                    depth=0,
                ),
            ),
            True,
        )
    )
    await news.set_runtime_state("news_last_comment_success", NOW.isoformat())
    title_job = (await news.claim_localized_jobs(1, NOW))[0]
    await news.complete_localized_job(title_job, "日元上涨", "k3-256k")
    media_job = (await news.claim_media_jobs(1, NOW))[0]
    media_path = settings.news_media_dir / "image.png"
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    await news.complete_media_job(
        media_job.media_id,
        media_job.original_url,
        str(media_path),
        "image/png",
        media_path.stat().st_size,
        "abc",
    )
    app = create_app(settings, repository=legacy)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    yield client
    await client.aclose()
    await database.close()


async def test_v2_routes_require_authentication(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/v2/news/sections")
    assert response.status_code == 401


async def test_sections_and_latest_are_stable_and_bilingual(api: httpx.AsyncClient) -> None:
    headers = {"X-API-Key": "api-secret"}
    sections = await api.get("/api/v2/news/sections", headers=headers)
    listing = await api.get("/api/v2/news?section=latest&impact=high&limit=1", headers=headers)

    assert sections.status_code == 200, sections.text
    assert [item["id"] for item in sections.json()["items"]] == [
        "latest",
        "hot",
        "fundamental",
        "technical",
        "industry",
        "entertainment",
        "educational",
        "latest-comments",
    ]
    assert listing.status_code == 200
    assert listing.json()["items"][0]["title"] == {"en": "Yen rises", "zh_hans": "日元上涨"}
    assert listing.json()["items"][0]["breaking_impact"] == "high"
    assert listing.json()["items"][0]["thumbnail_url"] == "https://assets.example/yen.png"
    assert listing.json()["source_updated_at"] == "2026-09-03T00:00:00Z"


async def test_latest_comments_exposes_listing_collection_time(api: httpx.AsyncClient) -> None:
    repo = api._transport.app.state.news_repository
    await repo.set_runtime_state(
        "news_last_comment_success", (NOW + timedelta(hours=1)).isoformat()
    )
    response = await api.get(
        "/api/v2/news/comments/latest", headers={"X-API-Key": "api-secret"}
    )

    assert response.status_code == 200
    assert response.json()["source_updated_at"] == "2026-09-03T00:00:00Z"


async def test_detail_comments_and_media_contract(api: httpx.AsyncClient) -> None:
    headers = {"X-API-Key": "api-secret"}
    detail = await api.get("/api/v2/news/100", headers=headers)
    comments = await api.get("/api/v2/news/100/comments", headers=headers)
    assert detail.status_code == 200, detail.text
    media_id = detail.json()["segments"][1]["media"][0]["id"]
    media = await api.get(f"/api/v2/news/media/{media_id}", headers=headers)

    assert [segment["position"] for segment in detail.json()["segments"]] == [0, 1]
    source_link = detail.json()["segments"][1]["links"][0]
    assert source_link["kind"] == "full_story"
    assert source_link["url"] == "https://publisher.example/story"
    assert "source_document" not in source_link
    assert detail.json()["segments"][1]["presentation"] == {
        "mode": "full",
        "max_lines": None,
        "action_label": None,
    }
    assert detail.json()["segments"][1]["text"]["en"] == ("Forex Factory excerpt...")
    assert "full story" not in detail.json()["segments"][1]["text"]["en"].lower()
    assert "local_path" not in detail.text
    assert detail.json()["comments_complete"] is True
    assert detail.json()["comments_state"] == "complete"
    assert comments.json()["items"][0]["parent_comment_id"] is None
    assert comments.json()["items"][0]["position"] == 0
    assert comments.json()["items"][0]["depth"] == 0
    assert comments.json()["comments_complete"] is True
    assert media.status_code == 200
    assert media.headers["content-type"] == "image/png"
    assert media.headers["etag"] == '"abc"'

    removed_source_route = await api.get(
        "/api/v2/news/source-documents/7",
        headers=headers,
    )
    assert removed_source_route.status_code == 404


async def test_invalid_section_cursor_and_missing_article(api: httpx.AsyncClient) -> None:
    headers = {"X-API-Key": "api-secret"}
    invalid_section = await api.get("/api/v2/news?section=other", headers=headers)
    invalid_cursor = await api.get("/api/v2/news?section=latest&cursor=not-valid", headers=headers)
    missing = await api.get("/api/v2/news/999", headers=headers)

    assert invalid_section.status_code == 422
    assert invalid_cursor.status_code == 422
    assert missing.status_code == 404


async def test_v1_news_is_derived_from_v2_during_client_migration(
    api: httpx.AsyncClient,
) -> None:
    headers = {"X-API-Key": "api-secret"}

    listing = await api.get("/api/v1/news", headers=headers)
    detail = await api.get("/api/v1/news/100", headers=headers)

    assert listing.json()["items"][0]["title_en"] == "Yen rises"
    assert listing.json()["items"][0]["body_en"] == ("First alert\n\nForex Factory excerpt...")
    assert detail.json()["title_zh"] == "日元上涨"
    assert detail.json()["body_en"] == "First alert\n\nForex Factory excerpt..."
    assert "local_path" not in detail.text


async def test_status_exposes_schema_queues_and_sanitized_collector_state(
    api: httpx.AsyncClient,
) -> None:
    response = await api.get("/api/v2/status", headers={"X-API-Key": "api-secret"})

    assert response.status_code == 200
    assert response.json()["schema_version"] == 8
    assert "detail_jobs" in response.json()
    assert "comment_jobs" in response.json()
    assert "last_comment_success" in response.json()
    assert "last_comment_error" in response.json()
    assert "source_documents" not in response.json()
    assert response.json()["last_listing_error"] is None


async def test_status_exposes_listing_failure_diagnostics(api: httpx.AsyncClient) -> None:
    repo = api._transport.app.state.news_repository
    await repo.set_runtime_state("news_last_listing_error_at", NOW.isoformat())
    await repo.set_runtime_state("news_listing_consecutive_failures", "3")

    response = await api.get("/api/v2/status", headers={"X-API-Key": "api-secret"})

    assert response.json()["last_listing_error_at"] == NOW.isoformat()
    assert response.json()["listing_consecutive_failures"] == 3


async def test_status_reports_collection_failure_instead_of_ok(api):
    repo = api._transport.app.state.news_repository
    await repo.set_runtime_state("news_last_listing_success", datetime.now(UTC).isoformat())
    await repo.set_runtime_state("news_last_detail_error", "SourcePageError")
    response = await api.get("/api/v2/status", headers={"X-API-Key": "api-secret"})
    assert response.json()["status"] == "degraded"
    assert "detail_error" in response.json()["issues"]


async def test_status_reports_stale_listing_even_without_recent_exception(api):
    repo = api._transport.app.state.news_repository
    await repo.set_runtime_state(
        "news_last_listing_success", (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    )
    response = await api.get("/api/v2/status", headers={"X-API-Key": "api-secret"})
    assert response.json()["status"] == "degraded"
    assert "listing_stale" in response.json()["issues"]


async def test_status_reports_partial_comments_when_source_counts_match(api):
    repo = api._transport.app.state.news_repository
    now = datetime.now(UTC)
    await repo.set_runtime_state("news_last_listing_success", now.isoformat())
    await repo.replace_comments(CommentCollectionObservation(
        article_id="100", observed_at=now, expected_count=1, comments=(),
        is_complete=False, source_complete=True, visible_count=1,
    ))
    response = await api.get("/api/v2/status", headers={"X-API-Key": "api-secret"})
    assert response.json()["status"] == "degraded"
    assert "comments_partial" in response.json()["issues"]


async def test_status_exposes_backfill_coverage_and_reason(api):
    import json

    repo = api._transport.app.state.news_repository
    await repo.set_runtime_state("news_backfill:latest", json.dumps({
        "complete": True, "reached_cutoff": False, "stop_reason": "source_exhausted",
        "oldest_published_at": "2026-09-01T00:00:00Z",
    }))
    response = await api.get("/api/v2/status", headers={"X-API-Key": "api-secret"})
    coverage = response.json()["backfill"]["latest"]
    assert coverage["reached_cutoff"] is False
    assert coverage["stop_reason"] == "source_exhausted"
