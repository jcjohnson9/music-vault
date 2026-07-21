from __future__ import annotations

"""Prepare/verify the isolated Batch 10.5 official-EXE smoke runtime."""

import argparse
import contextlib
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

from tools.dev import batch10_3_acceptance as acceptance  # noqa: E402
from tools.dev import run_batch10_5_review as review  # noqa: E402
from music_vault.core.db import CURRENT_SCHEMA_VERSION  # noqa: E402


RUNTIME_PREFIX = "MusicVault_Batch10_5_PackagedSmoke_"
DATABASE_RELATIVE_PATH = Path("data/music_vault.sqlite3")
REVIEW_PLAN_NAME = "batch10_5-ui-review-plan.json"
REVIEW_OUTPUT_SUFFIX = "_Review"
MANIFEST_FORMAT_VERSION = 1
LEGACY_FAILURE_IMPORT_MARKER = "legacy_failure_file_imported_v2"
REQUIRED_BEHAVIOR_FIELDS = frozenset(
    {
        "one_canonical_artist_cluster",
        "no_duplicate_normalized_artist_cards",
        "canonical_artist_sections_complete",
        "real_artist_section_handlers",
        "preferred_cached_portrait",
        "low_resolution_portrait_not_selected",
        "portrait_provider_deferred",
        "metadata_dashboard_zero_review",
        "ordinary_review_eliminated",
        "backwards_title_orientation_repaired",
        "soundtrack_album_applied",
        "virtual_uncatalogued_singleton",
        "no_unknown_album_cards",
        "version_suffix_artist_repaired",
        "global_spacebar_guarded",
        "playback_preserved",
        "queue_preserved",
        "base_context_preserved",
        "same_media_player",
        "network_guard_active",
        "credential_files_absent",
        "dist_runtime_data_absent",
    }
)


class SmokeFailure(RuntimeError):
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_snapshot(root: Path) -> dict[str, dict[str, Any]]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): {
            "size": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns,
            "sha256": _sha256(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _database_counts(database: Path) -> dict[str, int]:
    uri = database.resolve().as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return {
            "schema_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            "track_count": int(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]),
            "playlist_count": int(conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0]),
            "membership_count": int(
                conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0]
            ),
            "source_count": int(conn.execute("SELECT COUNT(*) FROM sync_sources").fetchone()[0]),
            "artist_count": int(conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0]),
            "review_count": int(
                conn.execute(
                    "SELECT COUNT(*) FROM metadata_intelligence_items "
                    "WHERE state IN ('review','ready')"
                ).fetchone()[0]
            ),
        }
    finally:
        conn.close()


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

    (runtime / "music_vault").mkdir(parents=True)
    (runtime / "run.py").write_text(
        "# disposable packaged Batch 10.5 review marker\n", encoding="utf-8"
    )
    (runtime / "data" / "youtube_downloads").mkdir(parents=True)
    (runtime / "data" / "covers").mkdir()
    (runtime / "profile" / "LocalAppData").mkdir(parents=True)
    (runtime / "profile" / "RoamingAppData").mkdir(parents=True)
    (runtime / "profile" / "Temp").mkdir(parents=True)
    source_icons = root / "assets" / "icons"
    if source_icons.is_dir():
        shutil.copytree(source_icons, runtime / "assets" / "icons")

    fixture = review.ReviewRuntime(runtime, "packaged-smoke")
    with review._review_environment(runtime):
        review._seed_batch10_5(fixture)
    database = runtime / DATABASE_RELATIVE_PATH
    # The normal application startup records that an absent legacy failure
    # file has already been considered. Seed that deterministic compatibility
    # marker before the byte-preservation baseline so the packaged smoke can
    # distinguish real startup writes from this expected one-time bookkeeping.
    with contextlib.closing(sqlite3.connect(database)) as connection:
        with connection:
            connection.execute(
                "INSERT OR IGNORE INTO app_meta(key,value) VALUES(?,?)",
                (LEGACY_FAILURE_IMPORT_MARKER, "synthetic_no_legacy_failures"),
            )
    counts = _database_counts(database)
    if (
        counts["schema_version"] != CURRENT_SCHEMA_VERSION
        or counts["review_count"] != 0
    ):
        raise SmokeFailure("synthetic_fixture_state_invalid")
    for secret_name in ("youtube_api_key.txt", "discogs_token.txt"):
        if (runtime / "data" / secret_name).exists():
            raise SmokeFailure("temporary_credential_present")

    output = runtime.with_name(runtime.name + REVIEW_OUTPUT_SUFFIX)
    if output.exists():
        raise SmokeFailure("temporary_review_output_already_exists")
    acceptance.atomic_write_json(
        runtime / REVIEW_PLAN_NAME,
        {
            "schema_version": 1,
            "runtime_root": str(runtime),
            "output_dir": str(output),
            "sizes": [{"width": 1280, "height": 720}],
            "scenes": ["batch10_5_smoke"],
            "settle_ms": 100,
            "expected_capture_count": 1,
        },
    )
    return {
        "manifest_format_version": MANIFEST_FORMAT_VERSION,
        "database": {
            "sha256": _sha256(database),
            "size": database.stat().st_size,
            "counts": counts,
        },
        "media": _tree_snapshot(runtime / "data" / "youtube_downloads"),
        "covers": _tree_snapshot(runtime / "data" / "covers"),
        "artist_images": _tree_snapshot(runtime / "data" / "artist_images"),
        "seed": fixture.seed_evidence,
        "execution_policy": {
            "official_executable_required": True,
            "no_secrets": True,
            "providers_disabled": True,
            "network_observation_required": True,
            "synthetic_current_schema": True,
        },
        "raw_library_values_emitted": False,
    }


def _review_evidence(path: Path) -> dict[str, Any]:
    manifest_path = path.expanduser().resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure("packaged_ui_review_manifest_unavailable") from exc
    runtime_checks = payload.get("runtime_checks")
    behaviors = (
        runtime_checks.get("batch10_5_behaviors")
        if isinstance(runtime_checks, dict)
        else None
    )
    captures = payload.get("captures")
    if (
        payload.get("status") != "complete"
        or payload.get("runtime") != "isolated_temporary"
        or payload.get("requested_capture_count") != 1
        or payload.get("capture_count") != 1
        or not isinstance(captures, list)
        or len(captures) != 1
        or captures[0].get("scene") != "batch10_5_smoke"
        or not isinstance(behaviors, dict)
        or behaviors.get("packaged_process") is not True
        or int(behaviors.get("schema_version", -1)) != CURRENT_SCHEMA_VERSION
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
        or _sha256(screenshot) != str(captures[0].get("sha256") or "")
    ):
        raise SmokeFailure("packaged_ui_review_capture_invalid")
    return {
        "verified": True,
        "capture_count": 1,
        "artist_card_count": int(behaviors.get("artist_card_count", 0)),
        "review_count": sum(
            int(behaviors.get("review_outcome_counts", {}).get(state, 0))
            for state in ("review", "ready")
        ),
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
    before_database = manifest["database"]
    counts = _database_counts(database)
    status_path = runtime / "data" / "music_vault_status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure("app_status_unavailable") from exc
    checks = {
        "current_schema_preserved": (
            counts["schema_version"] == CURRENT_SCHEMA_VERSION
        ),
        "database_bytes_unchanged": (
            _sha256(database) == before_database["sha256"]
            and database.stat().st_size == before_database["size"]
        ),
        "aggregate_counts_unchanged": counts == before_database["counts"],
        "ordinary_review_zero": counts["review_count"] == 0,
        "media_unchanged": _tree_snapshot(runtime / "data" / "youtube_downloads")
        == manifest["media"],
        "covers_unchanged": _tree_snapshot(runtime / "data" / "covers")
        == manifest["covers"],
        "artist_cache_unchanged": _tree_snapshot(runtime / "data" / "artist_images")
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
        "temporary_dist_data_absent": not (
            runtime / "dist" / "MusicVault" / "data"
        ).exists(),
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
                "review_count": int(manifest["database"]["counts"]["review_count"]),
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
        print(json.dumps({"ok": False, "error_code": "batch10_5_packaged_smoke_failed"}))
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
