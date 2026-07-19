from __future__ import annotations

"""Prepare and verify the packaged Batch 10.4 two-launch quiescence smoke.

The PowerShell wrapper owns both frozen processes.  This helper only prepares
an isolated synthetic schema-6 runtime and verifies aggregate evidence after:

1. a guarded schema-6 to schema-7 migration launch; and
2. a normal schema-7 launch using the synthetic artist provider.

No live runtime file is opened by this helper.  Reports deliberately exclude
track, artist, album, source, media-path, URL, and credential values.
"""

import argparse
import copy
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from music_vault.metadata.artist_images import (  # noqa: E402
    ARTIST_IMAGE_CACHE_SCHEMA_VERSION,
)
from music_vault.ui.party_mode import PARTY_MODE_CONFIG_VERSION  # noqa: E402
from tools.dev import batch10_3_acceptance as acceptance  # noqa: E402
from tools.dev import batch10_4_acceptance as batch104  # noqa: E402
from tools.dev import run_batch10_3_source_migration_proof as source_proof  # noqa: E402
from tools.dev import verify_batch10_3_live_migration as migration_gate  # noqa: E402


RUNTIME_PREFIX = "MusicVault_Batch10_4_PackagedQuiescence_"
DATABASE_RELATIVE_PATH = Path("data/music_vault.sqlite3")
EXPLICIT_BACKUP_NAME = "batch10_4_packaged_schema6_rollback.sqlite3"
MANIFEST_FORMAT_VERSION = 1
SECOND_REVIEW_PLAN_NAME = "batch10_4-second-launch-review.json"
SECOND_REVIEW_OUTPUT_SUFFIX = "_SecondLaunchReview"
SECOND_REVIEW_SCENE = "artists_fetch_enabled"


class SmokeFailure(acceptance.AcceptanceFailure):
    """A stable, aggregate-only packaged-smoke failure."""


def _safe_runtime(path: Path) -> Path:
    runtime = Path(path).expanduser().resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    if (
        not acceptance.is_within(runtime, temporary)
        or runtime == temporary
        or not runtime.name.startswith(RUNTIME_PREFIX)
        or runtime.is_symlink()
    ):
        raise SmokeFailure("unsafe_temporary_runtime")
    return runtime


def _safe_review_output(runtime: Path) -> Path:
    output = runtime.with_name(runtime.name + SECOND_REVIEW_OUTPUT_SUFFIX).resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    if (
        not acceptance.is_within(output, temporary)
        or output == temporary
        or output.name != runtime.name + SECOND_REVIEW_OUTPUT_SUFFIX
        or output.is_symlink()
    ):
        raise SmokeFailure("unsafe_review_output")
    return output


def _empty_artist_cache(data: Path) -> None:
    root = data / "artist_images"
    root.mkdir(parents=True)
    acceptance.atomic_write_json(
        root / "index.json",
        {"schema_version": ARTIST_IMAGE_CACHE_SCHEMA_VERSION, "entries": {}},
    )


def _review_plan(runtime: Path, output: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runtime_root": str(runtime),
        "output_dir": str(output),
        "sizes": [{"width": 1280, "height": 720}],
        "scenes": [SECOND_REVIEW_SCENE],
        "settle_ms": 100,
        "expected_capture_count": 1,
    }


def prepare(runtime: Path, project_root: Path) -> dict[str, Any]:
    """Create a disposable schema-6 runtime and its preservation evidence."""

    runtime = _safe_runtime(runtime)
    root = Path(project_root).expanduser().resolve()
    executable = root / "dist" / "MusicVault" / "MusicVault.exe"
    if not executable.is_file():
        raise SmokeFailure("official_executable_unavailable")
    if (root / "dist" / "MusicVault" / "data").exists():
        raise SmokeFailure("packaged_distribution_data_folder_present")
    if runtime.exists():
        raise SmokeFailure("temporary_runtime_already_exists")
    review_output = _safe_review_output(runtime)
    if review_output.exists():
        raise SmokeFailure("temporary_review_output_already_exists")

    data = runtime / "data"
    backups = data / "backups"
    downloads = data / "youtube_downloads"
    database = runtime / DATABASE_RELATIVE_PATH
    data.mkdir(parents=True)
    downloads.mkdir()
    (runtime / "music_vault").mkdir()
    (runtime / "run.py").write_text(
        "# disposable packaged Batch 10.4 quiescence marker\n",
        encoding="utf-8",
    )
    acceptance.atomic_write_json(
        runtime / "music-vault.portable.json",
        {
            "schema_version": 1,
            "product": "Music Vault",
            "portable": True,
            "data_directory": "data",
        },
    )
    acceptance.atomic_write_json(
        data / "music_vault_config.json",
        {
            "onboarding_completed": True,
            "download_folder": str(downloads),
            "audio_quality": "320",
            "volume_percent": 23,
            "artist_image_fetch_enabled": False,
            "party_mode_config_version": PARTY_MODE_CONFIG_VERSION,
            "metadata_intelligence_enabled": False,
            "metadata_discogs_enabled": False,
            "metadata_musicbrainz_secondary_enabled": False,
            "metadata_writeback_enabled": False,
            "metadata_fill_missing_artwork_enabled": False,
            "metadata_intelligence_consent_version": 0,
            "metadata_discogs_consent_version": 0,
        },
    )
    _empty_artist_cache(data)
    acceptance.atomic_write_json(
        runtime / SECOND_REVIEW_PLAN_NAME,
        _review_plan(runtime, review_output),
    )

    source_proof._create_synthetic_schema6(database, backups, runtime)
    baseline = acceptance.capture_database_baseline(
        project_root=runtime,
        data_dir=data,
        database=database,
        expected_schema=acceptance.PRE_SCHEMA_VERSION,
    )
    dry_run = migration_gate.clone_dry_run(
        project_root=runtime,
        data_dir=data,
        database=database,
        baseline=baseline,
        temporary_parent=runtime.parent,
    )
    explicit_backup = acceptance.create_verified_sqlite_backup(
        database=database,
        backup=backups / EXPLICIT_BACKUP_NAME,
        baseline=baseline,
    )
    cache = batch104.audit_artist_cache(
        data / "artist_images",
        expected_file_count=1,
        expected_total_bytes=(data / "artist_images" / "index.json").stat().st_size,
        allow_synthetic=True,
    )
    if not cache["ok"]:
        raise SmokeFailure("initial_artist_cache_invalid")
    if any((data / name).exists() for name in acceptance.CREDENTIAL_FILE_NAMES.values()):
        raise SmokeFailure("temporary_credential_present")

    return {
        "manifest_format_version": MANIFEST_FORMAT_VERSION,
        "baseline": baseline,
        "dry_run": dry_run,
        "explicit_backup": explicit_backup,
        "initial_artist_cache": cache,
        "second_review": {
            "plan_name": SECOND_REVIEW_PLAN_NAME,
            "output_suffix": SECOND_REVIEW_OUTPUT_SUFFIX,
            "scene": SECOND_REVIEW_SCENE,
        },
        "execution_policy": {
            "first_launch_no_secrets": True,
            "first_launch_no_network": True,
            "first_launch_migration_quiescent": True,
            "second_launch_acceptance_blocks_removed": True,
            "second_launch_synthetic_provider_only": True,
            "second_launch_external_network_observation": True,
            "graceful_close_required": True,
        },
        "raw_library_values_emitted": False,
        "credential_contents_read": False,
        "media_contents_read": False,
    }


def _status_payload(path: Path) -> dict[str, Any]:
    payload = batch104._strict_json(path, maximum_bytes=2 * 1024 * 1024)
    if not isinstance(payload, dict):
        raise SmokeFailure("app_status_invalid")
    return payload


def _normal_status(path: Path) -> dict[str, Any]:
    payload = _status_payload(path)
    health = payload.get("health")
    paths = payload.get("paths")
    playback = payload.get("playback")
    sync = payload.get("sync")
    checks = {
        "api_not_ready_without_fixture_secret": (
            isinstance(health, dict) and health.get("api_ready") is False
        ),
        "discogs_not_ready_without_fixture_secret": payload.get("discogs_ready") is False,
        "provider_work_not_deferred": payload.get("provider_work_deferred") is False,
        "defer_reason_cleared": payload.get("provider_work_defer_reason") is None,
        "private_paths_suppressed": isinstance(paths, dict)
        and all(paths.get(name) is None for name in batch104.PRIVATE_STATUS_PATH_FIELDS),
        "playback_identity_suppressed": isinstance(playback, dict)
        and all(
            playback.get(name) is None
            for name in batch104.PRIVATE_STATUS_PLAYBACK_FIELDS
        ),
        "sync_identity_suppressed": isinstance(sync, dict)
        and all(sync.get(name) is None for name in batch104.PRIVATE_STATUS_SYNC_FIELDS),
        "sync_item_details_suppressed": isinstance(sync, dict)
        and sync.get("last_sync_failures") in (None, []),
        "no_secret_value_fields": not batch104._contains_secret_field(payload),
    }
    return {
        "verified": all(checks.values()),
        "checks": checks,
        "provider_work_deferred": False,
        "provider_work_defer_reason": None,
        "identity_values_emitted": False,
        "paths_emitted": False,
        "credential_contents_read": False,
    }


def verify_first_launch(
    runtime: Path,
    project_root: Path,
    manifest: Mapping[str, Any],
    *,
    graceful_close_confirmed: bool,
    network_report: Path,
    observed_network_connection_count: int = 0,
) -> dict[str, Any]:
    """Verify migration quiescence and capture the schema-7 second baseline."""

    runtime = _safe_runtime(runtime)
    root = Path(project_root).expanduser().resolve()
    if manifest.get("manifest_format_version") != MANIFEST_FORMAT_VERSION:
        raise SmokeFailure("manifest_version_invalid")
    if not graceful_close_confirmed:
        raise SmokeFailure("first_launch_graceful_close_not_confirmed")
    if int(observed_network_connection_count) != 0:
        raise SmokeFailure("first_launch_network_connection_observed")

    data = runtime / "data"
    database = runtime / DATABASE_RELATIVE_PATH
    migration = migration_gate.verify_migration(
        baseline=manifest["baseline"],
        dry_run=manifest["dry_run"],
        project_root=runtime,
        data_dir=data,
        database=database,
        backup_path=data / "backups" / EXPLICIT_BACKUP_NAME,
        network_report=network_report,
    )
    status = batch104.validate_safe_app_status(data / "music_vault_status.json")
    network = acceptance.verify_acceptance_network_report(network_report)
    post_first = batch104.capture_quiescence_baseline(
        project_root=runtime,
        data_dir=data,
        database=database,
        expected_cache_file_count=1,
        expected_cache_total_bytes=(data / "artist_images" / "index.json").stat().st_size,
    )
    cache_counts = post_first["artist_cache_audit"]["counts"]
    checks = {
        "schema_migrated_6_to_7": (
            int(manifest["baseline"]["database"]["health"]["schema_version"]) == 6
            and int(post_first["database_state"]["database"]["health"]["schema_version"])
            == batch104.SCHEMA_VERSION
        ),
        "migration_semantics_and_preservation_passed": migration.get("ok") is True,
        "migration_startup_status_safe": status.get("verified") is True,
        "migration_startup_defer_reason": (
            status.get("provider_work_defer_reason") == "migration_startup"
        ),
        "provider_work_deferred": status.get("checks", {}).get(
            "provider_work_deferred"
        )
        is True,
        "artist_provider_cache_untouched": (
            int(cache_counts.get("index_entry_count", -1)) == 0
            and int(cache_counts.get("physical_image_count", -1)) == 0
        ),
        "metadata_intelligence_did_not_run": migration.get("checks", {}).get(
            "intelligence_item_identity_and_evidence_preserved"
        )
        is True,
        "acceptance_network_guard_finalized": network.get("verified") is True,
        "zero_guarded_network_attempts": int(network.get("attempt_count", -1)) == 0,
        "zero_provider_factory_invocations": int(
            network.get("provider_factory_invocation_count", -1)
        )
        == 0,
        "zero_provider_task_dispatches": int(
            network.get("provider_task_dispatch_count", -1)
        )
        == 0,
        "zero_observed_network_connections": int(observed_network_connection_count) == 0,
        "credentials_absent_and_unread": (
            not any(
                (data / name).exists()
                for name in acceptance.CREDENTIAL_FILE_NAMES.values()
            )
            and migration.get("credential_contents_read") is False
            and network.get("credential_contents_read") is False
        ),
        "graceful_close_confirmed": graceful_close_confirmed,
        "official_dist_data_absent": not (root / "dist" / "MusicVault" / "data").exists(),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "post_first_baseline": post_first,
        "counts": {
            "track_count": int(migration["counts"]["track_count"]),
            "canonical_album_count": int(migration["counts"]["canonical_album_count"]),
            "canonical_album_membership_count": int(
                migration["counts"]["canonical_album_membership_count"]
            ),
            "canonical_artist_count": int(migration["counts"]["canonical_artist_count"]),
            "artist_alias_count": int(migration["counts"]["artist_alias_count"]),
            "network_attempt_count": 0,
            "artist_cache_entry_count": 0,
        },
        "status": status,
        "raw_library_values_emitted": False,
        "credential_contents_read": False,
        "media_contents_read": False,
    }


def _without_artist_cache(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    artwork = result.get("artwork")
    if isinstance(artwork, dict):
        artwork.pop("artist_image_tree", None)
    return result


def _verify_review_manifest(path: Path, runtime: Path) -> dict[str, Any]:
    manifest_path = Path(path).expanduser().resolve()
    output = _safe_review_output(runtime)
    if manifest_path != output / "manifest.json":
        raise SmokeFailure("second_review_manifest_scope_invalid")
    payload = batch104._strict_json(manifest_path, maximum_bytes=4 * 1024 * 1024)
    captures = payload.get("captures") if isinstance(payload, dict) else None
    capture = captures[0] if isinstance(captures, list) and len(captures) == 1 else None
    metrics = capture.get("browser_metrics") if isinstance(capture, dict) else None
    filename = str(capture.get("file") or "") if isinstance(capture, dict) else ""
    screenshot = output / filename
    screenshot_valid = bool(
        filename
        and Path(filename).name == filename
        and screenshot.is_file()
        and hashlib.sha256(screenshot.read_bytes()).hexdigest()
        == str(capture.get("sha256") or "")
    )
    checks = {
        "review_complete": isinstance(payload, dict) and payload.get("status") == "complete",
        "isolated_runtime": isinstance(payload, dict)
        and payload.get("runtime") == "isolated_temporary",
        "single_expected_scene": (
            isinstance(payload, dict)
            and payload.get("requested_capture_count") == 1
            and payload.get("capture_count") == 1
            and isinstance(capture, dict)
            and capture.get("scene") == SECOND_REVIEW_SCENE
        ),
        "capture_hash_valid": screenshot_valid,
        "artist_browser_active": isinstance(metrics, dict)
        and metrics.get("kind") == "artists"
        and int(metrics.get("model_rows", 0)) > 0,
        "synthetic_provider_active": isinstance(metrics, dict)
        and metrics.get("synthetic_provider_active") is True,
        "synthetic_provider_called": isinstance(metrics, dict)
        and int(metrics.get("synthetic_provider_call_count", 0)) > 0,
        "public_provider_not_called": isinstance(metrics, dict)
        and int(metrics.get("public_provider_call_count", -1)) == 0,
    }
    return {
        "verified": all(checks.values()),
        "checks": checks,
        "capture_count": 1 if isinstance(captures, list) else 0,
        "synthetic_provider_call_count": (
            int(metrics.get("synthetic_provider_call_count", 0))
            if isinstance(metrics, dict)
            else 0
        ),
        "raw_identity_values_emitted": False,
        "urls_emitted": False,
    }


def verify_second_launch(
    runtime: Path,
    project_root: Path,
    manifest: Mapping[str, Any],
    *,
    graceful_close_confirmed: bool,
    review_manifest: Path,
    observed_network_connection_count: int = 0,
) -> dict[str, Any]:
    """Verify that the next ordinary launch resumes only fake provider work."""

    runtime = _safe_runtime(runtime)
    root = Path(project_root).expanduser().resolve()
    if manifest.get("manifest_format_version") != MANIFEST_FORMAT_VERSION:
        raise SmokeFailure("manifest_version_invalid")
    first = manifest.get("post_first_baseline")
    if not isinstance(first, dict):
        raise SmokeFailure("first_launch_baseline_missing")
    if not graceful_close_confirmed:
        raise SmokeFailure("second_launch_graceful_close_not_confirmed")
    if int(observed_network_connection_count) != 0:
        raise SmokeFailure("second_launch_network_connection_observed")
    if os.environ.get("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "").strip() == "1":
        raise SmokeFailure("second_launch_no_secret_block_still_active")
    if os.environ.get("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK", "").strip() == "1":
        raise SmokeFailure("second_launch_no_network_block_still_active")

    data = runtime / "data"
    database = runtime / DATABASE_RELATIVE_PATH
    current_database = acceptance.capture_database_baseline(
        project_root=runtime,
        data_dir=data,
        database=database,
        expected_schema=batch104.SCHEMA_VERSION,
    )
    current_file = batch104._database_file_guard(database)
    current_cache = batch104.audit_artist_cache(
        data / "artist_images",
        allow_synthetic=True,
    )
    status = _normal_status(data / "music_vault_status.json")
    review = _verify_review_manifest(review_manifest, runtime)
    first_database = first.get("database_state")
    first_cache = first.get("artist_cache_audit", {}).get("counts", {})
    current_cache_counts = current_cache.get("counts", {})
    checks = {
        "schema_remained_7": (
            int(current_database["database"]["health"]["schema_version"])
            == batch104.SCHEMA_VERSION
        ),
        "no_second_migration_or_database_change": (
            isinstance(first_database, dict)
            and _without_artist_cache(current_database)
            == _without_artist_cache(first_database)
            and current_file == first.get("database_file")
        ),
        "migration_backup_inventory_unchanged": (
            isinstance(first_database, dict)
            and current_database.get("backup_inventory")
            == first_database.get("backup_inventory")
        ),
        "provider_work_no_longer_deferred": status.get("verified") is True,
        "defer_reason_cleared": status.get("provider_work_defer_reason") is None,
        "synthetic_provider_work_resumed": (
            review.get("verified") is True
            and int(review.get("synthetic_provider_call_count", 0)) > 0
            and current_cache.get("ok") is True
            and int(current_cache_counts.get("resolved_entry_count", 0)) > 0
            and int(current_cache_counts.get("provider_counts", {}).get("synthetic", 0))
            > 0
            and int(current_cache_counts.get("index_entry_count", 0))
            > int(first_cache.get("index_entry_count", -1))
        ),
        "no_public_provider_called": review.get("checks", {}).get(
            "public_provider_not_called"
        )
        is True,
        "zero_observed_network_connections": int(observed_network_connection_count) == 0,
        "acceptance_blocks_removed": True,
        "credentials_remained_absent": not any(
            (data / name).exists() for name in acceptance.CREDENTIAL_FILE_NAMES.values()
        ),
        "cover_tree_unchanged": batch104._tree_guard(data / "covers")
        == first.get("cover_tree"),
        "discogs_provider_cache_unchanged": batch104._tree_guard(
            data / "provider_cache" / "discogs"
        )
        == first.get("discogs_provider_cache"),
        "discogs_release_art_cache_unchanged": batch104._tree_guard(
            data / "covers" / "providers" / "cover_art_archive"
        )
        == first.get("discogs_release_art_cache"),
        "graceful_close_confirmed": graceful_close_confirmed,
        "official_dist_data_absent": not (root / "dist" / "MusicVault" / "data").exists(),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "counts": {
            "track_count": int(current_database["database"]["tables"]["tracks"]["count"]),
            "artist_cache_entry_count": int(
                current_cache_counts.get("index_entry_count", 0)
            ),
            "artist_cache_image_count": int(
                current_cache_counts.get("physical_image_count", 0)
            ),
            "synthetic_provider_call_count": int(
                review.get("synthetic_provider_call_count", 0)
            ),
            "observed_network_connection_count": 0,
        },
        "status": status,
        "review": review,
        "raw_library_values_emitted": False,
        "credential_contents_read": False,
        "media_contents_read": False,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "verify-first", "verify-second"):
        child = subparsers.add_parser(name)
        child.add_argument("--runtime", type=Path, required=True)
        child.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
        child.add_argument("--manifest", type=Path, required=True)
        if name != "prepare":
            child.add_argument("--graceful-close-confirmed", action="store_true")
            child.add_argument("--observed-network-connection-count", type=int, default=0)
        if name == "verify-first":
            child.add_argument("--network-report", type=Path, required=True)
        if name == "verify-second":
            child.add_argument("--review-manifest", type=Path, required=True)
    return parser.parse_args(argv)


def _summary(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok") is True,
        "checks": dict(result.get("checks") or {}),
        "counts": dict(result.get("counts") or {}),
        "raw_library_values_emitted": False,
        "credential_contents_read": False,
        "media_contents_read": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "prepare":
            manifest = prepare(args.runtime, args.project_root)
            acceptance.atomic_write_json(args.manifest, manifest)
            result = {
                "ok": True,
                "schema_before": int(
                    manifest["baseline"]["database"]["health"]["schema_version"]
                ),
                "track_count": int(
                    manifest["baseline"]["database"]["tables"]["tracks"]["count"]
                ),
                "explicit_backup_verified": bool(
                    manifest["explicit_backup"]["verified"]
                ),
                "raw_library_values_emitted": False,
            }
        elif args.command == "verify-first":
            manifest = acceptance.read_json(args.manifest)
            evidence = verify_first_launch(
                args.runtime,
                args.project_root,
                manifest,
                graceful_close_confirmed=args.graceful_close_confirmed,
                network_report=args.network_report,
                observed_network_connection_count=args.observed_network_connection_count,
            )
            manifest["post_first_baseline"] = evidence.pop("post_first_baseline")
            manifest["first_launch_summary"] = _summary(evidence)
            acceptance.atomic_write_json(args.manifest, manifest)
            result = _summary(evidence)
        else:
            manifest = acceptance.read_json(args.manifest)
            evidence = verify_second_launch(
                args.runtime,
                args.project_root,
                manifest,
                graceful_close_confirmed=args.graceful_close_confirmed,
                review_manifest=args.review_manifest,
                observed_network_connection_count=args.observed_network_connection_count,
            )
            result = _summary(evidence)
    except (
        SmokeFailure,
        batch104.Batch104Failure,
        acceptance.AcceptanceFailure,
        OSError,
        sqlite3.Error,
        KeyError,
        TypeError,
        ValueError,
    ):
        print(
            json.dumps(
                {"ok": False, "error_code": "batch10_4_packaged_quiescence_smoke_failed"}
            )
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DATABASE_RELATIVE_PATH",
    "EXPLICIT_BACKUP_NAME",
    "MANIFEST_FORMAT_VERSION",
    "RUNTIME_PREFIX",
    "SECOND_REVIEW_OUTPUT_SUFFIX",
    "SECOND_REVIEW_PLAN_NAME",
    "SECOND_REVIEW_SCENE",
    "SmokeFailure",
    "prepare",
    "verify_first_launch",
    "verify_second_launch",
]
