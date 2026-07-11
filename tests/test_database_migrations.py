from __future__ import annotations

import sqlite3
from pathlib import Path

from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB


def test_empty_database_is_created_at_latest_schema(tmp_path: Path):
    db = MusicVaultDB(tmp_path / "new.sqlite3", backup_dir=tmp_path / "backups")
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert {"source_kind", "source_video_id", "source_upload_date"} <= {
        row[1] for row in db.conn.execute("PRAGMA table_info(tracks)")
    }
    assert db.last_migration_backup is None
    db.close()


def test_v0_migration_preserves_tracks_playlists_and_relationships(v0_database, tmp_path):
    path = v0_database()
    db = MusicVaultDB(path, backup_dir=tmp_path / "backups")
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 3
    assert db.conn.execute("SELECT name FROM playlists").fetchone()[0] == "Mix"
    assert db.conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0] == 1
    db.close()


def test_v0_migration_backfills_youtube_and_clears_false_year(v0_database, tmp_path):
    db = MusicVaultDB(v0_database(), backup_dir=tmp_path / "backups")
    row = db.conn.execute("SELECT * FROM tracks WHERE title='Source date'").fetchone()
    assert row["source_kind"] == "youtube"
    assert row["source_video_id"] == "abcdefghijk"
    assert row["source_upload_date"] == "2021"
    assert row["year"] is None
    db.close()


def test_v0_migration_preserves_credible_canonical_year(v0_database, tmp_path):
    db = MusicVaultDB(v0_database(), backup_dir=tmp_path / "backups")
    canonical = db.conn.execute("SELECT * FROM tracks WHERE title='Canonical'").fetchone()
    local = db.conn.execute("SELECT * FROM tracks WHERE title='Local'").fetchone()
    assert canonical["year"] == "1984"
    assert canonical["source_kind"] == "youtube"
    assert local["year"] == "1999"
    assert local["source_kind"] is None
    db.close()


def test_migration_creates_sqlite_backup_before_nonempty_change(v0_database, tmp_path):
    db = MusicVaultDB(v0_database(), backup_dir=tmp_path / "backups")
    backup = db.last_migration_backup
    assert backup and backup.is_file()
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 3
    db.close()


def test_migration_is_idempotent(v0_database, tmp_path):
    path = v0_database()
    first = MusicVaultDB(path, backup_dir=tmp_path / "backups")
    first.close()
    backup_count = len(list((tmp_path / "backups").glob("*.sqlite3")))
    second = MusicVaultDB(path, backup_dir=tmp_path / "backups")
    assert second.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 3
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == backup_count
    assert second.last_migration_backup is None
    second.close()
