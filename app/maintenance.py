"""Consistent SQLite/file backups and isolated restore rehearsals.

Run ``python -m app.maintenance --help``. No command overwrites a destination.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def contained(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"path outside allowed directory: {value}")
    return path


def check_database(path: Path) -> None:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("SQLite foreign key check failed")


def backup(database: Path, media_dir: Path, output: Path, snapshot_dir: Path | None = None) -> dict:
    output = output.resolve()
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        saved = staging / "database.sqlite3"
        with (
            closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(saved)) as destination,
        ):
            source.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
        check_database(saved)
        manifest = {
            "version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "database_sha256": digest(saved),
            "files": [],
            "missing_snapshots": [],
        }
        sources = [
            ("media", media_dir, "news_media", "local_path", "WHERE download_state='complete'")
        ]
        if snapshot_dir:
            sources.append(("snapshots", snapshot_dir, "source_snapshots", "compressed_path", ""))
        with closing(sqlite3.connect(saved)) as connection:
            for kind, root, table, column, condition in sources:
                paths = connection.execute(
                    f"SELECT DISTINCT {column} FROM {table} {condition}"
                ).fetchall()
                for (stored_path,) in paths:
                    if not stored_path:
                        raise ValueError(f"missing path in {table}")
                    source = contained(root, stored_path)
                    if kind == "snapshots" and not source.exists():
                        manifest["missing_snapshots"].append(stored_path)
                        continue
                    content_hash = digest(source)
                    relative = f"{kind}/{content_hash}{source.suffix}"
                    destination = staging / relative
                    destination.parent.mkdir(exist_ok=True)
                    if not destination.exists():
                        shutil.copyfile(source, destination)
                    if digest(destination) != content_hash:
                        raise ValueError("file changed during backup")
                    manifest["files"].append(
                        {
                            "kind": kind,
                            "source_path": stored_path,
                            "path": relative,
                            "sha256": content_hash,
                        }
                    )
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        verify(staging)
        os.rename(staging, output)
        return {
            "backup": str(output),
            "files": len(manifest["files"]),
            "missing_snapshots": len(manifest["missing_snapshots"]),
        }
    except BaseException:
        shutil.rmtree(staging)
        raise


def verify(directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["version"] != 1:
        raise ValueError("unsupported backup format")
    database = contained(directory, "database.sqlite3")
    if digest(database) != manifest["database_sha256"]:
        raise ValueError("database checksum mismatch")
    check_database(database)
    for record in manifest["files"]:
        if record["kind"] not in {"media", "snapshots"}:
            raise ValueError("unknown backup file kind")
        if digest(contained(directory, record["path"])) != record["sha256"]:
            raise ValueError("file checksum mismatch")
    with closing(sqlite3.connect(database)) as connection:
        required = {
            row[0]
            for row in connection.execute(
                "SELECT local_path FROM news_media WHERE download_state='complete'"
            )
        }
    covered = {row["source_path"] for row in manifest["files"] if row["kind"] == "media"}
    if not required.issubset(covered):
        raise ValueError("backup is missing required media")
    return manifest


def restore(directory: Path, output: Path) -> dict:
    manifest = verify(directory)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    try:
        database = output / "forex_factory.sqlite3"
        shutil.copyfile(directory / "database.sqlite3", database)
        with closing(sqlite3.connect(database)) as connection, connection:
            for record in manifest["files"]:
                target = contained(output, record["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(contained(directory, record["path"]), target)
                table, column = (
                    ("news_media", "local_path")
                    if record["kind"] == "media"
                    else ("source_snapshots", "compressed_path")
                )
                connection.execute(
                    f"UPDATE {table} SET {column}=? WHERE {column}=?",
                    (str(target), record["source_path"]),
                )
            if not any(record["kind"] == "snapshots" for record in manifest["files"]):
                connection.execute("DELETE FROM source_snapshots")
            for missing in manifest.get("missing_snapshots", []):
                connection.execute(
                    "DELETE FROM source_snapshots WHERE compressed_path=?", (missing,)
                )
        check_database(database)
        return {"restored": str(output), "files": len(manifest["files"])}
    except BaseException:
        shutil.rmtree(output)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("backup")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--media-dir", type=Path, required=True)
    create.add_argument("--snapshot-dir", type=Path)
    create.add_argument("--output", type=Path, required=True)
    inspect = commands.add_parser("verify")
    inspect.add_argument("directory", type=Path)
    recover = commands.add_parser("restore")
    recover.add_argument("directory", type=Path)
    recover.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "backup":
        result = backup(args.database, args.media_dir, args.output, args.snapshot_dir)
    elif args.command == "verify":
        manifest = verify(args.directory)
        result = {"verified": str(args.directory), "files": len(manifest["files"])}
    else:
        result = restore(args.directory, args.output)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
