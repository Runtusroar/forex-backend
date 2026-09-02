from __future__ import annotations

import gzip
import hashlib
import os
import re
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from app.news.repository import NewsRepository


def _normalized_hash(html: str) -> str:
    normalized = re.sub(r"\s+", " ", html).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")[:80] or "page"


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
        content_hash = _normalized_hash(html)
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
        except Exception:
            for path in (temporary, destination):
                with suppress(FileNotFoundError):
                    os.unlink(path)
            raise
        return destination
