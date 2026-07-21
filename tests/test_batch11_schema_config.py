from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from music_vault.core.audio_quality_config import (
    BEST_ORIGINAL_PROFILE,
    DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS,
    INHERIT_PROFILE,
    MP3_320_COMPATIBILITY_PROFILE,
    migrate_audio_quality_config,
    normalize_download_quality_profile,
    normalize_source_download_quality_profile,
)
from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
from music_vault.core.media_quality_schema import required_media_quality_indexes
from music_vault.core.sync_sources import SyncSourceService


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema7_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, int]]:
    database_path = tmp_path / "music-vault.sqlite3"
    media = tmp_path / "synthetic-existing.mp3"
    media.write_bytes(b"synthetic media remains byte-identical")
    db = MusicVaultDB(database_path, backup_dir=tmp_path / "initial-backups")
    db.upsert_track(
        media,
        title="Synthetic YouTube",
        source_kind="youtube",
        source_video_id="abcdefghijk",
    )
    db.upsert_track(
        tmp_path / "synthetic-local.opus",
        title="Synthetic Local",
        source_kind="local",
    )
    source = SyncSourceService(db).create_source("PLsynthetic12345")
    assert source.download_quality_profile == INHERIT_PROFILE
    counts = {
        table: int(db.conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in ("tracks", "playlists", "playlist_tracks", "sync_sources")
    }
    db.close()

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE track_media_quality")
        connection.execute("PRAGMA user_version=7")
    return database_path, media, counts


def test_schema7_to_8_is_backed_up_preserving_and_idempotent(tmp_path: Path) -> None:
    database_path, media, before_counts = _schema7_fixture(tmp_path)
    before_hash = _sha256(media)
    before_stat = media.stat()
    backups = tmp_path / "migration-backups"

    db = MusicVaultDB(database_path, backup_dir=backups)

    assert CURRENT_SCHEMA_VERSION == 8
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert db.migration_performed is True
    assert db.migrated_from_version == 7
    assert db.migrated_to_version == 8
    assert db.last_migration_backup is not None
    assert db.last_migration_backup.is_file()
    with sqlite3.connect(db.last_migration_backup) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 7
        assert backup.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_media_quality'"
        ).fetchone() is None

    for table, count in before_counts.items():
        assert db.conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == count
    assert db.conn.execute("SELECT COUNT(*) FROM track_media_quality").fetchone()[0] == 2
    assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    index_names = {
        str(row[0])
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert set(required_media_quality_indexes()) <= index_names
    db.close()

    assert _sha256(media) == before_hash
    assert media.stat().st_size == before_stat.st_size
    assert media.stat().st_mtime_ns == before_stat.st_mtime_ns

    reopened = MusicVaultDB(database_path, backup_dir=backups)
    assert reopened.migration_performed is False
    assert reopened.last_migration_backup is None
    assert reopened.conn.execute("SELECT COUNT(*) FROM track_media_quality").fetchone()[0] == 2
    reopened.close()
    assert len(list(backups.glob("*.sqlite3"))) == 1


def test_legacy_quality_classification_does_not_invent_source_facts(tmp_path: Path) -> None:
    database_path, _media, _counts = _schema7_fixture(tmp_path)
    db = MusicVaultDB(database_path, backup_dir=tmp_path / "backups")
    youtube = db.conn.execute(
        """
        SELECT quality.*
        FROM track_media_quality AS quality
        JOIN tracks ON tracks.id=quality.track_id
        WHERE tracks.source_kind='youtube'
        """
    ).fetchone()
    local = db.conn.execute(
        """
        SELECT quality.*
        FROM track_media_quality AS quality
        JOIN tracks ON tracks.id=quality.track_id
        WHERE tracks.source_kind='local'
        """
    ).fetchone()

    assert youtube["acquisition_profile"] == "legacy_youtube_mp3"
    assert youtube["transformation_kind"] == "legacy_inferred_transcode"
    assert youtube["inspection_state"] == "legacy_inferred"
    assert youtube["stored_extension"] == ".mp3"
    for name in (
        "source_format_id",
        "source_extension",
        "source_container",
        "source_codec",
        "source_bitrate_kbps",
        "source_sample_rate_hz",
        "source_channels",
        "source_filesize_bytes",
        "stored_container",
        "stored_codec",
        "stored_bitrate_kbps",
        "stored_sample_rate_hz",
        "stored_channels",
        "stored_filesize_bytes",
        "inspected_at",
    ):
        assert youtube[name] is None

    assert local["acquisition_profile"] == "local_import"
    assert local["transformation_kind"] == "local_original"
    assert local["stored_extension"] == ".opus"
    assert local["stored_codec"] is None
    db.close()


def test_quality_schema_constraints_and_new_track_inventory(tmp_path: Path) -> None:
    db = MusicVaultDB(tmp_path / "new.sqlite3")
    track_id = db.upsert_track(tmp_path / "new-local.m4a", source_kind="local")
    row = db.conn.execute(
        "SELECT * FROM track_media_quality WHERE track_id=?", (track_id,)
    ).fetchone()
    assert row is not None
    assert row["acquisition_profile"] == "local_import"
    assert row["stored_extension"] == ".m4a"

    recorded = db.upsert_track_media_quality(
        track_id,
        acquisition_profile=BEST_ORIGINAL_PROFILE,
        source_format_id="synthetic-format",
        source_extension="webm",
        source_container="webm",
        source_codec="OPUS",
        source_bitrate_kbps=160,
        source_sample_rate_hz=48000,
        source_channels=2,
        source_filesize_bytes=10,
        stored_extension="opus",
        stored_container="ogg",
        stored_codec="opus",
        stored_bitrate_kbps=160,
        stored_sample_rate_hz=48000,
        stored_channels=2,
        stored_filesize_bytes=9,
        transformation_kind="source_preserved_remux",
        inspection_state="inspected",
        provenance={"kind": "synthetic_test"},
        inspected_at="2026-01-01T00:00:00Z",
    )
    assert recorded["source_codec"] == "opus"
    assert recorded["stored_extension"] == ".opus"
    assert recorded["provenance"] == '{"kind":"synthetic_test"}'
    assert dict(db.get_track_media_quality(track_id)) == dict(recorded)
    assert db.track_media_quality_summary()[BEST_ORIGINAL_PROFILE] == 1

    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "UPDATE track_media_quality SET acquisition_profile='lossless_youtube' "
            "WHERE track_id=?",
            (track_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "UPDATE track_media_quality SET source_bitrate_kbps=0 WHERE track_id=?",
            (track_id,),
        )
    db.close()


def test_config_defaults_and_numeric_legacy_migration_are_idempotent() -> None:
    migrated, changed = migrate_audio_quality_config({"audio_quality": "320"})
    assert changed is True
    assert migrated["download_quality_profile"] == BEST_ORIGINAL_PROFILE
    assert (
        migrated["compatibility_mp3_bitrate_kbps"]
        == DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS
    )
    assert migrated["audio_quality"] == "320"

    second, changed_again = migrate_audio_quality_config(migrated)
    assert second == migrated
    assert changed_again is False
    assert normalize_download_quality_profile("320") == BEST_ORIGINAL_PROFILE
    assert normalize_download_quality_profile("unsupported") == BEST_ORIGINAL_PROFILE


def test_source_quality_override_round_trip_and_constraint(tmp_path: Path) -> None:
    db = MusicVaultDB(tmp_path / "sources.sqlite3")
    service = SyncSourceService(db)
    inherited = service.create_source("PLsourceinherit123")
    explicit = service.create_source(
        "PLsourcequality123",
        download_quality_profile=BEST_ORIGINAL_PROFILE,
    )
    assert inherited.download_quality_profile == INHERIT_PROFILE
    assert explicit.download_quality_profile == BEST_ORIGINAL_PROFILE

    updated = service.update_source(
        explicit.id,
        download_quality_profile=MP3_320_COMPATIBILITY_PROFILE,
    )
    assert updated.download_quality_profile == MP3_320_COMPATIBILITY_PROFILE
    assert normalize_source_download_quality_profile("bad-value") == INHERIT_PROFILE
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "UPDATE sync_sources SET download_quality_profile='invalid' WHERE id=?",
            (explicit.id,),
        )
    db.close()
