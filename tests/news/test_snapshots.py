from datetime import UTC, datetime, timedelta
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


async def test_cleanup_removes_only_expired_snapshot_files_and_metadata(
    snapshot_store: SnapshotStore,
) -> None:
    expired = await snapshot_store.capture("listing", "old", "old", NOW)
    recent = await snapshot_store.capture(
        "listing", "recent", "recent", NOW + timedelta(days=20)
    )

    removed = await snapshot_store.cleanup(30, NOW + timedelta(days=31))

    assert removed == 1
    assert expired is not None and not expired.exists()
    assert recent is not None and recent.exists()
    assert await snapshot_store.repository.snapshot_count() == 1


async def test_script_clock_changes_do_not_duplicate_source_snapshot(snapshot_store):
    html = '<script>var serverTime=100;</script><div class="news__article">Story</div>'
    first = await snapshot_store.capture("detail", "1", html, NOW)
    repeated = await snapshot_store.capture(
        "detail", "1", html.replace("=100", "=101"), NOW + timedelta(seconds=30)
    )
    assert first is not None
    assert repeated is None
    assert decompress(first.read_bytes()).decode() == html


async def test_unchanged_source_retains_a_daily_replay_sample(snapshot_store):
    first = await snapshot_store.capture("listing", "news", "unchanged", NOW)
    next_day = await snapshot_store.capture(
        "listing", "news", "unchanged", NOW + timedelta(days=1)
    )
    assert first is not None and next_day is not None
    assert first != next_day


async def test_error_snapshots_preserve_different_diagnostic_scripts(snapshot_store):
    html = '<script>error="challenge"</script><div>Unavailable</div>'
    first = await snapshot_store.capture("detail", "1", html, NOW, ValueError())
    second = await snapshot_store.capture(
        "detail", "1", html.replace("challenge", "network"), NOW, ValueError()
    )
    assert first is not None and second is not None
