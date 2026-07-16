from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from music_vault.core import paths as runtime_paths
from music_vault.core.app_status import write_app_status
from music_vault.core.db import MusicVaultDB
from tools.dev import run_batch10_2_packaged_migration_smoke as packaged


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_runtime_resolution() -> None:
    runtime_paths.clear_configured_data_dir()
    runtime_paths._resolved_project_root.cache_clear()
    yield
    runtime_paths.clear_configured_data_dir()
    runtime_paths._resolved_project_root.cache_clear()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _schema5_project(root: Path) -> tuple[Path, Path, str]:
    (root / "music_vault").mkdir(parents=True)
    (root / "run.py").write_text("# synthetic project marker\n", encoding="utf-8")
    backup = root / packaged.ROLLBACK_RELATIVE_PATH
    backup.parent.mkdir(parents=True)
    media = root / "private-looking-source-media"
    covers = root / "private-looking-source-covers"
    media.mkdir()
    covers.mkdir()

    db = MusicVaultDB(backup, backup_dir=backup.parent / "schema-backups")
    artists = (
        "CASE Ensemble",
        "  case   ensemble  ",
        "Signal & Noise",
        "Distinct Synthetic Artist",
    )
    for index, artist in enumerate(artists):
        media_path = media / f"track-{index}.media"
        media_path.write_bytes(f"synthetic-media-{index}".encode("ascii"))
        cover_path = covers / f"cover-{index}.png"
        cover_path.write_bytes(f"synthetic-cover-{index}".encode("ascii"))
        db.upsert_track(
            media_path,
            title=f"SYNTHETIC_PRIVATE_TITLE_{index}",
            artist=artist,
            album="SYNTHETIC_PRIVATE_ALBUM",
            cover_path=str(cover_path) if index % 2 == 0 else None,
            source_kind="youtube",
            source_video_id=f"fakeid{index:05d}",
        )
    db.close()

    # Recreate the exact migration boundary needed by this targeted smoke.
    with closing(sqlite3.connect(backup)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "DELETE FROM track_metadata_fields "
            "WHERE field_name IN ('original_release_date','version_type','version_label')"
        )
        for table in (
            "metadata_intelligence_items",
            "metadata_intelligence_jobs",
            "track_release_context",
            "track_artist_credits",
            "artists",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute("PRAGMA user_version=5")
    return backup, media, _sha256(backup)


def _temp_runtime() -> Path:
    return Path(tempfile.gettempdir()) / (
        packaged.RUNTIME_PREFIX + "pytest_" + uuid.uuid4().hex
    )


def _migrate_in_source_mode(
    runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(runtime))
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    runtime_paths._resolved_project_root.cache_clear()
    database = runtime / packaged.DATABASE_RELATIVE_PATH
    db = MusicVaultDB(database, backup_dir=runtime / "data" / "backups")
    write_app_status(db, {})
    db.close()


def test_prepare_hashes_pristine_copy_then_sanitizes_only_temp_database(
    tmp_path: Path,
) -> None:
    project = tmp_path / "synthetic-project"
    source, media_root, source_hash = _schema5_project(project)
    source_stat = source.stat()
    media_stats = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns, _sha256(path))
        for path in media_root.iterdir()
    }
    runtime = _temp_runtime()
    try:
        manifest = packaged.prepare(
            runtime,
            project,
            expected_sha256=source_hash,
        )
        assert manifest["source"]["sha256_before_temp_sanitization"] == source_hash
        assert manifest["source"]["pristine_copy_sha256"] == source_hash
        assert manifest["source"]["schema_version"] == 5
        assert manifest["source"]["integrity_ok"] is True
        assert manifest["sanitization"][
            "all_track_and_cover_paths_confined_to_temp"
        ] is True
        assert manifest["sanitization"][
            "all_sanitized_media_and_cover_files_absent"
        ] is True
        assert manifest["explicit_backup"]["verified"] is True
        assert manifest["baseline"]["runtime_guards"]["config"]["exists"] is True
        assert json.loads(
            (runtime / "data" / "music_vault_config.json").read_text(
                encoding="utf-8"
            )
        ) == {
            "onboarding_completed": True,
            "party_mode_config_version": 2,
        }

        copied = runtime / packaged.DATABASE_RELATIVE_PATH
        with closing(sqlite3.connect(copied)) as connection:
            rows = connection.execute("SELECT path,cover_path FROM tracks").fetchall()
        assert rows
        for media_path, cover_path in rows:
            assert Path(media_path).resolve().is_relative_to(runtime.resolve())
            assert not Path(media_path).exists()
            if cover_path:
                assert Path(cover_path).resolve().is_relative_to(runtime.resolve())
                assert not Path(cover_path).exists()

        encoded = json.dumps(manifest, sort_keys=True)
        assert "SYNTHETIC_PRIVATE_TITLE" not in encoded
        assert "SYNTHETIC_PRIVATE_ALBUM" not in encoded
        assert str(media_root) not in encoded
        assert _sha256(source) == source_hash
        assert source.stat().st_size == source_stat.st_size
        assert source.stat().st_mtime_ns == source_stat.st_mtime_ns
        assert media_stats == {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns, _sha256(path))
            for path in media_root.iterdir()
        }
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


def test_source_mode_packaged_helper_proves_full_schema6_preservation_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "synthetic-project"
    _source, _media, source_hash = _schema5_project(project)
    runtime = _temp_runtime()
    try:
        manifest = packaged.prepare(
            runtime,
            project,
            expected_sha256=source_hash,
        )
        identity_before = manifest["baseline"]["preserved_tables"][
            "source_track_identities"
        ]
        _migrate_in_source_mode(runtime, monkeypatch)
        result = packaged.verify(
            runtime,
            project,
            manifest,
            graceful_close_confirmed=True,
            network_attempt_count=0,
        )

        assert result["ok"] is True
        assert all(result["checks"].values())
        assert result["checks"]["identity_mapping_and_timestamps_preserved"] is True
        assert result["checks"]["all_old_field_states_preserved"] is True
        assert result["checks"]["exact_expected_v6_field_states_added"] is True
        assert result["checks"][
            "artist_display_and_normalized_credit_reuse_preserved"
        ] is True
        assert result["checks"][
            "no_provider_lookup_or_network_work_observed"
        ] is True
        assert result["counts"]["tracks"] == 4
        assert result["counts"]["identities"] == identity_before["count"] == 4
        assert result["counts"]["expected_new_v6_field_state_rows"] == 12
        assert result["counts"]["actual_new_v6_field_state_rows"] == 12
        assert result["counts"]["intelligence_job_count"] == 0
        assert result["counts"]["intelligence_item_count"] == 0
        assert result["verifier"]["checks"][
            "automatic_schema_backup_created_and_verified"
        ] is True
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


def test_verify_rejects_any_identity_timestamp_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "synthetic-project"
    _source, _media, source_hash = _schema5_project(project)
    runtime = _temp_runtime()
    try:
        manifest = packaged.prepare(
            runtime,
            project,
            expected_sha256=source_hash,
        )
        _migrate_in_source_mode(runtime, monkeypatch)
        with closing(
            sqlite3.connect(runtime / packaged.DATABASE_RELATIVE_PATH)
        ) as connection, connection:
            connection.execute(
                "UPDATE source_track_identities SET updated_at='2099-01-01T00:00:00Z' "
                "WHERE rowid=(SELECT MIN(rowid) FROM source_track_identities)"
            )
        result = packaged.verify(
            runtime,
            project,
            manifest,
            graceful_close_confirmed=True,
            network_attempt_count=0,
        )
        assert result["ok"] is False
        assert result["checks"]["corrected_batch10_1_verifier_passed"] is False
        assert result["checks"]["identity_mapping_and_timestamps_preserved"] is False
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


def test_prepare_rejects_wrong_source_hash_before_creating_runtime(
    tmp_path: Path,
) -> None:
    project = tmp_path / "synthetic-project"
    source, _media, source_hash = _schema5_project(project)
    runtime = _temp_runtime()
    with pytest.raises(packaged.SmokeFailure, match="hash_mismatch"):
        packaged.prepare(runtime, project, expected_sha256="0" * 64)
    assert not runtime.exists()
    assert _sha256(source) == source_hash


def test_verify_requires_graceful_close_and_zero_observed_network_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "synthetic-project"
    _source, _media, source_hash = _schema5_project(project)
    runtime = _temp_runtime()
    try:
        manifest = packaged.prepare(
            runtime,
            project,
            expected_sha256=source_hash,
        )
        _migrate_in_source_mode(runtime, monkeypatch)
        with pytest.raises(packaged.SmokeFailure, match="close_not_confirmed"):
            packaged.verify(
                runtime,
                project,
                manifest,
                graceful_close_confirmed=False,
            )
        with pytest.raises(packaged.SmokeFailure, match="network_connection"):
            packaged.verify(
                runtime,
                project,
                manifest,
                graceful_close_confirmed=True,
                network_attempt_count=1,
            )
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


def test_powershell_wrapper_owns_hidden_offline_process_and_safe_cleanup() -> None:
    wrapper = (
        PROJECT_ROOT / "tools" / "dev" / "run_batch10_2_packaged_migration_smoke.ps1"
    ).read_text(encoding="utf-8")
    assert "dist\\MusicVault\\MusicVault.exe" in wrapper
    assert "music_vault_batch10_1_explicit_rollback_20260716_003442_649.sqlite3" in wrapper
    assert "MUSIC_VAULT_PROJECT_ROOT" in wrapper
    assert 'MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"' in wrapper
    assert "-WindowStyle Hidden" in wrapper
    assert "Get-NetTCPConnection" in wrapper
    assert "CloseMainWindow" in wrapper
    assert "PostCloseOwned" in wrapper
    assert "WM_CLOSE" in wrapper
    assert "WaitForExit" in wrapper
    assert "--graceful-close-confirmed" in wrapper
    assert "--network-attempt-count 0" in wrapper
    assert "Stop-Process" not in wrapper
    assert "Remove-Item -LiteralPath $ResolvedRuntime -Recurse -Force" in wrapper
    assert "StartsWith($ResolvedTemp" in wrapper
    assert packaged.RUNTIME_PREFIX in wrapper
