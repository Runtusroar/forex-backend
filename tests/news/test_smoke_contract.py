from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.smoke_news_v2 import ContractError, validate_contract


def valid_payloads():
    sections = {
        "items": [
            {"id": value}
            for value in (
                "latest",
                "hot",
                "fundamental",
                "technical",
                "industry",
                "entertainment",
                "educational",
                "latest-comments",
            )
        ]
    }
    listing = {
        "items": [
            {
                "source_id": "100",
                "published_at": "2026-09-03T00:00:00Z",
                "breaking_impact": "high",
            }
        ]
    }
    detail = {
        "source_id": "100",
        "segments": [
            {
                "position": 0,
                "media": [],
                "presentation": {
                    "mode": "full",
                    "max_lines": None,
                    "action_label": None,
                },
                "links": [],
            },
            {
                "position": 1,
                "media": [{"url": "/api/v2/news/media/1"}],
                "presentation": {
                    "mode": "clamped",
                    "max_lines": 10,
                    "action_label": "Show More",
                },
                "links": [
                    {
                        "kind": "full_story",
                        "label": "full story",
                        "url": "https://publisher.example/story",
                    }
                ],
            },
        ],
    }
    return sections, listing, detail


def test_valid_contract_passes() -> None:
    validate_contract(*valid_payloads())


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_section",
        "naive_timestamp",
        "duplicate_id",
        "invalid_impact",
        "unordered_segments",
        "unsafe_media_url",
        "missing_presentation",
        "publisher_document_leak",
    ],
)
def test_contract_rejects_semantic_corruption(corruption: str) -> None:
    sections, listing, detail = deepcopy(valid_payloads())
    if corruption == "missing_section":
        sections["items"].pop()
    elif corruption == "naive_timestamp":
        listing["items"][0]["published_at"] = "2026-09-03T00:00:00"
    elif corruption == "duplicate_id":
        listing["items"].append(dict(listing["items"][0]))
    elif corruption == "invalid_impact":
        listing["items"][0]["breaking_impact"] = "critical"
    elif corruption == "unordered_segments":
        detail["segments"][0]["position"] = 2
    elif corruption == "unsafe_media_url":
        detail["segments"][1]["media"][0]["url"] = "file:///app/data/media/x.png"
    elif corruption == "missing_presentation":
        detail["segments"][1].pop("presentation")
    else:
        detail["segments"][1]["links"][0]["source_document"] = {"id": 7}

    with pytest.raises(ContractError):
        validate_contract(sections, listing, detail)
