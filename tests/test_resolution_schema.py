from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from music_vault.core import db as database_module
from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
from music_vault.core.listening_schema import LISTENING_TABLES
from music_vault.core.playlist_membership import PlaylistMembershipService
from music_vault.core.sync_sources import SyncSourceService
from music_vault.metadata.resolution_schema import RESOLUTION_TABLES, required_resolution_indexes
from music_vault.metadata.service import MetadataAction, MetadataService


def snapshot(conn):
    definitions = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
    return {
        table: {
            "sql": sql,
            "columns": tuple(tuple(row) for row in conn.execute(f'PRAGMA table_info("{table}")')),
            "rows": sorted((tuple(row) for row in conn.execute(f'SELECT * FROM "{table}"')), key=repr),
        } for table, sql in definitions.items()
    }


def established_database(tmp_path, version, *, populated=True):
    path = tmp_path / "synthetic.sqlite3"
    db = MusicVaultDB(path)
    if populated:
        media = tmp_path / "synthetic.media"
        media.write_bytes(b"synthetic payload, not a real audio file")
        track = db.upsert_track(media, title="Synthetic", artist="Synthetic Group & Co", album="Fixture")
        MetadataService(db).apply_actions(track, {"album": MetadataAction.clear()})
        playlist = db.create_playlist("Synthetic playlist")
        db.add_track_to_playlist(playlist, track)
        source = SyncSourceService(db).create_source(
            "PLsynthetic12345", destination_kind="playlist", destination_playlist_id=playlist,
        )
        PlaylistMembershipService(db).set_source_origins(source.id, playlist, [(track, 0)])
        db.register_source_identity("youtube", "abcdefghijk", track)
        if version == 9:
            db.listening.set_favorite(track, True)
            db.listening.save_event({
                "event_id": "fixture-event", "run_id": "fixture-run", "track_id": track,
                "recorded_track_id": track, "title_at_start": "Synthetic", "artist_at_start": "Fixture",
                "album_at_start": "", "started_at": "2026-01-01T00:00:00Z",
                "last_observed_at": "2026-01-01T00:00:01Z", "listened_ms": 1000,
                "playback_origin": "manual_queue", "update_sequence": 1,
            })
        with db.conn:
            db.conn.execute("UPDATE tracks SET updated_at='unchanged-track-time'")
            db.conn.execute("UPDATE track_artist_credits SET updated_at='unchanged-credit-time'")
    with db.conn:
        for table in RESOLUTION_TABLES:
            db.conn.execute(f"DROP TABLE {table}")
        db.conn.execute("ALTER TABLE track_artist_credits DROP COLUMN credited_as")
        if version == 8:
            for table in LISTENING_TABLES:
                db.conn.execute(f"DROP TABLE {table}")
        db.conn.execute(f"PRAGMA user_version={version}")
    before = snapshot(db.conn)
    db.close()
    return path, before


def forbid_seeds(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Additive schema-10 migration must never seed or repair metadata")
    for name in (
        "create_metadata_schema", "create_remediation_schema", "create_sync_schema",
        "create_metadata_intelligence_schema", "seed_existing_metadata",
        "seed_existing_playlist_origins", "backfill_source_track_identities",
        "seed_existing_metadata_field_extensions", "seed_existing_artist_credits",
        "create_canonical_media_schema", "seed_existing_canonical_albums",
        "create_media_quality_schema", "seed_existing_track_media_quality",
    ):
        monkeypatch.setattr(database_module, name, denied)


def assert_old_projection_preserved(conn, before):
    after = snapshot(conn)
    for table, old in before.items():
        columns = old["columns"]
        assert after[table]["columns"][:len(columns)] == columns
        projection = ",".join(f'"{column[1]}"' for column in columns)
        rows = sorted((tuple(row) for row in conn.execute(f'SELECT {projection} FROM "{table}"')), key=repr)
        assert rows == old["rows"]
        if table == "track_artist_credits":
            assert after[table]["columns"][len(columns):] == ((len(columns), "credited_as", "TEXT", 0, None, 0),)
            normalized = re.sub(r",\s*credited_as\s+TEXT(?=\s*[,\)])", "", after[table]["sql"], flags=re.I)
            assert normalized == old["sql"]
        else:
            assert after[table]["sql"] == old["sql"]
    assert conn.execute("SELECT COUNT(*) FROM track_artist_credits WHERE credited_as IS NOT NULL").fetchone()[0] == 0


@pytest.mark.parametrize("version", [8, 9])
@pytest.mark.parametrize("populated", [False, True])
def test_additive_upgrade_preserves_original_values_and_verified_backup(tmp_path, monkeypatch, version, populated):
    path, before = established_database(tmp_path, version, populated=populated)
    media = tmp_path / "synthetic.media"
    media_before = (media.read_bytes(), media.stat().st_mtime_ns) if populated else None
    forbid_seeds(monkeypatch)
    original_open = Path.open
    def no_media_open(self, *args, **kwargs):
        if self.suffix == ".media":
            raise AssertionError("Migration must not inspect media")
        return original_open(self, *args, **kwargs)
    with monkeypatch.context() as guard:
        guard.setattr(Path, "open", no_media_open)
        db = MusicVaultDB(path, backup_dir=tmp_path / "backups")
    try:
        assert CURRENT_SCHEMA_VERSION == 10
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 10
        assert db.migration_performed and db.migrated_from_version == version
        assert db.migrated_to_version == 10
        assert_old_projection_preserved(db.conn, before)
        expected_new = set(RESOLUTION_TABLES) | (set(LISTENING_TABLES) if version == 8 else set())
        assert set(snapshot(db.conn)) - set(before) == expected_new
        for table in expected_new:
            assert db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        indexes = {row[0] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert set(required_resolution_indexes()) <= indexes
        assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if populated:
            assert db.last_migration_backup and db.last_migration_backup.is_file()
            with sqlite3.connect(db.last_migration_backup) as backup:
                assert backup.execute("PRAGMA user_version").fetchone()[0] == version
                assert snapshot(backup) == before
            assert (media.read_bytes(), media.stat().st_mtime_ns) == media_before
        after = snapshot(db.conn)
    finally:
        db.close()
    reopened = MusicVaultDB(path, backup_dir=tmp_path / "backups")
    try:
        assert not reopened.migration_performed
        assert reopened.last_migration_backup is None
        assert snapshot(reopened.conn) == after
    finally:
        reopened.close()


@pytest.mark.parametrize("failure", ["ddl", "old_value", "credited_backfill", "populated_new", "index", "integrity"])
def test_failed_migration_rolls_back_schema_rows_and_version(tmp_path, monkeypatch, failure):
    path, before = established_database(tmp_path, 9)
    original = database_module.create_resolution_schema
    def failing(conn):
        original(conn)
        if failure == "ddl":
            raise sqlite3.OperationalError("Synthetic DDL failure")
        if failure == "old_value":
            conn.execute("UPDATE tracks SET updated_at='must roll back'")
        if failure == "credited_backfill":
            conn.execute("UPDATE track_artist_credits SET credited_as='must remain NULL'")
        if failure == "populated_new":
            conn.execute("INSERT INTO metadata_evidence_bundles SELECT id,'key','fixture',1,'{}','now' FROM tracks")
        if failure == "index":
            conn.execute("DROP INDEX idx_metadata_materializations_track_created")
    monkeypatch.setattr(database_module, "create_resolution_schema", failing)
    if failure == "integrity":
        def deny_integrity(self):
            raise RuntimeError("Synthetic integrity failure")
        monkeypatch.setattr(MusicVaultDB, "_verify_database_integrity", deny_integrity)
    with pytest.raises((RuntimeError, sqlite3.OperationalError)):
        MusicVaultDB(path, backup_dir=tmp_path / "backups")
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
        assert snapshot(conn) == before
    backups = list((tmp_path / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert snapshot(backup) == before


def test_backup_with_same_counts_but_wrong_values_stops_before_ddl(tmp_path, monkeypatch):
    path, before = established_database(tmp_path, 8)
    original = MusicVaultDB._create_pre_migration_backup
    def bad_backup(self, version):
        path = original(self, version)
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE tracks SET updated_at='wrong-backup'")
        return path
    monkeypatch.setattr(MusicVaultDB, "_create_pre_migration_backup", bad_backup)
    with pytest.raises(RuntimeError, match="backup failed full-row"):
        MusicVaultDB(path, backup_dir=tmp_path / "backups")
    with sqlite3.connect(path) as conn:
        assert snapshot(conn) == before
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8


def test_fresh_and_legacy_database_receive_schema10(v0_database, tmp_path):
    for path in (v0_database(), tmp_path / "empty.sqlite3"):
        db = MusicVaultDB(path)
        try:
            assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 10
            assert "credited_as" in {row[1] for row in db.conn.execute("PRAGMA table_info(track_artist_credits)")}
            for table in RESOLUTION_TABLES:
                assert db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            db.close()


def test_evidence_keys_journal_constraints_and_track_foreign_keys(tmp_path):
    db = MusicVaultDB(tmp_path / "constraints.sqlite3")
    try:
        track = db.upsert_track(tmp_path / "not-opened.media", title="Synthetic")
        with db.conn:
            db.conn.execute("INSERT INTO metadata_evidence_bundles VALUES(?,?,?,?,?,?)", (track, "key", "fixture", 1, "{}", "now"))
            db.conn.execute("INSERT INTO metadata_materializations VALUES(?,?,?,?,?,?,?,?,?)", ("materialization", track, "proposal", "expected", "{}", "{}", "[]", "now", None))
        for sql, args in (
            ("INSERT INTO metadata_evidence_bundles VALUES(?,?,?,?,?,?)", (track, "key", "fixture", 1, "{}", "now")),
            ("INSERT INTO metadata_evidence_bundles VALUES(?,?,?,?,?,?)", (999, "key", "fixture", 1, "{}", "now")),
            ("INSERT INTO metadata_materializations VALUES(?,?,?,?,?,?,?,?,?)", ("different", track, "proposal", "expected", "{}", "{}", "[]", "now", None)),
        ):
            with pytest.raises(sqlite3.IntegrityError), db.conn:
                db.conn.execute(sql, args)
        with db.conn:
            db.conn.execute("DELETE FROM tracks WHERE id=?", (track,))
        for table in RESOLUTION_TABLES:
            assert db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        db.close()


def test_populated_current10_reopen_preserves_every_table_and_nonnull_credit(tmp_path, monkeypatch):
    path, _before_upgrade = established_database(tmp_path, 9)
    backups = tmp_path / "backups"
    db = MusicVaultDB(path, backup_dir=backups)
    try:
        track = db.conn.execute("SELECT id FROM tracks").fetchone()[0]
        with db.conn:
            db.conn.execute("UPDATE track_artist_credits SET credited_as='Recording Credit'")
            db.conn.execute(
                "INSERT INTO metadata_evidence_bundles VALUES(?,?,?,?,?,?)",
                (track, "fixture-evidence", "fixture", 1, '{"immutable":true}', "unchanged-evidence-time"),
            )
            db.conn.execute(
                "INSERT INTO metadata_materializations VALUES(?,?,?,?,?,?,?,?,?)",
                ("fixture-journal", track, "fixture-proposal", "fingerprint", "{}", "{}", '["fixture-evidence"]', "unchanged-journal-time", None),
            )
        before = snapshot(db.conn)
    finally:
        db.close()
    backup_paths = set(backups.iterdir())
    forbid_seeds(monkeypatch)
    reopened = MusicVaultDB(path, backup_dir=backups)
    try:
        assert snapshot(reopened.conn) == before
        assert reopened.conn.total_changes == 0
        assert not reopened.migration_performed and reopened.last_migration_backup is None
        assert reopened.conn.execute("PRAGMA user_version").fetchone()[0] == 10
        assert reopened.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert reopened.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert set(backups.iterdir()) == backup_paths
    finally:
        reopened.close()
