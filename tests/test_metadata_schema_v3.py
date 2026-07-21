from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
from music_vault.metadata.schema import normalize_release_date, required_metadata_indexes


def _v2_database(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            title TEXT,
            artist TEXT,
            album TEXT,
            album_artist TEXT,
            year TEXT,
            duration_seconds REAL,
            cover_path TEXT,
            source_url TEXT,
            musicbrainz_recording_id TEXT,
            musicbrainz_release_id TEXT,
            source_kind TEXT,
            source_video_id TEXT,
            source_upload_date TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE playlist_tracks (
            playlist_id INTEGER NOT NULL,
            track_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (playlist_id, track_id)
        );
        CREATE TABLE sync_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id TEXT NOT NULL,
            playlist_title TEXT,
            video_id TEXT NOT NULL,
            title TEXT,
            reason TEXT NOT NULL,
            error_category TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 1,
            first_attempt_at TEXT NOT NULL,
            last_attempt_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unresolved',
            resolved_at TEXT,
            UNIQUE (playlist_id, video_id)
        );
        CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT);
        PRAGMA user_version=2;
        """
    )
    local = path.parent / "local.synthetic"
    youtube = path.parent / "youtube.synthetic"
    canonical = path.parent / "canonical.synthetic"
    for item in (local, youtube, canonical):
        item.write_bytes(b"synthetic")
    connection.execute(
        "INSERT INTO tracks(path,title,artist,album,year) VALUES(?,?,?,?,?)",
        (str(local), "Local", "Artist", "Album", "1999"),
    )
    connection.execute(
        """INSERT INTO tracks(
               path,title,artist,year,source_kind,source_video_id,source_upload_date
           ) VALUES(?,?,?,?,?,?,?)""",
        (str(youtube), "Source", "Uploader", None, "youtube", "abcdefghijk", "2024-03-02"),
    )
    connection.execute(
        """INSERT INTO tracks(
               path,title,artist,album,year,source_kind,source_video_id,
               musicbrainz_recording_id,musicbrainz_release_id
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            str(canonical),
            "Canonical",
            "Confirmed Artist",
            "Confirmed Album",
            "1984",
            "youtube",
            "lmnopqrstuv",
            "recording-id",
            "release-id",
        ),
    )
    connection.execute("INSERT INTO playlists(name) VALUES('Synthetic Mix')")
    connection.execute(
        "INSERT INTO playlist_tracks(playlist_id,track_id,position) VALUES(1,1,0)"
    )
    connection.commit()
    connection.close()
    return path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1999", "1999"),
        ("2000-02", "2000-02"),
        ("2000-02-29", "2000-02-29"),
        (" 1984 ", "1984"),
    ],
)
def test_release_date_accepts_supported_precision(raw, expected):
    assert normalize_release_date(raw) == expected


@pytest.mark.parametrize("raw", ["20240202", "2024-13", "2023-02-29", "2024-04-31", "text"])
def test_release_date_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        normalize_release_date(raw)


def test_v2_to_v3_migration_is_additive_backed_up_and_idempotent(tmp_path):
    path = _v2_database(tmp_path / "library.sqlite3")
    backups = tmp_path / "backups"
    db = MusicVaultDB(path, backup_dir=backups)
    assert CURRENT_SCHEMA_VERSION == 8
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert db.last_migration_backup and db.last_migration_backup.is_file()
    with sqlite3.connect(db.last_migration_backup) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 2
        assert backup.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 3

    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 3
    assert db.conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0] == 1
    local = db.conn.execute("SELECT * FROM tracks WHERE title='Local'").fetchone()
    youtube = db.conn.execute("SELECT * FROM tracks WHERE title='Source'").fetchone()
    canonical = db.conn.execute("SELECT * FROM tracks WHERE title='Canonical'").fetchone()
    assert local["release_date"] == "1999" and local["year"] == "1999"
    assert youtube["release_date"] is None and youtube["year"] is None
    assert canonical["release_date"] == "1984" and canonical["year"] == "1984"
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_fields WHERE is_manual=1"
    ).fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM track_metadata_fields"
    ).fetchone()[0] == 3 * 9
    confirmed = db.conn.execute(
        """SELECT provenance,is_locked FROM track_metadata_fields
           WHERE track_id=? AND field_name='title'""",
        (canonical["id"],),
    ).fetchone()
    assert tuple(confirmed) == ("musicbrainz_confirmed", 1)
    assert db.conn.execute(
        """SELECT COUNT(*) FROM track_metadata_observations
           WHERE field_name='source_upload_date'"""
    ).fetchone()[0] == 1
    index_names = {
        row[0]
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert set(required_metadata_indexes()) <= index_names
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()

    backup_count = len(list(backups.glob("*.sqlite3")))
    reopened = MusicVaultDB(path, backup_dir=backups)
    assert reopened.last_migration_backup is None
    assert len(list(backups.glob("*.sqlite3"))) == backup_count
    assert reopened.conn.execute("SELECT COUNT(*) FROM track_metadata_fields").fetchone()[0] > 0
    reopened.close()


def test_nonempty_support_history_also_requires_verified_backup(tmp_path):
    path = _v2_database(tmp_path / "support.sqlite3")
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM playlist_tracks")
        connection.execute("DELETE FROM playlists")
        connection.execute("DELETE FROM tracks")
        connection.execute(
            """
            INSERT INTO sync_failures (
                playlist_id, video_id, reason, error_category, attempt_count,
                first_attempt_at, last_attempt_at, status
            ) VALUES ('synthetic', 'abcdefghijk', 'synthetic', 'test', 1, 't', 't', 'unresolved')
            """
        )
        connection.execute(
            "INSERT INTO app_meta(key, value) VALUES('synthetic', 'present')"
        )

    backups = tmp_path / "backups"
    db = MusicVaultDB(path, backup_dir=backups)
    assert db.last_migration_backup and db.last_migration_backup.is_file()
    with sqlite3.connect(db.last_migration_backup) as backup:
        assert backup.execute("SELECT COUNT(*) FROM sync_failures").fetchone()[0] == 1
        assert backup.execute("SELECT COUNT(*) FROM app_meta").fetchone()[0] == 1
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()


def test_new_database_has_v3_tables_and_foreign_keys(tmp_path):
    db = MusicVaultDB(tmp_path / "new.sqlite3")
    tables = {
        row[0]
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "track_metadata_fields",
        "track_metadata_observations",
        "track_metadata_history",
    } <= tables
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    db.close()
