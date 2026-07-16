from __future__ import annotations

import json
import importlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from music_vault.core.app_status import write_app_status
from music_vault.core.db import MusicVaultDB
from music_vault.core import paths as runtime_paths
from music_vault.ui import review as ui_review
from tools.dev import profile_metadata_intelligence as profile
from tools.dev import capture_ui_review
from tools.dev import run_batch10_1_packaged_smoke as packaged
from tools.dev import verify_batch10_1_live_migration as live_gate
from tools.dev.synthetic_metadata_providers import (
    SYNTHETIC_SCENARIOS,
    SyntheticDiscogsProvider,
    SyntheticMusicBrainzProvider,
    scenario_keys,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_runtime_path_cache():
    runtime_paths._resolved_project_root.cache_clear()
    yield
    runtime_paths._resolved_project_root.cache_clear()


def test_synthetic_provider_matrix_is_complete_normalized_and_not_bundled() -> None:
    assert len(SYNTHETIC_SCENARIOS) == 18
    assert len(set(scenario_keys())) == 18
    assert {
        "older_random_uploader",
        "older_many_reissues",
        "record_label_uploader",
        "group_duo",
        "primary_featured",
        "joint_collaboration",
        "studio_version",
        "unofficial_live",
        "remix_conflict",
        "youtube_exclusive",
        "provider_agreement",
        "provider_disagreement",
        "missing_artwork",
        "preserve_valid_artwork",
        "ambiguous_reissue",
        "rate_limit",
        "temporary_failure",
        "file_write_rollback",
    } == set(scenario_keys())
    assert all("token" not in item.__slots__ for item in SYNTHETIC_SCENARIOS)
    assert "synthetic_metadata_providers" not in (
        PROJECT_ROOT / "MusicVault.spec"
    ).read_text(encoding="utf-8")
    assert ui_review.METADATA_INTELLIGENCE_REVIEW_SCENES == (
        "metadata_intelligence_smoke",
    )
    assert capture_ui_review.METADATA_INTELLIGENCE_SCENES == (
        "metadata_intelligence_smoke",
    )


def test_metadata_intelligence_profiler_small_case_proves_dedup_and_persistence(
    tmp_path: Path,
) -> None:
    result = profile.profile_case(tmp_path, name="test_12_tracks", track_count=12)
    assert result["schema_version"] == 6
    assert result["source_membership_count"] == 36
    assert result["overlapping_membership_count"] == 24
    assert result["job_item_count"] == 12
    assert result["job_distinct_track_count"] == 12
    assert result["discogs_query_count"] == 12
    assert result["musicbrainz_query_count"] == 12
    assert result["job_max_attempt_count"] == 2
    assert all(result["checks"].values())
    assert profile.PROFILE_CASES == (("300_tracks", 300), ("1000_tracks", 1_000))


def _downgraded_v5_runtime(root: Path) -> tuple[Path, Path]:
    (root / "music_vault").mkdir(exist_ok=True)
    (root / "run.py").write_text("# synthetic runtime marker\n", encoding="utf-8")
    data = root / "data"
    data.mkdir(parents=True)
    database = data / "music_vault.sqlite3"
    first = root / "synthetic-one.media"
    second = root / "synthetic-two.media"
    first.write_bytes(b"synthetic acceptance media one")
    second.write_bytes(b"synthetic acceptance media two")
    db = MusicVaultDB(database, backup_dir=data / "backups")
    first_id = db.upsert_track(
        first,
        title="PRIVATE_SYNTHETIC_TITLE_ONE",
        artist="Synthetic Duo & Company",
        album="PRIVATE_SYNTHETIC_ALBUM",
    )
    second_id = db.upsert_track(
        second,
        title="PRIVATE_SYNTHETIC_TITLE_TWO",
        artist="Another Synthetic Artist",
    )
    playlist = db.create_playlist("PRIVATE_SYNTHETIC_PLAYLIST")
    db.add_track_to_playlist(playlist, first_id)
    db.add_track_to_playlist(playlist, second_id)
    db.close()
    with sqlite3.connect(database) as connection:
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
    (data / "music_vault_config.json").write_text("{}\n", encoding="utf-8")
    (data / "youtube_download_archive.txt").write_text("PRIVATE_ARCHIVE\n", encoding="utf-8")
    (data / "youtube_failed_ids.txt").write_text("PRIVATE_FAILURE\n", encoding="utf-8")
    (data / "youtube_api_key.txt").write_text("PRIVATE_API_KEY", encoding="utf-8")
    (data / "discogs_token.txt").write_text("PRIVATE_DISCOGS_TOKEN", encoding="utf-8")
    return data, database


def test_live_schema_gate_preserves_values_credits_sources_media_and_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(tmp_path))
    data, database = _downgraded_v5_runtime(tmp_path)
    baseline = live_gate.capture_baseline(
        project_root=tmp_path,
        data_dir=data,
        database=database,
    )
    assert baseline["database"]["schema_version"] == 5
    encoded = json.dumps(baseline, sort_keys=True)
    for private in (
        "PRIVATE_SYNTHETIC_TITLE_ONE",
        "PRIVATE_SYNTHETIC_ALBUM",
        "PRIVATE_SYNTHETIC_PLAYLIST",
        "PRIVATE_API_KEY",
        "PRIVATE_DISCOGS_TOKEN",
        str(tmp_path),
    ):
        assert private not in encoded

    rollback = data / "backups" / "explicit-rollback.sqlite3"
    backup = live_gate.create_verified_backup(
        database=database,
        backup_dir=data / "backups",
        baseline=baseline,
        backup_path=rollback,
    )
    assert backup["verified"] is True

    migrated = MusicVaultDB(database, backup_dir=data / "backups")
    write_app_status(migrated, {})
    migrated.close()
    result = live_gate.verify_migration(
        baseline=baseline,
        project_root=tmp_path,
        data_dir=data,
        database=database,
        backup_path=rollback,
    )
    assert result["ok"] is True
    assert all(result["checks"].values())
    assert result["counts"]["tracks"] == 2
    assert result["counts"]["seeded_artist_credits"] == 2
    assert result["counts"]["baseline_field_state_rows"] == 12
    assert result["counts"]["preserved_baseline_field_state_rows"] == 12
    assert result["counts"]["expected_new_v6_field_state_rows"] == 6
    assert result["counts"]["actual_new_v6_field_state_rows"] == 6
    assert result["counts"]["new_original_release_date_field_rows"] == 2
    assert result["counts"]["new_version_type_field_rows"] == 2
    assert result["counts"]["new_version_label_field_rows"] == 2
    assert result["counts"]["intelligence_job_count"] == 0


def test_live_schema_gate_rejects_field_rewrites_and_unsafe_v6_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(tmp_path))
    data, database = _downgraded_v5_runtime(tmp_path)
    baseline = live_gate.capture_baseline(
        project_root=tmp_path,
        data_dir=data,
        database=database,
    )
    rollback = data / "backups" / "explicit-rollback.sqlite3"
    live_gate.create_verified_backup(
        database=database,
        backup_dir=data / "backups",
        baseline=baseline,
        backup_path=rollback,
    )
    migrated = MusicVaultDB(database, backup_dir=data / "backups")
    write_app_status(migrated, {})
    migrated.close()

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE track_metadata_fields SET updated_at='tampered' "
            "WHERE track_id=(SELECT MIN(id) FROM tracks) AND field_name='title'"
        )
        connection.execute(
            "UPDATE track_metadata_fields SET provider_reference='fabricated', "
            "confidence=100,is_manual=1,is_locked=1 "
            "WHERE track_id=(SELECT MIN(id) FROM tracks) AND field_name='version_label'"
        )
        connection.execute(
            "UPDATE track_metadata_fields SET value='2099-01-01' "
            "WHERE track_id=(SELECT MIN(id) FROM tracks) "
            "AND field_name='original_release_date'"
        )
    result = live_gate.verify_migration(
        baseline=baseline,
        project_root=tmp_path,
        data_dir=data,
        database=database,
        backup_path=rollback,
    )
    assert result["ok"] is False
    assert result["checks"]["all_baseline_field_state_rows_preserved_byte_identically"] is False
    assert (
        result["checks"]["v6_field_rows_have_no_fabricated_provider_manual_or_lock"]
        is False
    )
    assert (
        result["checks"]["v6_field_rows_do_not_fabricate_canonical_dates_or_versions"]
        is False
    )


def test_live_schema_gate_accepts_deterministic_normalized_artist_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(tmp_path))
    data, database = _downgraded_v5_runtime(tmp_path)
    db = MusicVaultDB(database, backup_dir=data / "backups")
    variants = (
        "CASE Ensemble",
        "  case   ensemble  ",
        "Signal & Noise",
        "Signal/Noise",
        "\uff21\uff06\uff22",
        "A&B",
    )
    for index, artist in enumerate(variants):
        path = tmp_path / f"identity-{index}.media"
        path.write_bytes(f"synthetic artist identity {index}".encode("ascii"))
        db.upsert_track(path, title=f"Synthetic {index}", artist=artist)
    db.close()
    with sqlite3.connect(database) as connection:
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

    baseline = live_gate.capture_baseline(
        project_root=tmp_path,
        data_dir=data,
        database=database,
    )
    rollback = data / "backups" / "explicit-rollback.sqlite3"
    live_gate.create_verified_backup(
        database=database,
        backup_dir=data / "backups",
        baseline=baseline,
        backup_path=rollback,
    )
    migrated = MusicVaultDB(database, backup_dir=data / "backups")
    write_app_status(migrated, {})
    migrated.close()
    result = live_gate.verify_migration(
        baseline=baseline,
        project_root=tmp_path,
        data_dir=data,
        database=database,
        backup_path=rollback,
    )
    assert result["ok"] is True
    assert result["checks"]["artist_display_strings_preserved"] is True
    assert result["checks"]["artist_credit_normalized_identity_matches_track_display"] is True
    assert result["checks"]["normalized_artist_entity_reuse_is_deterministic"] is True
    assert result["checks"]["ampersand_names_not_split"] is True
    assert result["counts"]["seeded_artist_entities"] == 6
    assert result["counts"]["seeded_artist_credits"] == 8

    with sqlite3.connect(database) as connection:
        fabricated_id = int(
            connection.execute(
                """
                INSERT INTO artists(
                    display_name,normalized_name,sort_name,entity_type,
                    created_at,updated_at
                ) VALUES('Fabricated Label','fabricated label','fabricated label',
                         'unknown','t0','t0')
                """
            ).lastrowid
        )
        connection.execute(
            "UPDATE track_artist_credits SET artist_id=? "
            "WHERE track_id=(SELECT MIN(id) FROM tracks)",
            (fabricated_id,),
        )
    rejected = live_gate.verify_migration(
        baseline=baseline,
        project_root=tmp_path,
        data_dir=data,
        database=database,
        backup_path=rollback,
    )
    assert rejected["ok"] is False
    assert rejected["checks"]["artist_credit_normalized_identity_matches_track_display"] is False
    assert rejected["checks"]["no_label_or_unrelated_artist_fabricated"] is False


def test_live_schema_cli_guard_requires_ack_no_secrets_and_closed_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", raising=False)
    monkeypatch.setattr(live_gate, "_music_vault_running", lambda: False)
    with pytest.raises(live_gate.GateFailure, match="acknowledgement"):
        live_gate._execution_guard("wrong")
    with pytest.raises(live_gate.GateFailure, match="no_secrets"):
        live_gate._execution_guard(live_gate.ACKNOWLEDGEMENT)
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setattr(live_gate, "_music_vault_running", lambda: True)
    with pytest.raises(live_gate.GateFailure, match="process"):
        live_gate._execution_guard(live_gate.ACKNOWLEDGEMENT)


def test_packaged_smoke_prepare_verify_is_isolated_and_offline(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    runtime = Path(tempfile.gettempdir()) / (
        packaged.RUNTIME_PREFIX + "pytest_" + next(tempfile._get_candidate_names())
    )
    try:
        manifest = packaged.prepare(runtime, project)
        result = packaged.verify(runtime, project, manifest)
        assert manifest["schema_version"] == 6
        assert manifest["network_attempt_count"] == 0
        assert manifest["secret_file_read_count"] == 0
        assert manifest["media_file_write_count"] == 0
        assert manifest["discogs_query_count"] == manifest["track_count"]
        assert result["ok"] is True
        assert all(result["checks"].values())
        assert not (runtime / "data" / "youtube_api_key.txt").exists()
        assert not (runtime / "data" / "discogs_token.txt").exists()
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


def test_acceptance_powershell_wrappers_are_explicit_and_project_local() -> None:
    live = (PROJECT_ROOT / "tools" / "dev" / "verify_batch10_1_live_migration.ps1").read_text(encoding="utf-8")
    smoke = (PROJECT_ROOT / "tools" / "dev" / "run_batch10_1_packaged_smoke.ps1").read_text(encoding="utf-8")
    profile_wrapper = (PROJECT_ROOT / "tools" / "dev" / "profile_metadata_intelligence.ps1").read_text(encoding="utf-8")
    assert "[Parameter(Mandatory = $true)]" in live
    assert "AcknowledgeLiveLibrary" in live
    assert 'MUSIC_VAULT_ACCEPTANCE_NO_SECRETS = "1"' in live
    assert ".venv\\Scripts\\python.exe" in live
    assert "MUSIC_VAULT_PROJECT_ROOT" in smoke
    assert "CloseMainWindow" in smoke
    assert "MainWindowHandle" in smoke
    assert "-WindowStyle Hidden" not in smoke
    assert "WaitForExit" in smoke
    assert "dist\\MusicVault\\MusicVault.exe" in smoke
    assert ".venv\\Scripts\\python.exe" in profile_wrapper


def test_fake_provider_objects_have_no_network_session() -> None:
    discogs = SyntheticDiscogsProvider()
    musicbrainz = SyntheticMusicBrainzProvider()
    assert not hasattr(discogs, "session")
    assert not hasattr(musicbrainz, "session")


def test_acceptance_tool_imports_do_not_mutate_the_parent_secret_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", raising=False)
    importlib.reload(profile)
    importlib.reload(packaged)
    assert "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS" not in os.environ


def test_in_app_metadata_intelligence_smoke_is_bounded_and_synthetic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "review-runtime"
    (runtime / "data").mkdir(parents=True)
    (runtime / "music_vault").mkdir()
    (runtime / "run.py").write_text("# synthetic marker\n", encoding="utf-8")
    db = MusicVaultDB(runtime / "data" / "music_vault.sqlite3")
    plan = ui_review.ReviewPlan(
        request_path=runtime / "plan.json",
        runtime_root=runtime,
        output_dir=tmp_path / "output",
        sizes=(ui_review.ReviewSize(1280, 720),),
        scenes=("metadata_intelligence_smoke",),
        settle_ms=100,
    )
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setattr(ui_review, "_REVIEW_NETWORK_GUARD_INSTALLED", True)
    monkeypatch.setattr(ui_review, "_REVIEW_NETWORK_EVENTS", [])
    try:
        evidence = ui_review.validate_metadata_intelligence_review_behaviors(
            SimpleNamespace(db=db),
            plan,
        )
        assert evidence["schema_6"] is True
        assert evidence["exact_random_uploader_corrected"] is True
        assert evidence["label_excluded_from_artist_credits"] is True
        assert evidence["group_and_featured_credits_structured"] is True
        assert evidence["provider_conflict_requires_review"] is True
        assert evidence["youtube_exclusive_fallback_reviewed"] is True
        assert evidence["network_attempt_count"] == 0
        assert evidence["synthetic_media_writes_confined_to_runtime"] is True
        assert evidence["file_writeback_enabled"] is True
        assert evidence["high_confidence_tag_writeback_verified"] is True
        assert evidence["exact_file_backups_verified"] is True
        assert evidence["audio_payload_unchanged"] is True
        assert evidence["artwork_gap_fill_enabled"] is True
        assert evidence["missing_artwork_filled"] is True
        assert evidence["valid_existing_artwork_preserved"] is True
        assert evidence["artwork_attribution_persisted"] is True
        assert evidence["discogs_artwork_not_embedded"] is True
    finally:
        db.close()


def test_packaged_review_evidence_requires_explicit_frozen_behavior_marker(
    tmp_path: Path,
) -> None:
    metadata = {
        name: True
        for name in (
            "manual_save",
            "candidate_apply",
            "artwork_replace",
            "undo",
            "approved_snapshot",
            "queue_context_preserved",
            "playlist_membership_preserved",
        )
    }
    intelligence = {
        name: True
        for name in (
            "packaged_process",
            "schema_6",
            "exact_random_uploader_corrected",
            "label_excluded_from_artist_credits",
            "group_and_featured_credits_structured",
            "studio_live_tracks_remain_separate",
            "unofficial_live_year_blank_original_date_separate",
            "provider_conflict_requires_review",
            "youtube_exclusive_fallback_reviewed",
            "source_memberships_preserved",
            "network_guard_active",
            "no_secret_files",
            "synthetic_media_writes_confined_to_runtime",
            "file_writeback_enabled",
            "high_confidence_tag_writeback_verified",
            "exact_file_backups_verified",
            "audio_payload_unchanged",
            "artwork_gap_fill_enabled",
            "missing_artwork_filled",
            "valid_existing_artwork_preserved",
            "artwork_attribution_persisted",
            "discogs_artwork_not_embedded",
        )
    }
    intelligence.update(
        {
            "network_attempt_count": 0,
            "discogs_query_count": 9,
            "musicbrainz_query_count": 9,
            "artwork_store_call_count": 2,
        }
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "status": "complete",
                "capture_count": 2,
                "runtime_checks": {
                    "metadata_behaviors": metadata,
                    "metadata_intelligence_behaviors": intelligence,
                },
            }
        ),
        encoding="utf-8",
    )
    evidence = packaged._packaged_review_evidence(manifest)
    assert evidence["packaged_process"] is True
    assert evidence["discogs_query_count"] == 9
    assert evidence["artwork_store_call_count"] == 2

    intelligence["packaged_process"] = False
    manifest.write_text(
        json.dumps(
            {
                "status": "complete",
                "capture_count": 2,
                "runtime_checks": {
                    "metadata_behaviors": metadata,
                    "metadata_intelligence_behaviors": intelligence,
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="incomplete"):
        packaged._packaged_review_evidence(manifest)


def test_ui_review_aggregate_preserves_behavior_checks_from_every_child(
    tmp_path: Path,
) -> None:
    first = {
        "status": "complete",
        "finished_at": "2026-01-01T00:00:00Z",
        "pages": ["Intelligence"],
        "captures": [{"scene": "metadata_intelligence_smoke"}],
        "runtime_checks": {
            "schema_version": 6,
            "metadata_intelligence_behaviors": {"packaged_process": True},
        },
    }
    second = {
        "status": "complete",
        "finished_at": "2026-01-01T00:00:01Z",
        "pages": ["Provenance"],
        "captures": [{"scene": "metadata_provenance_locks"}],
        "runtime_checks": {
            "schema_version": 6,
            "metadata_behaviors": {"manual_save": True},
        },
    }

    aggregate = capture_ui_review.write_aggregate_manifest(
        tmp_path,
        [first, second],
        ((1280, 720),),
        ("metadata_intelligence_smoke", "metadata_provenance_locks"),
    )

    checks = aggregate["runtime_checks"]
    assert checks["metadata_intelligence_behaviors"] == {"packaged_process": True}
    assert checks["metadata_behaviors"] == {"manual_save": True}
