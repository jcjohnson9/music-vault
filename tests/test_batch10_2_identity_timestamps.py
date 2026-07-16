from __future__ import annotations

from pathlib import Path

from music_vault.core import db as db_module
from music_vault.core.db import MusicVaultDB
from music_vault.core import sync_schema


FIRST_TIMESTAMP = "2026-07-16T01:00:00Z"
SECOND_TIMESTAMP = "2026-07-16T02:00:00Z"
VIDEO_ID = "abcdefghijk"


def _insert_youtube_track(db: MusicVaultDB, path: Path, video_id: str = VIDEO_ID) -> int:
    return int(
        db.conn.execute(
            """
            INSERT INTO tracks(path, title, source_kind, source_video_id)
            VALUES (?, 'Synthetic identity fixture', 'youtube', ?)
            """,
            (str(path), video_id),
        ).lastrowid
    )


def _identity_row(db: MusicVaultDB):
    return db.conn.execute(
        """
        SELECT source_kind, external_track_id, track_id, first_seen_at, updated_at
        FROM source_track_identities
        """
    ).fetchone()


def _audit_identity_updates(db: MusicVaultDB) -> None:
    db.conn.executescript(
        """
        CREATE TEMP TABLE identity_update_audit(event TEXT NOT NULL);
        CREATE TEMP TRIGGER audit_source_track_identity_updates
        AFTER UPDATE ON source_track_identities
        BEGIN
            INSERT INTO identity_update_audit(event) VALUES ('update');
        END;
        """
    )


def test_new_identity_gets_timestamps_and_identical_backfill_is_write_free(
    tmp_path, monkeypatch
):
    db = MusicVaultDB(tmp_path / "identity-noop.sqlite3")
    track_id = _insert_youtube_track(db, tmp_path / "synthetic.media")
    timestamps = iter((FIRST_TIMESTAMP, SECOND_TIMESTAMP))
    monkeypatch.setattr(sync_schema, "utc_now", lambda: next(timestamps))

    assert sync_schema.backfill_source_track_identities(db.conn) == (1, 0)
    original = tuple(_identity_row(db))
    assert original == (
        "youtube",
        VIDEO_ID,
        track_id,
        FIRST_TIMESTAMP,
        FIRST_TIMESTAMP,
    )

    _audit_identity_updates(db)
    changes_before = db.conn.total_changes
    assert sync_schema.backfill_source_track_identities(db.conn) == (1, 0)

    assert db.conn.total_changes == changes_before
    assert db.conn.execute("SELECT COUNT(*) FROM identity_update_audit").fetchone()[0] == 0
    assert tuple(_identity_row(db)) == original
    assert db.conn.execute("SELECT COUNT(*) FROM source_track_identities").fetchone()[0] == 1
    db.close()


def test_actual_canonical_mapping_change_updates_only_updated_at(
    tmp_path, monkeypatch
):
    db = MusicVaultDB(tmp_path / "identity-change.sqlite3")
    original_track_id = _insert_youtube_track(db, tmp_path / "original.media")
    timestamps = iter((FIRST_TIMESTAMP, SECOND_TIMESTAMP))
    monkeypatch.setattr(sync_schema, "utc_now", lambda: next(timestamps))
    sync_schema.backfill_source_track_identities(db.conn)

    db.conn.execute(
        "UPDATE tracks SET source_kind=NULL, source_video_id=NULL WHERE id=?",
        (original_track_id,),
    )
    replacement_track_id = _insert_youtube_track(db, tmp_path / "replacement.media")
    _audit_identity_updates(db)

    assert sync_schema.backfill_source_track_identities(db.conn) == (1, 0)
    changed = tuple(_identity_row(db))
    assert changed == (
        "youtube",
        VIDEO_ID,
        replacement_track_id,
        FIRST_TIMESTAMP,
        SECOND_TIMESTAMP,
    )
    assert db.conn.execute("SELECT COUNT(*) FROM identity_update_audit").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM source_track_identities").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
    assert db.get_track(original_track_id) is not None
    db.close()


def test_missing_file_recovery_and_duplicate_conflict_rerun_are_non_destructive(
    tmp_path, monkeypatch
):
    db = MusicVaultDB(tmp_path / "identity-conflict.sqlite3")
    missing_track_id = _insert_youtube_track(db, tmp_path / "missing.media")
    existing_path = tmp_path / "existing.media"
    existing_path.write_bytes(b"synthetic media sentinel")
    existing_track_id = _insert_youtube_track(db, existing_path)
    timestamps = iter((FIRST_TIMESTAMP, SECOND_TIMESTAMP))
    monkeypatch.setattr(sync_schema, "utc_now", lambda: next(timestamps))

    assert sync_schema.backfill_source_track_identities(db.conn) == (1, 1)
    original_identity = tuple(_identity_row(db))
    original_conflict = tuple(
        db.conn.execute(
            """
            SELECT source_kind, external_track_id, canonical_track_id,
                   conflicting_track_id, reason, created_at, resolved_at
            FROM source_identity_conflicts
            """
        ).fetchone()
    )
    assert original_identity == (
        "youtube",
        VIDEO_ID,
        existing_track_id,
        FIRST_TIMESTAMP,
        FIRST_TIMESTAMP,
    )
    assert original_conflict[0:4] == (
        "youtube",
        VIDEO_ID,
        existing_track_id,
        missing_track_id,
    )

    _audit_identity_updates(db)
    changes_before = db.conn.total_changes
    assert sync_schema.backfill_source_track_identities(db.conn) == (1, 1)

    assert db.conn.total_changes == changes_before
    assert tuple(_identity_row(db)) == original_identity
    assert tuple(
        db.conn.execute(
            """
            SELECT source_kind, external_track_id, canonical_track_id,
                   conflicting_track_id, reason, created_at, resolved_at
            FROM source_identity_conflicts
            """
        ).fetchone()
    ) == original_conflict
    assert db.conn.execute("SELECT COUNT(*) FROM source_track_identities").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM source_identity_conflicts").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
    assert db.get_track(missing_track_id) is not None
    assert db.get_track(existing_track_id) is not None
    db.close()


def test_current_schema_reopen_keeps_identity_mapping_and_timestamps(tmp_path):
    path = tmp_path / "identity-reopen.sqlite3"
    db = MusicVaultDB(path)
    track_id = _insert_youtube_track(db, tmp_path / "reopen.media")
    db.conn.execute(
        """
        INSERT INTO source_track_identities(
            source_kind, external_track_id, track_id, first_seen_at, updated_at
        ) VALUES ('youtube', ?, ?, ?, ?)
        """,
        (VIDEO_ID, track_id, FIRST_TIMESTAMP, FIRST_TIMESTAMP),
    )
    db.conn.commit()
    before = tuple(_identity_row(db))
    db.close()

    reopened = MusicVaultDB(path)
    assert reopened.conn.execute("PRAGMA user_version").fetchone()[0] == 6
    assert tuple(_identity_row(reopened)) == before
    assert reopened.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert reopened.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    reopened.close()


def test_registering_identical_mapping_is_write_free(tmp_path, monkeypatch):
    db = MusicVaultDB(tmp_path / "register-identical.sqlite3")
    track_id = _insert_youtube_track(db, tmp_path / "identical.media")
    timestamps = iter((FIRST_TIMESTAMP, SECOND_TIMESTAMP))
    monkeypatch.setattr(db_module, "sync_utc_now", lambda: next(timestamps))

    assert db.register_source_identity("youtube", VIDEO_ID, track_id) == track_id
    original = tuple(_identity_row(db))
    _audit_identity_updates(db)
    changes_before = db.conn.total_changes

    assert db.register_source_identity("youtube", VIDEO_ID, track_id) == track_id
    assert db.conn.total_changes == changes_before
    assert db.conn.execute("SELECT COUNT(*) FROM identity_update_audit").fetchone()[0] == 0
    assert tuple(_identity_row(db)) == original
    db.close()


def test_upsert_refresh_preserves_identical_identity_timestamp(tmp_path):
    db = MusicVaultDB(tmp_path / "upsert-identical.sqlite3")
    media = tmp_path / "upsert.media"
    media.write_bytes(b"synthetic upsert sentinel")
    track_id = db.upsert_track(
        media,
        title="Synthetic refresh fixture",
        source_kind="youtube",
        source_video_id=VIDEO_ID,
    )
    original = tuple(_identity_row(db))
    _audit_identity_updates(db)

    refreshed_id = db.upsert_track(
        media,
        title="Synthetic refresh fixture",
        source_kind="youtube",
        source_video_id=VIDEO_ID,
    )

    assert refreshed_id == track_id
    assert db.conn.execute("SELECT COUNT(*) FROM identity_update_audit").fetchone()[0] == 0
    assert tuple(_identity_row(db)) == original
    db.close()


def test_lower_priority_conflict_preserves_identity_timestamp_but_records_conflict(
    tmp_path, monkeypatch
):
    db = MusicVaultDB(tmp_path / "register-lower-priority.sqlite3")
    existing_media = tmp_path / "existing.media"
    existing_media.write_bytes(b"synthetic existing sentinel")
    existing_id = _insert_youtube_track(db, existing_media)
    lower_priority_id = _insert_youtube_track(db, tmp_path / "missing.media")
    timestamps = iter((FIRST_TIMESTAMP, SECOND_TIMESTAMP))
    monkeypatch.setattr(db_module, "sync_utc_now", lambda: next(timestamps))
    assert db.register_source_identity("youtube", VIDEO_ID, existing_id) == existing_id
    original = tuple(_identity_row(db))
    _audit_identity_updates(db)

    assert (
        db.register_source_identity("youtube", VIDEO_ID, lower_priority_id)
        == existing_id
    )
    assert db.conn.execute("SELECT COUNT(*) FROM identity_update_audit").fetchone()[0] == 0
    assert tuple(_identity_row(db)) == original
    conflict = db.conn.execute(
        """
        SELECT canonical_track_id, conflicting_track_id, resolved_at
        FROM source_identity_conflicts
        """
    ).fetchone()
    assert tuple(conflict) == (existing_id, lower_priority_id, None)
    db.close()


def test_higher_priority_conflict_changes_mapping_and_updated_at_only(
    tmp_path, monkeypatch
):
    db = MusicVaultDB(tmp_path / "register-higher-priority.sqlite3")
    missing_id = _insert_youtube_track(db, tmp_path / "missing.media")
    replacement_media = tmp_path / "replacement.media"
    replacement_media.write_bytes(b"synthetic replacement sentinel")
    replacement_id = _insert_youtube_track(db, replacement_media)
    timestamps = iter((FIRST_TIMESTAMP, SECOND_TIMESTAMP))
    monkeypatch.setattr(db_module, "sync_utc_now", lambda: next(timestamps))
    assert db.register_source_identity("youtube", VIDEO_ID, missing_id) == missing_id
    _audit_identity_updates(db)

    assert (
        db.register_source_identity("youtube", VIDEO_ID, replacement_id)
        == replacement_id
    )
    assert tuple(_identity_row(db)) == (
        "youtube",
        VIDEO_ID,
        replacement_id,
        FIRST_TIMESTAMP,
        SECOND_TIMESTAMP,
    )
    assert db.conn.execute("SELECT COUNT(*) FROM identity_update_audit").fetchone()[0] == 1
    db.close()
