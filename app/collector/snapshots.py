"""Best-effort source evidence; snapshot storage never replaces a collection error."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

logger = logging.getLogger(__name__)


class SourceSnapshots(Protocol):
    async def capture(
        self,
        page_type: str,
        page_key: str,
        html: str,
        captured_at: datetime | None = None,
        error: Exception | None = None,
    ) -> object: ...


async def capture_snapshot(
    store: SourceSnapshots | None,
    page_type: str,
    page_key: str,
    html: str | None,
    observed_at: datetime,
    error: Exception | None = None,
) -> None:
    if store is None or html is None:
        return
    try:
        await store.capture(page_type, page_key, html, captured_at=observed_at, error=error)
    except Exception:
        logger.exception("source snapshot capture failed for %s %s", page_type, page_key)
