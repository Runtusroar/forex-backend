from pathlib import Path

import aiosqlite

from app.migrations import migrate

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_events (
  source_id TEXT PRIMARY KEY,
  event_at TEXT NOT NULL,
  currency TEXT NOT NULL,
  impact TEXT NOT NULL,
  title_en TEXT NOT NULL,
  title_zh TEXT,
  actual TEXT,
  forecast TEXT,
  previous TEXT,
  source_hash TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calendar_event_at ON calendar_events(event_at);

CREATE TABLE IF NOT EXISTS news_items (
  source_id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  source TEXT,
  published_at TEXT,
  first_seen_at TEXT NOT NULL,
  title_en TEXT NOT NULL,
  title_zh TEXT,
  summary_en TEXT,
  summary_zh TEXT,
  body_en TEXT,
  body_zh TEXT,
  image_url TEXT,
  source_hash TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_time ON news_items(published_at, first_seen_at);

CREATE TABLE IF NOT EXISTS translation_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  last_error TEXT,
  UNIQUE(entity_type, entity_id, source_hash)
);

CREATE TABLE IF NOT EXISTS runtime_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR IGNORE INTO runtime_state(key, value) VALUES ('schema_version', '1');
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA foreign_keys=ON")
        await self.connection.execute("PRAGMA busy_timeout=5000")

    async def initialize(self) -> None:
        assert self.connection is not None
        await self.connection.executescript(SCHEMA)
        await migrate(self.connection)
        await self.connection.commit()

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None
