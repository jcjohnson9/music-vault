from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from music_vault.core import db as database_module
from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
from music_vault.core.listening_schema import LISTENING_TABLES, required_listening_indexes
from music_vault.core.playlist_membership import PlaylistMembershipService
from music_vault.core.sync_sources import SyncSourceService
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
from music_vault.metadata.service import MetadataService


def snapshot(conn, tables=None):
    definitions = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
    return {
        name: (definitions[name], sorted((tuple(row) for row in conn.execute(f'SELECT * FROM "{name}"')), key=repr))
        for name in sorted(definitions if tables is None else tables)
    }


def schema8(tmp_path, *, populated=True):
    path = tmp_path / "synthetic.sqlite3"
    db = MusicVaultDB(path)
    if populated:
        (tmp_path / "synthetic-one.fixture").write_bytes(b"synthetic media one; no embedded tag operations")
        (tmp_path / "synthetic-two.fixture").write_bytes(b"synthetic media two; no embedded tag operations")
        first = db.upsert_track(tmp_path / "synthetic-one.fixture", title="Synthetic One", artist="Synthetic Duo & Co", album="Synthetic Album", album_artist="Synthetic Duo & Co", release_date="2001-02-03")
        second = db.upsert_track(tmp_path / "synthetic-two.fixture", title="Synthetic Two", artist="Synthetic Soloist", album="Synthetic Album", release_date="2024-01-01")
        MetadataService(db).apply_manual_patch(first, {"title": "Synthetic Locked Title"})
        playlist = db.create_playlist("Synthetic Mix")
        db.add_track_to_playlist(playlist, first)
        source = SyncSourceService(db).create_source("PLsynthetic12345", destination_kind="playlist", destination_playlist_id=playlist)
        PlaylistMembershipService(db).set_source_origins(source.id, playlist, [(first, 0), (second, 1)])
        db.register_source_identity("youtube", "abcdefghijk", first)
        db.register_source_identity("youtube", "abcdefghijk", second)
        MetadataIntelligenceJobStore(db).create_existing_library_job([first, second])
        db.record_sync_failure(playlist_id="PLsynthetic12345", playlist_title="Synthetic", video_id="abcdefghijk", title="Synthetic", reason="Synthetic failure", error_category="unknown", sync_source_id=source.id)
        with db.conn:
            artists = [row[0] for row in db.conn.execute("SELECT id FROM artists ORDER BY id")]
            db.conn.execute("""INSERT INTO artist_aliases(artist_id,alias_name,normalized_alias,alias_kind,provenance,created_at)
                VALUES(?,'Synthetic Alias','synthetic alias','display_variant','manual','unchanged-created')""", (artists[0],))
            db.conn.execute("""INSERT INTO artist_relationships(subject_artist_id,related_artist_id,relationship_kind,provenance,created_at,updated_at)
                VALUES(?,?,'member_of','manual','unchanged-created','unchanged-updated')""", (artists[0], artists[-1]))
            db.conn.execute("""INSERT INTO metadata_remediation_jobs(id,created_at,updated_at,mode,provider,library_revision)
                VALUES('synthetic-job','unchanged-created','unchanged-updated','analyze','synthetic','synthetic-revision')""")
            db.conn.execute("""INSERT INTO metadata_remediation_items(job_id,track_id,current_snapshot,created_at,updated_at)
                VALUES('synthetic-job',?,'{}','unchanged-created','unchanged-updated')""", (first,))
            db.conn.execute("UPDATE tracks SET created_at='original-created',updated_at='original-updated'")
    with db.conn:
        for table in LISTENING_TABLES:
            db.conn.execute(f"DROP TABLE {table}")
        db.conn.execute("PRAGMA user_version=8")
    before = snapshot(db.conn)
    db.close()
    return path, before


def forbid_seeds(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Schema 8/9 listening startup must not reseed unrelated data")
    for name in (
        "create_metadata_schema", "create_remediation_schema", "create_sync_schema", "create_metadata_intelligence_schema",
        "seed_existing_metadata", "seed_existing_playlist_origins", "backfill_source_track_identities",
        "seed_existing_metadata_field_extensions", "seed_existing_artist_credits", "create_canonical_media_schema",
        "seed_existing_canonical_albums", "create_media_quality_schema", "seed_existing_track_media_quality",
    ):
        monkeypatch.setattr(database_module, name, forbidden)


def test_populated8_to9_full_row_preservation_backup_and_current9_idempotence(tmp_path, monkeypatch):
    path, before = schema8(tmp_path)
    assert before["track_metadata_history"][1]
    assert before["track_artist_credits"][1]
    assert before["track_album_memberships"][1]
    assert before["source_identity_conflicts"][1]
    assert before["playlist_track_origins"][1]
    assert before["metadata_intelligence_items"][1]
    assert before["metadata_remediation_items"][1]
    assert before["track_media_quality"][1]
    assert before["artist_aliases"][1] and before["artist_relationships"][1]
    media_before = {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in tmp_path.glob("*.fixture")}
    forbid_seeds(monkeypatch)
    original_open = Path.open
    def forbid_media_open(self, *args, **kwargs):
        if self.suffix == ".fixture":
            raise AssertionError("Listening migration must not open media")
        return original_open(self, *args, **kwargs)
    with monkeypatch.context() as media_guard:
        media_guard.setattr(Path, "open", forbid_media_open)
        migrated = MusicVaultDB(path, backup_dir=tmp_path / "backups")
    assert CURRENT_SCHEMA_VERSION == 9
    assert migrated.conn.execute("PRAGMA user_version").fetchone()[0] == 9
    assert migrated.migration_performed and migrated.migrated_from_version == 8
    assert migrated.migrated_to_version == 9
    assert snapshot(migrated.conn, before) == before
    assert set(snapshot(migrated.conn)) - set(before) == set(LISTENING_TABLES)
    for table in LISTENING_TABLES:
        assert migrated.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    indexes = {row[0] for row in migrated.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert set(required_listening_indexes()) <= indexes
    assert migrated.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert migrated.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrated.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    backup = migrated.last_migration_backup
    assert backup.is_file() and backup.stat().st_size > 0
    with sqlite3.connect(backup) as rollback:
        assert rollback.execute("PRAGMA user_version").fetchone()[0] == 8
        assert snapshot(rollback) == before
    after = snapshot(migrated.conn)
    migrated.close()
    reopened = MusicVaultDB(path, backup_dir=tmp_path / "backups")
    assert not reopened.migration_performed and reopened.last_migration_backup is None
    assert snapshot(reopened.conn) == after
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1
    reopened.close()
    assert {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in tmp_path.glob("*.fixture")} == media_before
    assert not (tmp_path / "dist" / "MusicVault" / "data").exists()


def test_empty8_to9_preserves_all_existing_empty_tables(tmp_path, monkeypatch):
    path, before = schema8(tmp_path, populated=False)
    forbid_seeds(monkeypatch)
    db = MusicVaultDB(path)
    assert snapshot(db.conn, before) == before
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 9
    assert db.listening.favorite_ids() == set()
    assert db.listening.history_page() == []
    db.close()


@pytest.mark.parametrize("failure", ["ddl", "index", "integrity", "preservation"])
def test_migration_failure_rolls_back_all_changes_and_preserves_backup(tmp_path, monkeypatch, failure):
    path, before = schema8(tmp_path)
    original = database_module.create_listening_schema

    def broken(conn):
        if failure == "ddl":
            conn.execute("CREATE TABLE listening_partial(value TEXT)")
            raise sqlite3.OperationalError("synthetic DDL failure")
        original(conn)
        if failure == "index":
            raise sqlite3.OperationalError("synthetic index failure")
        if failure == "preservation":
            conn.execute("UPDATE tracks SET updated_at='must roll back'")

    monkeypatch.setattr(database_module, "create_listening_schema", broken)
    if failure == "integrity":
        def broken_integrity(self):
            raise RuntimeError("synthetic integrity failure")
        monkeypatch.setattr(MusicVaultDB, "_verify_database_integrity", broken_integrity)
    with pytest.raises((RuntimeError, sqlite3.OperationalError)):
        MusicVaultDB(path, backup_dir=tmp_path / "backups")
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
        assert snapshot(conn) == before
    backups = list((tmp_path / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert snapshot(conn) == before


def test_backup_equal_counts_but_changed_values_is_rejected_before_migration(tmp_path, monkeypatch):
    path, before = schema8(tmp_path)
    original = MusicVaultDB._create_pre_migration_backup

    def wrong_backup(self, version):
        backup = original(self, version)
        with sqlite3.connect(backup) as conn:
            conn.execute("UPDATE tracks SET updated_at='wrong backup values'")
        return backup

    monkeypatch.setattr(MusicVaultDB, "_create_pre_migration_backup", wrong_backup)
    with pytest.raises(RuntimeError, match="backup failed full-row"):
        MusicVaultDB(path, backup_dir=tmp_path / "backups")
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
        assert snapshot(conn) == before


def test_older_supported_upgrade_and_fresh_database_install_empty_listening_tables(v0_database, tmp_path):
    for path in (v0_database(), tmp_path / "fresh.sqlite3"):
        db = MusicVaultDB(path)
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 9
        assert db.listening.history_page() == []
        assert db.listening.favorite_ids() == set()
        assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        db.close()


def test_listening_writes_preserve_all_non_listening_rows_and_source_detach_preserves_listens(tmp_path):
    path, _before = schema8(tmp_path)
    db = MusicVaultDB(path)
    tables = set(snapshot(db.conn)) - set(LISTENING_TABLES)
    baseline = snapshot(db.conn, tables)
    track_id = db.conn.execute("SELECT id FROM tracks ORDER BY id LIMIT 1").fetchone()[0]
    playlist_id = db.conn.execute("SELECT id FROM playlists LIMIT 1").fetchone()[0]
    assert db.listening.set_favorite(track_id, True)
    assert db.listening.save_event({
        "event_id": "synthetic-occurrence", "run_id": "synthetic-run", "track_id": track_id,
        "recorded_track_id": track_id, "title_at_start": "Synthetic", "artist_at_start": "Synthetic",
        "album_at_start": "Synthetic", "started_at": "2026-04-01T00:00:00Z",
        "last_observed_at": "2026-04-01T00:00:30Z", "listened_ms": 30000,
        "duration_ms": 60000, "playback_origin": "manual", "context_playlist_id": playlist_id,
        "update_sequence": 1, "qualified_at": "2026-04-01T00:00:30Z",
    })
    assert snapshot(db.conn, tables) == baseline
    listening = snapshot(db.conn, LISTENING_TABLES)
    source_id = db.conn.execute("SELECT id FROM sync_sources LIMIT 1").fetchone()[0]
    PlaylistMembershipService(db).detach_source(source_id)
    assert snapshot(db.conn, LISTENING_TABLES) == listening
    db.close()
