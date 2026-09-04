from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from contextlib import suppress
from pathlib import Path

import httpx

from app.news.models import CachedMedia, MediaJob
from app.news.repository import NewsRepository

ALLOWED_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


class MediaDownloadError(Exception):
    pass


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
    ) -> None:
        self.repository = repository
        self.directory = directory
        self.max_bytes = max_bytes
        self.client = client or httpx.AsyncClient(timeout=30, follow_redirects=True)
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
                await self.repository.fail_media_job(job.media_id, error)
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

    async def _download(self, job: MediaJob) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            async with self.client.stream("GET", job.original_url) as response:
                response.raise_for_status()
                declared_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if declared_type not in ALLOWED_TYPES:
                    raise MediaDownloadError("unsupported response content type")
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > self.max_bytes:
                            raise MediaDownloadError("response exceeds media byte limit")
                    except ValueError as error:
                        raise MediaDownloadError("invalid response content length") from error

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
            if existing:
                os.unlink(temporary)
                temporary = None
                await self._complete_from_existing(job, existing)
                return
            destination = self.directory / f"{sha256}{ALLOWED_TYPES[mime_type]}"
            os.replace(temporary, destination)
            temporary = None
            await self.repository.complete_media_job(
                job.media_id, str(destination), mime_type, byte_size, sha256
            )
        finally:
            if temporary is not None:
                with suppress(FileNotFoundError):
                    os.unlink(temporary)

    async def _complete_from_existing(
        self, job: MediaJob, existing: CachedMedia
    ) -> None:
        await self.repository.complete_media_job(
            job.media_id,
            str(existing.path),
            existing.mime_type,
            existing.byte_size,
            existing.sha256,
        )
