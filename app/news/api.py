import base64
import binascii
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.config import Settings
from app.news.backfill import DEFAULT_SECTIONS
from app.news.repository import NewsRepository

Section = Literal[
    "latest",
    "hot",
    "fundamental",
    "technical",
    "industry",
    "entertainment",
    "educational",
]
Impact = Literal["high", "medium", "low"]
SECTION_DEFINITIONS = (
    ("latest", "Latest Stories", "最新新闻"),
    ("hot", "Hot Stories", "热门新闻"),
    ("fundamental", "Fundamental Analysis", "基本面分析"),
    ("technical", "Technical Analysis", "技术分析"),
    ("industry", "Forex Industry News", "外汇行业新闻"),
    ("entertainment", "Entertainment News", "财经轻读"),
    ("educational", "Educational News", "交易教育"),
    ("latest-comments", "Latest Comments", "最新评论"),
)


def _encode_cursor(section: str, value: dict[str, Any] | str | None) -> str | None:
    if value is None:
        return None
    raw = json.dumps({"section": section, "value": value}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None, section: str) -> dict[str, Any] | str | None:
    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(cursor + padding))
        if decoded["section"] != section or not isinstance(decoded["value"], (dict, str)):
            raise ValueError
        return decoded["value"]
    except (
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        binascii.Error,
        UnicodeDecodeError,
    ) as error:
        raise HTTPException(status_code=422, detail="Invalid cursor") from error


def _require_cursor_keys(
    value: dict[str, Any] | str | None, keys: tuple[str, ...]
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(keys):
        raise HTTPException(status_code=422, detail="Invalid cursor")
    if not isinstance(value.get("id"), str):
        raise HTTPException(status_code=422, detail="Invalid cursor")
    if "rank" in value and not isinstance(value["rank"], int):
        raise HTTPException(status_code=422, detail="Invalid cursor")
    if "time" in value and not isinstance(value["time"], str):
        raise HTTPException(status_code=422, detail="Invalid cursor")
    if "position" in value and not isinstance(value["position"], int):
        raise HTTPException(status_code=422, detail="Invalid cursor")
    return value


def _article(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": item["source_id"],
        "ff_url": item["ff_url"],
        "title": {"en": item["title_en"], "zh_hans": item.get("title_zh")},
        "teaser": {"en": item.get("teaser_en"), "zh_hans": item.get("teaser_zh")},
        "source_name": item.get("source_name"),
        "source_url": item.get("source_url"),
        "published_at": item.get("published_at"),
        "published_at_source_text": item.get("published_at_source_text"),
        "source_timezone": item.get("source_timezone"),
        "breaking_impact": item.get("breaking_impact"),
        "comment_count": item.get("comment_count", 0),
        "detail_state": item.get("detail_state"),
        "is_excerpt": bool(item.get("is_excerpt")),
        "thumbnail_url": item.get("listing_thumbnail_url"),
        "categories": item.get("categories", []),
    }


def _media(item: dict[str, Any]) -> dict[str, Any]:
    complete = item["download_state"] == "complete"
    return {
        "id": item["id"],
        "position": item["position"],
        "type": item["media_type"],
        "caption": item.get("caption"),
        "original_url": item["original_url"],
        "download_state": item["download_state"],
        "url": f"/api/v2/news/media/{item['id']}" if complete else None,
        "mime_type": item.get("mime_type") if complete else None,
        "byte_size": item.get("byte_size") if complete else None,
    }


def _comment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "comment_id": item["comment_id"],
        "article_id": item["article_id"],
        "parent_comment_id": item.get("parent_comment_id"),
        "author_name": item["author_name"],
        "published_at": item.get("published_at"),
        "published_at_source_text": item.get("published_at_source_text"),
        "text": {"en": item["text_en"], "zh_hans": item.get("text_zh")},
        "permalink": item["permalink"],
        "reaction_count": item.get("reaction_count"),
        "position": item.get("position", 0),
        "depth": item.get("depth", 0),
    }


def create_news_router(settings: Settings, authorize: Callable[..., None]) -> APIRouter:
    router = APIRouter(prefix="/api/v2", dependencies=[Depends(authorize)])

    async def repository(request: Request) -> AsyncIterator[NewsRepository]:
        live = request.app.state.repository
        async with live.database.read_connection() as reader:
            yield NewsRepository(live.db, live.write_lock, reader=reader)

    async def source_updated_at(repo: NewsRepository, state_key: str) -> datetime | None:
        raw = await repo.get_runtime_state(state_key)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    @router.get("/news/sections")
    async def sections(repo: Annotated[NewsRepository, Depends(repository)]) -> dict:
        counts = await repo.section_counts()
        return {
            "items": [
                {
                    "id": section_id,
                    "name": {"en": name_en, "zh_hans": name_zh},
                    "item_count": counts[section_id],
                    "supports_impact_filter": section_id != "latest-comments",
                }
                for section_id, name_en, name_zh in SECTION_DEFINITIONS
            ],
            "generated_at": datetime.now(UTC),
        }

    @router.get("/news")
    async def news(
        repo: Annotated[NewsRepository, Depends(repository)],
        section: Section,
        impact: Impact | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
    ) -> dict:
        keys = ("rank", "id") if section in ("latest", "hot") else ("time", "id")
        decoded = _require_cursor_keys(_decode_cursor(cursor, section), keys)
        rows, next_value = await repo.list_section(section, impact, limit, decoded)
        return {
            "items": [_article(row) for row in rows],
            "next_cursor": _encode_cursor(section, next_value),
            "generated_at": datetime.now(UTC),
            "source_updated_at": await source_updated_at(
                repo, "news_last_listing_success"
            ),
        }

    @router.get("/news/comments/latest")
    async def latest_comments(
        repo: Annotated[NewsRepository, Depends(repository)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
    ) -> dict:
        decoded = _require_cursor_keys(_decode_cursor(cursor, "latest-comments"), ("rank", "id"))
        rows, next_value = await repo.list_comments(None, limit, decoded)
        return {
            "items": [_comment(row) for row in rows],
            "next_cursor": _encode_cursor("latest-comments", next_value),
            "comments_complete": False,
            "generated_at": datetime.now(UTC),
            "source_updated_at": await source_updated_at(
                repo, "news_last_listing_success"
            ),
        }

    @router.get("/news/media/{media_id}")
    async def media(
        media_id: int, repo: Annotated[NewsRepository, Depends(repository)]
    ) -> FileResponse:
        cached = await repo.resolve_media_path(media_id)
        if cached is None:
            raise HTTPException(status_code=404, detail="Not found")
        root = settings.news_media_dir.resolve()
        path = cached.path.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(
            path,
            media_type=cached.mime_type,
            headers={
                "ETag": f'"{cached.sha256}"',
                "Cache-Control": "private, max-age=31536000, immutable",
            },
        )

    @router.get("/news/{source_id}/comments")
    async def article_comments(
        source_id: str,
        repo: Annotated[NewsRepository, Depends(repository)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
    ) -> dict:
        if await repo.get_article(source_id) is None:
            raise HTTPException(status_code=404, detail="Not found")
        decoded = _require_cursor_keys(
            _decode_cursor(cursor, f"comments:{source_id}"), ("position", "id")
        )
        rows, next_value = await repo.list_comments(source_id, limit, decoded)
        state = await repo.comment_collection_state(source_id)
        return {
            "items": [_comment(row) for row in rows],
            "next_cursor": _encode_cursor(f"comments:{source_id}", next_value),
            "comments_complete": state == "complete",
            "comments_state": state,
            "generated_at": datetime.now(UTC),
        }

    @router.get("/news/{source_id}")
    async def detail(source_id: str, repo: Annotated[NewsRepository, Depends(repository)]) -> dict:
        data = await repo.detail_data(source_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Not found")
        response = _article(data["article"])
        response.update(
            {
                "feeds": data["feeds"],
                "segments": [
                    {
                        "id": segment["id"],
                        "stable_key": segment["stable_key"],
                        "position": segment["position"],
                        "type": segment["segment_type"],
                        "author_name": segment.get("author_name"),
                        "author_handle": segment.get("author_handle"),
                        "published_at": segment.get("published_at"),
                        "published_at_source_text": segment.get("published_at_source_text"),
                        "text": {
                            "en": segment.get("text_en"),
                            "zh_hans": segment.get("text_zh"),
                        },
                        "source_url": segment.get("source_url"),
                        "is_excerpt": bool(segment.get("is_excerpt")),
                        "media": [_media(item) for item in segment["media"]],
                        "presentation": {
                            "mode": segment.get("display_mode", "full"),
                            "max_lines": segment.get("max_lines"),
                            "action_label": segment.get("external_action_label"),
                        },
                        "links": [
                            {
                                "id": link["id"],
                                "position": link["position"],
                                "kind": link["link_type"],
                                "label": link["label"],
                                "url": link["original_url"],
                            }
                            for link in segment["links"]
                        ],
                    }
                    for segment in data["segments"]
                ],
                "comment_count_collected": data["comment_count_collected"],
                "comments_state": data["article"].get("comments_state", "pending"),
                "comments_complete": data["article"].get("comments_state") == "complete",
                "comments_source_complete": bool(
                    data["article"].get("comments_source_complete", False)
                ),
                "comments_visible_count": data["article"].get("comments_visible_count"),
                "comments_count_mismatch": bool(
                    data["article"].get("comments_source_complete")
                    and data["article"].get("comments_visible_count")
                    != data["article"].get("comment_count")
                ),
                "generated_at": datetime.now(UTC),
            }
        )
        return response

    @router.get("/status")
    async def status(repo: Annotated[NewsRepository, Depends(repository)]) -> dict:
        result = await repo.status_counts()
        issues = []
        for worker in ("listing", "detail", "comment"):
            if result.get(f"last_{worker}_error"):
                issues.append(f"{worker}_error")
        for queue in ("detail_jobs", "comment_jobs", "media_jobs"):
            if result.get(queue, {}).get("failed", 0):
                issues.append(f"{queue}_failed")
        last_listing = result.get("last_listing_success")
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(last_listing)).total_seconds()
        except (TypeError, ValueError):
            age = None
        if age is None or age > max(300, settings.collect_interval_seconds * 5):
            issues.append("listing_stale")
        backfill = {}
        for section in DEFAULT_SECTIONS:
            raw = await repo.get_runtime_state(f"news_backfill:{section}")
            if raw:
                try:
                    backfill[section] = json.loads(raw)
                except ValueError:
                    backfill[section] = {"stop_reason": "invalid_checkpoint"}
        if result.get("comments_count_mismatch", 0):
            issues.append("comment_count_mismatch")
        if result.get("comment_states", {}).get("partial", 0):
            issues.append("comments_partial")
        result.update({
            "status": "degraded" if issues else "ok",
            "issues": issues,
            "model": settings.kimi_model,
            "listing_age_seconds": age,
            "backfill": backfill,
        })
        return result

    return router
