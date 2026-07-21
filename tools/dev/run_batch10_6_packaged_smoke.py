from __future__ import annotations

"""Prepare and verify the isolated Batch 10.6 official-EXE smoke."""

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.dev import batch10_3_acceptance as acceptance  # noqa: E402
from tools.dev import run_batch10_5_packaged_smoke as base  # noqa: E402
from music_vault.core.db import CURRENT_SCHEMA_VERSION  # noqa: E402


RUNTIME_PREFIX = "MusicVault_Batch10_6_PackagedSmoke_"
DATABASE_RELATIVE_PATH = base.DATABASE_RELATIVE_PATH
REVIEW_PLAN_NAME = "batch10_6-ui-review-plan.json"
REVIEW_OUTPUT_SUFFIX = "_Review"
MANIFEST_FORMAT_VERSION = 1
REQUIRED_BEHAVIOR_FIELDS = frozenset(
    {
        "exactly_one_target_processed",
        "weak_first_triggered_reverse",
        "musicbrainz_bounded",
        "reverse_orientation_selected",
        "orientation_evidence_complete",
        "raw_source_preserved",
        "year_and_version_preserved",
        "title_artist_corrected",
        "album_metadata_applied",
        "structured_primary_credit_correct",
        "canonical_album_membership_rebuilt",
        "metadata_history_written",
        "ordinary_review_zero",
        "terminal_non_review_outcome",
        "no_media_tag_write",
        "no_artwork_request",
        "media_unchanged",
        "covers_unchanged",
        "portrait_cache_unchanged",
        "source_memberships_preserved",
        "playlists_preserved",
        "sync_sources_preserved",
        "playback_preserved",
        "queue_preserved",
        "same_media_player",
        "network_guard_active",
        "credential_files_absent",
        "dist_runtime_data_absent",
    }
)


class SmokeFailure(RuntimeError):
    """A deliberately aggregate-only packaged-smoke failure."""


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


def _seed_orientation_target(runtime: Path) -> int:
    from music_vault.core.db import MusicVaultDB
    from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
    from music_vault.metadata.service import AutomaticMetadataField, MetadataService

    media = runtime / "data" / "youtube_downloads" / "batch10_6.synthetic-audio"
    media.write_bytes(b"synthetic Batch 10.6 fixture; not playable media\n")
    db = MusicVaultDB(runtime / DATABASE_RELATIVE_PATH)
    try:
        track_id = db.upsert_track(
            media,
            title="Anthem of the Republic - The Cosmic Assembly - Live (1978)",
            artist="Synthetic Archive",
            duration_seconds=222.0,
            source_kind="youtube",
            source_video_id="b106smoke01",
        )
        MetadataService(db).apply_automatic_fields(
            track_id,
            {
                "title": AutomaticMetadataField(
                    "The Cosmic Assembly", 68.0, "youtube_title_parsed"
                ),
                "artist": AutomaticMetadataField(
                    "Anthem of the Republic", 68.0, "youtube_title_parsed"
                ),
            },
            provider="youtube_title_parsed",
            minimum_confidence=0.0,
            reason="batch10_6_synthetic_reverse_orientation",
        )
        MetadataIntelligenceJobStore(db).enqueue_track(
            track_id,
            reason="batch10_6_synthetic_orientation",
            priority=100,
        )
        queued = int(
            db.conn.execute(
                "SELECT COUNT(*) FROM metadata_intelligence_items WHERE state='queued'"
            ).fetchone()[0]
        )
        target_count = int(
            db.conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE source_video_id='b106smoke01'"
            ).fetchone()[0]
        )
        if queued != 1 or target_count != 1:
            raise SmokeFailure("synthetic_target_queue_invalid")
        return int(track_id)
    finally:
        db.close()


def prepare(runtime: Path, project_root: Path) -> dict[str, Any]:
    runtime = _safe_runtime(runtime)
    original_prefix = base.RUNTIME_PREFIX
    try:
        base.RUNTIME_PREFIX = RUNTIME_PREFIX
        manifest = base.prepare(runtime, project_root)
    finally:
        base.RUNTIME_PREFIX = original_prefix

    _seed_orientation_target(runtime)
    database = runtime / DATABASE_RELATIVE_PATH
    counts = base._database_counts(database)
    if (
        counts["schema_version"] != CURRENT_SCHEMA_VERSION
        or counts["review_count"] != 0
    ):
        raise SmokeFailure("synthetic_fixture_state_invalid")

    old_plan = runtime / base.REVIEW_PLAN_NAME
    if old_plan.name != REVIEW_PLAN_NAME:
        old_plan.unlink(missing_ok=True)
    output = runtime.with_name(runtime.name + REVIEW_OUTPUT_SUFFIX)
    acceptance.atomic_write_json(
        runtime / REVIEW_PLAN_NAME,
        {
            "schema_version": 1,
            "runtime_root": str(runtime),
            "output_dir": str(output),
            "sizes": [{"width": 1280, "height": 720}],
            "scenes": ["batch10_6_smoke"],
            "settle_ms": 100,
            "expected_capture_count": 1,
        },
    )
    manifest.update(
        {
            "manifest_format_version": MANIFEST_FORMAT_VERSION,
            "database": {
                "sha256": base._sha256(database),
                "size": database.stat().st_size,
                "counts": counts,
            },
            "media": base._tree_snapshot(runtime / "data" / "youtube_downloads"),
            "covers": base._tree_snapshot(runtime / "data" / "covers"),
            "artist_images": base._tree_snapshot(runtime / "data" / "artist_images"),
            "execution_policy": {
                "official_executable_required": True,
                "no_secrets": True,
                "no_network": True,
                "synthetic_injected_providers": True,
                "discogs_query_limit": 2,
                "musicbrainz_query_limit": 1,
                "synthetic_current_schema": True,
            },
            "seed": {"orientation_target_count": 1, "automatic_queued_count": 1},
            "raw_library_values_emitted": False,
        }
    )
    return manifest


def _review_evidence(path: Path) -> dict[str, Any]:
    manifest_path = path.expanduser().resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure("packaged_ui_review_manifest_unavailable") from exc
    checks = payload.get("runtime_checks")
    behaviors = checks.get("batch10_6_behaviors") if isinstance(checks, dict) else None
    captures = payload.get("captures")
    if (
        payload.get("status") != "complete"
        or payload.get("runtime") != "isolated_temporary"
        or payload.get("capture_count") != 1
        or not isinstance(captures, list)
        or len(captures) != 1
        or captures[0].get("scene") != "batch10_6_smoke"
        or not isinstance(behaviors, dict)
        or behaviors.get("packaged_process") is not True
        or int(behaviors.get("schema_version", -1)) != CURRENT_SCHEMA_VERSION
        or int(behaviors.get("processed_count", -1)) != 1
        or int(behaviors.get("discogs_query_count", -1)) != 2
        or int(behaviors.get("musicbrainz_query_count", -1)) > 1
        or int(behaviors.get("network_attempt_count", -1)) != 0
        or any(behaviors.get(name) is not True for name in REQUIRED_BEHAVIOR_FIELDS)
    ):
        raise SmokeFailure("packaged_ui_review_evidence_invalid")
    filename = str(captures[0].get("file") or "")
    screenshot = manifest_path.parent / filename
    if (
        not filename
        or Path(filename).name != filename
        or not screenshot.is_file()
        or base._sha256(screenshot) != str(captures[0].get("sha256") or "")
    ):
        raise SmokeFailure("packaged_ui_review_capture_invalid")
    return {
        "verified": True,
        "capture_count": 1,
        "processed_count": 1,
        "review_count": 0,
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
    network = acceptance.verify_acceptance_network_report(network_report)
    ui = _review_evidence(review_manifest)
    database = runtime / DATABASE_RELATIVE_PATH
    counts = base._database_counts(database)
    baseline = manifest["database"]["counts"]
    status_path = runtime / "data" / "music_vault_status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure("app_status_unavailable") from exc
    checks = {
        "current_schema_preserved": (
            counts["schema_version"] == CURRENT_SCHEMA_VERSION
        ),
        "track_count_preserved": counts["track_count"] == baseline["track_count"],
        "playlist_count_preserved": counts["playlist_count"] == baseline["playlist_count"],
        "membership_count_preserved": counts["membership_count"] == baseline["membership_count"],
        "source_count_preserved": counts["source_count"] == baseline["source_count"],
        "ordinary_review_zero": counts["review_count"] == 0,
        "media_unchanged": base._tree_snapshot(runtime / "data" / "youtube_downloads")
        == manifest["media"],
        "covers_unchanged": base._tree_snapshot(runtime / "data" / "covers")
        == manifest["covers"],
        "artist_cache_unchanged": base._tree_snapshot(runtime / "data" / "artist_images")
        == manifest["artist_images"],
        "credentials_absent": not any(
            (runtime / "data" / name).exists()
            for name in ("youtube_api_key.txt", "discogs_token.txt")
        ),
        "network_guard_verified": network["verified"] is True,
        "zero_network_connections": int(network["attempt_count"]) == 0,
        "production_ui_review_verified": ui["verified"] is True,
        "process_closed_gracefully": graceful_close_confirmed,
        "app_status_written": isinstance(status, dict),
        "official_dist_data_absent": not (root / "dist" / "MusicVault" / "data").exists(),
        "temporary_dist_data_absent": not (runtime / "dist" / "MusicVault" / "data").exists(),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "counts": counts,
        "ui_review": ui,
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
                "schema_version": int(manifest["database"]["counts"]["schema_version"]),
                "track_count": int(manifest["database"]["counts"]["track_count"]),
                "queued_count": 1,
            }
        else:
            if args.network_report is None or args.review_manifest is None:
                raise SmokeFailure("verification_evidence_required")
            manifest = acceptance.read_json(args.manifest)
            result = verify(
                args.runtime,
                args.project_root,
                manifest,
                graceful_close_confirmed=args.graceful_close_confirmed,
                network_report=args.network_report,
                review_manifest=args.review_manifest,
            )
    except (RuntimeError, OSError, sqlite3.Error, KeyError, TypeError, ValueError):
        print(json.dumps({"ok": False, "error_code": "batch10_6_packaged_smoke_failed"}))
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
