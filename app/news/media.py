from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import os
import tempfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from app.news.models import CachedMedia, MediaJob
from app.news.repository import NewsRepository

if TYPE_CHECKING:
    from app.collector.browser import BrowserSession


ALLOWED_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


ATTACHMENT_ORIGIN_PAGE = "https://www.forexfactory.com/news"
BROWSER_ATTACHMENT_FETCH = r"""async ({url, maxBytes, timeoutMs}) => {
    const response = await fetch(url, {
        redirect: 'error', signal: AbortSignal.timeout(timeoutMs),
    });
    const mime = (response.headers.get('content-type') || '').split(';')[0].toLowerCase();
    const length = response.headers.get('content-length');
    if (response.status !== 200 || !['image/png','image/jpeg','image/webp','image/gif']
            .includes(mime) || (length !== null &&
            (!/^\d+$/.test(length) || Number(length) > maxBytes))) {
        if (response.body) await response.body.cancel();
        throw new Error('Invalid browser attachment response');
    }
    if (!response.body) throw new Error('Empty attachment response');
    const reader = response.body.getReader();
    const parts = [];
    let byteSize = 0;
    try {
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            byteSize += value.byteLength;
            if (byteSize > maxBytes) {
                await reader.cancel();
                throw new Error('Attachment exceeds media byte limit');
            }
            for (let offset = 0; offset < value.length; offset += 32768) {
                parts.push(String.fromCharCode(...value.subarray(offset, offset + 32768)));
            }
        }
    } finally {
        reader.releaseLock();
    }
    return {status: response.status, headers: {'content-type': mime},
        bodyBase64: btoa(parts.join(''))};
}"""


class MediaDownloadError(Exception):
    pass


def _validate_media_url(url: str) -> None:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise MediaDownloadError("invalid media URL") from exc
    host = parsed.host
    if parsed.scheme != "https" or not host:
        raise MediaDownloadError("media URL must use HTTPS")
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise MediaDownloadError("private media URL is not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise MediaDownloadError("private media URL is not allowed")


async def _validate_media_request(request: httpx.Request) -> None:
    _validate_media_url(str(request.url))


def _is_ff_attachment(url: str) -> bool:
    parsed = httpx.URL(url)
    return (
        parsed.scheme == "https"
        and parsed.host == "www.forexfactory.com"
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
        and parsed.path.startswith("/attachment/image/")
    )


def _mime_type_from_signature(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


class MediaWorker:
    def __init__(
        self,
        repository: NewsRepository,
        directory: Path,
        max_bytes: int,
        client: httpx.AsyncClient | None = None,
        *,
        browser: BrowserSession | None = None,
    ) -> None:
        self.browser = browser
        self.repository = repository
        self.directory = directory
        self.max_bytes = max_bytes
        self.client = client or httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            event_hooks={"request": [_validate_media_request]},
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def run_once(self, limit: int = 2) -> int:
        jobs = await self.repository.claim_media_jobs(limit)
        completed = 0
        for job in jobs:
            try:
                await self._download(job)
            except Exception as error:
                await self.repository.fail_media_job(job.media_id, job.original_url, error)
            else:
                completed += 1
        return completed

    async def run(self, stop: asyncio.Event, interval: int = 5) -> None:
        while not stop.is_set():
            with suppress(Exception):
                await self.run_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    def _validate_headers(self, headers: httpx.Headers | dict[str, str]) -> None:
        declared_type = headers.get("content-type", "").split(";", 1)[0].lower()
        if declared_type not in ALLOWED_TYPES:
            raise MediaDownloadError("unsupported response content type")
        content_length = headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    raise MediaDownloadError("response exceeds media byte limit")
            except ValueError as error:
                raise MediaDownloadError("invalid response content length") from error

    async def _browser_attachment(self, url: str) -> httpx.Response:
        assert self.browser is not None
        await self.browser.connect()
        assert self.browser.browser is not None
        page = await self.browser.browser.contexts[0].new_page()
        try:

            async def attachment_only(route):
                if route.request.url == ATTACHMENT_ORIGIN_PAGE:
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        body="<!doctype html><title>Attachment fetch</title>",
                    )
                elif _is_ff_attachment(route.request.url):
                    await route.continue_()
                else:
                    await route.abort()

            # Establish a same-origin document without loading source scripts or resources.
            await page.route("**/*", attachment_only)
            await page.goto(ATTACHMENT_ORIGIN_PAGE, wait_until="domcontentloaded", timeout=30000)
            result = await page.evaluate(
                BROWSER_ATTACHMENT_FETCH,
                {"url": url, "maxBytes": self.max_bytes, "timeoutMs": 30000},
            )
            if result["status"] != 200:
                raise MediaDownloadError("browser attachment response was unsuccessful")
            self._validate_headers(result["headers"])
            body = base64.b64decode(result["bodyBase64"], validate=True)
            if len(body) > self.max_bytes:
                raise MediaDownloadError("response exceeds media byte limit")
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": result["headers"]["content-type"]},
                request=httpx.Request("GET", url),
            )
        finally:
            await page.close()

    @asynccontextmanager
    async def _response(self, url: str):
        async with self.client.stream("GET", url) as response:
            if (
                self.browser is not None
                and response.status_code == 403
                and _is_ff_attachment(url)
                and _is_ff_attachment(str(response.url))
            ):
                yield await self._browser_attachment(url)
            else:
                yield response

    async def _download(self, job: MediaJob) -> None:
        _validate_media_url(job.original_url)
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            async with self._response(job.original_url) as response:
                response.raise_for_status()
                self._validate_headers(response.headers)

                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".download-", dir=self.directory
                )
                temporary = Path(temporary_name)
                digest = hashlib.sha256()
                byte_size = 0
                header = bytearray()
                with os.fdopen(descriptor, "wb") as output:
                    async for chunk in response.aiter_bytes():
                        byte_size += len(chunk)
                        if byte_size > self.max_bytes:
                            raise MediaDownloadError("response exceeds media byte limit")
                        digest.update(chunk)
                        if len(header) < 16:
                            header.extend(chunk[: 16 - len(header)])
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                mime_type = _mime_type_from_signature(bytes(header))
                if not byte_size or mime_type not in ALLOWED_TYPES:
                    raise MediaDownloadError("response bytes do not match supported image type")

            sha256 = digest.hexdigest()
            existing = await self.repository.completed_media_by_hash(sha256)
            if existing and existing.path.is_file():
                os.unlink(temporary)
                temporary = None
                await self._complete_from_existing(job, existing)
                return
            destination = self.directory / f"{sha256}{ALLOWED_TYPES[mime_type]}"
            os.replace(temporary, destination)
            temporary = None
            await self.repository.complete_media_job(
                job.media_id,
                job.original_url,
                str(destination),
                mime_type,
                byte_size,
                sha256,
            )
        finally:
            if temporary is not None:
                with suppress(FileNotFoundError):
                    os.unlink(temporary)

    async def _complete_from_existing(self, job: MediaJob, existing: CachedMedia) -> None:
        await self.repository.complete_media_job(
            job.media_id,
            job.original_url,
            str(existing.path),
            existing.mime_type,
            existing.byte_size,
            existing.sha256,
        )
