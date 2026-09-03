from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

import aiosqlite

from app.news.models import (
    ArticleObservation,
    ArticleRecord,
    CachedMedia,
    CommentObservation,
    DetailJob,
    DetailObservation,
    FeedType,
    ListingApplyResult,
    LocalizedTextJob,
    MediaJob,
    NewsListingBatch,
    SourceDocumentJob,
    SourceDocumentObservation,
)


def _serialized_write(method):
    @wraps(method)
    async def wrapper(self, *args, **kwargs):
        async with self.write_lock:
            return await method(self, *args, **kwargs)

    return wrapper


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _hash(article: ArticleObservation) -> str:
    values = (
        article.title_en,
        article.teaser_en,
        article.source_name,
        article.source_url,
        _iso(article.published_at),
        article.breaking_impact,
        str(article.comment_count),
        article.listing_thumbnail_url,
    )
    return hashlib.sha256("\n".join(value or "" for value in values).encode()).hexdigest()


class NewsRepository:
    def __init__(
        self,
        connection: aiosqlite.Connection,
        write_lock: asyncio.Lock | None = None,
    ) -> None:
        self.db = connection
        self.write_lock = write_lock or asyncio.Lock()

    @_serialized_write
    async def apply_listing(self, batch: NewsListingBatch) -> ListingApplyResult:
        new_ids: list[str] = []
        changed_ids: list[str] = []
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            hashes: dict[str, str] = {}
            for article in batch.articles:
                source_hash = _hash(article)
                hashes[article.source_id] = source_hash
                current = await self.db.execute_fetchall(
                    "SELECT source_hash FROM news_articles WHERE source_id=?",
                    (article.source_id,),
                )
                if not current:
                    new_ids.append(article.source_id)
                elif current[0]["source_hash"] != source_hash:
                    changed_ids.append(article.source_id)
                await self._upsert_article(article, source_hash)

            for category in batch.categories:
                observed = _iso(category.observed_at)
                await self.db.execute(
                    """INSERT INTO news_category_memberships
                       (article_id,category,first_seen_at,last_seen_at) VALUES (?,?,?,?)
                       ON CONFLICT(article_id,category) DO UPDATE SET
                         last_seen_at=excluded.last_seen_at""",
                    (category.article_id, category.category, observed, observed),
                )

            if "latest_comments" in batch.observed_sections:
                await self.db.execute(
                    "UPDATE news_comment_feed SET is_current=0 WHERE is_current=1"
                )
            for comment in batch.comments:
                await self._upsert_comment(comment)
                if comment.feed_rank is not None:
                    await self.db.execute(
                        """INSERT INTO news_comment_feed
                           (comment_id,rank,first_seen_at,last_seen_at,is_current)
                           VALUES (?,?,?,?,1)
                           ON CONFLICT(comment_id) DO UPDATE SET
                             rank=excluded.rank,last_seen_at=excluded.last_seen_at,
                             is_current=1""",
                        (
                            comment.comment_id,
                            comment.feed_rank,
                            _iso(comment.observed_at),
                            _iso(comment.observed_at),
                        ),
                    )

            for feed_type in ("latest", "hot"):
                if feed_type in batch.observed_sections:
                    await self._apply_feed(batch, feed_type)

            for article_id in (*new_ids, *changed_ids):
                article = next(item for item in batch.articles if item.source_id == article_id)
                priority = 100 if article.breaking_impact == "high" else 10
                observed = _iso(batch.observed_at)
                await self.db.execute(
                    """INSERT INTO news_detail_jobs
                       (article_id,priority,state,attempts,next_attempt_at,desired_source_hash)
                       VALUES (?,?,'pending',0,?,?)
                       ON CONFLICT(article_id) DO UPDATE SET
                         priority=max(news_detail_jobs.priority,excluded.priority),
                         state='pending',attempts=0,next_attempt_at=excluded.next_attempt_at,
                         desired_source_hash=excluded.desired_source_hash,last_error=NULL""",
                    (article_id, priority, observed, hashes[article_id]),
                )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return ListingApplyResult(
            article_count=len(batch.articles),
            new_article_ids=tuple(new_ids),
            changed_article_ids=tuple(changed_ids),
        )

    async def _upsert_article(self, article: ArticleObservation, source_hash: str) -> None:
        observed = _iso(article.observed_at)
        await self.db.execute(
            """INSERT INTO news_articles
               (source_id,ff_url,title_en,teaser_en,source_name,source_url,published_at,
                published_at_source_text,source_timezone,breaking_impact,comment_count,
                detail_state,is_excerpt,listing_thumbnail_url,source_hash,
                first_seen_at,last_seen_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?)
               ON CONFLICT(source_id) DO UPDATE SET
                 ff_url=excluded.ff_url,title_en=excluded.title_en,
                 teaser_en=COALESCE(excluded.teaser_en,news_articles.teaser_en),
                 source_name=COALESCE(excluded.source_name,news_articles.source_name),
                 source_url=COALESCE(excluded.source_url,news_articles.source_url),
                 published_at=COALESCE(excluded.published_at,news_articles.published_at),
                 published_at_source_text=COALESCE(
                   excluded.published_at_source_text,news_articles.published_at_source_text),
                 source_timezone=COALESCE(excluded.source_timezone,news_articles.source_timezone),
                 breaking_impact=COALESCE(excluded.breaking_impact,news_articles.breaking_impact),
                 comment_count=max(excluded.comment_count,news_articles.comment_count),
                 is_excerpt=excluded.is_excerpt,
                 listing_thumbnail_url=COALESCE(
                   excluded.listing_thumbnail_url,news_articles.listing_thumbnail_url),
                 source_hash=excluded.source_hash,last_seen_at=excluded.last_seen_at,
                 updated_at=excluded.updated_at""",
            (
                article.source_id,
                article.ff_url,
                article.title_en,
                article.teaser_en,
                article.source_name,
                article.source_url,
                _iso(article.published_at),
                article.published_at_source_text,
                article.source_timezone,
                article.breaking_impact,
                article.comment_count,
                int(article.is_excerpt),
                article.listing_thumbnail_url,
                source_hash,
                observed,
                observed,
                observed,
            ),
        )
        await self._enqueue_localized(
            "article", article.source_id, "title", article.title_en, observed
        )
        await self._enqueue_localized(
            "article", article.source_id, "teaser", article.teaser_en, observed
        )

    async def _apply_feed(self, batch: NewsListingBatch, feed_type: FeedType) -> None:
        observed_at = _iso(batch.observed_at)
        observations = {row.article_id: row for row in batch.feeds if row.feed_type == feed_type}
        current_rows = await self.db.execute_fetchall(
            "SELECT * FROM news_feed_placements WHERE feed_type=? AND is_current=1",
            (feed_type,),
        )
        current = {str(row["article_id"]): row for row in current_rows}
        for article_id, observation in observations.items():
            previous = current.get(article_id)
            event_type: str | None = None
            previous_rank: int | None = None
            if previous is None:
                event_type = "entered"
            elif previous["rank"] != observation.rank:
                event_type = "moved"
                previous_rank = int(previous["rank"])
            await self.db.execute(
                """INSERT INTO news_feed_placements
                   (article_id,feed_type,rank,first_seen_at,last_seen_at,is_current,absence_count)
                   VALUES (?,?,?,?,?,1,0)
                   ON CONFLICT(article_id,feed_type) DO UPDATE SET
                     rank=excluded.rank,last_seen_at=excluded.last_seen_at,
                     is_current=1,absence_count=0""",
                (article_id, feed_type, observation.rank, observed_at, observed_at),
            )
            if event_type:
                await self._feed_event(
                    article_id,
                    feed_type,
                    event_type,
                    previous_rank,
                    observation.rank,
                    observed_at,
                )
        for article_id, row in current.items():
            if article_id in observations:
                continue
            absence_count = int(row["absence_count"]) + 1
            if absence_count >= 3:
                await self.db.execute(
                    """UPDATE news_feed_placements
                       SET is_current=0,absence_count=?,last_seen_at=?
                       WHERE article_id=? AND feed_type=?""",
                    (absence_count, observed_at, article_id, feed_type),
                )
                await self._feed_event(
                    article_id,
                    feed_type,
                    "left",
                    int(row["rank"]),
                    None,
                    observed_at,
                )
            else:
                await self.db.execute(
                    """UPDATE news_feed_placements SET absence_count=?
                       WHERE article_id=? AND feed_type=?""",
                    (absence_count, article_id, feed_type),
                )

    async def _feed_event(
        self,
        article_id: str,
        feed_type: FeedType,
        event_type: str,
        previous_rank: int | None,
        new_rank: int | None,
        observed_at: str | None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO news_feed_events
               (article_id,feed_type,event_type,previous_rank,new_rank,observed_at)
               VALUES (?,?,?,?,?,?)""",
            (article_id, feed_type, event_type, previous_rank, new_rank, observed_at),
        )

    async def count_articles(self) -> int:
        rows = await self.db.execute_fetchall("SELECT count(*) AS count FROM news_articles")
        return int(rows[0]["count"])

    @_serialized_write
    async def claim_detail_jobs(
        self, limit: int, now: datetime | None = None
    ) -> list[DetailJob]:
        claimed_at = now or datetime.now(UTC)
        ready_at = _iso(claimed_at)
        expired_lease = _iso(claimed_at - timedelta(minutes=5))
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = await self.db.execute_fetchall(
                """SELECT j.*,a.ff_url FROM news_detail_jobs j
                   JOIN news_articles a ON a.source_id=j.article_id
                   WHERE (j.state='pending' AND j.next_attempt_at<=?)
                      OR (j.state='processing' AND j.claimed_at<?)
                   ORDER BY j.priority DESC,j.next_attempt_at,j.article_id LIMIT ?""",
                (ready_at, expired_lease, limit),
            )
            if rows:
                await self.db.executemany(
                    """UPDATE news_detail_jobs SET state='processing',claimed_at=?
                       WHERE article_id=?""",
                    [(ready_at, row["article_id"]) for row in rows],
                )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return [
            DetailJob(
                article_id=row["article_id"],
                ff_url=row["ff_url"],
                desired_source_hash=row["desired_source_hash"],
                priority=int(row["priority"]),
                attempts=int(row["attempts"]),
                claimed_at=claimed_at,
            )
            for row in rows
        ]

    @_serialized_write
    async def complete_detail_job(
        self, article_id: str, desired_source_hash: str | None = None
    ) -> None:
        if desired_source_hash is None:
            await self.db.execute(
                """UPDATE news_detail_jobs SET state='done',claimed_at=NULL,last_error=NULL
                   WHERE article_id=?""",
                (article_id,),
            )
        else:
            await self.db.execute(
                """UPDATE news_detail_jobs SET state='done',claimed_at=NULL,last_error=NULL
                   WHERE article_id=? AND desired_source_hash=?""",
                (article_id, desired_source_hash),
            )
        await self.db.commit()

    @_serialized_write
    async def fail_detail_job(
        self,
        article_id: str,
        error: Exception,
        now: datetime | None = None,
        max_attempts: int = 8,
        desired_source_hash: str | None = None,
    ) -> None:
        failed_at = now or datetime.now(UTC)
        if desired_source_hash is None:
            rows = await self.db.execute_fetchall(
                "SELECT attempts FROM news_detail_jobs WHERE article_id=?", (article_id,)
            )
        else:
            rows = await self.db.execute_fetchall(
                """SELECT attempts FROM news_detail_jobs
                   WHERE article_id=? AND desired_source_hash=?""",
                (article_id, desired_source_hash),
            )
        if not rows:
            return
        attempts = int(rows[0]["attempts"]) + 1
        delay_minutes = (1, 5, 30, 120, 360)[min(attempts - 1, 4)]
        state = "failed" if attempts >= max_attempts else "pending"
        parameters = (
            state,
            attempts,
            _iso(failed_at + timedelta(minutes=delay_minutes)),
            type(error).__name__,
            article_id,
        )
        if desired_source_hash is None:
            await self.db.execute(
                """UPDATE news_detail_jobs SET state=?,attempts=?,next_attempt_at=?,
                   claimed_at=NULL,last_error=? WHERE article_id=?""",
                parameters,
            )
        else:
            await self.db.execute(
                """UPDATE news_detail_jobs SET state=?,attempts=?,next_attempt_at=?,
                   claimed_at=NULL,last_error=?
                   WHERE article_id=? AND desired_source_hash=?""",
                (*parameters, desired_source_hash),
            )
        await self.db.commit()

    async def detail_job_state(self, article_id: str) -> str | None:
        rows = await self.db.execute_fetchall(
            "SELECT state FROM news_detail_jobs WHERE article_id=?", (article_id,)
        )
        return str(rows[0]["state"]) if rows else None

    @_serialized_write
    async def claim_source_document_jobs(
        self, limit: int, now: datetime | None = None
    ) -> list[SourceDocumentJob]:
        claimed = now or datetime.now(UTC)
        ready = _iso(claimed)
        expired_lease = _iso(claimed - timedelta(minutes=5))
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = await self.db.execute_fetchall(
                """SELECT id,original_url,attempts FROM news_source_documents
                   WHERE (fetch_state='pending' AND next_attempt_at<=?)
                      OR (fetch_state='processing' AND claimed_at<?)
                   ORDER BY attempts,id LIMIT ?""",
                (ready, expired_lease, limit),
            )
            if rows:
                await self.db.executemany(
                    """UPDATE news_source_documents
                       SET fetch_state='processing',claimed_at=?,updated_at=? WHERE id=?""",
                    [(ready, ready, row["id"]) for row in rows],
                )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return [
            SourceDocumentJob(
                document_id=int(row["id"]),
                original_url=str(row["original_url"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    @_serialized_write
    async def complete_source_document(
        self, document_id: int, document: SourceDocumentObservation
    ) -> None:
        observed = _iso(document.fetched_at)
        content_hash = hashlib.sha256(
            "\n".join((document.final_url, document.title_en, document.body_en)).encode()
        ).hexdigest()
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            await self.db.execute(
                """UPDATE news_source_documents SET
                   final_url=?,source_host=?,title_en=?,author_name=?,
                   published_at_source_text=?,lead_image_url=?,paragraphs_json=?,body_en=?,
                   extraction_method=?,content_hash=?,fetch_state='complete',attempts=attempts+1,
                   next_attempt_at=?,claimed_at=NULL,http_status=?,last_error=NULL,
                   last_fetched_at=?,updated_at=? WHERE id=?""",
                (
                    document.final_url,
                    document.source_host,
                    document.title_en,
                    document.author_name,
                    document.published_at_source_text,
                    document.lead_image_url,
                    json.dumps(document.paragraphs, ensure_ascii=False),
                    document.body_en,
                    document.extraction_method,
                    content_hash,
                    _iso(document.fetched_at + timedelta(days=1)),
                    document.http_status,
                    observed,
                    observed,
                    document_id,
                ),
            )
            await self._enqueue_localized(
                "source_document", str(document_id), "title", document.title_en, observed
            )
            await self._enqueue_localized(
                "source_document", str(document_id), "body", document.body_en, observed
            )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise

    @_serialized_write
    async def fail_source_document(
        self,
        document_id: int,
        error: Exception,
        now: datetime | None = None,
        max_attempts: int = 5,
        *,
        blocked: bool = False,
        http_status: int | None = None,
    ) -> None:
        failed = now or datetime.now(UTC)
        rows = await self.db.execute_fetchall(
            "SELECT attempts FROM news_source_documents WHERE id=?", (document_id,)
        )
        if not rows:
            return
        attempts = int(rows[0]["attempts"]) + 1
        delay = (5, 30, 120, 360, 1440)[min(attempts - 1, 4)]
        state = "blocked" if blocked else ("failed" if attempts >= max_attempts else "pending")
        await self.db.execute(
            """UPDATE news_source_documents SET fetch_state=?,attempts=?,next_attempt_at=?,
               claimed_at=NULL,http_status=?,last_error=?,last_fetched_at=?,updated_at=?
               WHERE id=?""",
            (
                state,
                attempts,
                _iso(failed + timedelta(minutes=delay)),
                http_status,
                type(error).__name__,
                _iso(failed),
                _iso(failed),
                document_id,
            ),
        )
        await self.db.commit()

    @_serialized_write
    async def schedule_source_document_refresh(
        self, document_id: int, now: datetime | None = None
    ) -> None:
        scheduled = _iso(now or datetime.now(UTC))
        await self.db.execute(
            """UPDATE news_source_documents SET fetch_state='pending',next_attempt_at=?,
               claimed_at=NULL,updated_at=? WHERE id=?""",
            (scheduled, scheduled, document_id),
        )
        await self.db.commit()

    async def has_snapshot(
        self, page_type: str, page_key: str, content_hash: str, parse_status: str
    ) -> bool:
        rows = await self.db.execute_fetchall(
            """SELECT 1 FROM source_snapshots
               WHERE page_type=? AND page_key=? AND content_hash=? AND parse_status=?""",
            (page_type, page_key, content_hash, parse_status),
        )
        return bool(rows)

    @_serialized_write
    async def record_snapshot(
        self,
        *,
        page_type: str,
        page_key: str,
        content_hash: str,
        compressed_path: str,
        captured_at: datetime,
        parse_status: str,
        error_type: str | None,
    ) -> None:
        await self.db.execute(
            """INSERT OR IGNORE INTO source_snapshots
               (page_type,page_key,content_hash,compressed_path,captured_at,
                parse_status,error_type) VALUES (?,?,?,?,?,?,?)""",
            (
                page_type,
                page_key,
                content_hash,
                compressed_path,
                _iso(captured_at),
                parse_status,
                error_type,
            ),
        )
        await self.db.commit()

    async def snapshot_count(self) -> int:
        rows = await self.db.execute_fetchall(
            "SELECT count(*) AS count FROM source_snapshots"
        )
        return int(rows[0]["count"])

    async def expired_snapshots(self, cutoff: datetime) -> list[tuple[int, str]]:
        rows = await self.db.execute_fetchall(
            "SELECT id,compressed_path FROM source_snapshots WHERE captured_at<?",
            (_iso(cutoff),),
        )
        return [(int(row["id"]), str(row["compressed_path"])) for row in rows]

    @_serialized_write
    async def delete_snapshot_records(self, snapshot_ids: list[int]) -> None:
        if not snapshot_ids:
            return
        placeholders = ",".join("?" for _ in snapshot_ids)
        await self.db.execute(
            f"DELETE FROM source_snapshots WHERE id IN ({placeholders})", snapshot_ids
        )
        await self.db.commit()

    @_serialized_write
    async def replace_detail(self, article_id: str, detail: DetailObservation) -> None:
        observed = _iso(detail.observed_at)
        current_keys = {segment.stable_key for segment in detail.segments}
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            for segment in detail.segments:
                source_hash = hashlib.sha256(
                    "\n".join(
                        (
                            segment.segment_type,
                            segment.text_en or "",
                            segment.source_url or "",
                            segment.author_handle or "",
                        )
                    ).encode()
                ).hexdigest()
                await self.db.execute(
                    """INSERT INTO news_segments
                       (article_id,stable_key,position,segment_type,author_name,author_handle,
                        published_at,published_at_source_text,text_en,source_url,is_excerpt,
                        source_hash,is_current,first_seen_at,last_seen_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)
                       ON CONFLICT(article_id,stable_key) DO UPDATE SET
                         position=excluded.position,segment_type=excluded.segment_type,
                         author_name=excluded.author_name,author_handle=excluded.author_handle,
                         published_at=excluded.published_at,
                         published_at_source_text=excluded.published_at_source_text,
                         text_en=excluded.text_en,source_url=excluded.source_url,
                         is_excerpt=excluded.is_excerpt,source_hash=excluded.source_hash,
                         is_current=1,last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at""",
                    (
                        article_id,
                        segment.stable_key,
                        segment.position,
                        segment.segment_type,
                        segment.author_name,
                        segment.author_handle,
                        _iso(segment.published_at),
                        segment.published_at_source_text,
                        segment.text_en,
                        segment.source_url,
                        int(segment.is_excerpt),
                        source_hash,
                        observed,
                        observed,
                        observed,
                    ),
                )
            if detail.is_complete:
                if current_keys:
                    placeholders = ",".join("?" for _ in current_keys)
                    await self.db.execute(
                        f"""UPDATE news_segments SET is_current=0
                            WHERE article_id=? AND stable_key NOT IN ({placeholders})""",
                        (article_id, *sorted(current_keys)),
                    )
                else:
                    await self.db.execute(
                        "UPDATE news_segments SET is_current=0 WHERE article_id=?", (article_id,)
                    )
            segment_rows = await self.db.execute_fetchall(
                "SELECT id,stable_key FROM news_segments WHERE article_id=?",
                (article_id,),
            )
            segment_ids = {str(row["stable_key"]): int(row["id"]) for row in segment_rows}
            for segment in detail.segments:
                await self._enqueue_localized(
                    "segment",
                    str(segment_ids[segment.stable_key]),
                    "text",
                    segment.text_en,
                    observed,
                )
            current_link_keys = {item.stable_key for item in detail.links}
            for item in detail.links:
                await self.db.execute(
                    """INSERT INTO news_source_documents
                       (original_url,fetch_state,attempts,next_attempt_at,first_seen_at,updated_at)
                       VALUES (?,'pending',0,?,?,?)
                       ON CONFLICT(original_url) DO UPDATE SET updated_at=excluded.updated_at""",
                    (item.url, observed, observed, observed),
                )
                document_rows = await self.db.execute_fetchall(
                    "SELECT id FROM news_source_documents WHERE original_url=?", (item.url,)
                )
                await self.db.execute(
                    """INSERT INTO news_segment_links
                       (article_id,segment_id,source_document_id,stable_key,position,link_type,
                        label,original_url,is_current,first_seen_at,last_seen_at)
                       VALUES (?,?,?,?,?,?,?,?,1,?,?)
                       ON CONFLICT(article_id,stable_key) DO UPDATE SET
                         segment_id=excluded.segment_id,
                         source_document_id=excluded.source_document_id,
                         position=excluded.position,link_type=excluded.link_type,
                         label=excluded.label,original_url=excluded.original_url,
                         is_current=1,last_seen_at=excluded.last_seen_at""",
                    (
                        article_id,
                        segment_ids[item.segment_key],
                        document_rows[0]["id"],
                        item.stable_key,
                        item.position,
                        item.kind,
                        item.label,
                        item.url,
                        observed,
                        observed,
                    ),
                )
            if detail.is_complete:
                if current_link_keys:
                    placeholders = ",".join("?" for _ in current_link_keys)
                    await self.db.execute(
                        f"""UPDATE news_segment_links SET is_current=0
                            WHERE article_id=? AND stable_key NOT IN ({placeholders})""",
                        (article_id, *sorted(current_link_keys)),
                    )
                else:
                    await self.db.execute(
                        "UPDATE news_segment_links SET is_current=0 WHERE article_id=?",
                        (article_id,),
                    )
            current_media_keys = {item.stable_key for item in detail.media}
            for item in detail.media:
                await self.db.execute(
                    """INSERT INTO news_media
                       (article_id,segment_id,stable_key,position,media_type,original_url,
                        caption,download_state,next_attempt_at,is_current)
                       VALUES (?,?,?,?,?,?,?,'pending',?,1)
                       ON CONFLICT(article_id,stable_key) DO UPDATE SET
                         segment_id=excluded.segment_id,position=excluded.position,
                         media_type=excluded.media_type,original_url=excluded.original_url,
                         caption=excluded.caption,is_current=1,
                         download_state=CASE
                           WHEN news_media.original_url=excluded.original_url
                           THEN news_media.download_state ELSE 'pending' END,
                         next_attempt_at=CASE
                           WHEN news_media.original_url=excluded.original_url
                           THEN news_media.next_attempt_at ELSE excluded.next_attempt_at END""",
                    (
                        article_id,
                        segment_ids.get(item.segment_key or ""),
                        item.stable_key,
                        item.position,
                        item.media_type,
                        item.original_url,
                        item.caption,
                        observed,
                    ),
                )
            if detail.is_complete:
                if current_media_keys:
                    placeholders = ",".join("?" for _ in current_media_keys)
                    await self.db.execute(
                        f"""UPDATE news_media SET is_current=0
                            WHERE article_id=? AND stable_key NOT IN ({placeholders})""",
                        (article_id, *sorted(current_media_keys)),
                    )
                else:
                    await self.db.execute(
                        "UPDATE news_media SET is_current=0 WHERE article_id=?",
                        (article_id,),
                    )
            for comment in detail.comments:
                await self._upsert_comment(comment)
            await self.db.execute(
                """UPDATE news_articles SET detail_state=?,is_excerpt=?,updated_at=?
                   WHERE source_id=?""",
                (
                    "complete" if detail.is_complete else "partial",
                    int(any(segment.is_excerpt for segment in detail.segments)),
                    observed,
                    article_id,
                ),
            )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise

    async def _upsert_comment(self, comment: CommentObservation) -> None:
        observed = _iso(comment.observed_at)
        source_hash = hashlib.sha256(comment.text_en.encode()).hexdigest()
        await self.db.execute(
            """INSERT INTO news_comments
               (comment_id,article_id,parent_comment_id,author_name,published_at,
                published_at_source_text,text_en,permalink,reaction_count,source_hash,
                first_seen_at,last_seen_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(comment_id) DO UPDATE SET
                 parent_comment_id=excluded.parent_comment_id,
                 author_name=excluded.author_name,published_at=excluded.published_at,
                 published_at_source_text=excluded.published_at_source_text,
                 text_en=excluded.text_en,permalink=excluded.permalink,
                 reaction_count=excluded.reaction_count,source_hash=excluded.source_hash,
                 last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at""",
            (
                comment.comment_id,
                comment.article_id,
                comment.parent_comment_id,
                comment.author_name,
                _iso(comment.published_at),
                comment.published_at_source_text,
                comment.text_en,
                comment.permalink,
                comment.reaction_count,
                source_hash,
                observed,
                observed,
                observed,
            ),
        )
        await self._enqueue_localized(
            "comment", comment.comment_id, "text", comment.text_en, observed
        )

    async def _enqueue_localized(
        self,
        entity_type: str,
        entity_id: str,
        field_name: str,
        source_text: str | None,
        observed: str | None,
    ) -> None:
        if not source_text:
            return
        source_hash = hashlib.sha256(source_text.strip().encode()).hexdigest()
        await self.db.execute(
            """INSERT OR IGNORE INTO localized_texts
               (entity_type,entity_id,field_name,language,source_hash,status,
                attempts,next_attempt_at,created_at,updated_at)
               VALUES (?,?,?,'zh-Hans',?,'pending',0,?,?,?)""",
            (
                entity_type,
                entity_id,
                field_name,
                source_hash,
                observed,
                observed,
                observed,
            ),
        )

    @_serialized_write
    async def claim_localized_jobs(
        self, limit: int, now: datetime | None = None
    ) -> list[LocalizedTextJob]:
        claimed = now or datetime.now(UTC)
        ready = _iso(claimed)
        lease = _iso(claimed + timedelta(minutes=5))
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = await self.db.execute_fetchall(
                """SELECT * FROM localized_texts
                   WHERE status IN ('pending','processing')
                     AND (next_attempt_at IS NULL OR next_attempt_at<=?
                          OR (status='pending' AND attempts=0))
                   ORDER BY CASE entity_type
                     WHEN 'article' THEN 0 WHEN 'segment' THEN 1
                     WHEN 'source_document' THEN 2 ELSE 3 END,
                     id LIMIT ?""",
                (ready, limit),
            )
            jobs: list[LocalizedTextJob] = []
            for row in rows:
                source_text = await self._localized_source_text(
                    str(row["entity_type"]),
                    str(row["entity_id"]),
                    str(row["field_name"]),
                )
                if source_text is None:
                    await self.db.execute(
                        "UPDATE localized_texts SET status='stale',updated_at=? WHERE id=?",
                        (ready, row["id"]),
                    )
                    continue
                jobs.append(
                    LocalizedTextJob(
                        id=int(row["id"]),
                        entity_type=str(row["entity_type"]),
                        entity_id=str(row["entity_id"]),
                        field_name=str(row["field_name"]),
                        source_text=source_text,
                        source_hash=str(row["source_hash"]),
                        attempts=int(row["attempts"]),
                    )
                )
            if jobs:
                await self.db.executemany(
                    """UPDATE localized_texts SET status='processing',next_attempt_at=?,
                       updated_at=? WHERE id=?""",
                    [(lease, ready, job.id) for job in jobs],
                )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return jobs

    async def _localized_source_text(
        self, entity_type: str, entity_id: str, field_name: str
    ) -> str | None:
        fields = {
            ("article", "title"): ("news_articles", "source_id", "title_en"),
            ("article", "teaser"): ("news_articles", "source_id", "teaser_en"),
            ("segment", "text"): ("news_segments", "id", "text_en"),
            ("comment", "text"): ("news_comments", "comment_id", "text_en"),
            ("source_document", "title"): ("news_source_documents", "id", "title_en"),
            ("source_document", "body"): ("news_source_documents", "id", "body_en"),
        }
        target = fields.get((entity_type, field_name))
        if target is None:
            return None
        table, identity_column, value_column = target
        rows = await self.db.execute_fetchall(
            f"SELECT {value_column} AS value FROM {table} WHERE {identity_column}=?",
            (entity_id,),
        )
        return str(rows[0]["value"]) if rows and rows[0]["value"] is not None else None

    @_serialized_write
    async def complete_localized_job(
        self, job: LocalizedTextJob, translated_text: str, model: str
    ) -> bool:
        current = await self._localized_source_text(
            job.entity_type, job.entity_id, job.field_name
        )
        current_hash = hashlib.sha256((current or "").strip().encode()).hexdigest()
        now = _iso(datetime.now(UTC))
        if current is None or current_hash != job.source_hash:
            await self.db.execute(
                "UPDATE localized_texts SET status='stale',updated_at=? WHERE id=?",
                (now, job.id),
            )
            await self.db.commit()
            return False
        await self.db.execute(
            """UPDATE localized_texts SET translated_text=?,model=?,status='done',
               next_attempt_at=NULL,last_error=NULL,updated_at=? WHERE id=?""",
            (translated_text.strip(), model, now, job.id),
        )
        await self.db.commit()
        return True

    @_serialized_write
    async def fail_localized_job(
        self, job: LocalizedTextJob, error: Exception, now: datetime | None = None
    ) -> None:
        failed = now or datetime.now(UTC)
        delay = (1, 5, 30, 120, 360)[min(job.attempts, 4)]
        await self.db.execute(
            """UPDATE localized_texts SET status='pending',attempts=attempts+1,
               next_attempt_at=?,last_error=?,updated_at=? WHERE id=?""",
            (
                _iso(failed + timedelta(minutes=delay)),
                type(error).__name__,
                _iso(failed),
                job.id,
            ),
        )
        await self.db.commit()

    async def localized_text(
        self, entity_type: str, entity_id: str, field_name: str
    ) -> str | None:
        source_text = await self._localized_source_text(entity_type, entity_id, field_name)
        if source_text is None:
            return None
        source_hash = hashlib.sha256(source_text.strip().encode()).hexdigest()
        rows = await self.db.execute_fetchall(
            """SELECT translated_text FROM localized_texts
               WHERE entity_type=? AND entity_id=? AND field_name=? AND language='zh-Hans'
                 AND source_hash=? AND status='done'""",
            (entity_type, entity_id, field_name, source_hash),
        )
        return str(rows[0]["translated_text"]) if rows else None

    async def localized_status(
        self, entity_type: str, entity_id: str, field_name: str
    ) -> str | None:
        rows = await self.db.execute_fetchall(
            """SELECT status FROM localized_texts
               WHERE entity_type=? AND entity_id=? AND field_name=?
               ORDER BY id DESC LIMIT 1""",
            (entity_type, entity_id, field_name),
        )
        return str(rows[0]["status"]) if rows else None

    async def localized_status_by_id(self, job_id: int) -> str | None:
        rows = await self.db.execute_fetchall(
            "SELECT status FROM localized_texts WHERE id=?", (job_id,)
        )
        return str(rows[0]["status"]) if rows else None

    @_serialized_write
    async def claim_media_jobs(
        self, limit: int, now: datetime | None = None
    ) -> list[MediaJob]:
        claimed = now or datetime.now(UTC)
        ready = _iso(claimed)
        lease = _iso(claimed + timedelta(minutes=5))
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = await self.db.execute_fetchall(
                """SELECT id,article_id,original_url,attempts FROM news_media
                   WHERE is_current=1 AND download_state!='complete'
                     AND (next_attempt_at IS NULL OR next_attempt_at<=?
                          OR (download_state='pending' AND attempts=0))
                   ORDER BY attempts,id LIMIT ?""",
                (ready, limit),
            )
            if rows:
                await self.db.executemany(
                    """UPDATE news_media SET download_state='processing',next_attempt_at=?
                       WHERE id=?""",
                    [(lease, row["id"]) for row in rows],
                )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return [
            MediaJob(
                media_id=int(row["id"]),
                article_id=str(row["article_id"]),
                original_url=str(row["original_url"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    @_serialized_write
    async def complete_media_job(
        self,
        media_id: int,
        local_path: str,
        mime_type: str,
        byte_size: int,
        sha256: str,
    ) -> None:
        await self.db.execute(
            """UPDATE news_media SET local_path=?,mime_type=?,byte_size=?,sha256=?,
               download_state='complete',next_attempt_at=NULL,last_error=NULL WHERE id=?""",
            (local_path, mime_type, byte_size, sha256, media_id),
        )
        await self.db.commit()

    @_serialized_write
    async def fail_media_job(
        self, media_id: int, error: Exception, now: datetime | None = None
    ) -> None:
        failed = now or datetime.now(UTC)
        rows = await self.db.execute_fetchall(
            "SELECT attempts FROM news_media WHERE id=?", (media_id,)
        )
        attempts = int(rows[0]["attempts"]) + 1
        delay = (1, 5, 30, 120, 360)[min(attempts - 1, 4)]
        await self.db.execute(
            """UPDATE news_media SET download_state='failed',attempts=?,
               next_attempt_at=?,last_error=? WHERE id=?""",
            (
                attempts,
                _iso(failed + timedelta(minutes=delay)),
                type(error).__name__,
                media_id,
            ),
        )
        await self.db.commit()

    async def completed_media_by_hash(self, sha256: str) -> CachedMedia | None:
        rows = await self.db.execute_fetchall(
            """SELECT id,local_path,mime_type,byte_size,sha256 FROM news_media
               WHERE download_state='complete' AND sha256=? AND local_path IS NOT NULL
               ORDER BY id LIMIT 1""",
            (sha256,),
        )
        return self._cached_media(rows[0]) if rows else None

    async def resolve_media_path(self, media_id: int) -> CachedMedia | None:
        rows = await self.db.execute_fetchall(
            """SELECT id,local_path,mime_type,byte_size,sha256 FROM news_media
               WHERE id=? AND download_state='complete' AND is_current=1""",
            (media_id,),
        )
        return self._cached_media(rows[0]) if rows else None

    @staticmethod
    def _cached_media(row) -> CachedMedia:
        return CachedMedia(
            media_id=int(row["id"]),
            path=Path(str(row["local_path"])),
            mime_type=str(row["mime_type"]),
            byte_size=int(row["byte_size"]),
            sha256=str(row["sha256"]),
        )

    async def media_state(self, media_id: int) -> str | None:
        rows = await self.db.execute_fetchall(
            "SELECT download_state FROM news_media WHERE id=?", (media_id,)
        )
        return str(rows[0]["download_state"]) if rows else None

    async def comment_count(self, article_id: str) -> int:
        rows = await self.db.execute_fetchall(
            "SELECT count(*) AS count FROM news_comments WHERE article_id=?", (article_id,)
        )
        return int(rows[0]["count"])

    async def current_segment_keys(self, article_id: str) -> tuple[str, ...]:
        rows = await self.db.execute_fetchall(
            """SELECT stable_key FROM news_segments
               WHERE article_id=? AND is_current=1 ORDER BY position""",
            (article_id,),
        )
        return tuple(str(row["stable_key"]) for row in rows)

    async def segment_count(self, article_id: str) -> int:
        rows = await self.db.execute_fetchall(
            "SELECT count(*) AS count FROM news_segments WHERE article_id=?", (article_id,)
        )
        return int(rows[0]["count"])

    async def get_article(self, source_id: str) -> ArticleRecord | None:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM news_articles WHERE source_id=?", (source_id,)
        )
        if not rows:
            return None
        categories = await self.db.execute_fetchall(
            """SELECT category FROM news_category_memberships
               WHERE article_id=? ORDER BY category""",
            (source_id,),
        )
        row = rows[0]
        return ArticleRecord(
            source_id=row["source_id"],
            ff_url=row["ff_url"],
            title_en=row["title_en"],
            teaser_en=row["teaser_en"],
            source_name=row["source_name"],
            source_url=row["source_url"],
            published_at=_dt(row["published_at"]),
            published_at_source_text=row["published_at_source_text"],
            source_timezone=row["source_timezone"],
            breaking_impact=row["breaking_impact"],
            comment_count=int(row["comment_count"]),
            detail_state=row["detail_state"],
            is_excerpt=bool(row["is_excerpt"]),
            first_seen_at=_dt(row["first_seen_at"]),
            last_seen_at=_dt(row["last_seen_at"]),
            updated_at=_dt(row["updated_at"]),
            categories=tuple(row["category"] for row in categories),
        )  # type: ignore[arg-type]

    async def current_feed_ids(self, feed_type: FeedType) -> tuple[str, ...]:
        rows = await self.db.execute_fetchall(
            """SELECT article_id FROM news_feed_placements
               WHERE feed_type=? AND is_current=1 ORDER BY rank""",
            (feed_type,),
        )
        return tuple(str(row["article_id"]) for row in rows)

    async def feed_event_types(
        self, article_id: str, feed_type: FeedType
    ) -> tuple[str, ...]:
        rows = await self.db.execute_fetchall(
            """SELECT event_type FROM news_feed_events
               WHERE article_id=? AND feed_type=? ORDER BY id""",
            (article_id, feed_type),
        )
        return tuple(str(row["event_type"]) for row in rows)

    async def section_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for feed_type in ("latest", "hot"):
            rows = await self.db.execute_fetchall(
                """SELECT count(*) AS count FROM news_feed_placements
                   WHERE feed_type=? AND is_current=1""",
                (feed_type,),
            )
            counts[feed_type] = int(rows[0]["count"])
        for category in (
            "fundamental",
            "technical",
            "industry",
            "entertainment",
            "educational",
        ):
            rows = await self.db.execute_fetchall(
                "SELECT count(*) AS count FROM news_category_memberships WHERE category=?",
                (category,),
            )
            counts[category] = int(rows[0]["count"])
        rows = await self.db.execute_fetchall(
            "SELECT count(*) AS count FROM news_comment_feed WHERE is_current=1"
        )
        counts["latest-comments"] = int(rows[0]["count"])
        return counts

    async def list_section(
        self,
        section: str,
        impact: str | None,
        limit: int,
        cursor: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        parameters: list[Any] = []
        impact_sql = ""
        if impact:
            impact_sql = " AND a.breaking_impact=?"
            parameters.append(impact)
        if section in ("latest", "hot"):
            cursor_sql = ""
            if cursor:
                cursor_sql = " AND (p.rank>? OR (p.rank=? AND a.source_id>?))"
                parameters.extend((cursor["rank"], cursor["rank"], cursor["id"]))
            rows = await self.db.execute_fetchall(
                f"""SELECT a.*,p.rank AS sort_rank FROM news_articles a
                    JOIN news_feed_placements p ON p.article_id=a.source_id
                    WHERE p.feed_type=? AND p.is_current=1{impact_sql}{cursor_sql}
                    ORDER BY p.rank,a.source_id LIMIT ?""",
                (section, *parameters, limit + 1),
            )
        else:
            sort_value = "COALESCE(a.published_at,a.first_seen_at)"
            cursor_sql = ""
            if cursor:
                cursor_sql = (
                    f" AND ({sort_value}<? OR ({sort_value}=? AND a.source_id<?))"
                )
                parameters.extend((cursor["time"], cursor["time"], cursor["id"]))
            rows = await self.db.execute_fetchall(
                f"""SELECT a.*,{sort_value} AS sort_time FROM news_articles a
                    JOIN news_category_memberships c ON c.article_id=a.source_id
                    WHERE c.category=?{impact_sql}{cursor_sql}
                    ORDER BY sort_time DESC,a.source_id DESC LIMIT ?""",
                (section, *parameters, limit + 1),
            )
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = [dict(row) for row in selected]
        translations = await self._current_translations("article", items)
        categories = await self._categories_for(
            [str(item["source_id"]) for item in items]
        )
        for item in items:
            item["title_zh"] = translations.get((item["source_id"], "title"))
            item["teaser_zh"] = translations.get((item["source_id"], "teaser"))
            item["categories"] = categories.get(str(item["source_id"]), [])
        next_cursor: dict[str, Any] | None = None
        if has_more and items:
            last = items[-1]
            next_cursor = (
                {"rank": last["sort_rank"], "id": last["source_id"]}
                if section in ("latest", "hot")
                else {"time": last["sort_time"], "id": last["source_id"]}
            )
        return items, next_cursor

    async def _article_categories(self, article_id: str) -> list[str]:
        rows = await self.db.execute_fetchall(
            """SELECT category FROM news_category_memberships
               WHERE article_id=? ORDER BY category""",
            (article_id,),
        )
        return [str(row["category"]) for row in rows]

    async def _categories_for(self, article_ids: list[str]) -> dict[str, list[str]]:
        if not article_ids:
            return {}
        placeholders = ",".join("?" for _ in article_ids)
        rows = await self.db.execute_fetchall(
            f"""SELECT article_id,category FROM news_category_memberships
                WHERE article_id IN ({placeholders}) ORDER BY article_id,category""",
            article_ids,
        )
        result: dict[str, list[str]] = {article_id: [] for article_id in article_ids}
        for row in rows:
            result[str(row["article_id"])].append(str(row["category"]))
        return result

    async def _current_translations(
        self, entity_type: str, entities: list[dict[str, Any]]
    ) -> dict[tuple[str, str], str]:
        if not entities:
            return {}
        identity_key = {
            "article": "source_id",
            "segment": "id",
            "comment": "comment_id",
        }[entity_type]
        identities = [str(item[identity_key]) for item in entities]
        placeholders = ",".join("?" for _ in identities)
        rows = await self.db.execute_fetchall(
            f"""SELECT entity_id,field_name,source_hash,translated_text
                FROM localized_texts WHERE entity_type=? AND entity_id IN ({placeholders})
                  AND language='zh-Hans' AND status='done' ORDER BY id DESC""",
            (entity_type, *identities),
        )
        sources: dict[tuple[str, str], str | None] = {}
        for item in entities:
            identity = str(item[identity_key])
            if entity_type == "article":
                sources[(identity, "title")] = item.get("title_en")
                sources[(identity, "teaser")] = item.get("teaser_en")
            else:
                sources[(identity, "text")] = item.get("text_en")
        result: dict[tuple[str, str], str] = {}
        for row in rows:
            key = (str(row["entity_id"]), str(row["field_name"]))
            source = sources.get(key)
            if source is None or key in result:
                continue
            source_hash = hashlib.sha256(source.strip().encode()).hexdigest()
            if source_hash == row["source_hash"] and row["translated_text"]:
                result[key] = str(row["translated_text"])
        return result

    async def detail_data(self, article_id: str) -> dict[str, Any] | None:
        article_rows = await self.db.execute_fetchall(
            "SELECT * FROM news_articles WHERE source_id=?", (article_id,)
        )
        if not article_rows:
            return None
        article = dict(article_rows[0])
        article["categories"] = await self._article_categories(article_id)
        article_translations = await self._current_translations("article", [article])
        article["title_zh"] = article_translations.get((article_id, "title"))
        article["teaser_zh"] = article_translations.get((article_id, "teaser"))
        segment_rows = await self.db.execute_fetchall(
            """SELECT * FROM news_segments WHERE article_id=? AND is_current=1
               ORDER BY position,id""",
            (article_id,),
        )
        segments = [dict(row) for row in segment_rows]
        segment_translations = await self._current_translations("segment", segments)
        media_rows = await self.db.execute_fetchall(
            """SELECT * FROM news_media WHERE article_id=? AND is_current=1
               ORDER BY position,id""",
            (article_id,),
        )
        media_by_segment: dict[int | None, list[dict[str, Any]]] = {}
        for row in media_rows:
            media_by_segment.setdefault(row["segment_id"], []).append(dict(row))
        link_rows = await self.db.execute_fetchall(
            """SELECT l.*,d.fetch_state,d.title_en AS source_title_en,
                      d.author_name AS source_author_name,d.source_host,
                      d.published_at_source_text AS source_published_at_source_text,
                      d.lead_image_url AS source_lead_image_url
               FROM news_segment_links l
               JOIN news_source_documents d ON d.id=l.source_document_id
               WHERE l.article_id=? AND l.is_current=1
               ORDER BY l.segment_id,l.position,l.id""",
            (article_id,),
        )
        links_by_segment: dict[int, list[dict[str, Any]]] = {}
        for row in link_rows:
            links_by_segment.setdefault(int(row["segment_id"]), []).append(dict(row))
        for segment in segments:
            identity = str(segment["id"])
            segment["text_zh"] = segment_translations.get((identity, "text"))
            segment["media"] = media_by_segment.get(segment["id"], [])
            segment["links"] = links_by_segment.get(int(segment["id"]), [])
        feed_rows = await self.db.execute_fetchall(
            """SELECT feed_type,rank FROM news_feed_placements
               WHERE article_id=? AND is_current=1 ORDER BY feed_type""",
            (article_id,),
        )
        comments = await self.comment_count(article_id)
        return {
            "article": article,
            "segments": segments,
            "feeds": [dict(row) for row in feed_rows],
            "comment_count_collected": comments,
        }

    async def source_document_data(self, document_id: int) -> dict[str, Any] | None:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM news_source_documents WHERE id=?", (document_id,)
        )
        if not rows:
            return None
        item = dict(rows[0])
        item["title_zh"] = await self.localized_text(
            "source_document", str(document_id), "title"
        )
        item["body_zh"] = await self.localized_text(
            "source_document", str(document_id), "body"
        )
        try:
            item["paragraphs"] = json.loads(item.get("paragraphs_json") or "[]")
        except json.JSONDecodeError:
            item["paragraphs"] = []
        return item

    async def list_comments(
        self,
        article_id: str | None,
        limit: int,
        cursor: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        parameters: list[Any] = []
        if article_id is None:
            source = """news_comments c JOIN news_comment_feed f
                        ON f.comment_id=c.comment_id"""
            where = "f.is_current=1"
            order = "f.rank,c.comment_id"
            fields = "c.*,f.rank AS sort_rank"
            if cursor:
                where += " AND (f.rank>? OR (f.rank=? AND c.comment_id>?))"
                parameters.extend((cursor["rank"], cursor["rank"], cursor["id"]))
        else:
            source = "news_comments c"
            where = "c.article_id=?"
            parameters.append(article_id)
            sort_value = "COALESCE(c.published_at,c.first_seen_at)"
            order = f"{sort_value} DESC,c.comment_id DESC"
            fields = f"c.*,{sort_value} AS sort_time"
            if cursor:
                where += (
                    f" AND ({sort_value}<? OR ({sort_value}=? AND c.comment_id<?))"
                )
                parameters.extend((cursor["time"], cursor["time"], cursor["id"]))
        rows = await self.db.execute_fetchall(
            f"SELECT {fields} FROM {source} WHERE {where} ORDER BY {order} LIMIT ?",
            (*parameters, limit + 1),
        )
        selected = [dict(row) for row in rows[:limit]]
        translations = await self._current_translations("comment", selected)
        for item in selected:
            item["text_zh"] = translations.get((str(item["comment_id"]), "text"))
        next_cursor: dict[str, Any] | None = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_cursor = (
                {"rank": last["sort_rank"], "id": last["comment_id"]}
                if article_id is None
                else {"time": last["sort_time"], "id": last["comment_id"]}
            )
        return selected, next_cursor

    async def status_counts(self) -> dict[str, Any]:
        result: dict[str, Any] = {"sections": await self.section_counts()}
        for table, column, key in (
            ("news_detail_jobs", "state", "detail_jobs"),
            ("news_media", "download_state", "media_jobs"),
            ("localized_texts", "status", "translation_jobs"),
        ):
            rows = await self.db.execute_fetchall(
                f"SELECT {column} AS state,count(*) AS count FROM {table} GROUP BY {column}"
            )
            result[key] = {str(row["state"]): int(row["count"]) for row in rows}
        rows = await self.db.execute_fetchall(
            "SELECT value FROM runtime_state WHERE key='schema_version'"
        )
        result["schema_version"] = int(rows[0]["value"])
        for key in (
            "news_last_listing_success",
            "news_last_listing_error",
            "news_last_detail_success",
            "news_last_detail_error",
            "news_last_translation_success",
        ):
            result[key.removeprefix("news_")] = await self.get_runtime_state(key)
        return result

    async def list_articles(
        self, limit: int, before: datetime | None = None
    ) -> list[dict[str, Any]]:
        if before:
            rows = await self.db.execute_fetchall(
                """SELECT * FROM news_articles
                   WHERE COALESCE(published_at,first_seen_at)<?
                   ORDER BY COALESCE(published_at,first_seen_at) DESC,source_id DESC
                   LIMIT ?""",
                (_iso(before), limit),
            )
        else:
            rows = await self.db.execute_fetchall(
                """SELECT * FROM news_articles
                   ORDER BY COALESCE(published_at,first_seen_at) DESC,source_id DESC
                   LIMIT ?""",
                (limit,),
            )
        items = [dict(row) for row in rows]
        translations = await self._current_translations("article", items)
        for item in items:
            article_id = str(item["source_id"])
            item["title_zh"] = translations.get((article_id, "title"))
            item["teaser_zh"] = translations.get((article_id, "teaser"))
        return items

    async def get_runtime_state(self, key: str) -> str | None:
        rows = await self.db.execute_fetchall(
            "SELECT value FROM runtime_state WHERE key=?", (key,)
        )
        return str(rows[0]["value"]) if rows else None

    @_serialized_write
    async def set_runtime_state(self, key: str, value: str) -> None:
        await self.db.execute(
            """INSERT INTO runtime_state(key,value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )
        await self.db.commit()

    async def ready_detail_job_count(self, now: datetime | None = None) -> int:
        ready = _iso(now or datetime.now(UTC))
        rows = await self.db.execute_fetchall(
            """SELECT count(*) AS count FROM news_detail_jobs
               WHERE state='pending' AND next_attempt_at<=?""",
            (ready,),
        )
        return int(rows[0]["count"])
