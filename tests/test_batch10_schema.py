from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
from music_vault.core.sync_schema import (
    PLAYLIST_TRACK_ORIGINS_TABLE,
    SOURCE_IDENTITY_CONFLICTS_TABLE,
    SOURCE_TRACK_IDENTITIES_TABLE,
    SYNC_SOURCE_ITEMS_TABLE,
    SYNC_SOURCE_RUNS_TABLE,
    SYNC_SOURCES_TABLE,
    required_sync_indexes,
)
from music_vault.metadata.remediation_schema import create_remediation_schema
from music_vault.metadata.schema import create_metadata_schema, seed_existing_metadata


def _schema_v4_database(path: Path, existing_file: Path) -> Path:
    missing_file = path.parent / "missing.media"
    existing_file.write_bytes(b"synthetic")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
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
            release_date TEXT,
            metadata_updated_at TEXT,
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
            PRIMARY KEY (playlist_id, track_id),
            FOREIGN KEY (playlist_id) REFERENCES playlists(id),
            FOREIGN KEY (track_id) REFERENCES tracks(id)
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
        """
    )
    connection.execute(
        """
        INSERT INTO tracks(path,title,source_kind,source_video_id)
        VALUES (?, 'Missing duplicate', 'youtube', 'abcdefghijk')
        """,
        (str(missing_file),),
    )
    connection.execute(
        """
        INSERT INTO tracks(path,title,source_kind,source_video_id)
        VALUES (?, 'Existing duplicate', 'youtube', 'abcdefghijk')
        """,
        (str(existing_file),),
    )
    connection.execute("INSERT INTO playlists(name) VALUES ('Synthetic Mix')")
    connection.execute(
        "INSERT INTO playlist_tracks(playlist_id,track_id,position) VALUES(1,2,0)"
    )
    connection.execute(
        "INSERT INTO playlist_tracks(playlist_id,track_id,position) VALUES(1,1,1)"
    )
    create_metadata_schema(connection)
    seed_existing_metadata(connection)
    create_remediation_schema(connection)
    connection.execute(
        """
        INSERT INTO metadata_remediation_jobs (
            id, created_at, updated_at, mode, provider, library_revision
        ) VALUES ('job', 't0', 't0', 'dry_run', 'synthetic', 'revision')
        """
    )
    connection.execute(
        """
        INSERT INTO metadata_remediation_items (
            job_id, track_id, current_snapshot, created_at, updated_at
        ) VALUES ('job', 2, '{}', 't0', 't0')
        """
    )
    connection.execute("PRAGMA user_version=4")
    connection.commit()
    connection.close()
    return path


def _source_values(external_id: str, storage_key: str, destination=None):
    kind = "playlist" if destination is not None else "library"
    return (
        "youtube_playlist",
        external_id,
        f"https://www.youtube.com/playlist?list={external_id}",
        1,
        0,
        kind,
        destination,
        storage_key,
        "t0",
        "t0",
    )


def _insert_source(connection, external_id: str, storage_key: str, destination=None):
    return connection.execute(
        """
        INSERT INTO sync_sources (
            source_kind, external_id, source_url, enabled, sort_order,
            destination_kind, destination_playlist_id, storage_key,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _source_values(external_id, storage_key, destination),
    ).lastrowid


def test_new_database_keeps_batch10_structures_at_latest_schema(tmp_path):
    db = MusicVaultDB(tmp_path / "new.sqlite3")
    assert CURRENT_SCHEMA_VERSION == 6
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 6
    tables = {
        str(row[0])
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        SYNC_SOURCES_TABLE,
        SYNC_SOURCE_ITEMS_TABLE,
        SOURCE_TRACK_IDENTITIES_TABLE,
        SOURCE_IDENTITY_CONFLICTS_TABLE,
        PLAYLIST_TRACK_ORIGINS_TABLE,
        SYNC_SOURCE_RUNS_TABLE,
    } <= tables
    indexes = {
        str(row[0])
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert set(required_sync_indexes()) <= indexes
    failure_columns = {
        str(row[1]) for row in db.conn.execute("PRAGMA table_info(sync_failures)")
    }
    assert {"sync_source_id", "source_item_id"} <= failure_columns
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()


def test_v4_to_latest_migration_is_backed_up_preserving_and_idempotent(tmp_path):
    path = _schema_v4_database(tmp_path / "library.sqlite3", tmp_path / "exists.media")
    backups = tmp_path / "backups"
    db = MusicVaultDB(path, backup_dir=backups)

    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 6
    assert db.last_migration_backup and db.last_migration_backup.is_file()
    with sqlite3.connect(db.last_migration_backup) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 4
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
        assert backup.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0] == 2

    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
    assert db.conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0] == 2
    assert [
        tuple(row)
        for row in db.conn.execute(
            "SELECT track_id,position FROM playlist_tracks ORDER BY position"
        )
    ] == [(2, 0), (1, 1)]
    assert [
        tuple(row)
        for row in db.conn.execute(
            """
            SELECT track_id,origin_position FROM playlist_track_origins
            WHERE origin_kind='manual' ORDER BY origin_position
            """
        )
    ] == [(2, 0), (1, 1)]
    assert db.conn.execute(
        "SELECT COUNT(*) FROM playlist_track_origins WHERE origin_kind='sync_source'"
    ).fetchone()[0] == 0
    identity = db.conn.execute(
        "SELECT track_id FROM source_track_identities "
        "WHERE source_kind='youtube' AND external_track_id='abcdefghijk'"
    ).fetchone()
    assert identity[0] == 2
    conflict = db.conn.execute(
        """
        SELECT canonical_track_id,conflicting_track_id
        FROM source_identity_conflicts WHERE resolved_at IS NULL
        """
    ).fetchone()
    assert tuple(conflict) == (2, 1)
    assert db.conn.execute("SELECT COUNT(*) FROM track_metadata_fields").fetchone()[0] == 18
    assert db.conn.execute("SELECT COUNT(*) FROM metadata_remediation_jobs").fetchone()[0] == 1
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()

    backup_count = len(list(backups.glob("*.sqlite3")))
    reopened = MusicVaultDB(path, backup_dir=backups)
    assert reopened.last_migration_backup is None
    assert len(list(backups.glob("*.sqlite3"))) == backup_count
    assert reopened.conn.execute("SELECT COUNT(*) FROM playlist_track_origins").fetchone()[0] == 2
    assert reopened.conn.execute("SELECT COUNT(*) FROM source_track_identities").fetchone()[0] == 1
    assert reopened.conn.execute("SELECT COUNT(*) FROM source_identity_conflicts").fetchone()[0] == 1
    reopened.close()


def test_batch10_constraints_prevent_ambiguous_sources_and_origins(tmp_path):
    db = MusicVaultDB(tmp_path / "constraints.sqlite3")
    track = db.upsert_track(tmp_path / "synthetic.media", title="Synthetic")
    playlist_a = db.create_playlist("A")
    playlist_b = db.create_playlist("B")
    source_a = _insert_source(db.conn, "PL-source-a", "youtube_source_a", playlist_a)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_source(db.conn, "PL-source-a", "youtube_duplicate", playlist_b)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_source(db.conn, "PL-source-b", "youtube_source_b", playlist_a)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            """
            INSERT INTO sync_sources (
                source_kind, external_id, source_url, enabled, sort_order,
                destination_kind, destination_playlist_id, storage_key,
                created_at, updated_at
            ) VALUES ('youtube_playlist','PL-bad','https://example.test',1,0,
                      'library',1,'bad','t0','t0')
            """
        )

    db.add_track_to_playlist(playlist_a, track)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            """
            INSERT INTO playlist_track_origins (
                playlist_id,track_id,origin_kind,sync_source_id,
                origin_position,created_at,updated_at
            ) VALUES (?,?,'manual',NULL,9,'t0','t0')
            """,
            (playlist_a, track),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            """
            INSERT INTO playlist_track_origins (
                playlist_id,track_id,origin_kind,sync_source_id,
                origin_position,created_at,updated_at
            ) VALUES (?,?,'sync_source',NULL,0,'t0','t0')
            """,
            (playlist_a, track),
        )
    assert source_a
    db.close()


def test_identity_registration_prefers_existing_file_without_deleting_duplicate(tmp_path):
    db = MusicVaultDB(tmp_path / "identity.sqlite3")
    missing = db.upsert_track(
        tmp_path / "missing.media",
        source_kind="youtube",
        source_video_id="abcdefghijk",
    )
    media = tmp_path / "existing.media"
    media.write_bytes(b"synthetic")
    existing = db.upsert_track(
        media,
        source_kind="youtube",
        source_video_id="abcdefghijk",
    )
    assert db.canonical_track_id("youtube", "abcdefghijk") == existing
    assert db.canonical_track_id(
        "youtube", "abcdefghijk", require_existing_file=True
    ) == existing
    assert db.source_identity_conflict_count() == 1
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
    assert db.get_track(missing) is not None
    db.close()
