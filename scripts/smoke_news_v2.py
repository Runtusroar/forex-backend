from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any

import httpx

REQUIRED_SECTIONS = {
    "latest",
    "hot",
    "fundamental",
    "technical",
    "industry",
    "entertainment",
    "educational",
    "latest-comments",
}
VALID_IMPACTS = {None, "high", "medium", "low"}


class ContractError(ValueError):
    pass


def _aware_timestamp(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_contract(
    sections: dict[str, Any],
    listing: dict[str, Any],
    detail: dict[str, Any] | None,
) -> None:
    section_ids = {item.get("id") for item in sections.get("items", [])}
    missing = REQUIRED_SECTIONS - section_ids
    if missing:
        raise ContractError(f"missing sections: {','.join(sorted(missing))}")
    items = listing.get("items")
    if not isinstance(items, list):
        raise ContractError("listing items must be an array")
    identities = [item.get("source_id") for item in items]
    if None in identities or len(identities) != len(set(identities)):
        raise ContractError("listing source IDs are missing or duplicated")
    for item in items:
        if not _aware_timestamp(item.get("published_at")):
            raise ContractError("listing contains an invalid or naive timestamp")
        if item.get("breaking_impact") not in VALID_IMPACTS:
            raise ContractError("listing contains an invalid impact")
    if detail is None:
        return
    if identities and detail.get("source_id") not in identities:
        raise ContractError("detail source ID does not match listing")
    segments = detail.get("segments")
    if not isinstance(segments, list):
        raise ContractError("detail segments must be an array")
    positions = [segment.get("position") for segment in segments]
    if positions != sorted(positions) or len(positions) != len(set(positions)):
        raise ContractError("detail segments are out of order or duplicated")
    for segment in segments:
        for media in segment.get("media", []):
            media_url = media.get("url")
            if media_url is not None and not str(media_url).startswith(
                "/api/v2/news/media/"
            ):
                raise ContractError("detail exposes an unsafe media URL")


async def run_smoke(base_url: str, api_key: str) -> dict[str, int]:
    headers = {"X-API-Key": api_key}
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), headers=headers, timeout=30
    ) as client:
        section_response = await client.get("/api/v2/news/sections")
        section_response.raise_for_status()
        listing_response = await client.get(
            "/api/v2/news", params={"section": "latest", "limit": 20}
        )
        listing_response.raise_for_status()
        sections = section_response.json()
        listing = listing_response.json()
        detail = None
        items = listing.get("items", [])
        if items:
            detail_response = await client.get(
                f"/api/v2/news/{items[0]['source_id']}"
            )
            detail_response.raise_for_status()
            detail = detail_response.json()
        validate_contract(sections, listing, detail)
        media_checked = 0
        if detail:
            media_url = next(
                (
                    media.get("url")
                    for segment in detail["segments"]
                    for media in segment.get("media", [])
                    if media.get("url")
                ),
                None,
            )
            if media_url:
                media_response = await client.get(media_url)
                media_response.raise_for_status()
                if not media_response.headers.get("content-type", "").startswith("image/"):
                    raise ContractError("media response is not an image")
                if not media_response.content:
                    raise ContractError("media response is empty")
                media_checked = 1
        return {
            "sections": len(sections["items"]),
            "articles": len(items),
            "details": int(detail is not None),
            "media": media_checked,
        }


def main() -> None:
    base_url = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000")
    api_key = os.environ.get("APP_API_KEY")
    if not api_key:
        raise SystemExit("APP_API_KEY is required")
    try:
        counts = asyncio.run(run_smoke(base_url, api_key))
    except (httpx.HTTPError, ContractError) as error:
        raise SystemExit(f"News V2 smoke failed: {type(error).__name__}") from error
    print(
        "News V2 smoke passed: "
        + ", ".join(f"{name}={count}" for name, count in counts.items())
    )


if __name__ == "__main__":
    main()
