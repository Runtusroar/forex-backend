from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.db import Database
from app.news.models import (
    ArticleObservation,
    DetailObservation,
    NewsListingBatch,
    SegmentLinkObservation,
    SegmentObservation,
)
from app.news.repository import NewsRepository
from app.news.source_document import (
    SourceDocumentFetcher,
    SourceDocumentSecurityError,
    SourceDocumentWorker,
    extract_source_document,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/news_v2"
NOW = datetime(2026, 9, 3, tzinfo=UTC)


def test_json_ld_article_is_extracted_with_provenance() -> None:
    html = (FIXTURES / "source_jsonld.html").read_text()

    document = extract_source_document(html, "https://publisher.example/story", NOW)

    assert document.title_en == "Central bank keeps rates unchanged"
    assert document.author_name == "Alex Smith"
    assert document.published_at_source_text == "2026-09-03T10:00:00Z"
    assert document.lead_image_url == "https://publisher.example/lead.jpg"
    assert document.paragraphs == (
        "The central bank kept its benchmark rate unchanged after the September meeting.",
        "Officials said inflation continued to moderate while employment remained resilient.",
        "The committee will assess incoming data before deciding whether policy needs to "
        "change later this year.",
    )
    assert document.extraction_method == "json_ld"


def test_dom_fallback_excludes_page_chrome_and_preserves_order() -> None:
    html = (FIXTURES / "source_dom.html").read_text()

    document = extract_source_document(html, "https://news.example/markets/story", NOW)

    assert document.title_en == "Dollar declines after report"
    assert document.author_name == "Jamie Lee"
    assert document.lead_image_url == "https://news.example/images/dollar.jpg"
    assert document.paragraphs[0].startswith("The dollar declined")
    assert document.paragraphs[2] == "Markets reassess the outlook"
    assert all("Subscribe" not in paragraph for paragraph in document.paragraphs)
    assert all("advertisements" not in paragraph for paragraph in document.paragraphs)
    assert document.extraction_method == "dom"


async def public_resolver(host: str) -> tuple[str, ...]:
    return ("93.184.216.34",) if host != "private.example" else ("127.0.0.1",)


async def test_fetcher_rejects_private_redirect_before_second_request() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SourceDocumentFetcher(client, public_resolver, max_bytes=100_000)
    with pytest.raises(SourceDocumentSecurityError):
        await fetcher.fetch("https://publisher.example/story")
    await client.aclose()

    assert calls == ["https://publisher.example/story"]


async def test_worker_persists_success_and_marks_403_blocked(tmp_path: Path) -> None:
    database = Database(tmp_path / "source.sqlite3")
    await database.open()
    await database.initialize()
    assert database.connection is not None
    repository = NewsRepository(database.connection)
    await _seed_link(repository, "https://publisher.example/story")
    good_html = (FIXTURES / "source_jsonld.html").read_text()

    async def success(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=good_html, headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(success))
    worker = SourceDocumentWorker(
        repository,
        SourceDocumentFetcher(client, public_resolver, max_bytes=500_000),
        max_attempts=3,
    )
    assert await worker.run_once(NOW) == 1
    jobs = await repository.claim_source_document_jobs(1, NOW)
    assert jobs == []
    detail = await repository.detail_data("article-1")
    assert detail is not None
    document_id = detail["segments"][0]["links"][0]["source_document_id"]
    document = await repository.source_document_data(document_id)
    assert document is not None
    assert document["fetch_state"] == "complete"
    assert document["title_en"] == "Central bank keeps rates unchanged"
    await client.aclose()

    await _seed_link(repository, "https://blocked.example/story", article_id="article-2")

    async def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="blocked", headers={"content-type": "text/html"})

    blocked_client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
    blocked_worker = SourceDocumentWorker(
        repository,
        SourceDocumentFetcher(blocked_client, public_resolver, max_bytes=500_000),
        max_attempts=3,
    )
    assert await blocked_worker.run_once(NOW) == 0
    blocked_detail = await repository.detail_data("article-2")
    assert blocked_detail is not None
    blocked_id = blocked_detail["segments"][0]["links"][0]["source_document_id"]
    blocked = await repository.source_document_data(blocked_id)
    assert blocked is not None
    assert blocked["fetch_state"] == "blocked"
    assert blocked["http_status"] == 403
    await blocked_client.aclose()
    await database.close()


async def _seed_link(
    repository: NewsRepository,
    url: str,
    *,
    article_id: str = "article-1",
) -> None:
    article = ArticleObservation(
        article_id,
        f"https://www.forexfactory.com/news/{article_id}",
        "Title",
        NOW,
    )
    await repository.apply_listing(
        NewsListingBatch(
            articles=(article,),
            observed_at=NOW,
            source_hash=f"listing-{article_id}",
            source_timezone="Asia/Shanghai",
            observed_sections=frozenset({"latest"}),
        )
    )
    segment = SegmentObservation("body", 0, "article", text_en="Excerpt", is_excerpt=True)
    link = SegmentLinkObservation("link", "body", 0, "full_story", "full story", url)
    await repository.replace_detail(
        article_id,
        DetailObservation(article_id, NOW, f"detail-{article_id}", (segment,), (link,)),
    )
