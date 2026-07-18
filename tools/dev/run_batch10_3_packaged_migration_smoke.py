from __future__ import annotations

"""Prepare and verify the official frozen schema-6 to schema-7 smoke."""

import argparse
import hashlib
import json
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
from tools.dev import batch10_3_acceptance as acceptance  # noqa: E402
from tools.dev import run_batch10_3_source_migration_proof as source_proof  # noqa: E402
from tools.dev import verify_batch10_3_live_migration as live_gate  # noqa: E402


RUNTIME_PREFIX = "MusicVault_Batch10_3_PackagedMigration_"
DATABASE_RELATIVE_PATH = Path("data/music_vault.sqlite3")
EXPLICIT_BACKUP_NAME = "batch10_3_packaged_schema6_rollback.sqlite3"
MANIFEST_FORMAT_VERSION = 2
UI_REVIEW_PLAN_NAME = "batch10_3-ui-review-plan.json"
UI_REVIEW_OUTPUT_SUFFIX = "_Review"
UI_REVIEW_REQUIRED_TRUE_FIELDS = frozenset(
    {
        "canonical_album_grouping",
        "soundtrack_and_score_distinct",
        "canonical_artist_sections_complete",
        "group_tracks_present",
        "verified_group_appearance_present",
        "real_album_artist_handlers",
        "malformed_version_artist_repaired",
        "review_outcomes_complete",
        "review_dialog_outcomes_visible",
        "artist_provider_lazy_and_deferred",
        "global_spacebar_guarded",
        "app_status_aggregate_only",
        "playback_preserved",
        "queue_preserved",
        "base_context_preserved",
        "same_media_player",
        "party_mode_surface_preserved",
        "lyrics_surface_preserved",
        "network_guard_active",
        "credential_files_absent",
    }
)


class SmokeFailure(acceptance.AcceptanceFailure):
    """A deliberately non-identifying packaged-smoke failure."""


def _safe_runtime(path: Path) -> Path:
    runtime = path.expanduser().resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if (
        not acceptance.is_within(runtime, temp)
        or runtime == temp
        or not runtime.name.startswith(RUNTIME_PREFIX)
        or runtime.is_symlink()
    ):
        raise SmokeFailure("unsafe_temporary_runtime")
    return runtime


def prepare(runtime: Path, project_root: Path) -> dict[str, Any]:
    runtime = _safe_runtime(runtime)
    root = Path(project_root).expanduser().resolve()
    executable = root / "dist" / "MusicVault" / "MusicVault.exe"
    if not executable.is_file():
        raise SmokeFailure("official_executable_unavailable")
    if (root / "dist" / "MusicVault" / "data").exists():
        raise SmokeFailure("packaged_distribution_data_folder_present")
    if runtime.exists():
        raise SmokeFailure("temporary_runtime_already_exists")
    data = runtime / "data"
    backups = data / "backups"
    database = runtime / DATABASE_RELATIVE_PATH
    data.mkdir(parents=True)
    (runtime / "music_vault").mkdir()
    (runtime / "run.py").write_text(
        "# disposable packaged Batch 10.3 review marker\n", encoding="utf-8"
    )
    downloads = data / "youtube_downloads"
    downloads.mkdir()
    review_output = runtime.with_name(runtime.name + UI_REVIEW_OUTPUT_SUFFIX)
    if review_output.exists():
        raise SmokeFailure("temporary_review_output_already_exists")
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
    acceptance.atomic_write_json(
        runtime / UI_REVIEW_PLAN_NAME,
        {
            "schema_version": 1,
            "runtime_root": str(runtime),
            "output_dir": str(review_output),
            "sizes": [{"width": 1280, "height": 720}],
            "scenes": ["batch10_3_smoke"],
            "settle_ms": 100,
            "expected_capture_count": 1,
        },
    )
    source_proof._create_synthetic_schema6(database, backups, runtime)
    baseline = acceptance.capture_database_baseline(
        project_root=runtime,
        data_dir=data,
        database=database,
        expected_schema=acceptance.PRE_SCHEMA_VERSION,
    )
    dry_run = live_gate.clone_dry_run(
        project_root=runtime,
        data_dir=data,
        database=database,
        baseline=baseline,
        temporary_parent=runtime.parent,
    )
    explicit = backups / EXPLICIT_BACKUP_NAME
    backup = acceptance.create_verified_sqlite_backup(
        database=database,
        backup=explicit,
        baseline=baseline,
    )
    if any((data / name).exists() for name in acceptance.CREDENTIAL_FILE_NAMES.values()):
        raise SmokeFailure("temporary_credential_present")
    return {
        "manifest_format_version": MANIFEST_FORMAT_VERSION,
        "baseline": baseline,
        "dry_run": dry_run,
        "explicit_backup": backup,
        "execution_policy": {
            "official_executable_required": True,
            "no_secrets": True,
            "providers_disabled": True,
            "network_observation_required": True,
            "media_unavailable": True,
            "production_ui_review_required": True,
        },
        "ui_review": {
            "plan_schema_version": 1,
            "scene_count": 1,
            "capture_count": 1,
            "plan_name": UI_REVIEW_PLAN_NAME,
            "output_suffix": UI_REVIEW_OUTPUT_SUFFIX,
        },
        "raw_library_values_emitted": False,
    }


def _verify_ui_review_manifest(path: Path) -> dict[str, Any]:
    manifest_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure("packaged_ui_review_manifest_unavailable") from exc
    if not isinstance(payload, dict) or payload.get("status") != "complete":
        raise SmokeFailure("packaged_ui_review_incomplete")
    runtime_checks = payload.get("runtime_checks")
    behaviors = (
        runtime_checks.get("batch10_3_behaviors")
        if isinstance(runtime_checks, dict)
        else None
    )
    captures = payload.get("captures")
    if (
        payload.get("runtime") != "isolated_temporary"
        or payload.get("requested_capture_count") != 1
        or payload.get("capture_count") != 1
        or not isinstance(captures, list)
        or len(captures) != 1
        or captures[0].get("scene") != "batch10_3_smoke"
        or not isinstance(behaviors, dict)
        or behaviors.get("packaged_process") is not True
        or behaviors.get("schema_version") != acceptance.POST_SCHEMA_VERSION
        or int(behaviors.get("network_attempt_count", -1)) != 0
        or any(behaviors.get(name) is not True for name in UI_REVIEW_REQUIRED_TRUE_FIELDS)
    ):
        raise SmokeFailure("packaged_ui_review_evidence_invalid")
    capture = captures[0]
    filename = str(capture.get("file") or "")
    screenshot = manifest_path.parent / filename
    if (
        not filename
        or Path(filename).name != filename
        or not screenshot.is_file()
        or hashlib.sha256(screenshot.read_bytes()).hexdigest()
        != str(capture.get("sha256") or "")
    ):
        raise SmokeFailure("packaged_ui_review_capture_invalid")
    browser = capture.get("browser_metrics")
    if (
        not isinstance(browser, dict)
        or browser.get("kind") != "artists"
        or int(browser.get("model_rows", 0)) < 1
        or int(browser.get("per_item_widget_count", -1)) != 0
        or browser.get("synthetic_provider_active") is not True
        or int(browser.get("public_provider_call_count", -1)) != 0
    ):
        raise SmokeFailure("packaged_ui_review_browser_evidence_invalid")
    return {
        "verified": True,
        "capture_count": 1,
        "canonical_album_count": int(behaviors.get("canonical_album_count", 0)),
        "artist_card_count": int(behaviors.get("artist_card_count", 0)),
        "network_attempt_count": 0,
    }


def verify(
    runtime: Path,
    project_root: Path,
    manifest: Mapping[str, Any],
    *,
    graceful_close_confirmed: bool,
    network_report: Path,
    review_manifest: Path,
) -> dict[str, Any]:
    runtime = _safe_runtime(runtime)
    root = Path(project_root).expanduser().resolve()
    if manifest.get("manifest_format_version") != MANIFEST_FORMAT_VERSION:
        raise SmokeFailure("acceptance_manifest_version_unsupported")
    if not graceful_close_confirmed:
        raise SmokeFailure("graceful_process_close_not_confirmed")
    try:
        network_evidence = acceptance.verify_acceptance_network_report(network_report)
    except acceptance.AcceptanceFailure as exc:
        raise SmokeFailure("packaged_network_guard_evidence_failed") from exc
    review_evidence = _verify_ui_review_manifest(review_manifest)
    data = runtime / "data"
    database = runtime / DATABASE_RELATIVE_PATH
    result = live_gate.verify_migration(
        baseline=manifest["baseline"],
        dry_run=manifest["dry_run"],
        project_root=runtime,
        data_dir=data,
        database=database,
        backup_path=data / "backups" / EXPLICIT_BACKUP_NAME,
        network_report=network_report,
    )
    checks = {
        "batch10_3_preservation_gate_passed": result["ok"] is True,
        "schema_migrated_6_to_7": result["checks"]["schema_migrated_6_to_7"],
        "canonical_album_memberships_created": (
            result["counts"]["canonical_album_membership_count"] > 0
        ),
        "artist_consolidation_preserved_credits": result["checks"][
            "artist_credits_have_no_orphans"
        ],
        "review_reclassification_preserved_items": result["checks"][
            "intelligence_item_identity_and_evidence_preserved"
        ],
        "cover_paths_unchanged": result["checks"]["track_cover_paths_exact"],
        "media_metadata_unchanged": result["checks"]["media_metadata_unchanged"],
        "credentials_absent_and_unread": (
            result["checks"]["credential_metadata_unchanged_without_reading_contents"]
            and not any((data / name).exists() for name in acceptance.CREDENTIAL_FILE_NAMES.values())
        ),
        "acceptance_network_guard_verified": network_evidence["verified"] is True,
        "zero_network_connections": int(network_evidence["attempt_count"]) == 0,
        "frozen_process_closed_gracefully": graceful_close_confirmed,
        "production_ui_review_verified": review_evidence["verified"] is True,
        "production_ui_review_capture_complete": review_evidence["capture_count"] == 1,
        "official_dist_data_folder_absent": not (root / "dist" / "MusicVault" / "data").exists(),
        "temporary_dist_data_folder_absent": not (runtime / "dist" / "MusicVault" / "data").exists(),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "counts": result["counts"],
        "ui_review": review_evidence,
        "verifier": result,
        "raw_library_values_emitted": False,
        "credential_contents_read": False,
        "media_contents_read": False,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "verify"))
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--graceful-close-confirmed", action="store_true")
    parser.add_argument("--network-report", type=Path)
    parser.add_argument("--review-manifest", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.mode == "prepare":
            manifest = prepare(args.runtime, args.project_root)
            acceptance.atomic_write_json(args.manifest, manifest)
            result = {
                "ok": True,
                "schema_before": int(manifest["baseline"]["database"]["health"]["schema_version"]),
                "track_count": int(manifest["baseline"]["database"]["tables"]["tracks"]["count"]),
                "explicit_backup_verified": bool(manifest["explicit_backup"]["verified"]),
            }
        else:
            if args.network_report is None:
                raise SmokeFailure("network_report_required")
            if args.review_manifest is None:
                raise SmokeFailure("review_manifest_required")
            manifest = acceptance.read_json(args.manifest)
            result = verify(
                args.runtime,
                args.project_root,
                manifest,
                graceful_close_confirmed=args.graceful_close_confirmed,
                network_report=args.network_report,
                review_manifest=args.review_manifest,
            )
    except (acceptance.AcceptanceFailure, OSError, sqlite3.Error, KeyError, TypeError, ValueError):
        print(json.dumps({"ok": False, "error_code": "batch10_3_packaged_migration_smoke_failed"}))
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
