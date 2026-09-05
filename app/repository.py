from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiosqlite

from app.db import Database
from app.domain import (
    CalendarDetailJob,
    CalendarDetailObservation,
    CalendarDetailRecord,
    CalendarHistoryObservation,
    CalendarObservation,
    CalendarRecord,
    CalendarRelatedStoryObservation,
    NewsObservation,
    NewsRecord,
    TranslationJob,
)
from app.transactions import serialized_write as _serialized_write
from app.transactions import snapshot_read as _snapshot_read


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is not None and value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _hash(values: Iterable[str | None]) -> str:
    normalized = "\n".join((value or "").strip() for value in values)
    return hashlib.sha256(normalized.encode()).hexdigest()


def _calendar_detail_source_hash(
    event_at: str | None,
    currency: str | None,
    impact: str | None,
    title: str | None,
    actual: str | None,
    forecast: str | None,
    previous: str | None,
) -> str:
    return _hash((event_at, currency, impact, title, actual, forecast, previous))


class Repository:
    def __init__(self, database: Database, reader: aiosqlite.Connection | None = None) -> None:
        assert database.connection is not None
        self.database = database
        self.reader = reader
        self.db = self._writer_db = database.connection
        self.write_lock = database.write_lock

    async def _upsert_calendar(self, items: list[CalendarObservation], now: str) -> None:
        for item in items:
            source_hash = _hash([item.title_en])
            detail_hash = _calendar_detail_source_hash(
                _iso(item.event_at),
                item.currency,
                item.impact,
                item.title_en,
                item.actual,
                item.forecast,
                item.previous,
            )
            current = await self.db.execute_fetchall(
                """SELECT source_hash,title_en,event_at,currency,impact,actual,forecast,previous
                   FROM calendar_events WHERE source_id = ?""",
                (item.source_id,),
            )
            changed = (
                not current
                or _calendar_detail_source_hash(
                    current[0]["event_at"],
                    current[0]["currency"],
                    current[0]["impact"],
                    current[0]["title_en"],
                    current[0]["actual"],
                    current[0]["forecast"],
                    current[0]["previous"],
                )
                != detail_hash
            )
            title_changed = not current or current[0]["title_en"] != item.title_en
            await self.db.execute(
                """INSERT INTO calendar_events
                   (source_id,event_at,currency,impact,title_en,actual,forecast,previous,
                    source_time_text,source_position,source_hash,updated_at,source_date)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     event_at=excluded.event_at,currency=excluded.currency,impact=excluded.impact,
                     title_en=excluded.title_en,actual=excluded.actual,forecast=excluded.forecast,
                     previous=excluded.previous,source_time_text=excluded.source_time_text,
                     source_position=excluded.source_position,source_hash=excluded.source_hash,
                     title_zh=CASE WHEN calendar_events.title_en=excluded.title_en
                                   THEN calendar_events.title_zh ELSE NULL END,
                     updated_at=excluded.updated_at,
                     source_date=COALESCE(excluded.source_date,calendar_events.source_date)""",
                (
                    item.source_id,
                    _iso(item.event_at),
                    item.currency,
                    item.impact,
                    item.title_en,
                    item.actual,
                    item.forecast,
                    item.previous,
                    item.source_time_text,
                    item.source_position,
                    source_hash,
                    now,
                    item.source_date.isoformat() if item.source_date else None,
                ),
            )
            if changed:
                priority = 100 if item.impact == "high" else 50 if item.impact == "medium" else 10
                await self.db.execute(
                    """INSERT INTO calendar_detail_jobs
                       (source_id,desired_source_hash,priority,state,attempts,next_attempt_at)
                       VALUES (?,?,?,'pending',0,?)
                       ON CONFLICT(source_id) DO UPDATE SET
                         desired_source_hash=excluded.desired_source_hash,
                         priority=max(calendar_detail_jobs.priority,excluded.priority),
                         state='pending',attempts=0,next_attempt_at=excluded.next_attempt_at,
                         claimed_at=NULL,last_error=NULL""",
                    (item.source_id, detail_hash, priority, now),
                )
            if title_changed:
                await self._enqueue(
                    "calendar",
                    item.source_id,
                    source_hash,
                    {"title": item.title_en},
                )

    @_serialized_write
    async def upsert_calendar(self, items: list[CalendarObservation]) -> None:
        now = _iso(_now())
        assert now is not None
        await self._upsert_calendar(items, now)
        await self.db.commit()

    @_serialized_write
    async def replace_calendar_window(
        self,
        items: list[CalendarObservation],
        start: datetime,
        end: datetime,
    ) -> None:
        if end <= start:
            raise ValueError("calendar window end must be after start")
        if any(item.event_at < start or item.event_at >= end for item in items):
            raise ValueError("calendar item is outside replacement window")

        now = _iso(_now())
        assert now is not None
        await self._upsert_calendar(items, now)
        source_ids = [item.source_id for item in items]
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            await self.db.execute(
                f"""DELETE FROM calendar_events
                    WHERE event_at >= ? AND event_at < ?
                      AND source_id NOT IN ({placeholders})""",
                (_iso(start), _iso(end), *source_ids),
            )
        else:
            await self.db.execute(
                "DELETE FROM calendar_events WHERE event_at >= ? AND event_at < ?",
                (_iso(start), _iso(end)),
            )
        await self.db.execute(
            """DELETE FROM translation_jobs
               WHERE entity_type='calendar'
                 AND NOT EXISTS (
                   SELECT 1 FROM calendar_events
                   WHERE calendar_events.source_id=translation_jobs.entity_id
                 )"""
        )
        await self.db.commit()

    @_snapshot_read
    async def get_runtime_state(self, key: str) -> str | None:
        rows = await self.db.execute_fetchall("SELECT value FROM runtime_state WHERE key=?", (key,))
        return str(rows[0]["value"]) if rows else None

    @_serialized_write
    async def set_runtime_state(self, key: str, value: str) -> None:
        await self.db.execute(
            """INSERT INTO runtime_state(key,value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )
        await self.db.commit()

    @_serialized_write
    async def upsert_news(self, items: list[NewsObservation]) -> None:
        now = _iso(_now())
        for item in items:
            source_hash = _hash([item.title_en, item.summary_en, item.body_en])
            current = await self.db.execute_fetchall(
                "SELECT source_hash,first_seen_at FROM news_items WHERE source_id = ?",
                (item.source_id,),
            )
            changed = not current or current[0]["source_hash"] != source_hash
            first_seen = current[0]["first_seen_at"] if current else _iso(item.first_seen_at)
            await self.db.execute(
                """INSERT INTO news_items
                   (source_id,url,source,published_at,first_seen_at,title_en,summary_en,body_en,
                    image_url,source_hash,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     url=excluded.url,source=excluded.source,published_at=COALESCE(excluded.published_at,news_items.published_at),
                     title_en=excluded.title_en,summary_en=excluded.summary_en,body_en=excluded.body_en,
                     image_url=excluded.image_url,source_hash=excluded.source_hash,
                     title_zh=CASE WHEN news_items.source_hash=excluded.source_hash
                                   THEN news_items.title_zh ELSE NULL END,
                     summary_zh=CASE WHEN news_items.source_hash=excluded.source_hash
                                     THEN news_items.summary_zh ELSE NULL END,
                     body_zh=CASE WHEN news_items.source_hash=excluded.source_hash
                                  THEN news_items.body_zh ELSE NULL END,
                     updated_at=excluded.updated_at""",
                (
                    item.source_id,
                    item.url,
                    item.source,
                    _iso(item.published_at),
                    first_seen,
                    item.title_en,
                    item.summary_en,
                    item.body_en,
                    item.image_url,
                    source_hash,
                    now,
                ),
            )
            if changed:
                await self._enqueue(
                    "news",
                    item.source_id,
                    source_hash,
                    {"title": item.title_en, "summary": item.summary_en, "body": item.body_en},
                )
        await self.db.commit()

    async def _enqueue(
        self, entity_type: str, entity_id: str, source_hash: str, payload: dict[str, Any]
    ) -> None:
        await self.db.execute(
            """INSERT OR IGNORE INTO translation_jobs
               (entity_type,entity_id,source_hash,payload_json,next_attempt_at)
               VALUES (?,?,?,?,?)""",
            (entity_type, entity_id, source_hash, json.dumps(payload), _iso(_now())),
        )

    @_serialized_write
    async def claim_translation_jobs(self, limit: int) -> list[TranslationJob]:
        rows = await self.db.execute_fetchall(
            """SELECT * FROM translation_jobs
               WHERE state IN ('pending','processing') AND next_attempt_at <= ?
               ORDER BY id LIMIT ?""",
            (_iso(_now()), limit),
        )
        if rows:
            await self.db.executemany(
                "UPDATE translation_jobs SET state='processing',next_attempt_at=? WHERE id=?",
                [(_iso(_now() + timedelta(minutes=5)), row["id"]) for row in rows],
            )
            await self.db.commit()
        return [
            TranslationJob(
                id=row["id"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                source_hash=row["source_hash"],
                payload=json.loads(row["payload_json"]),
                attempts=row["attempts"],
            )
            for row in rows
        ]

    @_serialized_write
    async def complete_translation(
        self, job: TranslationJob, translated: dict[str, str | None]
    ) -> bool:
        table = "calendar_events" if job.entity_type == "calendar" else "news_items"
        rows = await self.db.execute_fetchall(
            f"SELECT source_hash FROM {table} WHERE source_id=?",
            (job.entity_id,),
        )
        if not rows or rows[0]["source_hash"] != job.source_hash:
            await self.db.execute("UPDATE translation_jobs SET state='stale' WHERE id=?", (job.id,))
            await self.db.commit()
            return False
        if job.entity_type == "calendar":
            await self.db.execute(
                "UPDATE calendar_events SET title_zh=? WHERE source_id=?",
                (translated.get("title_zh"), job.entity_id),
            )
        else:
            await self.db.execute(
                "UPDATE news_items SET title_zh=?,summary_zh=?,body_zh=? WHERE source_id=?",
                (
                    translated.get("title_zh"),
                    translated.get("summary_zh"),
                    translated.get("body_zh"),
                    job.entity_id,
                ),
            )
        await self.db.execute("UPDATE translation_jobs SET state='done' WHERE id=?", (job.id,))
        await self.db.commit()
        return True

    @_serialized_write
    async def fail_translation(self, job: TranslationJob, error: Exception) -> None:
        delays = (1, 5, 30, 120, 360)
        delay = delays[min(job.attempts, len(delays) - 1)]
        await self.db.execute(
            """UPDATE translation_jobs SET state='pending', attempts=attempts+1,
               next_attempt_at=?, last_error=? WHERE id=?""",
            (_iso(_now() + timedelta(minutes=delay)), type(error).__name__, job.id),
        )
        await self.db.commit()

    @_snapshot_read
    async def translation_job_count(self) -> int:
        row = await self.db.execute_fetchall("SELECT count(*) AS count FROM translation_jobs")
        return int(row[0]["count"])

    @_snapshot_read
    async def list_calendar(self, start: datetime, end: datetime) -> list[CalendarRecord]:
        rows = await self.db.execute_fetchall(
            """SELECT * FROM calendar_events
               WHERE event_at >= ? AND event_at < ? ORDER BY event_at""",
            (_iso(start), _iso(end)),
        )
        return [self._calendar(row) for row in rows]

    @_snapshot_read
    async def get_calendar(self, source_id: str) -> CalendarRecord | None:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM calendar_events WHERE source_id=?", (source_id,)
        )
        return self._calendar(rows[0]) if rows else None

    @_serialized_write
    async def claim_calendar_detail_jobs(
        self, limit: int, now: datetime | None = None
    ) -> list[CalendarDetailJob]:
        claimed_at = now or _now()
        ready_at = _iso(claimed_at)
        expired_lease = _iso(claimed_at - timedelta(minutes=5))
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = await self.db.execute_fetchall(
                """SELECT j.*,e.event_at,e.source_date FROM calendar_detail_jobs j
                   JOIN calendar_events e ON e.source_id=j.source_id
                   WHERE (j.state='pending' AND j.next_attempt_at<=?)
                      OR (j.state='processing' AND j.claimed_at<?)
                   ORDER BY j.priority DESC,e.event_at,j.source_id LIMIT ?""",
                (ready_at, expired_lease, limit),
            )
            if rows:
                await self.db.executemany(
                    """UPDATE calendar_detail_jobs SET state='processing',claimed_at=?
                       WHERE source_id=?""",
                    [(ready_at, row["source_id"]) for row in rows],
                )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return [
            CalendarDetailJob(
                source_id=str(row["source_id"]),
                event_at=_dt(str(row["event_at"])),
                source_date=date.fromisoformat(row["source_date"]) if row["source_date"] else None,
                desired_source_hash=str(row["desired_source_hash"]),
                priority=int(row["priority"]),
                attempts=int(row["attempts"]),
                claimed_at=claimed_at,
            )
            for row in rows
        ]

    @_serialized_write
    async def complete_calendar_detail_job(
        self,
        source_id: str,
        desired_source_hash: str,
        *,
        unavailable_reason: str | None = None,
        checked_at: datetime | None = None,
    ) -> None:
        await self.db.execute(
            """UPDATE calendar_detail_jobs SET state='done',attempts=0,
               claimed_at=NULL,last_error=NULL,unavailable_reason=?,last_checked_at=?
               WHERE source_id=? AND desired_source_hash=?""",
            (unavailable_reason, _iso(checked_at or _now()), source_id, desired_source_hash),
        )
        await self.db.commit()

    @_serialized_write
    async def fail_calendar_detail_job(
        self,
        source_id: str,
        error: Exception,
        now: datetime | None = None,
        max_attempts: int = 8,
        *,
        desired_source_hash: str | None = None,
    ) -> bool:
        failed_at = now or _now()
        if desired_source_hash is None:
            rows = await self.db.execute_fetchall(
                """SELECT attempts FROM calendar_detail_jobs
                   WHERE source_id=? AND state='processing'""",
                (source_id,),
            )
        else:
            rows = await self.db.execute_fetchall(
                """SELECT attempts FROM calendar_detail_jobs
                   WHERE source_id=? AND state='processing' AND desired_source_hash=?""",
                (source_id, desired_source_hash),
            )
        if not rows:
            return False
        attempts = int(rows[0]["attempts"]) + 1
        delay_minutes = (1, 5, 30, 120, 360)[min(attempts - 1, 4)]
        state = "failed" if attempts >= max_attempts else "pending"
        await self.db.execute(
            """UPDATE calendar_detail_jobs SET state=?,attempts=?,next_attempt_at=?,
               claimed_at=NULL,last_error=? WHERE source_id=?""",
            (
                state,
                attempts,
                _iso(failed_at + timedelta(minutes=delay_minutes)),
                type(error).__name__,
                source_id,
            ),
        )
        await self.db.commit()
        return True

    @_serialized_write
    async def enqueue_due_calendar_detail_refreshes(
        self,
        now: datetime | None = None,
        refresh_interval: timedelta = timedelta(days=1),
        limit: int = 16,
    ) -> int:
        observed_at = now or _now()
        rows = await self.db.execute_fetchall(
            """SELECT e.source_id,e.event_at,e.currency,e.impact,e.title_en,
                      e.actual,e.forecast,e.previous,d.source_id AS detail_id,
                      d.last_success_at,j.unavailable_reason,j.last_checked_at
               FROM calendar_events e
               LEFT JOIN calendar_event_details d ON d.source_id=e.source_id
               LEFT JOIN calendar_detail_jobs j ON j.source_id=e.source_id
               WHERE e.event_at>=? AND e.event_at<?
                 AND (j.source_id IS NULL OR j.state='done'
                      OR (j.state='failed' AND j.next_attempt_at<=?))
               ORDER BY e.event_at,e.source_position""",
            (
                _iso(observed_at - timedelta(days=2)),
                _iso(observed_at + timedelta(days=31)),
                _iso(observed_at),
            ),
        )
        selected = []
        for row in rows:
            event_at = _dt(row["event_at"])
            last_success_at = _dt(row["last_success_at"])
            if event_at is None:
                continue
            if (
                row["unavailable_reason"]
                and row["last_checked_at"]
                and _dt(row["last_checked_at"]) > observed_at - refresh_interval
            ):
                continue
            distance = event_at - observed_at
            if -timedelta(hours=6) <= distance <= timedelta(hours=6):
                event_refresh_interval = min(refresh_interval, timedelta(minutes=15))
            elif -timedelta(days=2) <= distance < -timedelta(hours=6):
                event_refresh_interval = min(refresh_interval, timedelta(hours=1))
            else:
                event_refresh_interval = refresh_interval
            if (
                row["detail_id"] is None
                or last_success_at is None
                or last_success_at <= observed_at - event_refresh_interval
            ):
                selected.append(row)
            if len(selected) >= limit:
                break
        ready = _iso(observed_at)
        for row in selected:
            priority = 100 if row["impact"] == "high" else 50 if row["impact"] == "medium" else 10
            desired_source_hash = _calendar_detail_source_hash(
                row["event_at"],
                row["currency"],
                row["impact"],
                row["title_en"],
                row["actual"],
                row["forecast"],
                row["previous"],
            )
            await self.db.execute(
                """INSERT INTO calendar_detail_jobs
                   (source_id,desired_source_hash,priority,state,attempts,next_attempt_at)
                   VALUES (?,?,?,'pending',0,?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     desired_source_hash=excluded.desired_source_hash,
                     priority=max(calendar_detail_jobs.priority,excluded.priority),
                     state='pending',attempts=0,next_attempt_at=excluded.next_attempt_at,
                     claimed_at=NULL,last_error=NULL
                   WHERE calendar_detail_jobs.state IN ('done','failed')""",
                (row["source_id"], desired_source_hash, priority, ready),
            )
        await self.db.commit()
        return len(selected)

    @_snapshot_read
    async def calendar_detail_job_counts(self) -> dict[str, int]:
        rows = await self.db.execute_fetchall(
            """SELECT state,count(*) AS count FROM calendar_detail_jobs
               GROUP BY state"""
        )
        return {str(row["state"]): int(row["count"]) for row in rows}

    @_serialized_write
    async def replace_calendar_detail(
        self,
        detail: CalendarDetailObservation,
        *,
        desired_source_hash: str | None = None,
    ) -> bool:
        if desired_source_hash is not None:
            jobs = await self.db.execute_fetchall(
                """SELECT 1 FROM calendar_detail_jobs
                   WHERE source_id=? AND state='processing' AND desired_source_hash=?""",
                (detail.source_id, desired_source_hash),
            )
            if not jobs:
                return False
        now = _iso(_now())
        assert now is not None
        await self.db.execute(
            """INSERT INTO calendar_event_details (
                 source_id,title_en,currency,currency_name,impact,actual,forecast,previous,
                 actual_state,previous_state,previous_revised_from,ff_url,source_name,
                 source_url,latest_release_url,measures,usual_effect,frequency,
                 next_release_text,next_release_url,ff_notes,why_traders_care,updated_at,
                 source_hash,last_success_at
               )
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_id) DO UPDATE SET
                 title_en=excluded.title_en,currency=excluded.currency,
                 currency_name=excluded.currency_name,impact=excluded.impact,
                 actual=excluded.actual,forecast=excluded.forecast,
                 previous=excluded.previous,actual_state=excluded.actual_state,
                 previous_state=excluded.previous_state,
                 previous_revised_from=excluded.previous_revised_from,
                 ff_url=excluded.ff_url,source_name=excluded.source_name,
                 source_url=excluded.source_url,
                 latest_release_url=excluded.latest_release_url,
                 measures=excluded.measures,usual_effect=excluded.usual_effect,
                 frequency=excluded.frequency,
                 next_release_text=excluded.next_release_text,
                 next_release_url=excluded.next_release_url,
                 ff_notes=excluded.ff_notes,
                 why_traders_care=excluded.why_traders_care,
                 updated_at=excluded.updated_at,source_hash=excluded.source_hash,
                 last_success_at=excluded.last_success_at""",
            (
                detail.source_id,
                detail.title_en,
                detail.currency,
                detail.currency_name,
                detail.impact,
                detail.actual,
                detail.forecast,
                detail.previous,
                detail.actual_state,
                detail.previous_state,
                detail.previous_revised_from,
                detail.ff_url,
                detail.source_name,
                detail.source_url,
                detail.latest_release_url,
                detail.measures,
                detail.usual_effect,
                detail.frequency,
                detail.next_release_text,
                detail.next_release_url,
                detail.ff_notes,
                detail.why_traders_care,
                now,
                _hash(
                    [
                        detail.title_en,
                        detail.source_name,
                        detail.measures,
                        detail.usual_effect,
                        detail.frequency,
                        detail.next_release_text,
                        detail.ff_notes,
                        detail.why_traders_care,
                    ]
                ),
                now,
            ),
        )
        await self.db.execute(
            "DELETE FROM calendar_event_history WHERE source_id=?", (detail.source_id,)
        )
        await self.db.execute(
            "DELETE FROM calendar_event_related_stories WHERE source_id=?",
            (detail.source_id,),
        )
        await self.db.executemany(
            """INSERT INTO calendar_event_history (
                 source_id,position,release_date_text,event_url,actual,forecast,previous,
                 actual_state,previous_state,previous_revised_from
               )
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    detail.source_id,
                    position,
                    row.release_date_text,
                    row.event_url,
                    row.actual,
                    row.forecast,
                    row.previous,
                    row.actual_state,
                    row.previous_state,
                    row.previous_revised_from,
                )
                for position, row in enumerate(detail.history)
            ],
        )
        await self.db.executemany(
            """INSERT INTO calendar_event_related_stories (
                 source_id,position,title_en,ff_url,source_name,source_url,
                 published_at_source_text,preview
               )
               VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    detail.source_id,
                    position,
                    story.title_en,
                    story.ff_url,
                    story.source_name,
                    story.source_url,
                    story.published_at_source_text,
                    story.preview,
                )
                for position, story in enumerate(detail.related_stories)
            ],
        )
        await self.db.commit()
        return True

    @_snapshot_read
    async def get_calendar_detail(self, source_id: str) -> CalendarDetailRecord | None:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM calendar_event_details WHERE source_id=?", (source_id,)
        )
        if not rows:
            return None
        history_rows = await self.db.execute_fetchall(
            """SELECT * FROM calendar_event_history
               WHERE source_id=? ORDER BY position,id""",
            (source_id,),
        )
        story_rows = await self.db.execute_fetchall(
            """SELECT * FROM calendar_event_related_stories
               WHERE source_id=? ORDER BY position,id""",
            (source_id,),
        )
        row = rows[0]
        return CalendarDetailRecord(
            source_id=row["source_id"],
            title_en=row["title_en"],
            currency=row["currency"],
            currency_name=row["currency_name"],
            impact=row["impact"],
            actual=row["actual"],
            forecast=row["forecast"],
            previous=row["previous"],
            actual_state=row["actual_state"],
            previous_state=row["previous_state"],
            previous_revised_from=row["previous_revised_from"],
            ff_url=row["ff_url"],
            source_name=row["source_name"],
            source_url=row["source_url"],
            latest_release_url=row["latest_release_url"],
            measures=row["measures"],
            usual_effect=row["usual_effect"],
            frequency=row["frequency"],
            next_release_text=row["next_release_text"],
            next_release_url=row["next_release_url"],
            ff_notes=row["ff_notes"],
            why_traders_care=row["why_traders_care"],
            history=tuple(
                CalendarHistoryObservation(
                    release_date_text=history["release_date_text"],
                    event_url=history["event_url"],
                    actual=history["actual"],
                    forecast=history["forecast"],
                    previous=history["previous"],
                    actual_state=history["actual_state"],
                    previous_state=history["previous_state"],
                    previous_revised_from=history["previous_revised_from"],
                )
                for history in history_rows
            ),
            related_stories=tuple(
                CalendarRelatedStoryObservation(
                    title_en=story["title_en"],
                    ff_url=story["ff_url"],
                    source_name=story["source_name"],
                    source_url=story["source_url"],
                    published_at_source_text=story["published_at_source_text"],
                    preview=story["preview"],
                )
                for story in story_rows
            ),
            updated_at=_dt(row["updated_at"]),
        )  # type: ignore[arg-type]

    @_snapshot_read
    async def list_news(self, limit: int = 50, before: datetime | None = None) -> list[NewsRecord]:
        if before:
            rows = await self.db.execute_fetchall(
                """SELECT * FROM news_items WHERE COALESCE(published_at,first_seen_at) < ?
                   ORDER BY COALESCE(published_at,first_seen_at) DESC LIMIT ?""",
                (_iso(before), limit),
            )
        else:
            rows = await self.db.execute_fetchall(
                """SELECT * FROM news_items ORDER BY COALESCE(published_at,first_seen_at) DESC
                   LIMIT ?""",
                (limit,),
            )
        return [self._news(row) for row in rows]

    @_snapshot_read
    async def get_news(self, source_id: str) -> NewsRecord | None:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM news_items WHERE source_id=?", (source_id,)
        )
        return self._news(rows[0]) if rows else None

    @staticmethod
    def _calendar(row: Any) -> CalendarRecord:
        return CalendarRecord(
            source_id=row["source_id"],
            event_at=_dt(row["event_at"]),
            currency=row["currency"],
            impact=row["impact"],
            title_en=row["title_en"],
            actual=row["actual"],
            forecast=row["forecast"],
            previous=row["previous"],
            source_time_text=row["source_time_text"],
            source_position=row["source_position"],
            title_zh=row["title_zh"],
            source_hash=row["source_hash"],
            updated_at=_dt(row["updated_at"]),
        )  # type: ignore[arg-type]

    @staticmethod
    def _news(row: Any) -> NewsRecord:
        return NewsRecord(
            source_id=row["source_id"],
            url=row["url"],
            source=row["source"],
            published_at=_dt(row["published_at"]),
            first_seen_at=_dt(row["first_seen_at"]),
            title_en=row["title_en"],
            summary_en=row["summary_en"],
            body_en=row["body_en"],
            image_url=row["image_url"],
            title_zh=row["title_zh"],
            summary_zh=row["summary_zh"],
            body_zh=row["body_zh"],
            source_hash=row["source_hash"],
            updated_at=_dt(row["updated_at"]),
        )  # type: ignore[arg-type]
