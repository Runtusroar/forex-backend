from datetime import UTC, datetime
from gzip import decompress
from pathlib import Path

import pytest

from app.db import Database
from app.news.repository import NewsRepository
from app.news.snapshots import SnapshotStore

NOW = datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC)


@pytest.fixture
async def snapshot_store(tmp_path: Path):
    database = Database(tmp_path / "snapshots.sqlite3")
    await database.open()
    await database.initialize()
    assert database.connection is not None
    yield SnapshotStore(tmp_path / "files", NewsRepository(database.connection))
    await database.close()


async def test_success_snapshot_is_written_only_when_content_changes(
    snapshot_store: SnapshotStore,
) -> None:
    first = await snapshot_store.capture("listing", "latest", "<html> one </html>", NOW)
    duplicate = await snapshot_store.capture(
        "listing", "latest", "<html>  one  </html>", NOW
    )
    changed = await snapshot_store.capture("listing", "latest", "<html>two</html>", NOW)

    assert first is not None and first.suffixes == [".html", ".gz"]
    assert decompress(first.read_bytes()).decode() == "<html> one </html>"
    assert duplicate is None
    assert changed is not None


async def test_failed_snapshot_is_recorded_separately_and_key_is_sanitized(
    snapshot_store: SnapshotStore,
) -> None:
    path = await snapshot_store.capture(
        "detail",
        "../../1416 unsafe",
        "<html>broken</html>",
        NOW,
        error=ValueError("secret should not be persisted"),
    )

    assert path is not None
    assert ".." not in path.name
    assert "secret" not in path.name
    assert await snapshot_store.repository.snapshot_count() == 1
