from __future__ import annotations

"""Prepare and verify the isolated Batch 10.2 packaged schema migration.

This helper never launches Music Vault itself.  The PowerShell wrapper owns
the frozen process.  Preparation copies the specifically authorized schema-5
rollback database, proves its pristine hash, sanitizes only paths in that TEMP
copy, captures the corrected aggregate baseline, and creates a second explicit
TEMP rollback backup.  Verification delegates the preservation decision to
``verify_batch10_1_live_migration`` and adds isolation/process evidence.

No credential file is opened by this helper and no library value or path is
printed.  Its command-line output contains aggregate evidence only.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from music_vault.ui.party_mode import PARTY_MODE_CONFIG_VERSION  # noqa: E402
from tools.dev import verify_batch10_1_live_migration as migration_gate  # noqa: E402


RUNTIME_PREFIX = "MusicVault_Batch10_2_PackagedMigration_"
ROLLBACK_RELATIVE_PATH = Path(
    "data/backups/music_vault_batch10_1_explicit_rollback_20260716_003442_649.sqlite3"
)
EXPECTED_ROLLBACK_SHA256 = (
    "394877de5fea67c0989096817ab76e0cdf38474ef304489c330b968159305ae7"
)
DATABASE_RELATIVE_PATH = Path("data/music_vault.sqlite3")
EXPLICIT_BACKUP_NAME = "batch10_2_packaged_schema5_rollback.sqlite3"
MANIFEST_FORMAT_VERSION = 1

REQUIRED_VERIFIER_CHECKS = frozenset(
    {
        "schema_is_current",
        "all_preexisting_table_rows_and_values_preserved",
        "all_baseline_field_state_rows_preserved_byte_identically",
        "exactly_three_safe_v6_field_rows_added_per_track",
        "v6_field_rows_have_no_fabricated_provider_manual_or_lock",
        "v6_field_rows_do_not_fabricate_canonical_dates_or_versions",
        "artist_display_strings_preserved",
        "one_conservative_credit_per_nonempty_artist",
        "artist_credit_normalized_identity_matches_track_display",
        "normalized_artist_entity_reuse_is_deterministic",
        "ampersand_names_not_split",
        "no_label_or_unrelated_artist_fabricated",
        "preexisting_provider_values_preserved",
        "no_provider_lookup_or_intelligence_job_ran",
        "required_v6_tables_present",
        "required_indexes_present",
        "foreign_keys_enabled",
        "foreign_key_check_clean",
        "integrity_ok",
        "extra_rollback_backup_created",
        "extra_rollback_backup_verified",
        "automatic_schema_backup_created_and_verified",
        "runtime_config_archive_failure_files_unchanged",
        "credential_file_metadata_unchanged_without_reading_contents",
        "media_content_and_timestamps_unchanged",
        "app_status_compatible",
        "app_status_private_and_no_job_activity",
        "sqlite_sidecars_absent",
        "packaged_data_folder_absent",
    }
)


class SmokeFailure(RuntimeError):
    """A deliberately path- and library-value-free smoke failure."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_runtime(path: Path) -> Path:
    runtime = path.expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if (
        not _is_relative_to(runtime, temp_root)
        or runtime == temp_root
        or not runtime.name.startswith(RUNTIME_PREFIX)
        or runtime.is_symlink()
    ):
        raise SmokeFailure("unsafe_temporary_runtime")
    return runtime


def _source_backup(project_root: Path) -> Path:
    root = project_root.expanduser().resolve()
    backup = (root / ROLLBACK_RELATIVE_PATH).resolve()
    if not _is_relative_to(backup, root) or not backup.is_file():
        raise SmokeFailure("verified_schema5_backup_unavailable")
    return backup


def _database_health(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
    return {
        "schema_version": schema,
        "integrity_ok": integrity.casefold() == "ok",
        "foreign_key_issue_count": len(foreign_keys),
    }


def _sanitize_temp_paths(database: Path, runtime: Path) -> dict[str, int]:
    """Replace only active media/artwork paths in the disposable DB copy."""

    media_root = (runtime / "isolated-media").resolve()
    cover_root = (runtime / "isolated-covers").resolve()
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT id,cover_path FROM tracks ORDER BY id"
        ).fetchall()
        with connection:
            for track_id, cover_path in rows:
                synthetic_media = media_root / f"track-{int(track_id):012d}.acceptance-media"
                synthetic_cover = (
                    str(cover_root / f"cover-{int(track_id):012d}.acceptance-image")
                    if cover_path is not None and str(cover_path).strip()
                    else None
                )
                connection.execute(
                    "UPDATE tracks SET path=?,cover_path=? WHERE id=?",
                    (str(synthetic_media), synthetic_cover, int(track_id)),
                )
    finally:
        connection.close()
    return {
        "track_path_count": len(rows),
        "nonempty_cover_path_count": sum(
            value is not None and bool(str(value).strip()) for _track_id, value in rows
        ),
    }


def _paths_are_isolated(database: Path, runtime: Path) -> tuple[bool, bool]:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT path,cover_path FROM tracks").fetchall()
    finally:
        connection.close()
    confined = True
    absent = True
    for media_value, cover_value in rows:
        for value in (media_value, cover_value):
            if value is None or not str(value).strip():
                continue
            candidate = Path(str(value)).expanduser().resolve()
            confined = confined and _is_relative_to(candidate, runtime)
            # This filesystem access is safe because confinement is checked
            # first; never stat a path that did not resolve beneath TEMP.
            if _is_relative_to(candidate, runtime):
                absent = absent and not candidate.exists()
    return confined, absent


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure("acceptance_manifest_unavailable") from exc
    if not isinstance(value, dict):
        raise SmokeFailure("acceptance_manifest_invalid")
    return value


def prepare(
    runtime: Path,
    project_root: Path,
    *,
    expected_sha256: str = EXPECTED_ROLLBACK_SHA256,
) -> dict[str, Any]:
    """Prepare a sanitized schema-5 TEMP clone and aggregate baseline."""

    runtime = _safe_runtime(runtime)
    project_root = project_root.expanduser().resolve()
    source = _source_backup(project_root)
    expected = str(expected_sha256).strip().casefold()

    # Prove the authoritative source before creating or mutating its TEMP copy.
    source_hash_before = _sha256_file(source)
    if source_hash_before != expected:
        raise SmokeFailure("verified_schema5_backup_hash_mismatch")
    source_health = _database_health(source)
    if source_health != {
        "schema_version": migration_gate.EXPECTED_PRE_SCHEMA,
        "integrity_ok": True,
        "foreign_key_issue_count": 0,
    }:
        raise SmokeFailure("verified_schema5_backup_health_failed")
    if (project_root / "dist" / "MusicVault" / "data").exists():
        raise SmokeFailure("packaged_distribution_data_folder_present")
    if runtime.exists():
        raise SmokeFailure("temporary_runtime_already_exists")

    data_dir = runtime / "data"
    backup_dir = data_dir / "backups"
    database = runtime / DATABASE_RELATIVE_PATH
    backup_dir.mkdir(parents=True)
    shutil.copyfile(source, database)
    pristine_copy_hash = _sha256_file(database)
    if pristine_copy_hash != source_hash_before:
        raise SmokeFailure("temporary_schema5_copy_hash_mismatch")

    # A portable marker makes the frozen root resolution independently valid;
    # the wrapper also sets MUSIC_VAULT_PROJECT_ROOT to this same TEMP root.
    _write_json(
        runtime / "music-vault.portable.json",
        {
            "schema_version": 1,
            "product": "Music Vault",
            "portable": True,
            "data_directory": "data",
        },
    )
    # The copied database represents an established installation.  Seed only
    # the neutral completion/version markers that prevent startup from
    # rewriting config for first-run or legacy Party Mode migration.  The
    # baseline can then require this TEMP runtime guard to remain byte- and
    # timestamp-identical without weakening the live preservation verifier.
    _write_json(
        data_dir / "music_vault_config.json",
        {
            "onboarding_completed": True,
            "party_mode_config_version": PARTY_MODE_CONFIG_VERSION,
        },
    )

    sanitization = _sanitize_temp_paths(database, runtime)
    paths_confined, media_absent = _paths_are_isolated(database, runtime)
    if not paths_confined or not media_absent:
        raise SmokeFailure("temporary_media_path_sanitization_failed")

    baseline = migration_gate.capture_baseline(
        project_root=project_root,
        data_dir=data_dir,
        database=database,
    )
    if int(baseline["database"]["schema_version"]) != migration_gate.EXPECTED_PRE_SCHEMA:
        raise SmokeFailure("temporary_baseline_schema_is_not_5")
    if any(value["exists"] for value in baseline["credential_metadata"].values()):
        raise SmokeFailure("temporary_credential_file_present")

    explicit_backup = backup_dir / EXPLICIT_BACKUP_NAME
    backup_evidence = migration_gate.create_verified_backup(
        database=database,
        backup_dir=backup_dir,
        baseline=baseline,
        backup_path=explicit_backup,
    )
    if not backup_evidence.get("verified"):
        raise SmokeFailure("temporary_explicit_backup_verification_failed")

    # Sanitizing the TEMP copy must never mutate the authoritative rollback.
    if _sha256_file(source) != source_hash_before:
        raise SmokeFailure("verified_schema5_backup_changed")

    return {
        "manifest_format_version": MANIFEST_FORMAT_VERSION,
        "source": {
            "sha256_before_temp_sanitization": source_hash_before,
            "pristine_copy_sha256": pristine_copy_hash,
            "schema_version": source_health["schema_version"],
            "integrity_ok": source_health["integrity_ok"],
            "foreign_key_issue_count": source_health["foreign_key_issue_count"],
            "size": int(source.stat().st_size),
        },
        "sanitization": {
            **sanitization,
            "all_track_and_cover_paths_confined_to_temp": paths_confined,
            "all_sanitized_media_and_cover_files_absent": media_absent,
        },
        "baseline": baseline,
        "explicit_backup": backup_evidence,
        "execution_policy": {
            "no_secrets": True,
            "no_provider_opt_in": True,
            "no_media_available": True,
            "official_frozen_executable_required": True,
        },
    }


def _summary_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    baseline = manifest["baseline"]
    return {
        "ok": True,
        "schema_before": int(baseline["database"]["schema_version"]),
        "track_count": int(baseline["database"]["track_count"]),
        "identity_count": int(
            baseline["preserved_tables"]["source_track_identities"]["count"]
        ),
        "baseline_field_state_count": int(
            baseline["preserved_tables"]["track_metadata_fields"]["count"]
        ),
        "source_hash_verified_before_temp_sanitization": True,
        "temp_paths_sanitized": True,
        "explicit_temp_backup_verified": bool(manifest["explicit_backup"]["verified"]),
    }


def verify(
    runtime: Path,
    project_root: Path,
    manifest: Mapping[str, Any],
    *,
    graceful_close_confirmed: bool,
    network_attempt_count: int = 0,
) -> dict[str, Any]:
    """Verify the frozen migration with the corrected Batch 10.1 gate."""

    runtime = _safe_runtime(runtime)
    project_root = project_root.expanduser().resolve()
    if manifest.get("manifest_format_version") != MANIFEST_FORMAT_VERSION:
        raise SmokeFailure("acceptance_manifest_version_unsupported")
    if not graceful_close_confirmed:
        raise SmokeFailure("graceful_process_close_not_confirmed")
    if int(network_attempt_count) != 0:
        raise SmokeFailure("packaged_network_connection_observed")

    data_dir = runtime / "data"
    database = runtime / DATABASE_RELATIVE_PATH
    explicit_backup = data_dir / "backups" / EXPLICIT_BACKUP_NAME
    source = _source_backup(project_root)
    expected_source_hash = str(
        manifest["source"]["sha256_before_temp_sanitization"]
    ).casefold()
    source_hash_unchanged = _sha256_file(source) == expected_source_hash

    paths_confined, media_absent = _paths_are_isolated(database, runtime)
    corrected = migration_gate.verify_migration(
        baseline=manifest["baseline"],
        project_root=project_root,
        data_dir=data_dir,
        database=database,
        backup_path=explicit_backup,
    )
    verifier_checks = corrected.get("checks", {})
    verifier_contract_complete = REQUIRED_VERIFIER_CHECKS <= set(verifier_checks)
    required_verifier_checks_pass = bool(
        verifier_contract_complete
        and all(verifier_checks[name] is True for name in REQUIRED_VERIFIER_CHECKS)
    )
    counts = corrected.get("counts", {})
    track_count = int(counts.get("tracks", -1))
    expected_v6_fields = track_count * 3
    baseline_identity_guard = manifest["baseline"]["preserved_tables"][
        "source_track_identities"
    ]
    current = migration_gate.capture_baseline(
        project_root=project_root,
        data_dir=data_dir,
        database=database,
        baseline=manifest["baseline"],
    )
    current_identity_guard = current["preserved_tables"]["source_track_identities"]

    checks = {
        "corrected_batch10_1_verifier_passed": corrected.get("ok") is True,
        "corrected_verifier_contract_complete": verifier_contract_complete,
        "all_required_verifier_checks_passed": required_verifier_checks_pass,
        "schema_migrated_5_to_6": (
            int(manifest["baseline"]["database"]["schema_version"]) == 5
            and verifier_checks.get("schema_is_current") is True
        ),
        "identity_mapping_and_timestamps_preserved": (
            current_identity_guard == baseline_identity_guard
        ),
        "all_old_field_states_preserved": (
            verifier_checks.get(
                "all_baseline_field_state_rows_preserved_byte_identically"
            )
            is True
            and int(counts.get("preserved_baseline_field_state_rows", -1))
            == int(counts.get("baseline_field_state_rows", -2))
        ),
        "exact_expected_v6_field_states_added": (
            verifier_checks.get("exactly_three_safe_v6_field_rows_added_per_track")
            is True
            and int(counts.get("expected_new_v6_field_state_rows", -1))
            == expected_v6_fields
            and int(counts.get("actual_new_v6_field_state_rows", -2))
            == expected_v6_fields
            and all(
                int(counts.get(name, -1)) == track_count
                for name in (
                    "new_original_release_date_field_rows",
                    "new_version_type_field_rows",
                    "new_version_label_field_rows",
                )
            )
        ),
        "artist_display_and_normalized_credit_reuse_preserved": all(
            verifier_checks.get(name) is True
            for name in (
                "artist_display_strings_preserved",
                "one_conservative_credit_per_nonempty_artist",
                "artist_credit_normalized_identity_matches_track_display",
                "normalized_artist_entity_reuse_is_deterministic",
                "ampersand_names_not_split",
                "no_label_or_unrelated_artist_fabricated",
            )
        ),
        "no_provider_lookup_or_network_work_observed": (
            verifier_checks.get("no_provider_lookup_or_intelligence_job_ran") is True
            and int(counts.get("intelligence_job_count", -1)) == 0
            and int(counts.get("intelligence_item_count", -1)) == 0
            and int(network_attempt_count) == 0
        ),
        "no_personal_or_synthetic_media_was_accessed_for_writing": (
            paths_confined
            and media_absent
            and verifier_checks.get("media_content_and_timestamps_unchanged") is True
        ),
        "credential_files_absent_and_unread": (
            verifier_checks.get(
                "credential_file_metadata_unchanged_without_reading_contents"
            )
            is True
            and not (data_dir / "youtube_api_key.txt").exists()
            and not (data_dir / "discogs_token.txt").exists()
        ),
        "official_dist_data_folder_absent": (
            verifier_checks.get("packaged_data_folder_absent") is True
            and not (project_root / "dist" / "MusicVault" / "data").exists()
        ),
        "verified_schema5_source_backup_unchanged": source_hash_unchanged,
        "frozen_process_closed_gracefully": graceful_close_confirmed,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "counts": {
            "tracks": track_count,
            "identities": int(counts.get("source_identities", baseline_identity_guard["count"])),
            "baseline_field_state_rows": int(
                counts.get("baseline_field_state_rows", -1)
            ),
            "preserved_baseline_field_state_rows": int(
                counts.get("preserved_baseline_field_state_rows", -1)
            ),
            "expected_new_v6_field_state_rows": int(
                counts.get("expected_new_v6_field_state_rows", -1)
            ),
            "actual_new_v6_field_state_rows": int(
                counts.get("actual_new_v6_field_state_rows", -1)
            ),
            "seeded_artist_credits": int(counts.get("seeded_artist_credits", -1)),
            "seeded_artist_entities": int(counts.get("seeded_artist_entities", -1)),
            "intelligence_job_count": int(counts.get("intelligence_job_count", -1)),
            "intelligence_item_count": int(counts.get("intelligence_item_count", -1)),
            "observed_network_connection_count": int(network_attempt_count),
        },
        "verifier": corrected,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "verify"))
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--graceful-close-confirmed", action="store_true")
    parser.add_argument("--network-attempt-count", type=int, default=0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.mode == "prepare":
            manifest = prepare(args.runtime, args.project_root)
            _write_json(args.manifest, manifest)
            result = _summary_from_manifest(manifest)
        else:
            manifest = _read_json(args.manifest)
            result = verify(
                args.runtime,
                args.project_root,
                manifest,
                graceful_close_confirmed=args.graceful_close_confirmed,
                network_attempt_count=args.network_attempt_count,
            )
    except (SmokeFailure, migration_gate.GateFailure, OSError, sqlite3.Error, KeyError, TypeError, ValueError):
        # Exceptions can contain personal values or paths.  Emit one stable,
        # aggregate-only failure code instead of their text.
        print(
            json.dumps({"ok": False, "error_code": "batch10_2_packaged_migration_smoke_failed"}),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    os.environ["MUSIC_VAULT_ACCEPTANCE_NO_SECRETS"] = "1"
    raise SystemExit(main())
