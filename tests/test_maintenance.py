import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


def cli(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.maintenance", *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def dataset(tmp_path: Path):
    media = tmp_path / "media"
    media.mkdir()
    photo = media / "image.jpg"
    photo.write_bytes(b"test media content")
    database = tmp_path / "live.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE news_media (local_path TEXT, download_state TEXT);
        CREATE TABLE source_snapshots (compressed_path TEXT);
        INSERT INTO records VALUES (1, 'committed');
    """)
    connection.execute("INSERT INTO news_media VALUES (?, 'complete')", (str(photo),))
    connection.commit()
    yield database, media, photo, connection
    connection.close()


def test_backup_uses_committed_wal_snapshot_and_restores_media(dataset, tmp_path):
    database, media, photo, connection = dataset
    connection.execute("UPDATE records SET value='uncommitted'")
    backup = tmp_path / "backup"
    result = cli("backup", "--database", database, "--media-dir", media, "--output", backup)
    assert result.returncode == 0, result.stderr
    assert cli("verify", backup).returncode == 0
    with sqlite3.connect(backup / "database.sqlite3") as saved:
        assert saved.execute("SELECT value FROM records").fetchone() == ("committed",)
    restored = tmp_path / "restored"
    result = cli("restore", backup, "--output", restored)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(restored / "forex_factory.sqlite3") as saved:
        restored_path = Path(saved.execute("SELECT local_path FROM news_media").fetchone()[0])
    assert restored_path.is_relative_to(restored)
    assert restored_path.read_bytes() == photo.read_bytes()
    assert cli("restore", backup, "--output", restored).returncode != 0
    assert connection.execute("SELECT value FROM records").fetchone() == ("uncommitted",)


def test_backup_missing_required_media_fails_without_publishing(dataset, tmp_path):
    database, media, photo, _ = dataset
    photo.unlink()
    backup = tmp_path / "backup"
    result = cli("backup", "--database", database, "--media-dir", media, "--output", backup)
    assert result.returncode != 0
    assert not backup.exists()


def test_verify_rejects_corruption_and_path_escape(dataset, tmp_path):
    database, media, _, _ = dataset
    backup = tmp_path / "backup"
    assert (
        cli("backup", "--database", database, "--media-dir", media, "--output", backup).returncode
        == 0
    )
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    target = backup / manifest["files"][0]["path"]
    target.write_bytes(b"corrupted")
    assert cli("verify", backup).returncode != 0
    manifest["files"][0]["sha256"] = hashlib.sha256(b"corrupted").hexdigest()
    manifest["files"][0]["path"] = "../outside"
    manifest_path.write_text(json.dumps(manifest))
    assert cli("verify", backup).returncode != 0
