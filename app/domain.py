from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CalendarObservation:
    source_id: str
    event_at: datetime
    currency: str
    impact: str
    title_en: str
    actual: str | None
    forecast: str | None
    previous: str | None
    source_time_text: str | None = None
    source_position: int = 0


@dataclass(frozen=True, slots=True)
class NewsObservation:
    source_id: str
    url: str
    source: str | None
    published_at: datetime | None
    first_seen_at: datetime
    title_en: str
    summary_en: str | None
    body_en: str | None
    image_url: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarRecord(CalendarObservation):
    title_zh: str | None
    source_hash: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NewsRecord(NewsObservation):
    title_zh: str | None
    summary_zh: str | None
    body_zh: str | None
    source_hash: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TranslationJob:
    id: int
    entity_type: str
    entity_id: str
    source_hash: str
    payload: dict[str, str | None]
    attempts: int


@dataclass(frozen=True, slots=True)
class NewsDetail:
    kind: str
    body_en: str
    image_url: str | None = None
