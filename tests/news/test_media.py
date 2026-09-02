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
