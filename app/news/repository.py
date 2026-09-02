from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import aiosqlite

from app.news.models import (
    ArticleObservation,
    ArticleRecord,
    DetailJob,
    DetailObservation,
    FeedType,
    ListingApplyResult,
    NewsListingBatch,
)


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
    )
    return hashlib.sha256("\n".join(value or "" for value in values).encode()).hexdigest()


class NewsRepository:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self.db = connection

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

    async def complete_detail_job(self, article_id: str) -> None:
        await self.db.execute(
            """UPDATE news_detail_jobs SET state='done',claimed_at=NULL,last_error=NULL
               WHERE article_id=?""",
            (article_id,),
        )
        await self.db.commit()

    async def fail_detail_job(
        self,
        article_id: str,
        error: Exception,
        now: datetime | None = None,
        max_attempts: int = 8,
    ) -> None:
        failed_at = now or datetime.now(UTC)
        rows = await self.db.execute_fetchall(
            "SELECT attempts FROM news_detail_jobs WHERE article_id=?", (article_id,)
        )
        attempts = int(rows[0]["attempts"]) + 1
        delay_minutes = (1, 5, 30, 120, 360)[min(attempts - 1, 4)]
        state = "failed" if attempts >= max_attempts else "pending"
        await self.db.execute(
            """UPDATE news_detail_jobs SET state=?,attempts=?,next_attempt_at=?,
               claimed_at=NULL,last_error=? WHERE article_id=?""",
            (
                state,
                attempts,
                _iso(failed_at + timedelta(minutes=delay_minutes)),
                type(error).__name__,
                article_id,
            ),
        )
        await self.db.commit()

    async def detail_job_state(self, article_id: str) -> str | None:
        rows = await self.db.execute_fetchall(
            "SELECT state FROM news_detail_jobs WHERE article_id=?", (article_id,)
        )
        return str(rows[0]["state"]) if rows else None

    async def has_snapshot(
        self, page_type: str, page_key: str, content_hash: str, parse_status: str
    ) -> bool:
        rows = await self.db.execute_fetchall(
            """SELECT 1 FROM source_snapshots
               WHERE page_type=? AND page_key=? AND content_hash=? AND parse_status=?""",
            (page_type, page_key, content_hash, parse_status),
        )
        return bool(rows)

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
            await self.db.execute(
                """UPDATE news_articles SET detail_state=?,updated_at=? WHERE source_id=?""",
                ("complete" if detail.is_complete else "partial", observed, article_id),
            )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise

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
