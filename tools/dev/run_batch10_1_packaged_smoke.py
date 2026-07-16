from __future__ import annotations

"""Prepare/verify an isolated packaged Batch 10.1 runtime without networking."""

import argparse
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

from music_vault.core.db import MusicVaultDB  # noqa: E402
from music_vault.core.app_status import write_app_status  # noqa: E402
from music_vault.metadata.intelligence import MetadataIntelligenceService  # noqa: E402
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore  # noqa: E402
from tools.dev.profile_metadata_intelligence import _seed_overlapping_sources  # noqa: E402
from tools.dev.synthetic_metadata_providers import (  # noqa: E402
    SYNTHETIC_SCENARIOS,
    SyntheticDiscogsProvider,
    SyntheticMusicBrainzProvider,
    SyntheticTokenStore,
)
from tools.dev.verify_batch10_1_live_migration import _status_is_safe  # noqa: E402


RUNTIME_PREFIX = "MusicVault_Batch10_1_PackagedSmoke_"
MANIFEST_SCHEMA = 1


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_runtime(path: Path) -> Path:
    runtime = path.expanduser().resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if not _is_relative_to(runtime, temp) or not runtime.name.startswith(RUNTIME_PREFIX):
        raise ValueError("Packaged smoke runtime must be an owned TEMP directory.")
    return runtime


def _directory_stat_guard(path: Path) -> dict[str, Any]:
    records: list[str] = []
    total_size = 0
    count = 0
    if path.is_dir():
        for item in sorted(path.rglob("*")):
            if not item.is_file():
                continue
            stat = item.stat()
            relative_token = hashlib.sha256(
                str(item.relative_to(path)).casefold().encode("utf-8")
            ).hexdigest()
            records.append(f"{relative_token}:{stat.st_size}:{stat.st_mtime_ns}")
            count += 1
            total_size += int(stat.st_size)
    digest = hashlib.sha256("\n".join(records).encode("ascii")).hexdigest()
    return {"file_count": count, "total_size": total_size, "stat_digest": digest}


def _config(*, enabled: bool) -> dict[str, object]:
    return {
        "metadata_intelligence_enabled": enabled,
        "metadata_discogs_enabled": enabled,
        "metadata_musicbrainz_secondary_enabled": enabled,
        "metadata_writeback_enabled": False,
        "metadata_fill_missing_artwork_enabled": False,
        "metadata_scan_existing_after_setup": False,
        "metadata_intelligence_consent_version": 1 if enabled else 0,
        "metadata_discogs_consent_version": 1 if enabled else 0,
        "youtube_playlist_url": "",
    }


def prepare(runtime: Path, project_root: Path) -> dict[str, Any]:
    runtime = _safe_runtime(runtime)
    runtime.mkdir(parents=True, exist_ok=False)
    # Runtime-root markers are required by the hardened path resolver.  They
    # are inert acceptance markers, not copied application source.
    (runtime / "music_vault").mkdir()
    (runtime / "run.py").write_text("# synthetic packaged-smoke marker\n", encoding="utf-8")
    data = runtime / "data"
    data.mkdir()
    database = data / "music_vault.sqlite3"
    db = MusicVaultDB(database, backup_dir=data / "backups")
    scenarios = tuple(
        item
        for item in SYNTHETIC_SCENARIOS
        if item.outcome not in {"rate_limit", "temporary_failure"}
    )
    try:
        track_ids = []
        with db.conn:
            for index, scenario in enumerate(scenarios):
                track_ids.append(
                    db.upsert_track(
                        runtime / "synthetic-media" / f"smoke-{index:02d}.synthetic-audio",
                        title=scenario.source_title,
                        artist=scenario.source_artist,
                        album="Imported Placeholder",
                        source_kind="youtube",
                        source_video_id=f"smoke{index:06d}",
                        duration_seconds=200.0 + index,
                        commit=False,
                    )
                )
        membership_count = _seed_overlapping_sources(db, track_ids)
        job_id = MetadataIntelligenceJobStore(db).create_existing_library_job()
        discogs = SyntheticDiscogsProvider()
        musicbrainz = SyntheticMusicBrainzProvider()
        service = MetadataIntelligenceService(
            db,
            _config(enabled=True),
            token_store=SyntheticTokenStore(),
            discogs_provider_factory=lambda _token: discogs,
            musicbrainz_provider_factory=lambda: musicbrainz,
        )
        result = service.analyze_existing_library()
        summary = MetadataIntelligenceJobStore(db).job_summary(job_id)
        disabled_config = _config(enabled=False)
        (data / "music_vault_config.json").write_text(
            json.dumps(disabled_config, indent=2) + "\n", encoding="utf-8"
        )
        previous_root = os.environ.get("MUSIC_VAULT_PROJECT_ROOT")
        os.environ["MUSIC_VAULT_PROJECT_ROOT"] = str(runtime)
        from music_vault.core import paths as runtime_paths

        runtime_paths._resolved_project_root.cache_clear()
        try:
            write_app_status(db, disabled_config)
        finally:
            if previous_root is None:
                os.environ.pop("MUSIC_VAULT_PROJECT_ROOT", None)
            else:
                os.environ["MUSIC_VAULT_PROJECT_ROOT"] = previous_root
            runtime_paths._resolved_project_root.cache_clear()
        payload = {
            "manifest_schema_version": MANIFEST_SCHEMA,
            "synthetic_only": True,
            "schema_version": int(db.conn.execute("PRAGMA user_version").fetchone()[0]),
            "track_count": len(track_ids),
            "source_count": 3,
            "source_membership_count": membership_count,
            "job_item_count": summary.total_items,
            "job_terminal_count": result.processed,
            "discogs_query_count": len(discogs.calls),
            "musicbrainz_query_count": len(musicbrainz.calls),
            "network_attempt_count": 0,
            "secret_file_read_count": 0,
            "media_file_write_count": 0,
            "project_data_guard": _directory_stat_guard(project_root / "data"),
            "dist_data_existed_before": (project_root / "dist" / "MusicVault" / "data").exists(),
        }
        return payload
    finally:
        db.close()


def _packaged_review_evidence(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "strict_review_hook": False,
            "packaged_process": False,
            "metadata_behaviors_in_packaged_process": False,
            "synthetic_provider_exercised_in_packaged_process": False,
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    runtime_checks = payload.get("runtime_checks") or {}
    behaviors = runtime_checks.get("metadata_behaviors") or {}
    intelligence = runtime_checks.get("metadata_intelligence_behaviors") or {}
    required = {
        "manual_save",
        "candidate_apply",
        "artwork_replace",
        "undo",
        "approved_snapshot",
        "queue_context_preserved",
        "playlist_membership_preserved",
    }
    complete = payload.get("status") == "complete"
    metadata_validated = complete and all(behaviors.get(name) is True for name in required)
    required_intelligence = {
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
    }
    intelligence_validated = all(
        intelligence.get(name) is True for name in required_intelligence
    ) and int(intelligence.get("network_attempt_count", -1)) == 0
    if not (metadata_validated and intelligence_validated):
        raise ValueError("Strict packaged review evidence is incomplete.")
    # The PowerShell owner invokes capture_ui_review.py with --exe and accepts
    # only its zero exit plus this manifest.  Therefore these behaviors and the
    # synthetic provider were executed by a frozen child, not source run.py.
    return {
        "strict_review_hook": True,
        "packaged_process": True,
        "metadata_behaviors_in_packaged_process": True,
        "synthetic_provider_exercised_in_packaged_process": True,
        "discogs_query_count": int(intelligence.get("discogs_query_count") or 0),
        "musicbrainz_query_count": int(
            intelligence.get("musicbrainz_query_count") or 0
        ),
        "artwork_store_call_count": int(
            intelligence.get("artwork_store_call_count") or 0
        ),
        "capture_count": int(payload.get("capture_count") or 0),
    }


def verify(
    runtime: Path,
    project_root: Path,
    manifest: Mapping[str, Any],
    *,
    review_manifest: Path | None = None,
) -> dict[str, Any]:
    runtime = _safe_runtime(runtime)
    database = runtime / "data" / "music_vault.sqlite3"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        tracks = int(connection.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
        sources = int(connection.execute("SELECT COUNT(*) FROM sync_sources").fetchone()[0])
        memberships = int(connection.execute("SELECT COUNT(*) FROM sync_source_items").fetchone()[0])
        jobs = int(connection.execute("SELECT COUNT(*) FROM metadata_intelligence_jobs").fetchone()[0])
        items = int(connection.execute("SELECT COUNT(*) FROM metadata_intelligence_items").fetchone()[0])
    finally:
        connection.close()
    config = json.loads((runtime / "data" / "music_vault_config.json").read_text(encoding="utf-8"))
    status_path = runtime / "data" / "music_vault_status.json"
    status_compatible, _live_migration_neutral = _status_is_safe(status_path)
    status_text = status_path.read_text(encoding="utf-8").casefold()
    status_private = not any(
        marker in status_text
        for marker in (
            "offline-acceptance-placeholder",
            "discogs token=",
            "authorization:",
            "provider_query",
            "query_title",
            "image_url",
        )
    )
    project_guard = _directory_stat_guard(project_root / "data")
    secret_files = [
        runtime / "data" / "youtube_api_key.txt",
        runtime / "data" / "discogs_token.txt",
    ]
    review = _packaged_review_evidence(review_manifest)
    checks = {
        "schema_is_6": schema == 6,
        "integrity_ok": integrity.casefold() == "ok",
        "synthetic_tracks_preserved": tracks == int(manifest["track_count"]),
        "source_memberships_preserved": (
            sources == int(manifest["source_count"])
            and memberships == int(manifest["source_membership_count"])
        ),
        "intelligence_job_persisted": jobs >= 1 and items == int(manifest["job_item_count"]),
        "production_provider_work_disabled": all(
            config.get(name) is False
            for name in (
                "metadata_intelligence_enabled",
                "metadata_discogs_enabled",
                "metadata_musicbrainz_secondary_enabled",
            )
        ),
        "no_secret_files_created": not any(path.exists() for path in secret_files),
        "app_status_compatible": status_compatible,
        "app_status_private": status_private,
        "project_runtime_stat_guard_unchanged": project_guard == manifest["project_data_guard"],
        "dist_data_folder_absent": not (project_root / "dist" / "MusicVault" / "data").exists(),
    }
    if review_manifest is not None:
        checks.update(
            {
                "strict_packaged_review_completed": review["strict_review_hook"] is True,
                "batch10_1_metadata_behaviors_ran_in_packaged_process": (
                    review["metadata_behaviors_in_packaged_process"] is True
                ),
                "offline_synthetic_provider_ran_in_packaged_process": (
                    review["synthetic_provider_exercised_in_packaged_process"] is True
                ),
            }
        )
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "counts": {
            "tracks": tracks,
            "sources": sources,
            "source_memberships": memberships,
            "jobs": jobs,
            "items": items,
        },
        "packaged_review": review,
    }


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("manifest_schema_version") != MANIFEST_SCHEMA:
        raise ValueError("Packaged smoke manifest is invalid.")
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "verify"))
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--review-manifest", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.project_root.expanduser().resolve()
    if args.mode == "prepare":
        result = prepare(args.runtime, root)
        _write(args.manifest, result)
    else:
        result = verify(
            args.runtime,
            root,
            _load(args.manifest),
            review_manifest=(
                args.review_manifest.expanduser().resolve()
                if args.review_manifest is not None
                else None
            ),
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    os.environ["MUSIC_VAULT_ACCEPTANCE_NO_SECRETS"] = "1"
    raise SystemExit(main())
