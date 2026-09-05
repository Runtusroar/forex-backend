from app.db import SCHEMA, Database
from app.migrations import (
    MIGRATION_2,
    MIGRATION_3,
    MIGRATION_4,
    MIGRATION_5,
    MIGRATION_6,
    MIGRATION_7,
)


async def test_schema_eight_upgrades_existing_seven_without_data_loss(tmp_path):
    database = Database(tmp_path / "legacy.sqlite3")
    await database.open()
    try:
        conn = database.connection
        await conn.executescript(SCHEMA)
        for migration in (
            MIGRATION_2,
            MIGRATION_3,
            MIGRATION_4,
            MIGRATION_5,
            MIGRATION_6,
            MIGRATION_7,
        ):
            await conn.executescript(migration)
        await conn.execute(
            """INSERT INTO calendar_events(source_id,event_at,currency,impact,title_en,
               source_hash,updated_at)
               VALUES ('old','2026-09-05','USD','high','Old','hash','now')"""
        )
        await conn.commit()
        await database.initialize()
        await database.initialize()
        rows = await conn.execute_fetchall("SELECT * FROM calendar_events")
        assert len(rows) == 1
        assert rows[0]["title_en"] == "Old"
        assert rows[0]["source_date"] is None
        columns = await conn.execute_fetchall("PRAGMA table_info(news_articles)")
        assert {"comments_source_complete", "comments_visible_count"} <= {
            row["name"] for row in columns
        }
        assert (await conn.execute_fetchall("PRAGMA integrity_check"))[0][0] == "ok"
        assert not await conn.execute_fetchall("PRAGMA foreign_key_check")
        assert (
            await conn.execute_fetchall(
                "SELECT value FROM runtime_state WHERE key='schema_version'"
            )
        )[0][0] == "8"
    finally:
        await database.close()
