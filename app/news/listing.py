from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import datetime
from typing import cast
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from selectolax.parser import HTMLParser, Node

from app.news.models import (
    ArticleObservation,
    BreakingImpact,
    CategoryObservation,
    CategorySlug,
    CommentObservation,
    FeedObservation,
    NewsListingBatch,
)
from app.parsers.errors import SourcePageError, reject_challenge
from app.parsers.source_time import SourceTime, wall_time_utc

SOURCE_ROOT = "https://www.forexfactory.com"
CATEGORY_HEADINGS: dict[str, CategorySlug] = {
    "Fundamental Analysis / Latest Stories": "fundamental",
    "Technical Analysis / Latest Stories": "technical",
    "Forex Industry News / Latest Stories": "industry",
    "Entertainment News / Latest Stories": "entertainment",
    "Educational News / Latest Stories": "educational",
}


def _text(node: Node, selector: str) -> str | None:
    found = node.css_first(selector)
    text = found.text(strip=True) if found else ""
    return re.sub(r"\s+", " ", text).strip() or None


def _source_id(url: str) -> str:
    match = re.search(r"/news/(\d+)", url)
    if not match:
        raise SourcePageError("news item missing numeric source identity")
    return match.group(1)


def _published(node: Node, zone: ZoneInfo) -> tuple[datetime | None, str | None]:
    time = node.css_first("span.nowrap[title], span[title]")
    raw_source_text = time.attributes.get("title") if time else None
    source_text = raw_source_text.strip() if raw_source_text else ""
    if not source_text:
        return None, None
    for pattern in ("%b %d, %Y, %I:%M%p", "%b %d, %Y %I:%M%p"):
        try:
            parsed = datetime.strptime(source_text, pattern)
        except ValueError:
            continue
        return wall_time_utc(parsed, zone), source_text
    return None, source_text


def _impact(node: Node) -> BreakingImpact | None:
    classes = " ".join(item.attributes.get("class", "") for item in node.css("[class*='impact']"))
    for value in ("high", "medium", "low"):
        if f"impact-ff-{value}" in classes:
            return cast(BreakingImpact, value)
    return None


def _comment_count(node: Node) -> int | None:
    text = _text(node, "a[data-comments-link]") or ""
    match = re.search(r"(\d[\d,]*)\s+comments?", text, re.I)
    return int(match.group(1).replace(",", "")) if match else None


def _comment_author(node: Node) -> str:
    commenter = node.css_first(".news-block__commenter")
    if commenter:
        username = commenter.css_first("a[href*='/member/'], a")
        if username:
            raw_username = re.sub(
                r"\s+", " ", username.text(separator=" ", strip=True)
            ).strip()
            name = re.sub(r"\s*commented\b.*$", "", raw_username, flags=re.I).strip()
            if name:
                return name
        raw = re.sub(r"\s+", " ", commenter.text(separator=" ", strip=True)).strip()
        name = re.sub(r"\s*commented\b.*$", "", raw, flags=re.I).strip()
        if name:
            return name
    return _text(node, ".news-block__comment-author") or "Unknown"


def _article(node: Node, observed_at: datetime, zone: ZoneInfo) -> ArticleObservation | None:
    link = node.css_first(".news-block__title a[href*='/news/']")
    if not link:
        return None
    href = link.attributes.get("href", "")
    title = re.sub(r"\s+", " ", link.text(strip=True)).strip()
    if not href or not title:
        return None
    published_at, source_text = _published(node, zone)
    source_link = node.css_first(".news-block__details a[href*='/hit']")
    source_name = (
        source_link.text(strip=True).removeprefix("From ").strip() if source_link else None
    )
    image = node.css_first(".news-block__image img")
    image_url = urljoin(SOURCE_ROOT, image.attributes.get("src", "")) if image else None
    comment_count = _comment_count(node)
    return ArticleObservation(
        source_id=_source_id(href),
        ff_url=urljoin(SOURCE_ROOT, href),
        title_en=title,
        observed_at=observed_at,
        teaser_en=_text(node, ".news-block__preview"),
        source_name=source_name or None,
        source_url=urljoin(SOURCE_ROOT, source_link.attributes.get("href", ""))
        if source_link
        else None,
        published_at=published_at,
        published_at_source_text=source_text,
        source_timezone=zone.key,
        breaking_impact=_impact(node),
        comment_count=comment_count or 0,
        comment_count_observed=comment_count is not None,
        listing_thumbnail_url=image_url,
    )


def _merge(old: ArticleObservation, new: ArticleObservation) -> ArticleObservation:
    return replace(
        old,
        teaser_en=new.teaser_en or old.teaser_en,
        source_name=new.source_name or old.source_name,
        source_url=new.source_url or old.source_url,
        published_at=new.published_at or old.published_at,
        published_at_source_text=new.published_at_source_text or old.published_at_source_text,
        breaking_impact=new.breaking_impact or old.breaking_impact,
        comment_count=(new.comment_count if new.comment_count_observed else old.comment_count),
        comment_count_observed=(old.comment_count_observed or new.comment_count_observed),
        listing_thumbnail_url=new.listing_thumbnail_url or old.listing_thumbnail_url,
    )


def parse_news_listing_v2(
    html: str, observed_at: datetime, source_timezone: ZoneInfo
) -> NewsListingBatch:
    reject_challenge(html)
    tree = HTMLParser(html)
    source_time = SourceTime.from_tree(tree, source_timezone, observed_at)
    source_timezone = cast(ZoneInfo, source_time.zone)
    articles: dict[str, ArticleObservation] = {}
    categories: list[CategoryObservation] = []
    feeds: list[FeedObservation] = []
    comments: list[CommentObservation] = []
    observed_sections: set[str] = set()

    for rank, node in enumerate(tree.css(".hot-stories .hot-story")):
        link = node.css_first("a.hot-story__title[href*='/news/']")
        if not link:
            continue
        observed_sections.add("hot")
        href = link.attributes.get("href", "")
        article_id = _source_id(href)
        published_at, source_text = _published(node, source_timezone)
        source_link = node.css_first(".hot-story__details a[href*='/hit']")
        source_name = (
            source_link.text(strip=True).removeprefix("From ").strip() if source_link else None
        )
        comment_count = _comment_count(node)
        article = ArticleObservation(
            source_id=article_id,
            ff_url=urljoin(SOURCE_ROOT, href),
            title_en=link.text(strip=True),
            observed_at=observed_at,
            source_name=source_name or None,
            source_url=urljoin(SOURCE_ROOT, source_link.attributes.get("href", ""))
            if source_link
            else None,
            published_at=published_at,
            published_at_source_text=source_text,
            source_timezone=source_timezone.key,
            breaking_impact=_impact(node),
            comment_count=comment_count or 0,
            comment_count_observed=comment_count is not None,
        )
        articles[article_id] = article
        feeds.append(FeedObservation(article_id, "hot", rank, observed_at))
    if tree.css_first(".hot-stories"):
        observed_sections.add("hot")

    for block in tree.css(".news-block"):
        heading = _text(block, "h2") or ""
        if heading == "News / Latest Comments":
            observed_sections.add("latest_comments")
            for rank, node in enumerate(block.css(".news-block__item--comment")):
                link = node.css_first("a[href*='/comment/']")
                article_link = node.css_first(
                    ".news-block__title[href*='/news/']"
                ) or node.css_first(".news-block__title a[href*='/news/']")
                if not link or not article_link:
                    continue
                comment_match = re.search(r"/comment/(\d+)", link.attributes.get("href", ""))
                if not comment_match:
                    continue
                article_href = article_link.attributes.get("href", "")
                article_id = _source_id(article_href)
                if article_id not in articles:
                    articles[article_id] = ArticleObservation(
                        source_id=article_id,
                        ff_url=urljoin(SOURCE_ROOT, article_href),
                        title_en=re.sub(r"\s+", " ", article_link.text(strip=True)).strip(),
                        observed_at=observed_at,
                        source_timezone=source_timezone.key,
                        comment_count_observed=False,
                    )
                comments.append(
                    CommentObservation(
                        comment_id=comment_match.group(1),
                        article_id=article_id,
                        author_name=_comment_author(node),
                        text_en=(
                            _text(node, ".news-block__preview")
                            or _text(node, ".news-block__comment-message")
                            or ""
                        ),
                        permalink=urljoin(SOURCE_ROOT, link.attributes.get("href", "")),
                        observed_at=observed_at,
                        feed_rank=rank,
                        observation_quality="listing",
                    )
                )
            continue
        category = CATEGORY_HEADINGS.get(heading)
        is_latest = heading == "News / Latest Stories"
        if not category and not is_latest:
            continue
        section = category or "latest"
        observed_sections.add(section)
        for rank, node in enumerate(block.css(".news-block__item:not(.news-block__item--comment)")):
            article = _article(node, observed_at, source_timezone)
            if not article:
                continue
            articles[article.source_id] = (
                _merge(articles[article.source_id], article)
                if article.source_id in articles
                else article
            )
            if is_latest:
                feeds.append(FeedObservation(article.source_id, "latest", rank, observed_at))
            elif category:
                categories.append(CategoryObservation(article.source_id, category, observed_at))

    if "latest" not in observed_sections or not any(row.feed_type == "latest" for row in feeds):
        raise SourcePageError("news page contains no Latest Stories")
    for article in articles.values():
        source_time.validate(article.published_at)
    return NewsListingBatch(
        articles=tuple(articles.values()),
        categories=tuple(categories),
        feeds=tuple(feeds),
        comments=tuple(comments),
        observed_at=observed_at,
        source_hash=hashlib.sha256(html.encode()).hexdigest(),
        source_timezone=source_timezone.key,
        observed_sections=frozenset(observed_sections),
    )
