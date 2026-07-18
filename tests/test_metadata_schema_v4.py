from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
from music_vault.metadata.remediation_schema import (
    PROVIDER_CACHE_TABLE,
    REMEDIATION_ITEMS_TABLE,
    REMEDIATION_JOBS_TABLE,
    required_remediation_indexes,
)
from music_vault.metadata.schema import create_metadata_schema


def _v3_database(path: Path) -> Path:
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
    create_metadata_schema(connection)
    connection.execute(
        """
        INSERT INTO tracks (
            path, title, artist, album, year, release_date, metadata_updated_at
        ) VALUES (?, 'Synthetic Title', 'Synthetic Artist', 'Synthetic Album',
                  '2001', '2001', '2026-01-01T00:00:00Z')
        """,
        (str(path.parent / "synthetic.media"),),
    )
    connection.execute("INSERT INTO playlists(name) VALUES('Synthetic Playlist')")
    connection.execute(
        "INSERT INTO playlist_tracks(playlist_id, track_id, position) VALUES(1, 1, 0)"
    )
    connection.execute(
        """
        INSERT INTO track_metadata_fields (
            track_id, field_name, value, provenance, confidence,
            is_manual, is_locked, updated_at
        ) VALUES (1, 'title', 'Synthetic Title', 'manual', 100, 1, 1,
                  '2026-01-01T00:00:00Z')
        """
    )
    connection.execute(
        """
        INSERT INTO track_metadata_observations (
            observation_key, track_id, provider, field_name, value,
            confidence, observed_at
        ) VALUES ('synthetic-observation', 1, 'embedded', 'title',
                  'Synthetic Title', 80, '2026-01-01T00:00:00Z')
        """
    )
    connection.execute(
        """
        INSERT INTO track_metadata_history (
            change_group_id, track_id, field_name, old_value, new_value,
            old_provenance, new_provenance, old_confidence, new_confidence,
            old_is_manual, new_is_manual, old_is_locked, new_is_locked,
            actor, reason, changed_at
        ) VALUES (
            'synthetic-group', 1, 'title', 'Old Synthetic Title', 'Synthetic Title',
            'embedded', 'manual', 80, 100, 0, 1, 0, 1,
            'test', 'synthetic change', '2026-01-01T00:00:00Z'
        )
        """
    )
    connection.execute("PRAGMA user_version=3")
    connection.commit()
    connection.close()
    return path


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def test_new_database_keeps_schema_v4_remediation_structures_at_latest_schema(tmp_path):
    db = MusicVaultDB(tmp_path / "new.sqlite3", backup_dir=tmp_path / "backups")

    assert CURRENT_SCHEMA_VERSION == 7
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert db.last_migration_backup is None
    tables = {
        str(row[0])
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        REMEDIATION_JOBS_TABLE,
        REMEDIATION_ITEMS_TABLE,
        PROVIDER_CACHE_TABLE,
    } <= tables
    assert {
        "id",
        "status",
        "mode",
        "provider",
        "library_revision",
        "total_items",
        "analyzed_items",
        "high_confidence_items",
        "review_items",
        "ambiguous_items",
        "no_match_items",
        "skipped_items",
        "failed_items",
        "applied_items",
        "file_written_items",
        "rolled_back_items",
        "last_error",
        "private_report_path",
    } <= _table_columns(db.conn, REMEDIATION_JOBS_TABLE)
    assert {
        "job_id",
        "track_id",
        "current_snapshot",
        "proposed_patch",
        "candidate_snapshot",
        "confidence_score",
        "confidence_class",
        "match_reasons",
        "provider_recording_id",
        "provider_release_id",
        "artwork_candidate",
        "approved_fields",
        "file_write_status",
        "original_file_hash",
        "original_audio_payload_hash",
        "backup_file",
        "prepared_file",
        "updated_file_hash",
        "updated_audio_payload_hash",
        "applied_change_group_id",
        "rollback_change_group_id",
        "applied_snapshot",
    } <= _table_columns(db.conn, REMEDIATION_ITEMS_TABLE)
    assert {
        "provider",
        "normalized_query_key",
        "response_status",
        "candidate_data",
        "fetched_at",
        "expires_at",
    } == _table_columns(db.conn, PROVIDER_CACHE_TABLE)
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    db.close()


def test_v3_to_latest_migration_is_backed_up_additive_and_preserves_all_state(tmp_path):
    path = _v3_database(tmp_path / "library.sqlite3")
    backups = tmp_path / "backups"
    db = MusicVaultDB(path, backup_dir=backups)

    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert db.last_migration_backup and db.last_migration_backup.is_file()
    with sqlite3.connect(db.last_migration_backup) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 3
        assert backup.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
        assert backup.execute("SELECT COUNT(*) FROM playlists").fetchone()[0] == 1
        assert backup.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0] == 1
        assert backup.execute("SELECT COUNT(*) FROM track_metadata_fields").fetchone()[0] == 1
        assert backup.execute("SELECT COUNT(*) FROM track_metadata_observations").fetchone()[0] == 1
        assert backup.execute("SELECT COUNT(*) FROM track_metadata_history").fetchone()[0] == 1

    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0] == 1
    field = db.conn.execute(
        """SELECT value, provenance, confidence, is_manual, is_locked
           FROM track_metadata_fields WHERE track_id=1 AND field_name='title'"""
    ).fetchone()
    assert tuple(field) == ("Synthetic Title", "manual", 100.0, 1, 1)
    assert db.conn.execute(
        """SELECT COUNT(*) FROM track_metadata_observations
           WHERE observation_key='synthetic-observation'"""
    ).fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT change_group_id FROM track_metadata_history"
    ).fetchone()[0] == "synthetic-group"
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.conn.execute(
        f"""
        INSERT INTO {REMEDIATION_JOBS_TABLE} (
            id, created_at, updated_at, mode, provider, library_revision,
            total_items
        ) VALUES ('migrated-job', 't0', 't0', 'dry_run', 'musicbrainz',
                  'revision-1', 1)
        """
    )
    db.conn.execute(
        f"""
        INSERT INTO {REMEDIATION_ITEMS_TABLE} (
            job_id, track_id, current_snapshot, created_at, updated_at
        ) VALUES ('migrated-job', 1, '{{}}', 't0', 't0')
        """
    )
    db.conn.execute(
        f"""
        INSERT INTO {PROVIDER_CACHE_TABLE} (
            provider, normalized_query_key, response_status, candidate_data,
            fetched_at, expires_at
        ) VALUES ('musicbrainz', 'migrated-query', 'no_match', '[]', 't0', 't1')
        """
    )
    db.conn.commit()
    db.close()
    backup_count = len(list(backups.glob("*.sqlite3")))
    reopened = MusicVaultDB(path, backup_dir=backups)
    assert reopened.last_migration_backup is None
    assert len(list(backups.glob("*.sqlite3"))) == backup_count
    assert reopened.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    assert reopened.conn.execute("SELECT COUNT(*) FROM track_metadata_history").fetchone()[0] == 1
    assert reopened.conn.execute(
        f"SELECT COUNT(*) FROM {REMEDIATION_JOBS_TABLE}"
    ).fetchone()[0] == 1
    assert reopened.conn.execute(
        f"SELECT COUNT(*) FROM {REMEDIATION_ITEMS_TABLE}"
    ).fetchone()[0] == 1
    assert reopened.conn.execute(
        f"SELECT COUNT(*) FROM {PROVIDER_CACHE_TABLE}"
    ).fetchone()[0] == 1
    reopened.close()


def test_current_v4_open_migrates_and_repairs_prerelease_remediation_schema(tmp_path):
    path = _v3_database(tmp_path / "prerelease-v4.sqlite3")
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=4")

    db = MusicVaultDB(path, backup_dir=tmp_path / "backups")
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert "approved_fields" in _table_columns(db.conn, REMEDIATION_ITEMS_TABLE)
        assert "prepared_file" in _table_columns(db.conn, REMEDIATION_ITEMS_TABLE)
        assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()


def test_remediation_constraints_foreign_keys_indexes_and_cache_identity(tmp_path):
    db = MusicVaultDB(tmp_path / "constraints.sqlite3")
    db.conn.execute(
        "INSERT INTO tracks(path, title) VALUES(?, 'Synthetic')",
        (str(tmp_path / "synthetic.media"),),
    )
    db.conn.execute(
        f"""
        INSERT INTO {REMEDIATION_JOBS_TABLE} (
            id, created_at, updated_at, status, mode, provider, library_revision,
            total_items
        ) VALUES ('job-1', 't0', 't0', 'created', 'dry_run', 'musicbrainz',
                  'revision-1', 1)
        """
    )
    db.conn.execute(
        f"""
        INSERT INTO {REMEDIATION_ITEMS_TABLE} (
            job_id, track_id, current_snapshot, created_at, updated_at
        ) VALUES ('job-1', 1, '{{}}', 't0', 't0')
        """
    )
    db.conn.execute(
        f"""
        INSERT INTO {PROVIDER_CACHE_TABLE} (
            provider, normalized_query_key, response_status, candidate_data,
            fetched_at, expires_at
        ) VALUES ('musicbrainz', 'query-1', 'success', '[]', 't0', 't1')
        """
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            f"UPDATE {REMEDIATION_JOBS_TABLE} SET status='not-a-status' WHERE id='job-1'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            f"UPDATE {REMEDIATION_ITEMS_TABLE} SET confidence_score=101 WHERE job_id='job-1'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            f"UPDATE {REMEDIATION_ITEMS_TABLE} SET file_write_status='pretend-success' "
            "WHERE job_id='job-1'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            f"""
            INSERT INTO {PROVIDER_CACHE_TABLE} (
                provider, normalized_query_key, response_status, candidate_data,
                fetched_at, expires_at
            ) VALUES ('musicbrainz', 'query-1', 'no_match', '[]', 't0', 't1')
            """
        )

    index_names = {
        str(row[0])
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert set(required_remediation_indexes()) <= index_names
    item_foreign_keys = {
        (str(row[2]), str(row[3]), str(row[4]), str(row[6]).upper())
        for row in db.conn.execute(f"PRAGMA foreign_key_list({REMEDIATION_ITEMS_TABLE})")
    }
    assert item_foreign_keys == {
        ("tracks", "track_id", "id", "CASCADE"),
        (REMEDIATION_JOBS_TABLE, "job_id", "id", "CASCADE"),
    }
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    db.conn.execute("DELETE FROM tracks WHERE id=1")
    assert db.conn.execute(
        f"SELECT COUNT(*) FROM {REMEDIATION_ITEMS_TABLE}"
    ).fetchone()[0] == 0
    assert db.conn.execute(
        f"SELECT COUNT(*) FROM {REMEDIATION_JOBS_TABLE}"
    ).fetchone()[0] == 1
    db.close()
