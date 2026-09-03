from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser, Node

from app.news.models import SourceDocumentObservation
from app.news.repository import NewsRepository
from app.news.snapshots import SnapshotStore

Resolver = Callable[[str], Awaitable[tuple[str, ...]]]
ARTICLE_TYPES = {"article", "newsarticle", "report", "analysisnewsarticle"}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
BLOCKED_STATUSES = {401, 403, 451}


class SourceDocumentError(Exception):
    pass


class SourceDocumentSecurityError(SourceDocumentError):
    pass


class SourceDocumentBlockedError(SourceDocumentError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"publisher returned {status_code}")
        self.status_code = status_code


class SourceDocumentExtractionError(SourceDocumentError):
    pass


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _paragraphs(value: str) -> tuple[str, ...]:
    parts = re.split(r"(?:\r?\n\s*){2,}", value)
    if len(parts) == 1:
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", value)
    return tuple(cleaned for item in parts if len(cleaned := _clean(item)) >= 20)


def _json_ld_nodes(value: object):
    if isinstance(value, list):
        for item in value:
            yield from _json_ld_nodes(item)
    elif isinstance(value, dict):
        graph = value.get("@graph")
        if graph is not None:
            yield from _json_ld_nodes(graph)
        yield value


def _type_names(value: object) -> set[str]:
    values = value if isinstance(value, list) else [value]
    return {str(item).lower() for item in values if item}


def _author_name(value: object) -> str | None:
    if isinstance(value, str):
        return _clean(value) or None
    if isinstance(value, dict):
        name = value.get("name")
        return _clean(str(name)) if name else None
    if isinstance(value, list):
        names = [name for item in value if (name := _author_name(item))]
        return ", ".join(names) or None
    return None


def _image_url(value: object, base_url: str) -> str | None:
    if isinstance(value, list) and value:
        return _image_url(value[0], base_url)
    if isinstance(value, dict):
        return _image_url(value.get("url") or value.get("contentUrl"), base_url)
    if isinstance(value, str) and value.strip():
        return urljoin(base_url, value.strip())
    return None


def _meta(tree: HTMLParser, selector: str) -> str | None:
    node = tree.css_first(selector)
    value = node.attributes.get("content", "") if node else ""
    return _clean(value) or None


def _validate_content(title: str, paragraphs: tuple[str, ...]) -> None:
    body_length = sum(len(item) for item in paragraphs)
    if not title or body_length < 120 or not paragraphs:
        raise SourceDocumentExtractionError("publisher page has no reliable article body")


def extract_source_document(
    html: str,
    final_url: str,
    fetched_at: datetime | None = None,
) -> SourceDocumentObservation:
    observed = fetched_at or datetime.now(UTC)
    tree = HTMLParser(html)
    for script in tree.css('script[type="application/ld+json"]'):
        raw = script.text(strip=True)
        if not raw:
            continue
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in _json_ld_nodes(decoded):
            if not (_type_names(item.get("@type")) & ARTICLE_TYPES):
                continue
            body = item.get("articleBody")
            title_value = item.get("headline") or item.get("name")
            if not isinstance(body, str) or not title_value:
                continue
            paragraphs = _paragraphs(body)
            title = _clean(str(title_value))
            _validate_content(title, paragraphs)
            return SourceDocumentObservation(
                original_url=final_url,
                final_url=final_url,
                source_host=urlparse(final_url).hostname or "",
                title_en=title,
                body_en="\n\n".join(paragraphs),
                paragraphs=paragraphs,
                fetched_at=observed,
                extraction_method="json_ld",
                author_name=_author_name(item.get("author")),
                published_at_source_text=str(item.get("datePublished"))
                if item.get("datePublished")
                else None,
                lead_image_url=_image_url(item.get("image"), final_url),
            )

    for selector in (
        "script",
        "style",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
        "noscript",
        "svg",
        ".advertisement",
        ".advert",
        ".related",
        ".recommended",
        ".newsletter",
    ):
        for node in tree.css(selector):
            node.decompose()
    candidates = tree.css(
        "article, [itemprop='articleBody'], .article-body, .story-body, "
        ".entry-content, .post-content, main"
    )
    if not candidates:
        raise SourceDocumentExtractionError("publisher page has no article container")
    container = max(candidates, key=_candidate_score)
    blocks: list[str] = []
    for node in container.traverse():
        if node.tag not in {"p", "h2", "h3", "li", "blockquote"}:
            continue
        value = _clean(node.text(separator=" ", strip=True))
        if len(value) >= 20 and (not blocks or blocks[-1] != value):
            blocks.append(value)
    paragraphs = tuple(blocks)
    title = (
        _meta(tree, 'meta[property="og:title"]')
        or _node_text(tree.css_first("h1"))
        or _node_text(tree.css_first("title"))
        or ""
    )
    _validate_content(title, paragraphs)
    return SourceDocumentObservation(
        original_url=final_url,
        final_url=final_url,
        source_host=urlparse(final_url).hostname or "",
        title_en=title,
        body_en="\n\n".join(paragraphs),
        paragraphs=paragraphs,
        fetched_at=observed,
        extraction_method="dom",
        author_name=_meta(tree, 'meta[name="author"]')
        or _node_text(tree.css_first('[rel="author"]')),
        published_at_source_text=_meta(tree, 'meta[property="article:published_time"]'),
        lead_image_url=_image_url(_meta(tree, 'meta[property="og:image"]'), final_url),
    )


def _node_text(node: Node | None) -> str | None:
    return _clean(node.text(separator=" ", strip=True)) or None if node else None


def _candidate_score(node: Node) -> int:
    paragraphs = node.css("p")
    return sum(len(_clean(item.text(separator=" ", strip=True))) for item in paragraphs)


async def default_resolver(host: str) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return tuple(sorted({str(record[4][0]) for record in records}))


async def validate_public_url(url: str, resolver: Resolver = default_resolver) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 80, 443}
    ):
        raise SourceDocumentSecurityError("publisher URL is not allowed")
    try:
        addresses = (parsed.hostname,) if _is_ip_literal(parsed.hostname) else await resolver(
            parsed.hostname
        )
    except (OSError, ValueError) as error:
        raise SourceDocumentSecurityError("publisher hostname cannot be validated") from error
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise SourceDocumentSecurityError("publisher URL resolves to a non-public address")
    return url


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class SourceDocumentFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        resolver: Resolver = default_resolver,
        *,
        max_bytes: int = 2_000_000,
        max_redirects: int = 5,
    ) -> None:
        self.client = client
        self.resolver = resolver
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects

    async def fetch(self, url: str) -> tuple[str, str, int]:
        current = url
        for redirect_count in range(self.max_redirects + 1):
            await validate_public_url(current, self.resolver)
            async with self.client.stream(
                "GET",
                current,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                    ),
                },
                follow_redirects=False,
            ) as response:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location or redirect_count >= self.max_redirects:
                        raise SourceDocumentError("publisher redirect limit exceeded")
                    current = urljoin(current, location)
                    continue
                if response.status_code in BLOCKED_STATUSES:
                    raise SourceDocumentBlockedError(response.status_code)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    raise SourceDocumentError("publisher response is not HTML")
                declared = response.headers.get("content-length")
                if declared and int(declared) > self.max_bytes:
                    raise SourceDocumentError("publisher response exceeds byte limit")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise SourceDocumentError("publisher response exceeds byte limit")
                encoding = response.encoding or "utf-8"
                return (
                    body.decode(encoding, errors="replace"),
                    str(response.url),
                    response.status_code,
                )
        raise SourceDocumentError("publisher redirect limit exceeded")


class SourceDocumentWorker:
    def __init__(
        self,
        repository: NewsRepository,
        fetcher: SourceDocumentFetcher,
        *,
        max_attempts: int = 5,
        snapshot_store: SnapshotStore | None = None,
    ) -> None:
        self.repository = repository
        self.fetcher = fetcher
        self.max_attempts = max_attempts
        self.snapshot_store = snapshot_store

    async def run_once(self, now: datetime | None = None, limit: int = 1) -> int:
        observed = now or datetime.now(UTC)
        jobs = await self.repository.claim_source_document_jobs(limit, observed)
        completed = 0
        for job in jobs:
            html: str | None = None
            try:
                html, final_url, status = await self.fetcher.fetch(job.original_url)
                document = extract_source_document(html, final_url, observed)
                document = replace(
                    document,
                    original_url=job.original_url,
                    http_status=status,
                )
                await self.repository.complete_source_document(job.document_id, document)
            except SourceDocumentBlockedError as error:
                await self.repository.fail_source_document(
                    job.document_id,
                    error,
                    observed,
                    self.max_attempts,
                    blocked=True,
                    http_status=error.status_code,
                )
            except Exception as error:
                await self.repository.fail_source_document(
                    job.document_id, error, observed, self.max_attempts
                )
            else:
                completed += 1
            if html is not None and self.snapshot_store:
                with suppress(Exception):
                    await self.snapshot_store.capture(
                        "source", str(job.document_id), html, observed
                    )
        return completed

    async def run(self, stop: asyncio.Event, interval: int = 5) -> None:
        while not stop.is_set():
            with suppress(Exception):
                await self.run_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue
