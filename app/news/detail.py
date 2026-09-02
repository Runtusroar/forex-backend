from __future__ import annotations

import hashlib
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from selectolax.parser import HTMLParser, Node

from app.news.models import (
    CommentObservation,
    DetailObservation,
    MediaObservation,
    SegmentObservation,
)
from app.parsers.errors import SourcePageError, reject_challenge

SOURCE_ROOT = "https://www.forexfactory.com"


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _key(*values: str | None) -> str:
    return hashlib.sha256("\n".join(value or "" for value in values).encode()).hexdigest()


def _comment_id(node: Node) -> tuple[str, str] | None:
    link = node.css_first("a[href*='/comment/']")
    if not link:
        return None
    href = link.attributes.get("href", "")
    match = re.search(r"/comment/(\d+)", href)
    return (match.group(1), urljoin(SOURCE_ROOT, href)) if match else None


def _parent_comment_id(node: Node) -> str | None:
    parent = node.parent
    while parent is not None:
        if "news-comment" in parent.attributes.get("class", "").split():
            identity = _comment_id(parent)
            return identity[0] if identity else None
        parent = parent.parent
    return None


def parse_news_detail_v2(
    html: str,
    article_id: str,
    observed_at: datetime,
    source_timezone: ZoneInfo,
) -> DetailObservation:
    reject_challenge(html)
    tree = HTMLParser(html)
    segments: list[SegmentObservation] = []
    media: list[MediaObservation] = []
    for position, article in enumerate(tree.css(".news__article")):
        social = article.css_first(".x-twitter-post-preview__text")
        if social:
            text = _clean(social.text(separator=" ", strip=True))
            author = article.css_first(".x-twitter-post-preview__name")
            handle = article.css_first(".x-twitter-post-preview__username")
            body_link = article.css_first("a.x-twitter-post-preview__body")
            source_url = body_link.attributes.get("href") if body_link else None
            segment = SegmentObservation(
                stable_key=_key("social", source_url, text),
                position=position,
                segment_type="social",
                text_en=text,
                author_name=_clean(author.text(strip=True)) if author else None,
                author_handle=_clean(handle.text(strip=True)) if handle else None,
                source_url=source_url,
            )
        else:
            copy = article.css_first(".news__copy")
            if not copy:
                continue
            paragraphs = [_clean(node.text(separator=" ", strip=True)) for node in copy.css("p")]
            text = "\n\n".join(value for value in paragraphs if value) or _clean(
                copy.text(separator=" ", strip=True)
            )
            full_story = next(
                (link for link in copy.css("a") if "full story" in link.text(strip=True).lower()),
                None,
            )
            source_url = full_story.attributes.get("href") if full_story else None
            segment = SegmentObservation(
                stable_key=_key("article", source_url, text),
                position=position,
                segment_type="article",
                text_en=text,
                source_url=source_url,
                is_excerpt=full_story is not None,
            )
        segments.append(segment)
        for media_position, attachment in enumerate(article.css("a.attachthumb")):
            href = attachment.attributes.get("href", "")
            if not href:
                continue
            label = article.css_first(".flexposts__storylabel")
            kind = "chart" if label and "chart" in label.text(strip=True).lower() else "attachment"
            caption = article.css_first(".flexposts__attachments-title, .title")
            media.append(
                MediaObservation(
                    stable_key=_key(segment.stable_key, href),
                    position=media_position,
                    media_type=kind,
                    original_url=urljoin(SOURCE_ROOT, href),
                    segment_key=segment.stable_key,
                    caption=_clean(caption.text(strip=True)) if caption else None,
                )
            )
    if not segments:
        raise SourcePageError("news detail contains no story segments")

    comments: list[CommentObservation] = []
    for node in tree.css(".news-comments__list .news-comment"):
        identity = _comment_id(node)
        message = node.css_first(".news-comment__comment-message")
        if not identity or not message:
            continue
        author = node.css_first(".news-comment__header-username")
        source_time = node.css_first(".news-comment__header-date")
        comments.append(
            CommentObservation(
                comment_id=identity[0],
                article_id=article_id,
                parent_comment_id=_parent_comment_id(node),
                author_name=_clean(author.text(strip=True)) if author else "Unknown",
                text_en=_clean(message.text(separator=" ", strip=True)),
                permalink=identity[1],
                observed_at=observed_at,
                published_at_source_text=(
                    _clean(source_time.text(strip=True)) if source_time else None
                ),
            )
        )
    return DetailObservation(
        article_id=article_id,
        observed_at=observed_at,
        source_hash=hashlib.sha256(html.encode()).hexdigest(),
        segments=tuple(segments),
        media=tuple(media),
        comments=tuple(comments),
        is_complete=True,
    )
