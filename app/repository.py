from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any

from app.db import Database
from app.domain import (
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


def _serialized_write(method):
    @wraps(method)
    async def wrapper(self, *args, **kwargs):
        async with self.write_lock:
            return await method(self, *args, **kwargs)

    return wrapper


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _hash(values: Iterable[str | None]) -> str:
    normalized = "\n".join((value or "").strip() for value in values)
    return hashlib.sha256(normalized.encode()).hexdigest()


class Repository:
    def __init__(self, database: Database) -> None:
        assert database.connection is not None
        self.db = database.connection
        self.write_lock = database.write_lock

    async def _upsert_calendar(self, items: list[CalendarObservation], now: str) -> None:
        for item in items:
            source_hash = _hash([item.title_en])
            current = await self.db.execute_fetchall(
                "SELECT source_hash FROM calendar_events WHERE source_id = ?", (item.source_id,)
            )
            changed = not current or current[0]["source_hash"] != source_hash
            await self.db.execute(
                """INSERT INTO calendar_events
                   (source_id,event_at,currency,impact,title_en,actual,forecast,previous,
                    source_time_text,source_position,source_hash,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     event_at=excluded.event_at,currency=excluded.currency,impact=excluded.impact,
                     title_en=excluded.title_en,actual=excluded.actual,forecast=excluded.forecast,
                     previous=excluded.previous,source_time_text=excluded.source_time_text,
                     source_position=excluded.source_position,source_hash=excluded.source_hash,
                     title_zh=CASE WHEN calendar_events.source_hash=excluded.source_hash
                                   THEN calendar_events.title_zh ELSE NULL END,
                     updated_at=excluded.updated_at""",
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
                ),
            )
            if changed:
                await self._enqueue(
                    "calendar", item.source_id, source_hash, {"title": item.title_en}
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
                "UPDATE translation_jobs SET state='processing' WHERE id=?",
                [(row["id"],) for row in rows],
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

    async def translation_job_count(self) -> int:
        row = await self.db.execute_fetchall("SELECT count(*) AS count FROM translation_jobs")
        return int(row[0]["count"])

    async def list_calendar(self, start: datetime, end: datetime) -> list[CalendarRecord]:
        rows = await self.db.execute_fetchall(
            """SELECT * FROM calendar_events
               WHERE event_at >= ? AND event_at < ? ORDER BY event_at""",
            (_iso(start), _iso(end)),
        )
        return [self._calendar(row) for row in rows]

    async def get_calendar(self, source_id: str) -> CalendarRecord | None:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM calendar_events WHERE source_id=?", (source_id,)
        )
        return self._calendar(rows[0]) if rows else None

    @_serialized_write
    async def replace_calendar_detail(self, detail: CalendarDetailObservation) -> None:
        now = _iso(_now())
        assert now is not None
        await self.db.execute(
            """INSERT INTO calendar_event_details (
                 source_id,title_en,currency,currency_name,impact,actual,forecast,previous,
                 actual_state,previous_state,previous_revised_from,ff_url,source_name,
                 source_url,latest_release_url,measures,usual_effect,frequency,
                 next_release_text,next_release_url,ff_notes,why_traders_care,updated_at
               )
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                 updated_at=excluded.updated_at""",
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
