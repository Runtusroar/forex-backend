from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from app.db import Database
from app.news.media import MediaWorker
from app.news.models import (
    ArticleObservation,
    DetailObservation,
    MediaObservation,
    NewsListingBatch,
    SegmentObservation,
)
from app.news.repository import NewsRepository

NOW = datetime(2026, 9, 3, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"image bytes"
WEBP = b"RIFF\x10\x00\x00\x00WEBP" + b"image bytes"


class ChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"\x89PNG\r\n\x1a\n"
        yield b"x" * 40


@pytest.fixture
async def media_repository(tmp_path: Path):
    database = Database(tmp_path / "media.sqlite3")
    await database.open()
    await database.initialize()
    assert database.connection is not None
    repository = NewsRepository(database.connection)
    yield repository
    await database.close()


async def add_media(repository: NewsRepository, *urls: str) -> None:
    article = ArticleObservation("1", "https://www.forexfactory.com/news/1-x", "X", NOW)
    await repository.apply_listing(
        NewsListingBatch(
            (article,),
            NOW,
            "listing",
            "Asia/Shanghai",
            frozenset({"latest"}),
        )
    )
    await repository.replace_detail(
        "1",
        DetailObservation(
            "1",
            NOW,
            "detail",
            segments=(SegmentObservation("body", 0, "article", text_en="Body"),),
            media=tuple(
                MediaObservation(f"media-{index}", index, "image", url)
                for index, url in enumerate(urls)
            ),
        ),
    )


@respx.mock
async def test_valid_image_is_cached_with_content_hash_name(
    media_repository: NewsRepository, tmp_path: Path
) -> None:
    url = "https://assets.example/chart.png"
    await add_media(media_repository, url)
    respx.get(url).mock(
        return_value=httpx.Response(
            200, content=PNG, headers={"content-type": "image/png"}
        )
    )
    worker = MediaWorker(media_repository, tmp_path / "files", max_bytes=1024)

    assert await worker.run_once() == 1
    cached = await media_repository.resolve_media_path(1)
    assert cached is not None
    assert cached.path.read_bytes() == PNG
    assert cached.path.name == f"{cached.sha256}.png"


@respx.mock
async def test_mislabeled_forex_factory_asset_is_cached_by_byte_signature(
    media_repository: NewsRepository, tmp_path: Path
) -> None:
    url = "https://assets.faireconomy.media/nfs/public/5/2/3/4/7/7/2/5234772.png"
    await add_media(media_repository, url)
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            content=WEBP,
            headers={"content-type": "image/png"},
        )
    )
    worker = MediaWorker(media_repository, tmp_path / "files", max_bytes=1024)

    assert await worker.run_once() == 1
    cached = await media_repository.resolve_media_path(1)
    assert cached is not None
    assert cached.path.read_bytes() == WEBP
    assert cached.path.name == f"{cached.sha256}.webp"
    assert cached.mime_type == "image/webp"


@respx.mock
@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"content-type": "image/png", "content-length": "2048"}, PNG),
        ({"content-type": "text/html"}, b"<html>blocked</html>"),
        ({"content-type": "image/png"}, b"<html>not really png</html>"),
    ],
)
async def test_invalid_download_fails_without_partial_file(
    media_repository: NewsRepository,
    tmp_path: Path,
    headers: dict[str, str],
    body: bytes,
) -> None:
    url = "https://assets.example/invalid"
    await add_media(media_repository, url)
    respx.get(url).mock(return_value=httpx.Response(200, content=body, headers=headers))
    directory = tmp_path / "files"
    worker = MediaWorker(media_repository, directory, max_bytes=32)

    assert await worker.run_once() == 0
    assert not list(directory.glob("*"))
    assert await media_repository.media_state(1) == "failed"


@respx.mock
async def test_identical_bytes_share_one_cached_path(
    media_repository: NewsRepository, tmp_path: Path
) -> None:
    first = "https://assets.example/one.png"
    second = "https://assets.example/two.png"
    await add_media(media_repository, first, second)
    for url in (first, second):
        respx.get(url).mock(
            return_value=httpx.Response(
                200, content=PNG, headers={"content-type": "image/png"}
            )
        )
    worker = MediaWorker(media_repository, tmp_path / "files", max_bytes=1024)

    assert await worker.run_once() == 2
    cached = [await media_repository.resolve_media_path(media_id) for media_id in (1, 2)]
    assert cached[0] is not None and cached[1] is not None
    assert cached[0].path == cached[1].path
    assert len(list((tmp_path / "files").glob("*"))) == 1


@respx.mock
async def test_stream_limit_and_timeout_are_retryable_without_temp_files(
    media_repository: NewsRepository, tmp_path: Path
) -> None:
    streamed = "https://assets.example/streamed.png"
    timed_out = "https://assets.example/timeout.png"
    await add_media(media_repository, streamed, timed_out)
    respx.get(streamed).mock(
        return_value=httpx.Response(
            200, stream=ChunkStream(), headers={"content-type": "image/png"}
        )
    )
    respx.get(timed_out).mock(side_effect=httpx.ReadTimeout("timeout"))
    directory = tmp_path / "files"
    worker = MediaWorker(media_repository, directory, max_bytes=32)

    assert await worker.run_once() == 0
    assert await media_repository.media_state(1) == "failed"
    assert await media_repository.media_state(2) == "failed"
    assert not list(directory.glob("*"))


async def test_stale_download_cannot_complete_media_after_url_changes(
    media_repository: NewsRepository, tmp_path: Path
) -> None:
    old_url = "https://assets.example/old.png"
    new_url = "https://assets.example/new.png"
    await add_media(media_repository, old_url)
    job = (await media_repository.claim_media_jobs(1, NOW))[0]
    await add_media(media_repository, new_url)

    applied = await media_repository.complete_media_job(
        job.media_id,
        job.original_url,
        str(tmp_path / "old.png"),
        "image/png",
        len(PNG),
        "old-hash",
    )
    jobs = await media_repository.claim_media_jobs(1, NOW)

    assert applied is False
    assert [item.original_url for item in jobs] == [new_url]


@respx.mock
async def test_missing_deduplicated_file_is_replaced_by_fresh_download(
    media_repository: NewsRepository, tmp_path: Path
) -> None:
    first = "https://assets.example/one.png"
    second = "https://assets.example/two.png"
    await add_media(media_repository, first, second)
    for url in (first, second):
        respx.get(url).mock(
            return_value=httpx.Response(
                200, content=PNG, headers={"content-type": "image/png"}
            )
        )
    worker = MediaWorker(media_repository, tmp_path / "files", max_bytes=1024)
    assert await worker.run_once(limit=1) == 1
    first_cached = await media_repository.resolve_media_path(1)
    assert first_cached is not None
    first_cached.path.unlink()

    assert await worker.run_once(limit=1) == 1
    second_cached = await media_repository.resolve_media_path(2)

    assert second_cached is not None
    assert second_cached.path.is_file()
    assert second_cached.path.read_bytes() == PNG


async def test_private_media_url_is_rejected_before_request(
    media_repository: NewsRepository, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    url = "http://127.0.0.1/internal.png"
    await add_media(media_repository, url)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    worker = MediaWorker(
        media_repository, tmp_path / "files", max_bytes=1024, client=client
    )
    try:
        completed = await worker.run_once()
    finally:
        await client.aclose()

    assert completed == 0
    assert requests == []
    assert await media_repository.media_state(1) == "failed"
