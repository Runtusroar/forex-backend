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
        return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
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
            return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
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
            return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
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
    worker = MediaWorker(media_repository, tmp_path / "files", max_bytes=1024, client=client)
    try:
        completed = await worker.run_once()
    finally:
        await client.aclose()

    assert completed == 0
    assert requests == []
    assert await media_repository.media_state(1) == "failed"


def browser_attachment_response(
    url, body=WEBP, content_type="image/webp", status=200, headers=None
):
    import base64
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    response = {
        "status": status,
        "headers": {"content-type": content_type, **(headers or {})},
        "bodyBase64": base64.b64encode(body).decode(),
    }
    page = SimpleNamespace(
        goto=AsyncMock(),
        evaluate=AsyncMock(return_value=response),
        close=AsyncMock(),
        route=AsyncMock(),
    )
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    session = SimpleNamespace(
        connect=AsyncMock(),
        browser=SimpleNamespace(contexts=[context]),
    )
    return session, page, response


@respx.mock
async def test_challenged_ff_attachment_uses_browser_and_closes_own_page(
    media_repository, tmp_path
):
    url = "https://www.forexfactory.com/attachment/image/5279811?d=1788356246"
    await add_media(media_repository, url)
    respx.get(url).mock(return_value=httpx.Response(403, text="Just a moment"))
    browser, page, _ = browser_attachment_response(url)
    worker = MediaWorker(media_repository, tmp_path / "files", 1024, browser=browser)
    try:
        assert await worker.run_once() == 1
        cached = await media_repository.resolve_media_path(1)
        assert cached.path.read_bytes() == WEBP
        assert cached.mime_type == "image/webp"
        assert cached.path.name == f"{cached.sha256}.webp"
        page.goto.assert_awaited_once_with(
            "https://www.forexfactory.com/news", wait_until="domcontentloaded", timeout=30000
        )
        page.close.assert_awaited_once()
    finally:
        await worker.close()


@respx.mock
@pytest.mark.parametrize(
    ("url", "status"),
    [
        ("https://other.example/attachment/image/1", 403),
        ("https://www.forexfactory.com/news/1", 403),
        ("https://www.forexfactory.com/attachment/image/1", 404),
    ],
)
async def test_browser_attachment_fallback_is_narrow(media_repository, tmp_path, url, status):
    await add_media(media_repository, url)
    respx.get(url).mock(return_value=httpx.Response(status))
    browser, _page, _ = browser_attachment_response(url)
    worker = MediaWorker(media_repository, tmp_path / "files", 1024, browser=browser)
    try:
        assert await worker.run_once() == 0
        browser.connect.assert_not_awaited()
        assert not list((tmp_path / "files").iterdir())
    finally:
        await worker.close()


@respx.mock
@pytest.mark.parametrize(
    ("body", "content_type", "status", "headers"),
    [
        (b"<html>challenge</html>", "text/html", 200, {}),
        (b"not an image", "image/png", 200, {}),
        (WEBP + b"x" * 1024, "image/webp", 200, {}),
        (WEBP, "image/webp", 200, {"content-length": "2048"}),
        (WEBP, "image/webp", 403, {}),
    ],
)
async def test_browser_attachment_rejects_invalid_payload_and_closes_page(
    media_repository,
    tmp_path,
    body,
    content_type,
    status,
    headers,
):
    url = "https://www.forexfactory.com/attachment/image/1"
    await add_media(media_repository, url)
    respx.get(url).mock(return_value=httpx.Response(403))
    browser, page, _ = browser_attachment_response(url, body, content_type, status, headers)
    worker = MediaWorker(media_repository, tmp_path / "files", 1024, browser=browser)
    try:
        assert await worker.run_once() == 0
        page.close.assert_awaited_once()
        assert not list((tmp_path / "files").iterdir())
    finally:
        await worker.close()


@respx.mock
async def test_browser_attachment_blocks_unrelated_redirect_and_closes_on_timeout(
    media_repository,
    tmp_path,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    url = "https://www.forexfactory.com/attachment/image/1"
    await add_media(media_repository, url)
    respx.get(url).mock(return_value=httpx.Response(403))
    browser, page, _ = browser_attachment_response(url)
    page.goto.side_effect = TimeoutError("navigation timeout")
    worker = MediaWorker(media_repository, tmp_path / "files", 1024, browser=browser)
    try:
        assert await worker.run_once() == 0
        handler = page.route.call_args.args[1]
        blocked = SimpleNamespace(
            request=SimpleNamespace(url="https://other.example/attachment/image/1"),
            abort=AsyncMock(),
            continue_=AsyncMock(),
        )
        await handler(blocked)
        blocked.abort.assert_awaited_once()
        blocked.continue_.assert_not_awaited()
        page.close.assert_awaited_once()
    finally:
        await worker.close()


async def test_browser_stream_rejects_redirect_cancels_oversize_and_times_out():
    import asyncio
    import json

    from playwright._impl._driver import compute_driver_executable

    from app.news import media

    script = (
        "const download = "
        + media.BROWSER_ATTACHMENT_FETCH
        + ";\n"
        + r"""
const http = require('node:http');
(async () => {
  let redirectedRequests = 0;
  const server = http.createServer((req, res) => {
    if (req.url === '/redirect') {
      res.writeHead(302, {Location: '/private'}); res.end();
    } else if (req.url === '/private') {
      redirectedRequests++; res.end('private');
    } else {
      res.writeHead(200, {'content-type':'image/png'});
      res.flushHeaders();
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = 'http://127.0.0.1:' + server.address().port;
  let redirectRejected = false, timedOut = false;
  try {await download({url: origin + '/redirect', maxBytes: 32, timeoutMs: 1000})}
  catch {redirectRejected = true}
  try {await download({url: origin + '/stall', maxBytes: 32, timeoutMs: 30})}
  catch {timedOut = true}
  server.closeAllConnections(); server.close();
  let readCalls = 0, cancelled = false;
  globalThis.fetch = async () => ({status:200,
    headers:new Headers({'content-type':'image/png'}),
    body:{getReader:()=>({
      read:async()=>{readCalls++; return {done:false,value:new Uint8Array(20)}},
      cancel:async()=>{cancelled=true}, releaseLock:()=>{},
    })}
  });
  let oversized = false;
  try {await download({url:'https://www.forexfactory.com/attachment/image/1',
    maxBytes:32,timeoutMs:1000})} catch {oversized=true}
  console.log(JSON.stringify({redirectRejected, redirectedRequests, timedOut,
    oversized, cancelled, readCalls}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
    )
    process = await asyncio.create_subprocess_exec(
        str(compute_driver_executable()[0]),
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    assert process.returncode == 0, stderr.decode()
    assert json.loads(stdout) == {
        "redirectRejected": True,
        "redirectedRequests": 0,
        "timedOut": True,
        "oversized": True,
        "cancelled": True,
        "readCalls": 2,
    }
