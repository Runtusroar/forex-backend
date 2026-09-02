from __future__ import annotations

from datetime import datetime
from typing import Any

from app.news.repository import NewsRepository


def _base(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": article["source_id"],
        "url": article["ff_url"],
        "source": article.get("source_name"),
        "published_at": article.get("published_at"),
        "first_seen_at": article["first_seen_at"],
        "title_en": article["title_en"],
        "title_zh": article.get("title_zh"),
        "summary_en": article.get("teaser_en"),
        "summary_zh": article.get("teaser_zh"),
        "body_en": None,
        "body_zh": None,
        "image_url": None,
        "updated_at": article["updated_at"],
    }


async def v1_news_list(
    repository: NewsRepository, limit: int, before: datetime | None
) -> list[dict[str, Any]]:
    articles = await repository.list_articles(limit, before)
    items: list[dict[str, Any]] = []
    for article in articles:
        data = await repository.detail_data(str(article["source_id"]))
        items.append(_from_detail(data) if data is not None else _base(article))
    return items


def _from_detail(data: dict[str, Any]) -> dict[str, Any]:
    result = _base(data["article"])
    english = [
        segment["text_en"] for segment in data["segments"] if segment.get("text_en")
    ]
    chinese = [
        segment["text_zh"] for segment in data["segments"] if segment.get("text_zh")
    ]
    result["body_en"] = "\n\n".join(english) or None
    result["body_zh"] = "\n\n".join(chinese) or None
    for segment in data["segments"]:
        for media in segment["media"]:
            if media["download_state"] == "complete":
                result["image_url"] = f"/api/v2/news/media/{media['id']}"
                return result
    return result


async def v1_news_detail(
    repository: NewsRepository, source_id: str
) -> dict[str, Any] | None:
    data = await repository.detail_data(source_id)
    if data is None:
        return None
    return _from_detail(data)
