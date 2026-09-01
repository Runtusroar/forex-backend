import re
from datetime import datetime
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from app.domain import NewsDetail, NewsObservation
from app.parsers.errors import SourcePageError, reject_challenge

SOURCE_ROOT = "https://www.forexfactory.com"


def _text(node: Node, selector: str) -> str | None:
    found = node.css_first(selector)
    value = found.text(strip=True) if found else ""
    return value or None


def _source_id(url: str) -> str:
    match = re.search(r"/news/(\d+)", url)
    if not match:
        raise SourcePageError("news item missing numeric source identity")
    return match.group(1)


def parse_news_listing(html: str, now: datetime) -> list[NewsObservation]:
    reject_challenge(html)
    tree = HTMLParser(html)
    items: list[NewsObservation] = []
    for node in tree.css(".news__item"):
        link = node.css_first("a.news__title") or node.css_first("a[href*='/news/']")
        if not link:
            continue
        url = urljoin(SOURCE_ROOT, link.attributes.get("href", ""))
        title = link.text(strip=True)
        if not title:
            continue
        image = node.css_first("img")
        items.append(
            NewsObservation(
                source_id=_source_id(url),
                url=url,
                source=_text(node, ".news__source"),
                published_at=None,
                first_seen_at=now,
                title_en=title,
                summary_en=_text(node, ".news__preview"),
                body_en=None,
                image_url=urljoin(SOURCE_ROOT, image.attributes.get("src", "")) if image else None,
            )
        )
    if not items:
        raise SourcePageError("news page contains no items")
    return items


def parse_news_detail(html: str) -> NewsDetail:
    reject_challenge(html)
    tree = HTMLParser(html)
    article = next((node for node in tree.css(".news__article") if node.css_first("h1")), None)
    if not article:
        raise SourcePageError("news detail contains no article")
    social = article.css_first(".x-twitter-post-preview__text")
    image = article.css_first("img")
    image_url = urljoin(SOURCE_ROOT, image.attributes.get("src", "")) if image else None
    if social:
        return NewsDetail(kind="social", body_en=social.text(strip=True), image_url=image_url)
    copy = article.css_first(".news__copy")
    if not copy:
        raise SourcePageError("news article contains no body")
    paragraphs = [node.text(strip=True) for node in copy.css("p") if node.text(strip=True)]
    body = "\n\n".join(paragraphs) or copy.text(strip=True)
    return NewsDetail(kind="article", body_en=body, image_url=image_url)
