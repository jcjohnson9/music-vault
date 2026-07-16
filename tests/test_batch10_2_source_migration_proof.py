from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from music_vault.core.db import MusicVaultDB
from tools.dev import run_batch10_2_source_migration_proof as proof


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _schema5_backup(root: Path) -> Path:
    root.mkdir(parents=True)
    database = root / "verified-schema5.sqlite3"
    db = MusicVaultDB(database, backup_dir=root / "creation-backups")
    artists = (
        "Synthetic Duo & Company",
        "  synthetic duo & company  ",
        "Another Synthetic Artist",
    )
    for index, artist in enumerate(artists):
        media = root / f"PRIVATE_MEDIA_{index}.bin"
        media.write_bytes(f"private synthetic media {index}".encode("ascii"))
        db.upsert_track(
            media,
            title=f"PRIVATE_TITLE_{index}",
            artist=artist,
            album="PRIVATE_ALBUM",
            cover_path=str(root / f"PRIVATE_COVER_{index}.jpg"),
            source_kind="youtube",
            source_video_id=f"proofid{index:04d}",
        )
    db.close()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "DELETE FROM track_metadata_fields WHERE field_name IN "
            "('original_release_date','version_type','version_label')"
        )
        for table in (
            "metadata_intelligence_items",
            "metadata_intelligence_jobs",
            "track_release_context",
            "track_artist_credits",
            "artists",
        ):
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        connection.execute("PRAGMA user_version=5")
    return database


def test_source_migration_proof_isolated_preserving_and_idempotent(tmp_path: Path) -> None:
    backup = _schema5_backup(tmp_path / "input")
    before_hash = _sha256(backup)
    before_stat = backup.stat()
    temporary_parent = tmp_path / "disposable"
    temporary_parent.mkdir()

    result = proof.run_source_migration_proof(
        schema5_backup=backup,
        expected_sha256=before_hash,
        expected_track_count=3,
        expected_identity_count=3,
        expected_old_field_count=18,
        expected_new_field_count=9,
        temporary_parent=temporary_parent,
    )

    assert result["ok"] is True
    assert all(result["checks"].values())
    assert result["counts"]["source_identity_count"] == 3
    assert result["counts"]["old_field_preserved_count"] == 18
    assert result["counts"]["new_field_count"] == 9
    assert result["counts"]["safe_new_field_count"] == 9
    assert result["counts"]["automatic_backup_count"] == 1
    assert result["counts"]["network_attempt_count"] == 0
    assert list(temporary_parent.iterdir()) == []
    assert _sha256(backup) == before_hash
    assert backup.stat().st_size == before_stat.st_size
    assert backup.stat().st_mtime_ns == before_stat.st_mtime_ns


def test_source_migration_proof_rejects_wrong_hash_before_copy(tmp_path: Path) -> None:
    backup = _schema5_backup(tmp_path / "input")
    with pytest.raises(proof.ProofFailure, match="hash_mismatch"):
        proof.run_source_migration_proof(
            schema5_backup=backup,
            expected_sha256="0" * 64,
            expected_track_count=3,
            expected_identity_count=3,
            expected_old_field_count=18,
            expected_new_field_count=9,
            temporary_parent=tmp_path,
        )
    assert not any(path.name.startswith(proof.TEMP_PREFIX) for path in tmp_path.iterdir())


def test_source_migration_proof_output_is_aggregate_only(tmp_path: Path) -> None:
    backup = _schema5_backup(tmp_path / "input")
    temporary_parent = tmp_path / "disposable"
    temporary_parent.mkdir()
    result = proof.run_source_migration_proof(
        schema5_backup=backup,
        expected_sha256=_sha256(backup),
        expected_track_count=3,
        expected_identity_count=3,
        expected_old_field_count=18,
        expected_new_field_count=9,
        temporary_parent=temporary_parent,
    )
    encoded = json.dumps(result, sort_keys=True)
    for private_value in (
        "PRIVATE_TITLE",
        "PRIVATE_ALBUM",
        "PRIVATE_MEDIA",
        "Synthetic Duo",
        "Another Synthetic Artist",
        "proofid",
        str(tmp_path),
        str(backup),
    ):
        assert private_value not in encoded


def test_source_migration_proof_wrapper_is_project_local_and_no_secret() -> None:
    wrapper = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "dev"
        / "run_batch10_2_source_migration_proof.ps1"
    ).read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe" in wrapper
    assert "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS" in wrapper
    assert "ExpectedSha256" in wrapper
    assert "ExpectedTrackCount = 304" in wrapper
    assert "ExpectedIdentityCount = 304" in wrapper
    assert "ExpectedOldFieldCount = 1824" in wrapper
    assert "ExpectedNewFieldCount = 912" in wrapper
