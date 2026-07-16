from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from music_vault.core.db import MusicVaultDB
from music_vault.core.sync_sources import SyncSourceService
from music_vault.metadata.remediation_schema import create_remediation_schema
from music_vault.metadata.schema import create_metadata_schema
from tools.dev import verify_batch10_live_migration as gate


def _seed_v4_runtime(root: Path) -> tuple[Path, Path, Path]:
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    database = data_dir / "music_vault.sqlite3"
    media = root / "personal-media.bin"
    media.write_bytes(b"aggregate-only synthetic media")

    connection = sqlite3.connect(database)
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
    create_metadata_schema(connection)
    create_remediation_schema(connection)
    connection.execute(
        """
        INSERT INTO tracks (
            path, title, artist, album, release_date, metadata_updated_at
        ) VALUES (?, ?, ?, ?, '2001', '2026-01-01T00:00:00Z')
        """,
        (str(media.resolve()), "PRIVATE_TRACK_TITLE", "PRIVATE_ARTIST", "PRIVATE_ALBUM"),
    )
    connection.execute("INSERT INTO playlists(name) VALUES(?)", ("PRIVATE_PLAYLIST",))
    connection.execute(
        "INSERT INTO playlist_tracks(playlist_id, track_id, position) VALUES(1, 1, 0)"
    )
    connection.execute(
        """
        INSERT INTO track_metadata_fields (
            track_id, field_name, value, provenance, confidence,
            is_manual, is_locked, updated_at
        ) VALUES (1, 'title', ?, 'manual', 100, 1, 1, '2026-01-01T00:00:00Z')
        """,
        ("PRIVATE_TRACK_TITLE",),
    )
    connection.execute("PRAGMA user_version=4")
    connection.commit()
    connection.close()

    (data_dir / "music_vault_config.json").write_text(
        '{"private_setting":"PRIVATE_CONFIG"}\n', encoding="utf-8"
    )
    (data_dir / "youtube_download_archive.txt").write_text(
        "PRIVATE_ARCHIVE_ENTRY\n", encoding="utf-8"
    )
    (data_dir / "youtube_failed_ids.txt").write_text(
        "PRIVATE_FAILURE_ENTRY\n", encoding="utf-8"
    )
    (data_dir / "youtube_api_key.txt").write_text(
        "SUPER_PRIVATE_API_KEY", encoding="utf-8"
    )
    (data_dir / "music_vault_status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "app": "Music Vault",
                "library": {"track_count": 1},
                "playback": {"is_playing": False},
                "sync": {
                    "sync_source_count": 0,
                    "enabled_sync_source_count": 0,
                    "last_sync_batch_status": None,
                    "last_sync_batch_failed_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    return data_dir, database, media


def test_baseline_is_immutable_aggregate_only_and_never_hashes_api_key(
    tmp_path: Path, monkeypatch
):
    data_dir, database, _media = _seed_v4_runtime(tmp_path)
    before = (database.stat().st_size, database.stat().st_mtime_ns, gate._sha256_file(database))
    hashed: list[Path] = []
    original_hash = gate._sha256_file

    def observing_hash(path: Path) -> str:
        hashed.append(path.resolve())
        return original_hash(path)

    monkeypatch.setattr(gate, "_sha256_file", observing_hash)
    baseline = gate.capture_baseline(
        project_root=tmp_path,
        data_dir=data_dir,
        database=database,
    )
    after = (database.stat().st_size, database.stat().st_mtime_ns, original_hash(database))

    assert before == after
    assert baseline["database"]["schema_version"] == 4
    assert baseline["database"]["track_count"] == 1
    assert baseline["database"]["playlist_count"] == 1
    assert baseline["database"]["membership_count"] == 1
    assert baseline["media"]["count"] == 1
    assert baseline["api_key"]["sha256"] is None
    assert (data_dir / "youtube_api_key.txt").resolve() not in hashed

    encoded = json.dumps(baseline, sort_keys=True)
    for private_value in (
        "PRIVATE_TRACK_TITLE",
        "PRIVATE_ARTIST",
        "PRIVATE_ALBUM",
        "PRIVATE_PLAYLIST",
        "PRIVATE_CONFIG",
        "SUPER_PRIVATE_API_KEY",
        str(tmp_path),
    ):
        assert private_value not in encoded


def test_create_backup_uses_sqlite_api_and_verifies_aggregate_counts(tmp_path: Path):
    data_dir, database, _media = _seed_v4_runtime(tmp_path)
    backup = data_dir / "backups" / "rollback.sqlite3"

    result = gate.create_verified_backup(
        database=database,
        backup_dir=data_dir / "backups",
        backup_path=backup,
    )

    assert result["created"] is True
    assert result["verified"] is True
    assert result["table_counts_match"] is True
    assert result["schema_version"] == 4
    assert backup.is_file()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1


def test_verify_accepts_v4_to_v6_migration_with_verified_rollback_backup(tmp_path: Path):
    data_dir, database, _media = _seed_v4_runtime(tmp_path)
    baseline = gate.capture_baseline(
        project_root=tmp_path,
        data_dir=data_dir,
        database=database,
    )
    rollback = data_dir / "backups" / "rollback.sqlite3"
    gate.create_verified_backup(
        database=database,
        backup_dir=data_dir / "backups",
        backup_path=rollback,
    )

    db = MusicVaultDB(database, backup_dir=data_dir / "backups")
    db.close()

    result = gate.verify_migration(
        baseline=baseline,
        project_root=tmp_path,
        data_dir=data_dir,
        database=database,
        backup_path=rollback,
    )

    assert result["ok"] is True
    assert all(result["checks"].values())
    assert result["counts"] == {
        "tracks": 1,
        "playlists": 1,
        "memberships": 1,
        "origins": 1,
        "saved_sources": 0,
        "identities": 0,
        "identity_conflicts": 0,
        "missing_required_indexes": 0,
        "orphan_memberships": 0,
        "duplicate_playlist_positions": 0,
        "backup_count_increase": 2,
        "new_backup_file_count": 2,
        "automatic_backup_candidate_count": 1,
        "verified_automatic_backup_count": 1,
        "unique_youtube_identity_claims": 0,
        "duplicate_youtube_identity_claims": 0,
    }
    with sqlite3.connect(database) as connection:
        origin = connection.execute(
            "SELECT origin_kind, sync_source_id FROM playlist_track_origins"
        ).fetchone()
        assert origin == ("manual", None)


def test_verify_rejects_private_per_source_status_detail(tmp_path: Path):
    data_dir, database, _media = _seed_v4_runtime(tmp_path)
    baseline = gate.capture_baseline(
        project_root=tmp_path,
        data_dir=data_dir,
        database=database,
    )
    rollback = data_dir / "backups" / "rollback.sqlite3"
    gate.create_verified_backup(
        database=database,
        backup_dir=data_dir / "backups",
        backup_path=rollback,
    )
    db = MusicVaultDB(database, backup_dir=data_dir / "backups")
    db.close()
    status_path = data_dir / "music_vault_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["sync"]["source_url"] = "https://www.youtube.com/playlist?list=private"
    status_path.write_text(json.dumps(status), encoding="utf-8")

    result = gate.verify_migration(
        baseline=baseline,
        project_root=tmp_path,
        data_dir=data_dir,
        database=database,
        backup_path=rollback,
    )

    assert result["ok"] is False
    assert result["checks"]["app_status_aggregate_private"] is False


def test_status_gate_rejects_identity_title_folder_and_error_details_but_allows_playback(
    tmp_path: Path,
):
    status_path = tmp_path / "music_vault_status.json"
    safe_status = {
        "schema_version": 1,
        "app": "Music Vault",
        "library": {"track_count": 1},
        "playback": {
            "currently_playing": 7,
            "current_title": "synthetic playback title",
            "current_artist": "synthetic playback artist",
            "current_album": "synthetic playback album",
            "is_playing": True,
        },
        "sync": {
            "sync_source_count": 2,
            "enabled_sync_source_count": 1,
            "last_sync_batch_status": "complete_with_issues",
            "last_sync_batch_failed_count": 1,
            "last_sync_error": None,
            "last_sync_failures": [],
        },
    }
    status_path.write_text(json.dumps(safe_status), encoding="utf-8")
    assert gate._status_is_compatible_and_private(status_path) == (True, True)

    adversarial_fields = {
        "remote_title": "private remote title",
        "storage_key": "private-storage-key",
        "last_sync_error": "private item failure",
        "source_display_title": "private source title",
        "item_error": "private item error",
        "download_folder": "private source folder",
    }
    for field, value in adversarial_fields.items():
        unsafe_status = json.loads(json.dumps(safe_status))
        unsafe_status["sync"][field] = value
        status_path.write_text(json.dumps(unsafe_status), encoding="utf-8")
        assert gate._status_is_compatible_and_private(status_path) == (True, False)


def test_invalid_second_sqlite_file_is_not_accepted_as_automatic_backup(
    tmp_path: Path,
):
    data_dir, database, _media = _seed_v4_runtime(tmp_path)
    baseline = gate.capture_baseline(
        project_root=tmp_path,
        data_dir=data_dir,
        database=database,
    )
    rollback = data_dir / "backups" / "rollback.sqlite3"
    gate.create_verified_backup(
        database=database,
        backup_dir=data_dir / "backups",
        backup_path=rollback,
    )
    db = MusicVaultDB(database, backup_dir=data_dir / "backups")
    automatic_backup = db.last_migration_backup
    db.close()
    assert automatic_backup is not None
    automatic_backup.unlink()
    invalid_backup = (
        data_dir
        / "backups"
        / "music_vault_pre_schema_v6_2099-01-01_00-00-00.sqlite3"
    )
    invalid_backup.write_bytes(b"not a sqlite database")

    result = gate.verify_migration(
        baseline=baseline,
        project_root=tmp_path,
        data_dir=data_dir,
        database=database,
        backup_path=rollback,
    )

    assert result["counts"]["backup_count_increase"] == 2
    assert result["counts"]["automatic_backup_candidate_count"] == 1
    assert result["counts"]["verified_automatic_backup_count"] == 0
    assert result["checks"]["acceptance_rollback_backup_verified"] is True
    assert result["checks"]["automatic_migration_backup_created"] is False
    assert result["ok"] is False


def test_verify_allows_exactly_one_legacy_library_only_source(tmp_path: Path):
    data_dir, database, _media = _seed_v4_runtime(tmp_path)
    legacy_id = "PL1234567890ABCDEF"
    config_path = data_dir / "music_vault_config.json"
    config_path.write_text(
        json.dumps(
            {"youtube_playlist_url": f"https://www.youtube.com/playlist?list={legacy_id}"}
        ),
        encoding="utf-8",
    )
    baseline = gate.capture_baseline(
        project_root=tmp_path,
        data_dir=data_dir,
        database=database,
    )
    assert baseline["valid_legacy_source_configured"] is True
    assert legacy_id not in json.dumps(baseline)

    rollback = data_dir / "backups" / "rollback.sqlite3"
    gate.create_verified_backup(
        database=database,
        backup_dir=data_dir / "backups",
        backup_path=rollback,
    )
    db = MusicVaultDB(database, backup_dir=data_dir / "backups")
    SyncSourceService(db).create_source(
        f"https://www.youtube.com/playlist?list={legacy_id}",
        destination_kind="library",
    )
    db.close()

    result = gate.verify_migration(
        baseline=baseline,
        project_root=tmp_path,
        data_dir=data_dir,
        database=database,
        backup_path=rollback,
    )
    assert result["ok"] is True
    assert result["counts"]["saved_sources"] == 1
    assert result["checks"]["legacy_source_is_library_only"] is True
    assert result["checks"]["existing_memberships_seeded_manual"] is True


def test_verify_checks_deterministic_identity_backfill_without_exposing_ids(tmp_path: Path):
    data_dir, database, first_media = _seed_v4_runtime(tmp_path)
    second_media = tmp_path / "second-personal-media.bin"
    second_media.write_bytes(b"second aggregate-only synthetic media")
    external_id = "PrivateVID1"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE tracks SET path=?, source_kind='youtube', source_video_id=? WHERE id=1",
            (str(tmp_path / "missing-media.bin"), external_id),
        )
        connection.execute(
            """
            INSERT INTO tracks(path, title, source_kind, source_video_id)
            VALUES (?, ?, 'youtube', ?)
            """,
            (str(second_media.resolve()), "ANOTHER_PRIVATE_TITLE", external_id),
        )
    first_media.unlink()

    baseline = gate.capture_baseline(
        project_root=tmp_path,
        data_dir=data_dir,
        database=database,
    )
    source_summary = baseline["database"]["sources"]
    assert source_summary["youtube_identity_claim_count"] == 2
    assert source_summary["unique_youtube_identity_count"] == 1
    assert source_summary["duplicate_youtube_identity_claim_count"] == 1
    assert source_summary["expected_identity_count"] == 1
    assert source_summary["expected_identity_conflict_count"] == 1
    assert external_id not in json.dumps(baseline)

    rollback = data_dir / "backups" / "rollback.sqlite3"
    gate.create_verified_backup(
        database=database,
        backup_dir=data_dir / "backups",
        backup_path=rollback,
    )
    db = MusicVaultDB(database, backup_dir=data_dir / "backups")
    canonical = db.conn.execute(
        "SELECT track_id FROM source_track_identities WHERE source_kind='youtube'"
    ).fetchone()[0]
    db.close()
    assert canonical == 2

    result = gate.verify_migration(
        baseline=baseline,
        project_root=tmp_path,
        data_dir=data_dir,
        database=database,
        backup_path=rollback,
    )
    assert result["ok"] is True
    assert result["checks"]["identity_mapping_matches_baseline_expectation"] is True
    assert result["checks"]["identity_conflicts_match_baseline_expectation"] is True


def test_cli_failure_does_not_echo_private_paths_or_values(tmp_path: Path, capsys):
    private_root = tmp_path / "PRIVATE_USER_FOLDER"
    result = gate.main(
        [
            "baseline",
            "--project-root",
            str(private_root),
            "--database",
            str(private_root / "PRIVATE_DATABASE.sqlite3"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "PRIVATE_USER_FOLDER" not in captured.err
    assert "PRIVATE_DATABASE" not in captured.err
    assert captured.out == ""
    assert json.loads(captured.err)["ok"] is False
