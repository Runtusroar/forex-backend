from __future__ import annotations

import asyncio
import gzip
import hashlib
import os
import re
import tempfile
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from selectolax.parser import HTMLParser

from app.news.repository import NewsRepository


def _normalized_hash(html: str) -> str:
    tree = HTMLParser(html)
    for node in tree.css("script, style, noscript"):
        node.decompose()
    roots = tree.css(
        ".calendar__table, .news-block, .hot-stories, .news__article, .news-comments"
    )
    # Compare source content, not rotating ads, analytics or the page's server clock.
    content = "\n".join(node.html for node in roots) if roots else tree.html or html
    normalized = re.sub(r"\s+", " ", content).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")[:80] or "page"


def _safe_snapshot_path(directory: Path, stored_path: str) -> str | None:
    root = os.path.realpath(directory)
    path = os.path.realpath(stored_path)
    return path if os.path.commonpath((root, path)) == root else None


class SnapshotStore:
    def __init__(self, directory: Path, repository: NewsRepository) -> None:
        self.directory = directory
        self.repository = repository

    async def capture(
        self,
        page_type: str,
        page_key: str,
        html: str,
        captured_at: datetime | None = None,
        error: Exception | None = None,
    ) -> Path | None:
        captured = captured_at or datetime.now(UTC)
        fingerprint = hashlib.sha256(html.encode()).hexdigest() if error else _normalized_hash(html)
        # Keep one complete replay sample per day even if semantic content never changes.
        content_hash = hashlib.sha256(
            f"{captured.astimezone(UTC).date()}:{fingerprint}".encode()
        ).hexdigest()
        parse_status = "failed" if error else "success"
        if await self.repository.has_snapshot(
            page_type, page_key, content_hash, parse_status
        ):
            return None

        self.directory.mkdir(parents=True, exist_ok=True)
        timestamp = captured.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        filename = (
            f"{_safe(page_type)}-{_safe(page_key)}-{timestamp}-"
            f"{content_hash[:12]}.html.gz"
        )
        destination = self.directory / filename
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{filename}.", dir=self.directory
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                    compressed.write(html.encode())
                raw.flush()
                os.fsync(raw.fileno())
            os.replace(temporary, destination)
            await self.repository.record_snapshot(
                page_type=page_type,
                page_key=page_key,
                content_hash=content_hash,
                compressed_path=str(destination),
                captured_at=captured,
                parse_status=parse_status,
                error_type=type(error).__name__ if error else None,
            )
        except BaseException:
            for path in (temporary, destination):
                with suppress(FileNotFoundError):
                    os.unlink(path)
            raise
        return destination

    async def cleanup(
        self, retention_days: int, now: datetime | None = None
    ) -> int:
        current = now or datetime.now(UTC)
        expired = await self.repository.expired_snapshots(
            current - timedelta(days=retention_days)
        )
        removed_ids: list[int] = []
        for snapshot_id, stored_path in expired:
            path = _safe_snapshot_path(self.directory, stored_path)
            if path is None:
                continue
            with suppress(FileNotFoundError):
                os.unlink(path)
            removed_ids.append(snapshot_id)
        await self.repository.delete_snapshot_records(removed_ids)
        return len(removed_ids)

    async def run_cleanup(
        self, stop: asyncio.Event, retention_days: int = 30, interval: int = 3600
    ) -> None:
        while not stop.is_set():
            with suppress(Exception):
                await self.cleanup(retention_days)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue
