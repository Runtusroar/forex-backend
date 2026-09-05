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
class CalendarHistoryObservation:
    release_date_text: str
    event_url: str | None
    actual: str | None
    forecast: str | None
    previous: str | None
    actual_state: str | None = None
    previous_state: str | None = None
    previous_revised_from: str | None = None


@dataclass(frozen=True, slots=True)
class CalendarRelatedStoryObservation:
    title_en: str
    ff_url: str
    source_name: str | None
    source_url: str | None
    published_at_source_text: str | None
    preview: str | None


@dataclass(frozen=True, slots=True)
class CalendarDetailObservation:
    source_id: str
    title_en: str
    currency: str | None
    currency_name: str | None
    impact: str | None
    actual: str | None
    forecast: str | None
    previous: str | None
    actual_state: str | None
    previous_state: str | None
    previous_revised_from: str | None
    ff_url: str | None
    source_name: str | None
    source_url: str | None
    latest_release_url: str | None
    measures: str | None
    usual_effect: str | None
    frequency: str | None
    next_release_text: str | None
    next_release_url: str | None
    ff_notes: str | None
    why_traders_care: str | None
    history: tuple[CalendarHistoryObservation, ...] = ()
    related_stories: tuple[CalendarRelatedStoryObservation, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarDetailRecord(CalendarDetailObservation):
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CalendarDetailJob:
    source_id: str
    event_at: datetime
    desired_source_hash: str
    priority: int
    attempts: int
    claimed_at: datetime


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
