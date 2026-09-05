from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

BreakingImpact = Literal["high", "medium", "low"]
CategorySlug = Literal["fundamental", "technical", "industry", "entertainment", "educational"]
FeedType = Literal["latest", "hot"]
SegmentType = Literal["article", "social", "update", "quote", "link"]
SegmentLinkType = Literal["full_story"]
SegmentDisplayMode = Literal["full", "clamped"]
MediaType = Literal["image", "chart", "attachment"]
CommentObservationQuality = Literal["listing", "detail"]


@dataclass(frozen=True, slots=True)
class ArticleObservation:
    source_id: str
    ff_url: str
    title_en: str
    observed_at: datetime
    teaser_en: str | None = None
    source_name: str | None = None
    source_url: str | None = None
    published_at: datetime | None = None
    published_at_source_text: str | None = None
    source_timezone: str | None = None
    breaking_impact: BreakingImpact | None = None
    comment_count: int = 0
    comment_count_observed: bool = True
    is_excerpt: bool = False
    listing_thumbnail_url: str | None = None


@dataclass(frozen=True, slots=True)
class CategoryObservation:
    article_id: str
    category: CategorySlug
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class FeedObservation:
    article_id: str
    feed_type: FeedType
    rank: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class CommentObservation:
    comment_id: str
    article_id: str
    author_name: str
    text_en: str
    permalink: str
    observed_at: datetime
    parent_comment_id: str | None = None
    published_at: datetime | None = None
    published_at_source_text: str | None = None
    reaction_count: int | None = None
    feed_rank: int | None = None
    position: int = 0
    depth: int = 0
    observation_quality: CommentObservationQuality = "detail"


@dataclass(frozen=True, slots=True)
class SegmentObservation:
    stable_key: str
    position: int
    segment_type: SegmentType
    text_en: str | None = None
    author_name: str | None = None
    author_handle: str | None = None
    published_at: datetime | None = None
    published_at_source_text: str | None = None
    source_url: str | None = None
    is_excerpt: bool = False
    display_mode: SegmentDisplayMode = "full"
    max_lines: int | None = None
    external_action_label: str | None = None


@dataclass(frozen=True, slots=True)
class SegmentLinkObservation:
    stable_key: str
    segment_key: str
    position: int
    kind: SegmentLinkType
    label: str
    url: str


@dataclass(frozen=True, slots=True)
class MediaObservation:
    stable_key: str
    position: int
    media_type: MediaType
    original_url: str
    segment_key: str | None = None
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class DetailObservation:
    article_id: str
    observed_at: datetime
    source_hash: str
    segments: tuple[SegmentObservation, ...] = ()
    links: tuple[SegmentLinkObservation, ...] = ()
    media: tuple[MediaObservation, ...] = ()
    comments: tuple[CommentObservation, ...] = ()
    is_complete: bool = True


@dataclass(frozen=True, slots=True)
class CommentCollectionObservation:
    article_id: str
    observed_at: datetime
    expected_count: int
    comments: tuple[CommentObservation, ...] = ()
    is_complete: bool = False


@dataclass(frozen=True, slots=True)
class NewsListingBatch:
    articles: tuple[ArticleObservation, ...]
    observed_at: datetime
    source_hash: str
    source_timezone: str
    observed_sections: frozenset[str]
    categories: tuple[CategoryObservation, ...] = ()
    feeds: tuple[FeedObservation, ...] = ()
    comments: tuple[CommentObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class ListingApplyResult:
    article_count: int
    new_article_ids: tuple[str, ...] = ()
    changed_article_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DetailJob:
    article_id: str
    ff_url: str
    desired_source_hash: str
    priority: int
    attempts: int
    claimed_at: datetime


@dataclass(frozen=True, slots=True)
class CommentJob:
    article_id: str
    ff_url: str
    expected_count: int
    expected_count_observed: bool
    priority: int
    attempts: int
    claimed_at: datetime


@dataclass(frozen=True, slots=True)
class MediaJob:
    media_id: int
    article_id: str
    original_url: str
    attempts: int


@dataclass(frozen=True, slots=True)
class CachedMedia:
    media_id: int
    path: Path
    mime_type: str
    byte_size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class LocalizedTextJob:
    id: int
    entity_type: str
    entity_id: str
    field_name: str
    source_text: str
    source_hash: str
    attempts: int


@dataclass(frozen=True, slots=True)
class ArticleRecord:
    source_id: str
    ff_url: str
    title_en: str
    teaser_en: str | None
    source_name: str | None
    source_url: str | None
    published_at: datetime | None
    published_at_source_text: str | None
    source_timezone: str | None
    breaking_impact: BreakingImpact | None
    comment_count: int
    detail_state: str
    is_excerpt: bool
    first_seen_at: datetime
    last_seen_at: datetime
    updated_at: datetime
    categories: tuple[CategorySlug, ...] = field(default_factory=tuple)
