from pathlib import Path

from app.db import Database


async def _table_names(database: Database) -> set[str]:
    assert database.connection is not None
    rows = await database.connection.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    return {str(row["name"]) for row in rows}


async def test_migration_creates_news_v2_without_losing_calendar(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    await database.open()
    await database.initialize()
    assert database.connection is not None

    names = await _table_names(database)
    version = await database.connection.execute_fetchall(
        "SELECT value FROM runtime_state WHERE key='schema_version'"
    )

    assert {
        "calendar_events",
        "news_articles",
        "news_category_memberships",
        "news_feed_placements",
        "news_feed_events",
        "news_segments",
        "news_segment_links",
        "news_source_documents",
        "news_media",
        "news_comments",
        "news_comment_feed",
        "localized_texts",
        "news_detail_jobs",
        "source_snapshots",
    } <= names
    assert version[0]["value"] == "3"
    await database.close()


async def test_v3_migration_seeds_full_story_documents_from_v2_segments(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    await database.open()
    await database.initialize()
    assert database.connection is not None
    await database.connection.executescript(
        """
        DROP TABLE news_segment_links;
        DROP TABLE news_source_documents;
        UPDATE runtime_state SET value='2' WHERE key='schema_version';
        INSERT INTO news_articles (
          source_id,ff_url,title_en,source_hash,first_seen_at,last_seen_at,updated_at
        ) VALUES (
          '9002','https://www.forexfactory.com/news/9002','A title','article-hash',
          '2026-09-03T00:00:00Z','2026-09-03T00:00:00Z','2026-09-03T00:00:00Z'
        );
        INSERT INTO news_segments (
          article_id,stable_key,position,segment_type,text_en,source_url,is_excerpt,
          source_hash,first_seen_at,last_seen_at,updated_at
        ) VALUES (
          '9002','body',0,'article','Excerpt ... ( full story )',
          'https://publisher.example/story',1,'segment-hash',
          '2026-09-03T00:00:00Z','2026-09-03T00:00:00Z','2026-09-03T00:00:00Z'
        );
        """
    )
    await database.connection.commit()

    await database.initialize()

    documents = await database.connection.execute_fetchall(
        "SELECT original_url,fetch_state FROM news_source_documents"
    )
    links = await database.connection.execute_fetchall(
        "SELECT link_type,label,original_url FROM news_segment_links"
    )
    assert [tuple(row) for row in documents] == [
        ("https://publisher.example/story", "pending")
    ]
    assert [tuple(row) for row in links] == [
        ("full_story", "full story", "https://publisher.example/story")
    ]
    await database.close()


async def test_migration_imports_legacy_news_and_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    await database.open()
    assert database.connection is not None
    await database.connection.executescript(
        """
        CREATE TABLE calendar_events (
          source_id TEXT PRIMARY KEY, event_at TEXT NOT NULL, currency TEXT NOT NULL,
          impact TEXT NOT NULL, title_en TEXT NOT NULL, title_zh TEXT, actual TEXT,
          forecast TEXT, previous TEXT, source_hash TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE news_items (
          source_id TEXT PRIMARY KEY, url TEXT NOT NULL, source TEXT, published_at TEXT,
          first_seen_at TEXT NOT NULL, title_en TEXT NOT NULL, title_zh TEXT,
          summary_en TEXT, summary_zh TEXT, body_en TEXT, body_zh TEXT, image_url TEXT,
          source_hash TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE translation_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL,
          entity_id TEXT NOT NULL, source_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt_at TEXT NOT NULL, last_error TEXT,
          UNIQUE(entity_type, entity_id, source_hash)
        );
        CREATE TABLE runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO runtime_state VALUES ('schema_version', '1');
        INSERT INTO calendar_events VALUES (
          'event-1','2026-09-03T00:00:00Z','USD','high','Payrolls',NULL,NULL,NULL,NULL,
          'calendar-hash','2026-09-03T00:00:00Z'
        );
        INSERT INTO news_items VALUES (
          '9001','https://www.forexfactory.com/news/9001-x','Reuters',
          '2026-09-03T01:00:00Z','2026-09-03T01:01:00Z','Dollar rises',NULL,
          'A summary',NULL,'A body',NULL,NULL,'news-hash','2026-09-03T01:02:00Z'
        );
        """
    )
    await database.connection.commit()

    await database.initialize()
    await database.initialize()

    calendar = await database.connection.execute_fetchall("SELECT * FROM calendar_events")
    articles = await database.connection.execute_fetchall("SELECT * FROM news_articles")
    assert len(calendar) == 1
    assert len(articles) == 1
    assert articles[0]["source_id"] == "9001"
    assert articles[0]["teaser_en"] == "A summary"
    assert articles[0]["detail_state"] == "partial"
    await database.close()
