from __future__ import annotations

import aiosqlite

LATEST_SCHEMA_VERSION = 2

MIGRATION_2 = """
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS news_articles (
  source_id TEXT PRIMARY KEY,
  ff_url TEXT NOT NULL UNIQUE,
  title_en TEXT NOT NULL,
  teaser_en TEXT,
  source_name TEXT,
  source_url TEXT,
  published_at TEXT,
  published_at_source_text TEXT,
  source_timezone TEXT,
  breaking_impact TEXT CHECK (breaking_impact IN ('high','medium','low')),
  comment_count INTEGER NOT NULL DEFAULT 0 CHECK (comment_count >= 0),
  detail_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (detail_state IN ('pending','complete','partial','failed')),
  is_excerpt INTEGER NOT NULL DEFAULT 0 CHECK (is_excerpt IN (0,1)),
  listing_thumbnail_url TEXT,
  source_hash TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_articles_published
  ON news_articles(published_at DESC, source_id DESC);

CREATE TABLE IF NOT EXISTS news_category_memberships (
  article_id TEXT NOT NULL REFERENCES news_articles(source_id) ON DELETE CASCADE,
  category TEXT NOT NULL
    CHECK (category IN ('fundamental','technical','industry','entertainment','educational')),
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY(article_id, category)
);
CREATE INDEX IF NOT EXISTS idx_news_category
  ON news_category_memberships(category, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS news_feed_placements (
  article_id TEXT NOT NULL REFERENCES news_articles(source_id) ON DELETE CASCADE,
  feed_type TEXT NOT NULL CHECK (feed_type IN ('latest','hot')),
  rank INTEGER NOT NULL CHECK (rank >= 0),
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
  absence_count INTEGER NOT NULL DEFAULT 0 CHECK (absence_count >= 0),
  PRIMARY KEY(article_id, feed_type)
);
CREATE INDEX IF NOT EXISTS idx_news_feed_current
  ON news_feed_placements(feed_type, is_current, rank);

CREATE TABLE IF NOT EXISTS news_feed_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id TEXT NOT NULL REFERENCES news_articles(source_id) ON DELETE CASCADE,
  feed_type TEXT NOT NULL CHECK (feed_type IN ('latest','hot')),
  event_type TEXT NOT NULL CHECK (event_type IN ('entered','moved','left')),
  previous_rank INTEGER,
  new_rank INTEGER,
  observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id TEXT NOT NULL REFERENCES news_articles(source_id) ON DELETE CASCADE,
  stable_key TEXT NOT NULL,
  position INTEGER NOT NULL CHECK (position >= 0),
  segment_type TEXT NOT NULL CHECK (segment_type IN ('article','social','update','quote','link')),
  author_name TEXT,
  author_handle TEXT,
  published_at TEXT,
  published_at_source_text TEXT,
  text_en TEXT,
  source_url TEXT,
  is_excerpt INTEGER NOT NULL DEFAULT 0 CHECK (is_excerpt IN (0,1)),
  source_hash TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(article_id, stable_key)
);
CREATE INDEX IF NOT EXISTS idx_news_segments_order
  ON news_segments(article_id, is_current, position);

CREATE TABLE IF NOT EXISTS news_media (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id TEXT NOT NULL REFERENCES news_articles(source_id) ON DELETE CASCADE,
  segment_id INTEGER REFERENCES news_segments(id) ON DELETE CASCADE,
  stable_key TEXT NOT NULL,
  position INTEGER NOT NULL CHECK (position >= 0),
  media_type TEXT NOT NULL CHECK (media_type IN ('image','chart','attachment')),
  original_url TEXT NOT NULL,
  local_path TEXT,
  mime_type TEXT,
  byte_size INTEGER,
  sha256 TEXT,
  caption TEXT,
  download_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (download_state IN ('pending','processing','complete','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error TEXT,
  is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
  UNIQUE(article_id, stable_key)
);
CREATE INDEX IF NOT EXISTS idx_news_media_ready
  ON news_media(download_state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_news_media_hash ON news_media(sha256);

CREATE TABLE IF NOT EXISTS news_comments (
  comment_id TEXT PRIMARY KEY,
  article_id TEXT NOT NULL REFERENCES news_articles(source_id) ON DELETE CASCADE,
  parent_comment_id TEXT REFERENCES news_comments(comment_id) ON DELETE CASCADE,
  author_name TEXT NOT NULL,
  published_at TEXT,
  published_at_source_text TEXT,
  text_en TEXT NOT NULL,
  permalink TEXT NOT NULL,
  reaction_count INTEGER,
  source_hash TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_comments_article
  ON news_comments(article_id, published_at DESC);

CREATE TABLE IF NOT EXISTS news_comment_feed (
  comment_id TEXT PRIMARY KEY REFERENCES news_comments(comment_id) ON DELETE CASCADE,
  rank INTEGER NOT NULL CHECK (rank >= 0),
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1))
);

CREATE TABLE IF NOT EXISTS localized_texts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  field_name TEXT NOT NULL,
  language TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  translated_text TEXT,
  model TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','processing','done','failed','stale')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(entity_type, entity_id, field_name, language, source_hash)
);

CREATE TABLE IF NOT EXISTS news_detail_jobs (
  article_id TEXT PRIMARY KEY REFERENCES news_articles(source_id) ON DELETE CASCADE,
  priority INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending','processing','done','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  claimed_at TEXT,
  desired_source_hash TEXT NOT NULL,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_detail_jobs_ready
  ON news_detail_jobs(state, next_attempt_at, priority DESC);

CREATE TABLE IF NOT EXISTS source_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  page_type TEXT NOT NULL,
  page_key TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  compressed_path TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  parse_status TEXT NOT NULL CHECK (parse_status IN ('success','failed')),
  error_type TEXT,
  UNIQUE(page_type, page_key, content_hash, parse_status)
);
CREATE INDEX IF NOT EXISTS idx_source_snapshots_time ON source_snapshots(captured_at);

INSERT OR IGNORE INTO news_articles (
  source_id, ff_url, title_en, teaser_en, source_name, published_at,
  comment_count, detail_state, is_excerpt, source_hash,
  first_seen_at, last_seen_at, updated_at
)
SELECT source_id, url, title_en, summary_en, source, published_at,
       0, 'partial', 0, source_hash, first_seen_at, updated_at, updated_at
FROM news_items;

INSERT OR IGNORE INTO news_detail_jobs (
  article_id, priority, state, attempts, next_attempt_at, desired_source_hash
)
SELECT source_id, 0, 'pending', 0, updated_at, source_hash FROM news_items;

INSERT INTO runtime_state(key, value) VALUES ('schema_version', '2')
ON CONFLICT(key) DO UPDATE SET value=excluded.value;

COMMIT;
"""


async def migrate(connection: aiosqlite.Connection) -> None:
    rows = await connection.execute_fetchall(
        "SELECT value FROM runtime_state WHERE key='schema_version'"
    )
    version = int(rows[0]["value"]) if rows else 1
    if version < LATEST_SCHEMA_VERSION:
        await connection.executescript(MIGRATION_2)
