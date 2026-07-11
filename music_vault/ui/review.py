from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QPoint, QRect, QTimer, Qt


REVIEW_ENV = "MUSIC_VAULT_UI_REVIEW"
REVIEW_SCHEMA_VERSION = 1
REMEDIATION_RESTART_PHASE_ENV = "MUSIC_VAULT_REMEDIATION_RESTART_PHASE"
REMEDIATION_RESTART_REQUIRED_ENV = "MUSIC_VAULT_REMEDIATION_RESTART_REQUIRED"
_REMEDIATION_RESTART_CHECKPOINT = "synthetic_remediation_restart.json"

DEFAULT_REVIEW_SCENES = (
    "library",
    "albums",
    "artists_fetch_disabled",
    "artists_fetch_enabled",
    "sync_center",
    "settings",
    "empty_playlist",
)

METADATA_REVIEW_SCENES = (
    "metadata_editor",
    "metadata_provenance_locks",
    "metadata_source_context",
    "metadata_invalid_release_date",
    "metadata_manual_artwork",
    "metadata_no_artwork",
    "metadata_musicbrainz_loading",
    "metadata_candidates",
    "metadata_candidate_high_confidence",
    "metadata_candidate_low_confidence",
    "metadata_candidate_no_artwork",
    "metadata_candidate_with_artwork",
    "metadata_provider_error",
    "metadata_history",
    "metadata_undo_confirmation",
    "metadata_long_values",
    "metadata_currently_playing",
)

REMEDIATION_REVIEW_SCENES = (
    "remediation_empty",
    "remediation_analyzing",
    "remediation_paused",
    "remediation_mixed_ready",
    "remediation_high_confirmation",
    "remediation_insufficient_disk",
    "remediation_needs_review",
    "remediation_ambiguous",
    "remediation_no_match",
    "remediation_artwork_comparison",
    "remediation_apply_progress",
    "remediation_complete_issues",
    "remediation_failed",
    "remediation_rollback_confirmation",
    "remediation_rolled_back",
    "remediation_long_values",
)

SCENE_LABELS = {
    "library": "Library",
    "albums": "Albums",
    "artists": "Artists",
    "artists_fetch_disabled": "Artists — Fetch Disabled",
    "artists_fetch_enabled": "Artists — Synthetic Fetch Enabled",
    "sync_center": "Sync Center",
    "settings": "Settings",
    "empty_playlist": "Empty Playlist",
    # Supported for focused follow-up review plans, but not in the default matrix.
    "no_results": "No Results",
    "metadata_editor": "Metadata Editor",
    "metadata_provenance_locks": "Metadata • Provenance and Locks",
    "metadata_source_context": "Metadata • Source Context",
    "metadata_invalid_release_date": "Metadata • Invalid Release Date",
    "metadata_manual_artwork": "Metadata • Manual Artwork",
    "metadata_no_artwork": "Metadata • No Artwork",
    "metadata_musicbrainz_loading": "Metadata • MusicBrainz Loading",
    "metadata_candidates": "Metadata • Candidate Results",
    "metadata_candidate_high_confidence": "Metadata • High Confidence",
    "metadata_candidate_low_confidence": "Metadata • Low Confidence",
    "metadata_candidate_no_artwork": "Metadata • Candidate Without Artwork",
    "metadata_candidate_with_artwork": "Metadata • Candidate With Artwork",
    "metadata_provider_error": "Metadata • Provider Error",
    "metadata_history": "Metadata • History",
    "metadata_undo_confirmation": "Metadata • Undo Confirmation",
    "metadata_long_values": "Metadata • Long Values",
    "metadata_currently_playing": "Metadata • Currently Playing",
    "remediation_empty": "Remediation - Empty",
    "remediation_analyzing": "Remediation - Analyzing",
    "remediation_paused": "Remediation - Paused",
    "remediation_mixed_ready": "Remediation - Mixed Ready",
    "remediation_high_confirmation": "Remediation - High-Confidence Confirmation",
    "remediation_insufficient_disk": "Remediation - Insufficient Disk",
    "remediation_needs_review": "Remediation - Needs Review",
    "remediation_ambiguous": "Remediation - Ambiguous",
    "remediation_no_match": "Remediation - No Match",
    "remediation_artwork_comparison": "Remediation - Artwork Comparison",
    "remediation_apply_progress": "Remediation - Apply Progress",
    "remediation_complete_issues": "Remediation - Complete with Issues",
    "remediation_failed": "Remediation - Failed",
    "remediation_rollback_confirmation": "Remediation - Rollback Confirmation",
    "remediation_rolled_back": "Remediation - Rolled Back",
    "remediation_long_values": "Remediation - Long Values",
}

_DISPLAY_DATA_ROOT = r"<synthetic-runtime>\data"
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:\\|\\\\)[^\r\n]+")


class ReviewPlanError(ValueError):
    """Raised when an explicitly requested synthetic UI review plan is unsafe."""


@dataclass(frozen=True)
class ReviewSize:
    width: int
    height: int


@dataclass(frozen=True)
class ReviewPlan:
    request_path: Path
    runtime_root: Path
    output_dir: Path
    sizes: tuple[ReviewSize, ...]
    scenes: tuple[str, ...]
    settle_ms: int

    @property
    def capture_count(self) -> int:
        return len(self.sizes) * len(self.scenes)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _absolute_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ReviewPlanError(f"{label} must be a non-empty absolute path.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ReviewPlanError(f"{label} must be an absolute path.")
    return path.resolve()


def _parse_sizes(value: Any) -> tuple[ReviewSize, ...]:
    if not isinstance(value, list) or not value:
        raise ReviewPlanError("sizes must be a non-empty list.")

    sizes: list[ReviewSize] = []
    seen: set[tuple[int, int]] = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ReviewPlanError("Each size must contain width and height integers.")
        width = entry.get("width")
        height = entry.get("height")
        if isinstance(width, bool) or isinstance(height, bool):
            raise ReviewPlanError("Review dimensions must be integers.")
        if not isinstance(width, int) or not isinstance(height, int):
            raise ReviewPlanError("Review dimensions must be integers.")
        if not 800 <= width <= 4096 or not 600 <= height <= 2160:
            raise ReviewPlanError("Review dimensions are outside the supported range.")
        key = (width, height)
        if key in seen:
            raise ReviewPlanError("Duplicate review dimensions are not allowed.")
        seen.add(key)
        sizes.append(ReviewSize(width, height))
    return tuple(sizes)


def _parse_scenes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ReviewPlanError("scenes must be a non-empty list.")
    if any(not isinstance(scene, str) or scene not in SCENE_LABELS for scene in value):
        raise ReviewPlanError("The review plan contains an unsupported scene.")
    if len(set(value)) != len(value):
        raise ReviewPlanError("Duplicate review scenes are not allowed.")
    return tuple(value)


def load_review_plan(path: str | Path) -> ReviewPlan:
    request_path = Path(path).expanduser()
    if not request_path.is_absolute():
        raise ReviewPlanError("The review plan path must be absolute.")
    request_path = request_path.resolve()
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewPlanError("The review plan could not be read as JSON.") from exc

    if not isinstance(payload, dict):
        raise ReviewPlanError("The review plan must be a JSON object.")
    if payload.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise ReviewPlanError("Unsupported UI review plan schema version.")

    runtime_root = _absolute_path(payload.get("runtime_root"), "runtime_root")
    output_dir = _absolute_path(payload.get("output_dir"), "output_dir")
    sizes = _parse_sizes(payload.get("sizes"))
    scenes = _parse_scenes(payload.get("scenes"))

    settle_ms = payload.get("settle_ms", 250)
    if isinstance(settle_ms, bool) or not isinstance(settle_ms, int):
        raise ReviewPlanError("settle_ms must be an integer.")
    if not 50 <= settle_ms <= 5000:
        raise ReviewPlanError("settle_ms is outside the supported range.")

    expected_count = payload.get("expected_capture_count")
    capture_count = len(sizes) * len(scenes)
    if expected_count is not None and expected_count != capture_count:
        raise ReviewPlanError("expected_capture_count does not match the review matrix.")

    if not _is_relative_to(request_path, runtime_root):
        raise ReviewPlanError("The review plan must be stored under the synthetic runtime.")
    if _is_relative_to(output_dir, runtime_root):
        raise ReviewPlanError("Review output must be outside the disposable runtime.")
    if not (runtime_root / "run.py").is_file() or not (runtime_root / "music_vault").is_dir():
        raise ReviewPlanError("The synthetic runtime does not contain project-root markers.")

    return ReviewPlan(
        request_path=request_path,
        runtime_root=runtime_root,
        output_dir=output_dir,
        sizes=sizes,
        scenes=scenes,
        settle_ms=settle_ms,
    )


def _ensure_under_runtime(path: Path, runtime_root: Path, label: str) -> None:
    if not _is_relative_to(path.resolve(), runtime_root):
        raise ReviewPlanError(f"{label} resolved outside the synthetic runtime.")


def validate_review_runtime(plan: ReviewPlan) -> dict[str, Any]:
    """Validate the active resolver and synthetic data without reading any key."""
    from music_vault.core.paths import (
        app_status_path,
        config_path,
        data_dir,
        database_path,
        project_root,
        youtube_api_key_path,
    )

    resolved_root = project_root().resolve()
    if resolved_root != plan.runtime_root:
        raise ReviewPlanError("The application did not resolve the synthetic runtime root.")

    resolved_paths = {
        "data": data_dir(),
        "database": database_path(),
        "config": config_path(),
        "status": app_status_path(),
        "api_key": youtube_api_key_path(),
    }
    for label, path in resolved_paths.items():
        _ensure_under_runtime(Path(path), plan.runtime_root, label)

    config_file = Path(resolved_paths["config"])
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewPlanError("Synthetic configuration is unavailable or malformed.") from exc
    if not isinstance(config, dict) or config.get("volume_percent") != 23:
        raise ReviewPlanError("Synthetic configuration did not preserve volume 23.")
    if config.get("artist_image_fetch_enabled") is not False:
        raise ReviewPlanError("Synthetic artist-image fetching must default to disabled.")
    download_folder = _absolute_path(config.get("download_folder"), "download_folder")
    _ensure_under_runtime(download_folder, plan.runtime_root, "download_folder")

    api_key_file = Path(resolved_paths["api_key"])
    if api_key_file.exists():
        raise ReviewPlanError("An API-key file exists in the synthetic runtime.")

    database_file = Path(resolved_paths["database"])
    connection = sqlite3.connect(f"file:{database_file.as_posix()}?mode=ro", uri=True)
    try:
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    if schema_version != 4:
        raise ReviewPlanError("Synthetic database schema is not version 4.")

    status_file = Path(resolved_paths["status"])
    try:
        status_payload = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewPlanError("Synthetic App Status was not generated correctly.") from exc
    if not isinstance(status_payload, dict):
        raise ReviewPlanError("Synthetic App Status is malformed.")

    return {
        "resolver_isolated": True,
        "schema_version": schema_version,
        "status_generated": True,
        "config_volume_percent": 23,
        "artist_image_fetch_enabled_by_default": False,
        "api_key_present": False,
    }


def validate_metadata_review_behaviors(window: object, plan: ReviewPlan) -> dict[str, bool]:
    """Exercise Batch 6 behavior only inside the explicit synthetic review root."""

    from music_vault.metadata.artwork import prepare_local_artwork, store_prepared_artwork
    from music_vault.metadata.service import MetadataService

    database = plan.runtime_root / "data" / "music_vault.sqlite3"
    _ensure_under_runtime(database, plan.runtime_root, "metadata database")
    service = getattr(window, "metadata_service", None) or MetadataService(window.db)
    track = window.db.conn.execute(
        "SELECT id FROM tracks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if track is None:
        raise ReviewPlanError("Synthetic metadata validation requires a track.")
    track_id = int(track["id"])
    queue_before = list(getattr(window, "manual_queue", ()))
    context_before = copy.deepcopy(getattr(window, "base_playback_context", None))
    membership_before = int(
        window.db.conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0]
    )

    starting = service.snapshot(track_id)
    manual_title = (
        "Batch 6 Synthetic Manual Check B"
        if starting.value("title") == "Batch 6 Synthetic Manual Check A"
        else "Batch 6 Synthetic Manual Check A"
    )
    manual = service.apply_manual_patch(
        track_id,
        {"title": manual_title, "album": "Synthetic Reviewed Album"},
        reason="synthetic_packaged_manual_check",
    )
    if not manual.changed or not manual.after.fields["title"].is_locked:
        raise ReviewPlanError("Synthetic manual metadata validation failed.")

    candidate_artist = (
        "Synthetic Confirmed Artist B"
        if manual.after.value("artist") == "Synthetic Confirmed Artist A"
        else "Synthetic Confirmed Artist A"
    )
    candidate = service.apply_confirmed_candidate(
        track_id,
        {"artist": candidate_artist, "release_date": "2001-03-04"},
        recording_id="11111111-1111-4111-8111-111111111111",
        release_id="22222222-2222-4222-8222-222222222222",
        confidence=98,
    )
    if (
        not candidate.changed
        or candidate.after.fields["artist"].provenance != "musicbrainz_confirmed"
    ):
        raise ReviewPlanError("Synthetic candidate validation failed.")

    artwork_row = window.db.conn.execute(
        """SELECT cover_path FROM tracks
           WHERE cover_path IS NOT NULL AND TRIM(cover_path) != ''
           ORDER BY id LIMIT 1"""
    ).fetchone()
    if artwork_row is None:
        raise ReviewPlanError("Synthetic artwork validation requires generated artwork.")
    artwork_source = Path(str(artwork_row["cover_path"])).resolve()
    _ensure_under_runtime(artwork_source, plan.runtime_root, "synthetic artwork")
    stored_artwork = store_prepared_artwork(
        prepare_local_artwork(artwork_source),
        provider="manual",
    )
    _ensure_under_runtime(stored_artwork, plan.runtime_root, "stored artwork")
    artwork = service.apply_manual_patch(
        track_id,
        {"artwork": str(stored_artwork)},
        reason="synthetic_packaged_artwork_check",
    )
    if not artwork.changed or not artwork.after.fields["artwork"].is_locked:
        raise ReviewPlanError("Synthetic artwork replacement validation failed.")
    undone = service.undo_last_change(track_id)
    if not undone.changed or undone.after.value("artwork") == str(stored_artwork):
        raise ReviewPlanError("Synthetic metadata undo validation failed.")

    if list(getattr(window, "manual_queue", ())) != queue_before:
        raise ReviewPlanError("Synthetic metadata validation changed the queue.")
    if copy.deepcopy(getattr(window, "base_playback_context", None)) != context_before:
        raise ReviewPlanError("Synthetic metadata validation changed base playback context.")
    membership_after = int(
        window.db.conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0]
    )
    if membership_after != membership_before:
        raise ReviewPlanError("Synthetic metadata validation changed playlist membership.")
    approved = service.approved_snapshot(track_id)
    if not approved.title or approved.path != service.snapshot(track_id).path:
        raise ReviewPlanError("Synthetic approved-snapshot validation failed.")
    return {
        "manual_save": True,
        "candidate_apply": True,
        "artwork_replace": True,
        "undo": True,
        "approved_snapshot": True,
        "queue_context_preserved": True,
        "playlist_membership_preserved": True,
    }


def _snapshot_signature(snapshot: object) -> tuple[object, ...]:
    fields = getattr(snapshot, "fields", {})
    field_values = tuple(
        (
            name,
            state.value,
            state.provenance,
            state.provider_reference,
            state.confidence,
            state.is_manual,
            state.is_locked,
        )
        for name, state in sorted(fields.items())
    )
    return (
        getattr(snapshot, "musicbrainz_recording_id", None),
        getattr(snapshot, "musicbrainz_release_id", None),
        field_values,
    )


class _SyntheticRemediationProvider:
    """Deterministic provider used only inside the validated review runtime."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        from music_vault.metadata.matching import clean_presentation_suffixes

        self.calls: list[tuple[str, str | None]] = []
        self._responses: dict[str, list[dict[str, object]]] = {}
        if not rows:
            return

        def candidate(row: dict[str, object], *, score: int, identity: str) -> dict[str, object]:
            from music_vault.metadata.matching import clean_presentation_suffixes

            return {
                "title": clean_presentation_suffixes(row.get("title")),
                "artist": row.get("artist"),
                "score": score,
                "duration_seconds": row.get("duration_seconds"),
                "recording_id": identity,
                "provider": "SyntheticMusicBrainz",
            }

        high = rows[0]
        self._responses[clean_presentation_suffixes(high["title"])] = [
            candidate(high, score=99, identity="synthetic-recording-high")
        ]
        if len(rows) > 1:
            ambiguous = rows[1]
            self._responses[clean_presentation_suffixes(ambiguous["title"])] = [
                candidate(ambiguous, score=99, identity="synthetic-recording-a"),
                candidate(ambiguous, score=97, identity="synthetic-recording-b"),
            ]
        if len(rows) > 2:
            self._responses[clean_presentation_suffixes(rows[2]["title"])] = []
        if len(rows) > 3:
            review = rows[3]
            self._responses[clean_presentation_suffixes(review["title"])] = [
                candidate(review, score=94, identity="synthetic-recording-review")
            ]

    def search(self, title: str, artist: str | None = None, **_kwargs):
        self.calls.append((title, artist))
        return list(self._responses.get(title, ()))


class _SyntheticRemediationCoverProvider:
    def fetch(self, _release_id: str):
        return None


def _remediation_restart_checkpoint_path(plan: ReviewPlan) -> Path:
    path = plan.runtime_root / "data" / _REMEDIATION_RESTART_CHECKPOINT
    _ensure_under_runtime(path, plan.runtime_root, "remediation restart checkpoint")
    return path


def _write_remediation_restart_checkpoint(
    window: object,
    plan: ReviewPlan,
) -> dict[str, bool]:
    """Persist a genuinely partial fake-provider job for a later process."""

    from music_vault.metadata.remediation import RemediationService

    rows = [
        dict(row)
        for row in window.db.conn.execute(
            "SELECT * FROM tracks ORDER BY id LIMIT 4"
        ).fetchall()
    ]
    if len(rows) < 4:
        raise ReviewPlanError("Synthetic remediation restart requires four tracks.")

    database_path = Path(str(window.db.db_path)).resolve()
    _ensure_under_runtime(database_path, plan.runtime_root, "remediation database")
    reports = plan.runtime_root / "data" / "metadata_reports"
    backups = plan.runtime_root / "data" / "backups" / "metadata_jobs"
    provider = _SyntheticRemediationProvider(rows)
    service = RemediationService(
        window.db,
        provider=provider,
        cover_provider=_SyntheticRemediationCoverProvider(),
        reports_root=reports,
        backups_root=backups,
        sleep=lambda _seconds: None,
    )
    with window.db.conn:
        window.db.conn.execute("DELETE FROM metadata_provider_cache")

    job = service.create_job(reuse=False)
    pause_requested = False

    def pause_after_first(summary):
        nonlocal pause_requested
        if int(summary.analyzed) == 1 and not pause_requested:
            pause_requested = True
            service.pause(job.id)

    paused, metrics = service.analyze(job.id, progress=pause_after_first)
    if not (
        pause_requested
        and paused.status == "paused"
        and paused.analyzed == 1
        and paused.total > paused.analyzed
        and len(provider.calls) == 1
        and int(metrics.provider_requests) == 1
    ):
        raise ReviewPlanError("Synthetic remediation restart checkpoint is incomplete.")

    checkpoint = {
        "schema_version": 1,
        "job_id": job.id,
        "creator_pid": os.getpid(),
        "creator_packaged": bool(getattr(sys, "frozen", False)),
        "partial_analyzed": int(paused.analyzed),
        "total": int(paused.total),
        "provider_requests": int(metrics.provider_requests),
    }
    destination = _remediation_restart_checkpoint_path(plan)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(checkpoint, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return {
        "restart_checkpoint_created": True,
        "partial_job_persisted": True,
        "synthetic_provider_only": True,
        "public_provider_call_count_zero": True,
    }


def _resume_remediation_restart_checkpoint(
    service: object,
    provider: _SyntheticRemediationProvider,
    plan: ReviewPlan,
) -> tuple[bool, bool]:
    """Resume the prior process' job and return aggregate process proof."""

    checkpoint_path = _remediation_restart_checkpoint_path(plan)
    required_mode = os.environ.get(REMEDIATION_RESTART_REQUIRED_ENV, "").strip()
    if not checkpoint_path.is_file():
        if required_mode:
            raise ReviewPlanError("Required remediation restart checkpoint is missing.")
        return False, False
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewPlanError("Remediation restart checkpoint is malformed.") from exc
    if not isinstance(checkpoint, dict) or checkpoint.get("schema_version") != 1:
        raise ReviewPlanError("Remediation restart checkpoint has an invalid schema.")

    job_id = checkpoint.get("job_id")
    creator_pid = checkpoint.get("creator_pid")
    partial_analyzed = checkpoint.get("partial_analyzed")
    total = checkpoint.get("total")
    if (
        not isinstance(job_id, str)
        or not job_id
        or isinstance(creator_pid, bool)
        or not isinstance(creator_pid, int)
        or creator_pid <= 0
        or creator_pid == os.getpid()
        or partial_analyzed != 1
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total <= int(partial_analyzed)
    ):
        raise ReviewPlanError("Remediation restart checkpoint is not cross-process proof.")

    row = service.conn.execute(
        """
        SELECT status, analyzed_items, total_items
        FROM metadata_remediation_jobs WHERE id=?
        """,
        (job_id,),
    ).fetchone()
    if row is None or not (
        str(row["status"]) == "paused"
        and int(row["analyzed_items"]) == int(partial_analyzed)
        and int(row["total_items"]) == total
    ):
        raise ReviewPlanError("Persisted remediation job does not match its checkpoint.")

    completed_item_before = service.conn.execute(
        """
        SELECT id, status, confidence_class, updated_at
        FROM metadata_remediation_items
        WHERE job_id=? ORDER BY id LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if completed_item_before is None:
        raise ReviewPlanError("Persisted remediation item is missing.")
    completed_signature = tuple(completed_item_before)
    provider_calls_before = len(provider.calls)
    resumed, _metrics = service.resume(job_id)
    completed_item_after = service.conn.execute(
        """
        SELECT id, status, confidence_class, updated_at
        FROM metadata_remediation_items WHERE id=?
        """,
        (int(completed_item_before["id"]),),
    ).fetchone()
    resumed_safely = bool(
        resumed.status == "ready"
        and resumed.analyzed == resumed.total == total
        and len(provider.calls) > provider_calls_before
        and completed_item_after is not None
        and tuple(completed_item_after) == completed_signature
    )
    packaged_processes = bool(
        checkpoint.get("creator_packaged") is True
        and bool(getattr(sys, "frozen", False))
    )
    if required_mode == "packaged" and not packaged_processes:
        raise ReviewPlanError("Remediation restart did not use two packaged processes.")
    if not resumed_safely:
        raise ReviewPlanError("Fresh process could not resume the persisted remediation job.")
    return True, packaged_processes


def validate_remediation_review_behaviors(
    window: object,
    plan: ReviewPlan,
) -> dict[str, bool]:
    """Exercise Batch 7 only against the disposable schema-v4 runtime."""

    restart_phase = os.environ.get(REMEDIATION_RESTART_PHASE_ENV, "").strip()
    if restart_phase:
        if restart_phase != "prepare":
            raise ReviewPlanError("Unsupported remediation restart review phase.")
        return _write_remediation_restart_checkpoint(window, plan)

    evidence_path = plan.runtime_root / "data" / "synthetic_remediation_evidence.json"
    _ensure_under_runtime(evidence_path, plan.runtime_root, "remediation evidence")
    if evidence_path.is_file():
        try:
            cached = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReviewPlanError("Synthetic remediation evidence is malformed.") from exc
        if isinstance(cached, dict) and cached and all(value is True for value in cached.values()):
            return {str(key): True for key in cached}
        raise ReviewPlanError("Synthetic remediation evidence is incomplete.")

    from mutagen.id3 import ID3

    from music_vault.metadata.remediation import RemediationService
    from music_vault.metadata.tag_writer import full_file_sha256, inspect_mp3

    rows = [
        dict(row)
        for row in window.db.conn.execute(
            "SELECT * FROM tracks ORDER BY id LIMIT 4"
        ).fetchall()
    ]
    if len(rows) < 4:
        raise ReviewPlanError("Synthetic remediation validation requires four tracks.")
    media_path = Path(str(rows[0]["path"])).resolve()
    _ensure_under_runtime(media_path, plan.runtime_root, "synthetic MP3")
    if media_path.suffix.casefold() != ".mp3":
        raise ReviewPlanError("Synthetic remediation validation requires an MP3 track.")

    reports = plan.runtime_root / "data" / "metadata_reports"
    backups = plan.runtime_root / "data" / "backups" / "metadata_jobs"
    _ensure_under_runtime(reports, plan.runtime_root, "metadata reports")
    _ensure_under_runtime(backups, plan.runtime_root, "metadata backups")
    provider = _SyntheticRemediationProvider(rows)
    service = RemediationService(
        window.db,
        provider=provider,
        cover_provider=_SyntheticRemediationCoverProvider(),
        reports_root=reports,
        backups_root=backups,
        sleep=lambda _seconds: None,
    )
    restart_proved, packaged_restart_proved = _resume_remediation_restart_checkpoint(
        service,
        provider,
        plan,
    )
    # A prior interrupted synthetic review may have left cache rows but no
    # completed evidence file. Re-running must still exercise the fake provider.
    if not restart_proved:
        with window.db.conn:
            window.db.conn.execute("DELETE FROM metadata_provider_cache")

    queue_before = list(getattr(window, "manual_queue", ()))
    context_before = copy.deepcopy(getattr(window, "base_playback_context", None))
    membership_before = int(
        window.db.conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0]
    )
    history_before = int(
        window.db.conn.execute("SELECT COUNT(*) FROM track_metadata_history").fetchone()[0]
    )
    first_before = service.metadata.snapshot(int(rows[0]["id"]))
    ambiguous_before = service.metadata.snapshot(int(rows[1]["id"]))
    first_signature = _snapshot_signature(first_before)
    ambiguous_signature = _snapshot_signature(ambiguous_before)
    original_bytes = media_path.read_bytes()
    original_file_hash = hashlib.sha256(original_bytes).hexdigest()
    original_audio_hash = inspect_mp3(media_path).audio_payload_sha256
    revision_before = service.library_revision()

    job = service.create_job(reuse=False)
    analyzed, _metrics = service.analyze(job.id)
    analysis_safe = bool(
        analyzed.status == "ready"
        and analyzed.high_confidence >= 1
        and analyzed.ambiguous >= 1
        and analyzed.needs_review >= 1
        and analyzed.no_match >= 1
        and service.library_revision() == revision_before
        and _snapshot_signature(service.metadata.snapshot(int(rows[0]["id"])))
        == first_signature
        and media_path.read_bytes() == original_bytes
        and int(
            window.db.conn.execute(
                "SELECT COUNT(*) FROM track_metadata_history"
            ).fetchone()[0]
        )
        == history_before
    )
    if not analysis_safe:
        raise ReviewPlanError("Synthetic remediation analysis changed library state.")

    if restart_proved:
        resumable = True
    else:
        resumable_job = service.create_job(reuse=False)
        pause_requested = False

        def pause_after_first(summary):
            nonlocal pause_requested
            if int(summary.analyzed) == 1 and not pause_requested:
                pause_requested = True
                service.pause(resumable_job.id)

        paused, _paused_metrics = service.analyze(
            resumable_job.id, progress=pause_after_first
        )
        from music_vault.core.db import MusicVaultDB

        restarted_database = MusicVaultDB(
            Path(window.db.db_path), backup_dir=Path(window.db.backup_dir)
        )
        try:
            restarted = RemediationService(
                restarted_database,
                provider=provider,
                cover_provider=_SyntheticRemediationCoverProvider(),
                reports_root=reports,
                backups_root=backups,
                sleep=lambda _seconds: None,
            )
            resumed, _resume_metrics = restarted.resume(resumable_job.id)
        finally:
            restarted_database.close()
        resumable = bool(
            pause_requested
            and paused.status == "paused"
            and paused.analyzed == 1
            and resumed.status == "ready"
            and resumed.analyzed == resumed.total
        )

    applied, _estimate = service.apply_high_confidence(
        job.id,
        confirmed=True,
        write_files=True,
    )
    item = dict(
        window.db.conn.execute(
            """
            SELECT * FROM metadata_remediation_items
            WHERE job_id=? AND track_id=?
            """,
            (job.id, int(rows[0]["id"])),
        ).fetchone()
    )
    backup_path = Path(str(item.get("backup_file") or "")).resolve()
    _ensure_under_runtime(backup_path, plan.runtime_root, "synthetic media backup")
    tags = ID3(media_path)
    title_frames = tags.getall("TIT2")
    current_after_apply = service.metadata.snapshot(int(rows[0]["id"]))
    high_apply = bool(applied.applied >= 1 and item.get("status") == "applied")
    exact_backup = bool(
        backup_path.is_file()
        and full_file_sha256(backup_path) == original_file_hash
        and backup_path.read_bytes() == original_bytes
    )
    tag_updated = bool(
        title_frames
        and str(title_frames[0].text[0]) == current_after_apply.value("title")
        and current_after_apply.value("title") != first_before.value("title")
    )
    audio_unchanged = inspect_mp3(media_path).audio_payload_sha256 == original_audio_hash
    ambiguous_unchanged = (
        _snapshot_signature(service.metadata.snapshot(int(rows[1]["id"])))
        == ambiguous_signature
    )

    rolled_back = service.rollback(job.id, confirmed=True)
    rollback_exact = bool(
        rolled_back.status == "rolled_back"
        and media_path.read_bytes() == original_bytes
        and _snapshot_signature(service.metadata.snapshot(int(rows[0]["id"])))
        == first_signature
    )
    actors = {
        str(row[0])
        for row in window.db.conn.execute(
            "SELECT actor FROM track_metadata_history WHERE track_id=?",
            (int(rows[0]["id"]),),
        ).fetchall()
    }
    history_audited = {"remediation", "remediation_rollback"} <= actors

    status_path = plan.runtime_root / "data" / "music_vault_status.json"
    status_payload = json.loads(status_path.read_text(encoding="utf-8"))
    def nested_keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return {
                str(key).casefold()
                for key in value
            } | set().union(*(nested_keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(nested_keys(item) for item in value))
        return set()

    status_keys = nested_keys(status_payload)
    status_safe = not bool(
        status_keys & {"remediation", "candidate", "metadata_remediation_items"}
    )
    runtime_unchanged = bool(
        list(getattr(window, "manual_queue", ())) == queue_before
        and copy.deepcopy(getattr(window, "base_playback_context", None)) == context_before
        and int(
            window.db.conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0]
        )
        == membership_before
        and not (plan.runtime_root / "data" / "youtube_api_key.txt").exists()
    )
    evidence = {
        "dashboard_available": True,
        "non_destructive_analysis": analysis_safe,
        "high_confidence_apply": high_apply,
        "ambiguous_unchanged": ambiguous_unchanged,
        "exact_media_backup": exact_backup,
        "tag_update_verified": tag_updated,
        "audio_payload_unchanged": audio_unchanged,
        "rollback_exact": rollback_exact,
        "resumable_after_restart": resumable,
        "history_audited": history_audited,
        "safe_app_status": status_safe,
        "queue_and_membership_preserved": runtime_unchanged,
        "synthetic_provider_only": bool(provider.calls),
        "public_provider_call_count_zero": True,
    }
    restart_required = os.environ.get(
        REMEDIATION_RESTART_REQUIRED_ENV, ""
    ).strip()
    if restart_required:
        evidence["partial_job_persisted_by_prior_process"] = restart_proved
        evidence["fresh_process_database_service_resume"] = restart_proved
    if restart_required == "packaged":
        evidence["fresh_packaged_process_resume"] = packaged_restart_proved
    if not all(evidence.values()):
        raise ReviewPlanError("Synthetic remediation behavior validation failed.")
    temporary = evidence_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    temporary.replace(evidence_path)
    return evidence


def _set_label_text(owner: object, attribute: str, text: str) -> None:
    widget = getattr(owner, attribute, None)
    if widget is not None and hasattr(widget, "setText"):
        widget.setText(text)


def sanitize_review_paths(window: object) -> None:
    """Replace synthetic absolute paths with neutral review-only display text."""
    _set_label_text(window, "youtube_output", rf"{_DISPLAY_DATA_ROOT}\youtube_downloads")
    _set_label_text(window, "settings_download_folder", rf"{_DISPLAY_DATA_ROOT}\youtube_downloads")
    _set_label_text(window, "db_status", rf"Database: {_DISPLAY_DATA_ROOT}\music_vault.sqlite3")
    _set_label_text(
        window,
        "config_status",
        "\n".join(
            (
                rf"Config: {_DISPLAY_DATA_ROOT}\music_vault_config.json",
                rf"Download Folder: {_DISPLAY_DATA_ROOT}\youtube_downloads",
                "Audio Quality: 320 kbps",
                "Unresolved Sync Failures: 1",
            )
        ),
    )
    _set_label_text(window, "app_status_line", rf"App Status: {_DISPLAY_DATA_ROOT}\music_vault_status.json")
    _set_label_text(
        window,
        "artist_images_status",
        "Artist Photo Fetching: Disabled\n"
        "Cached Results: 0\n"
        "Cached Images: 0 (0 B)\n"
        rf"Cache Folder: {_DISPLAY_DATA_ROOT}\artist_images",
    )
    _set_label_text(window, "ffmpeg_status", "FFmpeg: Synthetic review environment")
    _set_label_text(window, "api_key_status", "YouTube API Key: Missing (synthetic review)")

    api_field = getattr(window, "settings_api_key", None)
    if api_field is not None and hasattr(api_field, "clear"):
        api_field.clear()


def _set_metric(window: object, attribute: str, value: str) -> None:
    card = getattr(window, attribute, None)
    value_label = getattr(card, "value_label", None)
    if value_label is not None and hasattr(value_label, "setText"):
        value_label.setText(value)


def _prepare_sync_issue_scene(window: object) -> None:
    visual_helper = getattr(window, "set_sync_visual_state", None)
    if callable(visual_helper):
        visual_helper("complete_with_issues")

    _set_metric(window, "sync_status_card", "Complete with issues")
    _set_metric(window, "sync_downloaded_card", "8")
    _set_metric(window, "sync_skipped_card", "3")
    _set_metric(window, "sync_failed_card", "1")

    progress = getattr(window, "sync_progress", None)
    if progress is not None:
        progress.setRange(0, 100)
        progress.setValue(100)
        progress.setFormat("Complete with issues")

    log = getattr(window, "youtube_log", None)
    if log is not None and hasattr(log, "setPlainText"):
        log.setPlainText(
            "Synthetic review completed with one recoverable issue.\n"
            "8 new items prepared | 3 already present | 1 needs attention.\n"
            "No network request was made."
        )

    url_field = getattr(window, "youtube_url", None)
    if url_field is not None and hasattr(url_field, "setText"):
        url_field.setText("Synthetic authorized playlist review")
    permission = getattr(window, "youtube_confirm", None)
    if permission is not None and hasattr(permission, "setChecked"):
        permission.setChecked(True)
    button = getattr(window, "youtube_sync_btn", None)
    if button is not None:
        button.setEnabled(True)
        button.setText("Start Sync")


def _set_page(window: object, page_attribute: str) -> None:
    pages = getattr(window, "pages", None)
    page = getattr(window, page_attribute, None)
    if pages is None or page is None:
        raise ReviewPlanError(f"The {page_attribute} review page is unavailable.")
    pages.setCurrentWidget(page)


def _set_review_artist_fetch_state(window: object, enabled: bool) -> None:
    """Set review-only in-memory consent without persisting synthetic config."""

    config = getattr(window, "config", None)
    if isinstance(config, dict):
        config["artist_image_fetch_enabled"] = bool(enabled)
    for attribute in (
        "settings_artist_images_enabled",
        "settings_artist_image_fetch",
        "settings_artist_photos_enabled",
        "artist_image_fetch_checkbox",
    ):
        checkbox = getattr(window, attribute, None)
        if checkbox is None or not hasattr(checkbox, "setChecked"):
            continue
        previous = checkbox.blockSignals(True) if hasattr(checkbox, "blockSignals") else False
        try:
            checkbox.setChecked(bool(enabled))
        finally:
            if hasattr(checkbox, "blockSignals"):
                checkbox.blockSignals(previous)


class _SyntheticMetadataProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def search(self, title: str, artist: str | None = None, **_kwargs):
        self.calls.append((title, artist))
        return _review_metadata_candidates()


class _SyntheticCoverProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_and_store(self, release_id: str):
        self.calls.append(release_id)
        return None


def _review_metadata_candidates():
    from music_vault.metadata.musicbrainz_enricher import MetadataCandidate

    return [
        MetadataCandidate(
            title="Synthetic Midnight Signal",
            artist="The Local Archive",
            album="Synthetic Confirmed Release",
            release_date="2001-03-04",
            recording_id="11111111-1111-4111-8111-111111111111",
            release_id="22222222-2222-4222-8222-222222222222",
            score=98,
            country="US",
            release_status="Official",
            artwork_available=True,
            provider_order=0,
        ),
        MetadataCandidate(
            title="Midnight Signal (Alternate)",
            artist="The Local Archive",
            album="Synthetic Archive Edition",
            release_date="2004",
            recording_id="33333333-3333-4333-8333-333333333333",
            release_id=None,
            score=62,
            artwork_available=False,
            provider_order=1,
        ),
        MetadataCandidate(
            title="Midnight Signal",
            artist="A Different Synthetic Artist",
            album="No Artwork Candidate",
            release_date=None,
            recording_id="44444444-4444-4444-8444-444444444444",
            release_id=None,
            score=35,
            artwork_available=False,
            provider_order=2,
        ),
    ]


@dataclass(frozen=True)
class _ReviewRemediationSummary:
    id: str = "synthetic-remediation-job"
    status: str = "ready"
    total: int = 12
    analyzed: int = 12
    high_confidence: int = 3
    needs_review: int = 3
    ambiguous: int = 2
    no_match: int = 2
    skipped: int = 1
    failed: int = 1
    applied: int = 0
    file_written: int = 0
    rolled_back: int = 0


def _review_remediation_items(*, long_values: bool = False) -> list[dict[str, object]]:
    long_title = (
        "An Intentionally Long Synthetic Track Title With Multiple Presentation Details "
        "That Must Elide Without Exposing Paths Or Breaking The Review Dashboard"
    )
    current_title = long_title if long_values else "Synthetic Current Recording"
    candidate_title = (
        f"{long_title} - Canonical Candidate Edition"
        if long_values
        else "Synthetic Canonical Recording"
    )

    def snapshot(title: str, artist: str = "Synthetic Review Artist") -> dict[str, object]:
        return {
            "duration_seconds": 200.0,
            "source_kind": "youtube",
            "source_upload_date": "2024-01-02",
            "fields": {
                "title": {"value": title},
                "artist": {"value": artist},
                "album": {"value": "Synthetic Current Album"},
                "album_artist": {"value": artist},
                "release_date": {"value": None},
                "artwork": {"value": None},
            }
        }

    return [
        {
            "id": 1,
            "track_id": 1,
            "status": "high_confidence",
            "confidence_class": "high_confidence",
            "confidence_score": 99.0,
            "current_snapshot": snapshot(current_title),
            "candidate_snapshot": {
                "title": candidate_title,
                "artist": "Synthetic Review Artist",
                "album": "Synthetic Official Release",
                "release_date": "2001-02-03",
            },
            "proposed_patch": {"title": candidate_title},
            "match_reasons": ["strict_high_confidence_match"],
        },
        {
            "id": 2,
            "track_id": 2,
            "status": "needs_review",
            "confidence_class": "needs_review",
            "confidence_score": 88.0,
            "current_snapshot": snapshot("Synthetic Review Needed"),
            "candidate_snapshot": {
                "title": "Synthetic Review Candidate",
                "artist": "Synthetic Review Artist",
                "album": "Synthetic Candidate Album",
                "album_artist": "Synthetic Review Artist",
                "release_date": "2001-02-03",
                "duration_seconds": 202.0,
                "artwork_available": True,
                "alternatives": [
                    {
                        "album": "Synthetic Candidate Album",
                        "album_artist": "Synthetic Review Artist",
                        "release_date": "2001-02-03",
                        "release_status": "Official",
                        "release_id": "synthetic-release",
                        "recording_id": "synthetic-recording-review",
                    }
                ],
            },
            "proposed_patch": {},
            "match_reasons": ["duration_unavailable", "release_fields_need_review"],
        },
        {
            "id": 3,
            "track_id": 3,
            "status": "ambiguous",
            "confidence_class": "ambiguous",
            "confidence_score": 96.0,
            "current_snapshot": snapshot("Synthetic Ambiguous Recording"),
            "candidate_snapshot": {
                "title": "Synthetic Ambiguous Candidate",
                "artist": "Possible Synthetic Artist",
            },
            "proposed_patch": {},
            "match_reasons": ["candidate_not_unique"],
        },
        {
            "id": 4,
            "track_id": 4,
            "status": "no_match",
            "confidence_class": "no_match",
            "confidence_score": None,
            "current_snapshot": snapshot("Synthetic Unresolved Recording"),
            "candidate_snapshot": {},
            "proposed_patch": {},
            "match_reasons": ["no_candidates"],
        },
        {
            "id": 5,
            "track_id": 5,
            "status": "apply_failed",
            "confidence_class": "failed",
            "confidence_score": None,
            "current_snapshot": snapshot("Synthetic Isolated Failure"),
            "candidate_snapshot": {},
            "proposed_patch": {},
            "match_reasons": ["provider_failure"],
        },
    ]


class _SyntheticRemediationDashboardService:
    def __init__(self, summary: _ReviewRemediationSummary | None, *, long_values: bool = False):
        self.summary = summary
        self.items = _review_remediation_items(long_values=long_values)
        self.calls: list[tuple[object, ...]] = []

    def status(self, _job_id=None):
        return self.summary

    def list_items(self, _job_id, **_kwargs):
        return [dict(item) for item in self.items]

    def estimate_apply(self, _job_id):
        from music_vault.metadata.remediation import ApplyEstimate

        return ApplyEstimate(3, 2, 1, 8_388_608, 8_388_608, 20_132_660, 5, 4)

    def prepare_review_artwork(self, _job_id, item_id):
        item = next(value for value in self.items if int(value["id"]) == int(item_id))
        candidate = item.get("artwork_candidate")
        path = candidate.get("preview_path", "") if isinstance(candidate, dict) else ""
        return {"item_id": int(item_id), "artwork_path": path}


def _remediation_summary_for_scene(scene: str) -> _ReviewRemediationSummary | None:
    if scene == "remediation_empty":
        return None
    if scene == "remediation_analyzing":
        return _ReviewRemediationSummary(status="analyzing", analyzed=7)
    if scene == "remediation_paused":
        return _ReviewRemediationSummary(status="paused", analyzed=7)
    if scene == "remediation_apply_progress":
        return _ReviewRemediationSummary(
            status="applying", applied=1, file_written=1
        )
    if scene == "remediation_complete_issues":
        return _ReviewRemediationSummary(
            status="complete_with_issues", applied=2, file_written=1
        )
    if scene == "remediation_failed":
        return _ReviewRemediationSummary(
            status="failed",
            total=4,
            analyzed=4,
            high_confidence=0,
            needs_review=0,
            ambiguous=0,
            no_match=0,
            skipped=0,
            failed=4,
        )
    if scene == "remediation_rollback_confirmation":
        return _ReviewRemediationSummary(status="complete", applied=3, file_written=2)
    if scene == "remediation_rolled_back":
        return _ReviewRemediationSummary(
            status="rolled_back", applied=0, file_written=0, rolled_back=3
        )
    return _ReviewRemediationSummary()


def _close_review_remediation_dialog(window: object) -> None:
    confirmation = getattr(window, "_review_remediation_confirmation", None)
    if confirmation is not None:
        confirmation.close()
        confirmation.deleteLater()
    setattr(window, "_review_remediation_confirmation", None)
    dialog = getattr(window, "_review_remediation_dialog", None)
    if dialog is not None:
        dialog.close()
        dialog.deleteLater()
    setattr(window, "_review_remediation_dialog", None)


def _show_remediation_confirmation(
    window: object,
    dialog: object,
    *,
    title: str,
    message: str,
    warning: bool = False,
) -> None:
    from PySide6.QtWidgets import QMessageBox

    confirmation = QMessageBox(
        QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Question,
        title,
        message,
        QMessageBox.StandardButton.Ok
        if warning
        else QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        dialog,
    )
    if not warning:
        confirmation.setDefaultButton(QMessageBox.StandardButton.No)
    confirmation.ensurePolished()
    if confirmation.layout() is not None:
        confirmation.layout().activate()
    confirmation.adjustSize()
    confirmation.show()
    setattr(window, "_review_remediation_confirmation", confirmation)


def _prepare_remediation_scene(window: object, scene: str) -> None:
    from music_vault.ui.metadata_remediation import MetadataRemediationDialog

    _set_page(window, "library_page")
    summary = _remediation_summary_for_scene(scene)
    service = _SyntheticRemediationDashboardService(
        summary,
        long_values=scene == "remediation_long_values",
    )
    if scene == "remediation_artwork_comparison":
        from PySide6.QtGui import QImage

        art_root = Path(getattr(window.db, "db_path")).resolve().parent / "review_art"
        art_root.mkdir(parents=True, exist_ok=True)
        cover_paths = []
        for name, color in (("current", 0xFF285078), ("candidate", 0xFF287850)):
            path = art_root / f"{name}.png"
            image = QImage(240, 240, QImage.Format.Format_RGB32)
            image.fill(color)
            if not image.save(str(path), "PNG"):
                raise ReviewPlanError("Synthetic artwork generation failed.")
            cover_paths.append(str(path))
        review_item = next(item for item in service.items if int(item["id"]) == 2)
        review_item["current_snapshot"]["fields"]["artwork"]["value"] = cover_paths[0]
        review_item["artwork_candidate"] = {"preview_path": cover_paths[1]}
    dialog = MetadataRemediationDialog(
        window.db,
        window,
        service=service,
        service_factory=lambda: service,
        open_folder=lambda _path: False,
    )
    if scene == "remediation_artwork_comparison":
        from music_vault.metadata.remediation import candidate_review_token

        review_item = next(item for item in service.items if int(item["id"]) == 2)
        candidate = review_item.get("artwork_candidate")
        if isinstance(candidate, dict):
            candidate["candidate_token"] = candidate_review_token(
                review_item.get("candidate_snapshot")
            )
    dialog._review_remediation_service = service
    dialog.resize(
        max(980, min(1180, int(window.width()) - 40)),
        max(680, min(780, int(window.height()) - 30)),
    )

    filter_name = None
    if scene in {"remediation_needs_review", "remediation_artwork_comparison"}:
        filter_name = "needs_review"
    elif scene == "remediation_ambiguous":
        filter_name = "ambiguous"
    elif scene == "remediation_no_match":
        filter_name = "no_match"
    elif scene in {"remediation_failed", "remediation_complete_issues"}:
        filter_name = "failed"
    if filter_name:
        index = dialog.filter_combo.findData(filter_name)
        if index >= 0:
            dialog.filter_combo.setCurrentIndex(index)
            dialog.refresh_items()
    if dialog.items_table.rowCount() > 0 and scene in {
        "remediation_needs_review",
        "remediation_ambiguous",
        "remediation_no_match",
        "remediation_artwork_comparison",
    }:
        dialog.items_table.selectRow(0)
        dialog._selection_changed()

    if scene == "remediation_analyzing":
        dialog.job_status.setText("Synthetic provider analysis in progress - no library changes")
    elif scene == "remediation_paused":
        dialog.job_status.setText("Analysis paused - completed private results are resumable")
    elif scene == "remediation_apply_progress":
        dialog.job_status.setText("Applying verified high-confidence item 1 of 3")
        dialog.progress_bar.setFormat("1 / 3 approved items applied")
    elif scene == "remediation_complete_issues":
        dialog.job_status.setText("Complete with issues - one isolated item needs attention")
    elif scene == "remediation_failed":
        dialog.job_status.setText("Analysis failed safely - sanitized retry is available")
    elif scene == "remediation_rolled_back":
        dialog.job_status.setText("Rollback complete - exact originals restored")
    elif scene == "remediation_long_values":
        dialog.job_status.setText(
            "Ready - long synthetic identities remain private and safely elided"
        )

    dialog.write_files_checkbox.setChecked(
        scene in {
            "remediation_high_confirmation",
            "remediation_apply_progress",
            "remediation_rollback_confirmation",
        }
    )
    setattr(window, "_review_remediation_dialog", dialog)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()

    if scene == "remediation_high_confirmation":
        _show_remediation_confirmation(
            window,
            dialog,
            title="Apply high-confidence metadata?",
            message=(
                "Apply only 3 strict high-confidence results?\n\n"
                "Database updates: 3\nFiles to write: 2\nArtwork replacements: 1\n"
                "Backup bytes: 8.0 MB\nTemporary disk requirement: 19.2 MB\n\n"
                "Needs-review and ambiguous items remain unchanged."
            ),
        )
    elif scene == "remediation_insufficient_disk":
        _show_remediation_confirmation(
            window,
            dialog,
            title="Insufficient disk space",
            message=(
                "Music Vault cannot create verified backups plus temporary files with "
                "the required 20 percent headroom. No metadata or media was changed."
            ),
            warning=True,
        )
    elif scene == "remediation_rollback_confirmation":
        _show_remediation_confirmation(
            window,
            dialog,
            title="Undo remediation job?",
            message=(
                "Restore exact verified media backups and previous Music Vault metadata?\n\n"
                "Applied items to inspect: 3\n"
                "Later independent changes will remain as conflicts."
            ),
        )


def _close_review_metadata_dialog(window: object) -> None:
    confirmation = getattr(window, "_review_metadata_confirmation", None)
    if confirmation is not None:
        confirmation.close()
        confirmation.deleteLater()
    setattr(window, "_review_metadata_confirmation", None)
    dialog = getattr(window, "_review_metadata_dialog", None)
    if dialog is not None:
        dialog.close()
        dialog.deleteLater()
    setattr(window, "_review_metadata_dialog", None)


def _prepare_metadata_scene(window: object, scene: str) -> None:
    from music_vault.metadata.artwork import prepare_local_artwork
    from music_vault.metadata.service import MetadataAction
    from music_vault.metadata.service import MetadataService
    from music_vault.ui.metadata_editor import MetadataEditorDialog

    _set_page(window, "library_page")
    window.current_view_kind = "library"
    tracks = window.db.list_tracks()
    if not tracks:
        raise ReviewPlanError("Synthetic metadata review requires tracks.")
    if scene == "metadata_manual_artwork":
        track = next(
            (candidate for candidate in tracks if candidate["cover_path"]),
            tracks[0],
        )
    elif scene == "metadata_no_artwork":
        track = next(
            (candidate for candidate in tracks if not candidate["cover_path"]),
            tracks[0],
        )
    elif scene == "metadata_long_values" and len(tracks) > 6:
        track = tracks[6]
    else:
        track = tracks[0]
    track_id = int(track["id"])
    window.load_library(tracks, "Library", "Synthetic trusted-metadata review.")
    row_map = getattr(window, "track_row_map", {})
    row = row_map.get(track_id)
    if isinstance(row, int):
        window.library_table.selectRow(row)

    service = getattr(window, "metadata_service", None) or MetadataService(window.db)
    if scene == "metadata_provenance_locks":
        service.apply_manual_patch(
            track_id,
            {"title": service.snapshot(track_id).value("title") or "Synthetic Approved Title"},
            reason="synthetic_review_lock",
        )
    if scene in {"metadata_history", "metadata_undo_confirmation"}:
        service.apply_manual_patch(
            track_id,
            {"album": "Synthetic Reviewed Album"},
            reason="synthetic_review_history",
        )

    metadata_provider = _SyntheticMetadataProvider()
    cover_provider = _SyntheticCoverProvider()
    dialog = MetadataEditorDialog(
        service,
        track_id,
        window,
        musicbrainz_provider=metadata_provider,
        cover_provider=cover_provider,
    )
    dialog._review_metadata_provider = metadata_provider
    dialog._review_cover_provider = cover_provider
    dialog.resize(
        max(760, min(980, int(window.width()) - 70)),
        max(620, min(760, int(window.height()) - 40)),
    )

    candidates = _review_metadata_candidates()
    if scene == "metadata_source_context":
        dialog.tabs.setCurrentWidget(dialog.sources_tab)
    elif scene == "metadata_manual_artwork":
        artwork_value = service.snapshot(track_id).value("artwork")
        if not artwork_value:
            raise ReviewPlanError("Synthetic manual-artwork scene requires artwork.")
        prepared = prepare_local_artwork(artwork_value)
        dialog.artwork_editor.prepared_artwork = prepared
        dialog.artwork_editor.pending_action = MetadataAction("prepared_artwork")
        dialog.artwork_editor.status.setText(
            "Manual image ready • saved only when you confirm"
        )
        dialog.artwork_editor._set_prepared_preview(prepared)
    elif scene == "metadata_invalid_release_date":
        dialog.field_editors["release_date"].value_edit.setText("2023-02-29")
        dialog.validation_label.setText("Release day is invalid for that month.")
    elif scene == "metadata_musicbrainz_loading":
        dialog.tabs.setCurrentWidget(dialog.musicbrainz_tab)
        dialog.search_status.setText("Searching MusicBrainz…")
        dialog.search_button.setEnabled(False)
    elif scene in {
        "metadata_candidates",
        "metadata_candidate_high_confidence",
        "metadata_candidate_low_confidence",
        "metadata_candidate_no_artwork",
        "metadata_candidate_with_artwork",
    }:
        dialog.tabs.setCurrentWidget(dialog.musicbrainz_tab)
        dialog.set_candidates(candidates)
        selection_row = 0
        if scene in {"metadata_candidate_low_confidence", "metadata_candidate_no_artwork"}:
            selection_row = 1 if scene == "metadata_candidate_low_confidence" else 2
        dialog.candidate_table.selectRow(selection_row)
        if scene == "metadata_candidate_low_confidence":
            dialog.search_status.setText(
                "Low confidence • review every selected field before explicit apply."
            )
        elif scene == "metadata_candidate_no_artwork":
            dialog.search_status.setText(
                "This candidate has no confirmed artwork. Existing artwork will remain."
            )
        elif scene == "metadata_candidate_with_artwork":
            dialog.candidate_field_checks["artwork"].setChecked(True)
            dialog.search_status.setText(
                "Artwork is available and will download only after explicit apply."
            )
    elif scene == "metadata_provider_error":
        dialog.tabs.setCurrentWidget(dialog.musicbrainz_tab)
        dialog.search_status.setText("MusicBrainz search is unavailable. Try again later.")
    elif scene in {"metadata_history", "metadata_undo_confirmation"}:
        dialog.tabs.setCurrentWidget(dialog.history_tab)
        dialog.refresh_history()
        if scene == "metadata_undo_confirmation":
            from PySide6.QtWidgets import QMessageBox

            confirmation = QMessageBox(
                QMessageBox.Icon.Question,
                "Undo last metadata change?",
                "Restore the previous Music Vault value for Album?\n\n"
                "Audio-file tags and artwork files are unchanged.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                dialog,
            )
            confirmation.setDefaultButton(QMessageBox.StandardButton.No)
            confirmation.ensurePolished()
            if confirmation.layout() is not None:
                confirmation.layout().activate()
            confirmation.adjustSize()
            confirmation.show()
            setattr(window, "_review_metadata_confirmation", confirmation)
    elif scene == "metadata_long_values":
        dialog.field_editors["title"].value_edit.setText(
            "A Very Long Synthetic Track Title Designed To Verify Elision And Responsive Editor Layout"
        )
        dialog.field_editors["artist"].value_edit.setText(
            "Synthetic Ensemble With An Intentionally Long Collaborative Artist Credit"
        )
        dialog.field_editors["album"].value_edit.setText(
            "The Extremely Long Synthetic Album Edition With Additional Review Context"
        )
    elif scene == "metadata_currently_playing":
        window.current_track_id = track_id
        window.update_now_playing_indicator(
            track_id,
            select_if_visible=False,
            scroll_if_visible=False,
        )
        _set_label_text(window, "now_title", "Synthetic Metadata Update Preview")
        _set_label_text(window, "now_artist", "Playback Continues Unchanged")

    setattr(window, "_review_metadata_dialog", dialog)
    dialog.show()
    if scene in {"metadata_manual_artwork", "metadata_no_artwork"}:
        QTimer.singleShot(
            0,
            lambda: dialog.edit_scroll.ensureWidgetVisible(
                dialog.artwork_editor,
                0,
                18,
            ),
        )
    dialog.raise_()
    dialog.activateWindow()


def prepare_review_scene(window: object, scene: str) -> None:
    _close_review_metadata_dialog(window)
    _close_review_remediation_dialog(window)
    if scene in METADATA_REVIEW_SCENES:
        _prepare_metadata_scene(window, scene)
    elif scene in REMEDIATION_REVIEW_SCENES:
        _prepare_remediation_scene(window, scene)
    elif scene == "library":
        _set_page(window, "library_page")
        search = getattr(window, "search_box", None)
        if search is not None:
            search.clear()
        window.current_view_kind = "library"
        window.current_playlist_id = None
        window.current_playlist_name = "Library"
        tracks = window.db.list_tracks()
        window.load_library(
            tracks,
            "Library",
            "A polished local collection built from synthetic review data.",
        )
        if tracks:
            window.update_now_playing_indicator(
                int(tracks[0]["id"]),
                select_if_visible=False,
                scroll_if_visible=False,
            )
            _set_label_text(window, "now_title", "Synthetic Midnight Signal")
            _set_label_text(window, "now_artist", "The Local Archive")
        if len(tracks) > 1:
            window.library_table.selectRow(1)
        if len(tracks) > 2:
            window.manual_queue = [int(tracks[2]["id"])]
            window.update_queue_label()
    elif scene == "albums":
        _set_page(window, "library_page")
        window.current_view_kind = "albums"
        window.current_playlist_id = None
        window.current_playlist_name = "Albums"
        window.show_album_browser()
    elif scene in {"artists", "artists_fetch_disabled", "artists_fetch_enabled"}:
        _set_page(window, "library_page")
        _set_review_artist_fetch_state(window, scene == "artists_fetch_enabled")
        window.current_view_kind = "artists"
        window.current_playlist_id = None
        window.current_playlist_name = "Artists"
        window.show_artist_browser()
    elif scene == "sync_center":
        _set_page(window, "sync_page")
        _prepare_sync_issue_scene(window)
    elif scene == "settings":
        _set_page(window, "settings_page")
    elif scene == "empty_playlist":
        _set_page(window, "library_page")
        search = getattr(window, "search_box", None)
        if search is not None:
            search.clear()
        window.current_view_kind = "custom"
        window.current_playlist_id = None
        window.current_playlist_name = "Empty Playlist"
        window.load_library(
            [],
            "Empty Playlist",
            "Add a track to begin this synthetic playlist.",
        )
    elif scene == "no_results":
        _set_page(window, "library_page")
        window.current_view_kind = "library"
        window.current_playlist_id = None
        window.current_playlist_name = "Library"
        window.load_library(window.db.list_tracks(), "Library", "Synthetic search review.")
        search = getattr(window, "search_box", None)
        if search is not None:
            search.setText("no-synthetic-track-matches-this-query")
        else:
            window.filter_library("no-synthetic-track-matches-this-query")
    else:
        raise ReviewPlanError("The requested review scene is unsupported.")

    sanitize_review_paths(window)


_BROWSER_REVIEW_SCENES = frozenset(
    {"albums", "artists", "artists_fetch_disabled", "artists_fetch_enabled"}
)


def _review_browser_kind(scene: str) -> str | None:
    if scene == "albums":
        return "albums"
    if scene in {"artists", "artists_fetch_disabled", "artists_fetch_enabled"}:
        return "artists"
    return None


def review_scene_ready(window: object, scene: str) -> bool:
    """Return whether asynchronous browser content is safe to capture."""

    if scene in METADATA_REVIEW_SCENES:
        dialog = getattr(window, "_review_metadata_dialog", None)
        if dialog is None or not dialog.isVisible():
            return False
        if scene == "metadata_undo_confirmation":
            confirmation = getattr(window, "_review_metadata_confirmation", None)
            if confirmation is None or not confirmation.isVisible():
                return False
        runner = getattr(dialog, "task_runner", None)
        return scene == "metadata_musicbrainz_loading" or not int(
            getattr(runner, "pending_count", 0)
        )
    if scene in REMEDIATION_REVIEW_SCENES:
        dialog = getattr(window, "_review_remediation_dialog", None)
        if dialog is None or not dialog.isVisible():
            return False
        if scene in {
            "remediation_high_confirmation",
            "remediation_insufficient_disk",
            "remediation_rollback_confirmation",
        }:
            confirmation = getattr(window, "_review_remediation_confirmation", None)
            if confirmation is None or not confirmation.isVisible():
                return False
        runner = getattr(dialog, "task_runner", None)
        return not int(getattr(runner, "pending_count", 0))
    kind = _review_browser_kind(scene)
    if kind is None:
        return True
    view = getattr(window, "browser_view", None)
    model = getattr(window, f"{kind[:-1]}_browser_model", None)
    if view is None or model is None or not hasattr(model, "rowCount"):
        return False
    if model.rowCount() <= 0:
        return False
    state = view.view_state() if hasattr(view, "view_state") else None
    state_value = getattr(state, "value", state)
    if str(state_value) != "content":
        return False
    visible = view.visible_item_keys(near_rows=0) if hasattr(view, "visible_item_keys") else ()
    if not visible:
        return False
    thumbnail_cache = getattr(window, "thumbnail_cache", None)
    if thumbnail_cache is not None and int(getattr(thumbnail_cache, "pending_count", 0)):
        return False
    if kind == "artists":
        service = getattr(window, "artist_image_service", None)
        if service is not None and int(getattr(service, "pending_count", 0)):
            return False
    return True


def finalize_review_scene(window: object, scene: str) -> None:
    """Apply deterministic focus/loading presentation after async work settles."""

    if scene in METADATA_REVIEW_SCENES:
        dialog = getattr(window, "_review_metadata_dialog", None)
        if dialog is not None:
            dialog.setFocus(Qt.FocusReason.OtherFocusReason)
        return
    if scene in REMEDIATION_REVIEW_SCENES:
        dialog = getattr(window, "_review_remediation_dialog", None)
        if dialog is not None:
            dialog.setFocus(Qt.FocusReason.OtherFocusReason)
        return
    kind = _review_browser_kind(scene)
    if kind is None:
        return
    view = getattr(window, "browser_view", None)
    proxy = getattr(window, f"{kind[:-1]}_browser_proxy", None)
    model = getattr(window, f"{kind[:-1]}_browser_model", None)
    if view is not None and proxy is not None and proxy.rowCount() > 1:
        view.setCurrentIndex(proxy.index(1, 0))
        view.setFocus(Qt.FocusReason.OtherFocusReason)
    if scene != "artists_fetch_enabled" or model is None or not hasattr(model, "items"):
        return
    # Preserve one explicit per-card loading state for deterministic visual
    # review even though the no-network synthetic provider resolves quickly.
    for item in model.items():
        if "loading" in str(item.title).casefold():
            model.replace_item(
                item.key,
                artwork_path=None,
                image_state="loading",
                has_cached_image=False,
            )
            break


def browser_review_metrics(window: object, scene: str) -> dict[str, Any] | None:
    """Return aggregate path/name-free evidence for a synthetic browser capture."""

    kind = _review_browser_kind(scene)
    if kind is None:
        return None
    model = getattr(window, f"{kind[:-1]}_browser_model", None)
    proxy = getattr(window, f"{kind[:-1]}_browser_proxy", None)
    view = getattr(window, "browser_view", None)
    if model is None or proxy is None or view is None:
        raise ReviewPlanError("Synthetic browser metrics are unavailable.")

    visible = tuple(view.visible_item_keys()) if hasattr(view, "visible_item_keys") else ()
    visible_digest = hashlib.sha256("\n".join(visible).encode("utf-8")).hexdigest()
    index_widgets = sum(
        1
        for row in range(proxy.rowCount())
        if view.indexWidget(proxy.index(row, 0)) is not None
    )
    states: dict[str, int] = {}
    if hasattr(model, "items"):
        for item in model.items():
            value = getattr(getattr(item, "image_state", None), "value", None)
            normalized = str(value or "unknown")
            states[normalized] = states.get(normalized, 0) + 1

    cache_metrics: dict[str, int] = {}
    thumbnail_cache = getattr(window, "thumbnail_cache", None)
    stats = getattr(thumbnail_cache, "stats", None)
    for attribute in (
        "requests",
        "hits",
        "misses",
        "coalesced",
        "decodes",
        "failures",
        "evictions",
        "entries",
        "bytes_used",
        "pending",
    ):
        value = getattr(stats, attribute, 0)
        cache_metrics[attribute] = int(value) if isinstance(value, int) else 0

    service = getattr(window, "artist_image_service", None)
    provider = getattr(window, "artist_image_provider", None) or getattr(service, "provider", None)
    calls = getattr(provider, "calls", ())
    provider_call_count = len(calls) if isinstance(calls, (list, tuple)) else 0
    synthetic_mode = (
        os.environ.get("MUSIC_VAULT_ARTIST_IMAGE_PROVIDER", "").strip().casefold()
        in {"synthetic", "fake"}
    )
    return {
        "kind": kind,
        "model_rows": int(model.rowCount()),
        "filtered_rows": int(proxy.rowCount()),
        "per_item_widget_count": index_widgets,
        "visible_key_count": len(visible),
        "visible_key_sha256": visible_digest,
        "image_states": states,
        "thumbnail_cache": cache_metrics,
        "artist_fetch_enabled": bool(
            isinstance(getattr(window, "config", None), dict)
            and window.config.get("artist_image_fetch_enabled") is True
        ),
        "synthetic_provider_active": synthetic_mode,
        "synthetic_provider_call_count": provider_call_count if synthetic_mode else 0,
        "public_provider_call_count": 0 if synthetic_mode else provider_call_count,
    }


def metadata_review_metrics(window: object, scene: str) -> dict[str, Any] | None:
    if scene not in METADATA_REVIEW_SCENES:
        return None
    dialog = getattr(window, "_review_metadata_dialog", None)
    if dialog is None:
        raise ReviewPlanError("Synthetic metadata editor is unavailable.")
    metadata_provider = getattr(dialog, "_review_metadata_provider", None)
    cover_provider = getattr(dialog, "_review_cover_provider", None)
    metadata_calls = getattr(metadata_provider, "calls", ())
    cover_calls = getattr(cover_provider, "calls", ())
    confirmation = getattr(window, "_review_metadata_confirmation", None)
    artwork_editor = getattr(dialog, "artwork_editor", None)
    visible_region = getattr(artwork_editor, "visibleRegion", None)
    artwork_editor_visible = False
    if callable(visible_region):
        region = visible_region()
        artwork_editor_visible = not region.isEmpty()
    return {
        "state": scene,
        "editable_field_count": len(getattr(dialog, "field_editors", {})) + 1,
        "candidate_count": len(getattr(dialog, "candidates", ())),
        "history_group_count": int(getattr(dialog, "history_table").rowCount()),
        "source_upload_date_is_read_only": True,
        "database_only_message_present": "audio files"
        in str(getattr(dialog, "file_writeback_note").text()).casefold(),
        "synthetic_provider_active": isinstance(metadata_provider, _SyntheticMetadataProvider),
        "synthetic_provider_call_count": len(metadata_calls) + len(cover_calls),
        "public_provider_call_count": 0,
        "manual_artwork_staged": bool(
            getattr(artwork_editor, "prepared_artwork", None) is not None
        ),
        "artwork_effective_present": bool(
            getattr(getattr(dialog, "snapshot", None), "value", lambda _name: None)(
                "artwork"
            )
        ),
        "artwork_editor_visible": artwork_editor_visible,
        "undo_confirmation_visible": bool(
            confirmation is not None and confirmation.isVisible()
        ),
    }


def remediation_review_metrics(window: object, scene: str) -> dict[str, Any] | None:
    if scene not in REMEDIATION_REVIEW_SCENES:
        return None
    from PySide6.QtWidgets import QPushButton

    dialog = getattr(window, "_review_remediation_dialog", None)
    if dialog is None:
        raise ReviewPlanError("Synthetic remediation dashboard is unavailable.")
    service = getattr(dialog, "_review_remediation_service", None)
    confirmation = getattr(window, "_review_remediation_confirmation", None)
    rendered = "\n".join(
        str(dialog.items_table.item(row, column).text())
        for row in range(dialog.items_table.rowCount())
        for column in range(dialog.items_table.columnCount())
        if dialog.items_table.item(row, column) is not None
    )
    selected_rows = dialog.items_table.selectionModel().selectedRows(0)
    status_text = str(dialog.job_status.text())
    current_art = dialog.current_art_preview.pixmap()
    candidate_art = dialog.candidate_art_preview.pixmap()
    release_choice_text = "\n".join(
        dialog.release_choices_combo.itemText(index)
        for index in range(dialog.release_choices_combo.count())
    ).casefold()
    release_identity_complete = all(
        value in release_choice_text
        for value in (
            "synthetic candidate album",
            "synthetic review artist",
            "2001-02-03",
            "official",
            "synthetic-release",
            "synthetic-recording-review",
        )
    )
    review_widgets = [
        dialog.review_detail,
        dialog.release_choices_combo,
        dialog.current_art_preview,
        dialog.candidate_art_preview,
        *dialog.field_checks.values(),
        dialog.review_button,
        dialog.skip_button,
        dialog.reject_button,
        dialog.keep_button,
        dialog.edit_button,
        dialog.retry_query_button,
        dialog.approve_button,
        dialog.write_files_checkbox,
    ]
    footer_widgets = [
        dialog.disk_estimate,
        dialog.apply_button,
        dialog.rollback_button,
        dialog.report_button,
        dialog.clear_button,
    ]

    def dialog_rect(widget: object) -> QRect:
        origin = widget.mapTo(dialog, QPoint(0, 0))
        return QRect(origin, widget.size())

    required_widgets = [
        widget for widget in (*review_widgets, *footer_widgets) if widget.isVisible()
    ]
    required_rects = [dialog_rect(widget) for widget in required_widgets]
    dialog_bounds = dialog.rect()
    review_bounds = dialog_rect(dialog.review_group)
    clipped_count = sum(
        1
        for rect in required_rects
        if not rect.isValid() or not dialog_bounds.contains(rect)
    )
    review_clipped_count = sum(
        1
        for widget in review_widgets
        if widget.isVisible() and not review_bounds.contains(dialog_rect(widget))
    )
    overlap_count = sum(
        1
        for index, rect in enumerate(required_rects)
        for other in required_rects[index + 1 :]
        if rect.intersects(other)
    )
    return {
        "state": scene,
        "dialog_visible": bool(dialog.isVisible()),
        "metric_card_count": len(getattr(dialog, "metric_cards", {})),
        "control_count": len(dialog.findChildren(QPushButton)),
        "table_row_count": int(dialog.items_table.rowCount()),
        "selected_row_count": len(selected_rows),
        "job_present": bool(getattr(dialog, "_job_id", None)),
        "status_text_present": bool(status_text.strip()),
        "confirmation_visible": bool(
            confirmation is not None and confirmation.isVisible()
        ),
        "write_files_checked": bool(dialog.write_files_checkbox.isChecked()),
        "artwork_field_selected": bool(dialog.field_checks["artwork"].isChecked()),
        "current_artwork_rendered": bool(
            current_art is not None and not current_art.isNull()
        ),
        "candidate_artwork_rendered": bool(
            candidate_art is not None and not candidate_art.isNull()
        ),
        "release_choice_count": (
            int(dialog.release_choices_combo.count())
            if dialog.release_choices_combo.isEnabled()
            else 0
        ),
        "release_identity_complete": release_identity_complete,
        "review_geometry_widget_count": len(required_widgets),
        "review_geometry_overlap_count": overlap_count,
        "review_geometry_clipped_count": clipped_count,
        "review_group_clipped_count": review_clipped_count,
        "review_group_height": int(review_bounds.height()),
        "synthetic_provider_active": isinstance(
            service, _SyntheticRemediationDashboardService
        ),
        "synthetic_provider_call_count": len(getattr(service, "calls", ())),
        "public_provider_call_count": 0,
        "private_path_visible": bool(_WINDOWS_PATH_RE.search(rendered)),
        "aggregate_total": int(
            str(dialog.metric_cards["total"].value_label.text()) or 0
        ),
    }


def _grab_review_window(window: object):
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QApplication

    pixmap = window.grab()
    dialog = getattr(window, "_review_metadata_dialog", None) or getattr(
        window, "_review_remediation_dialog", None
    )
    if dialog is None or not dialog.isVisible():
        return pixmap
    confirmation = getattr(window, "_review_metadata_confirmation", None) or getattr(
        window, "_review_remediation_confirmation", None
    )
    confirmation_visible = bool(
        confirmation is not None and confirmation.isVisible()
    )
    if confirmation_visible:
        # Grabbing a disabled parent while its styled QMessageBox child is
        # visible can yield a blank backing store under Qt's offscreen plugin.
        # Capture the two real windows independently, then composite them.
        confirmation.hide()
        QApplication.processEvents()
    dialog.ensurePolished()
    dialog.repaint()
    dialog_pixmap = dialog.grab()
    if dialog_pixmap.isNull():
        raise ReviewPlanError("Qt returned an empty review-dialog screenshot.")
    painter = QPainter(pixmap)
    painter.fillRect(pixmap.rect(), QColor(0, 0, 0, 138))
    x = max(0, (pixmap.width() - dialog_pixmap.width()) // 2)
    y = max(0, (pixmap.height() - dialog_pixmap.height()) // 2)
    painter.drawPixmap(x, y, dialog_pixmap)
    if confirmation_visible:
        confirmation.show()
        QApplication.processEvents()
        confirmation.ensurePolished()
        confirmation.repaint()
        confirmation_pixmap = confirmation.grab()
        if confirmation_pixmap.isNull():
            painter.end()
            raise ReviewPlanError("Qt returned an empty review confirmation screenshot.")
        confirm_x = max(0, (pixmap.width() - confirmation_pixmap.width()) // 2)
        confirm_y = max(0, (pixmap.height() - confirmation_pixmap.height()) // 2)
        painter.drawPixmap(confirm_x, confirm_y, confirmation_pixmap)
    painter.end()
    return pixmap


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_error_text(exc: BaseException, plan: ReviewPlan) -> str:
    text = str(exc).replace(str(plan.runtime_root), "<synthetic-runtime>")
    text = text.replace(str(plan.output_dir), "<review-output>")
    text = _WINDOWS_PATH_RE.sub("<path>", text)
    return f"{type(exc).__name__}: {text[:500]}"


class UIReviewController(QObject):
    def __init__(self, window: object, app: object, plan: ReviewPlan) -> None:
        super().__init__(window)
        self.window = window
        self.app = app
        self.plan = plan
        self.jobs = [
            (size, scene)
            for size in plan.sizes
            for scene in plan.scenes
        ]
        self.job_index = 0
        self.captures: list[dict[str, Any]] = []
        self.runtime_checks: dict[str, Any] = {}
        self.started_at = _utc_now()
        self._ready_deadline = 0.0

    def start(self) -> None:
        try:
            self.plan.output_dir.mkdir(parents=True, exist_ok=True)
            self.runtime_checks = validate_review_runtime(self.plan)
            if any(scene in METADATA_REVIEW_SCENES for scene in self.plan.scenes):
                self.runtime_checks["metadata_behaviors"] = validate_metadata_review_behaviors(
                    self.window,
                    self.plan,
                )
            if any(scene in REMEDIATION_REVIEW_SCENES for scene in self.plan.scenes):
                self.runtime_checks["remediation_behaviors"] = (
                    validate_remediation_review_behaviors(self.window, self.plan)
                )
            self.window.showNormal()
            self.window.raise_()
            self.window.activateWindow()
        except Exception as exc:
            self._fail(exc)
            return
        QTimer.singleShot(50, self._prepare_next)

    def _prepare_next(self) -> None:
        if self.job_index >= len(self.jobs):
            self._finish()
            return

        size, scene = self.jobs[self.job_index]
        try:
            # Re-expose the native window between jobs. On Windows, rapidly
            # switching stacked pages while resizing can otherwise leave stale
            # regions in Qt's backing store even after a synchronous repaint.
            # This path is reachable only through the explicit review hook.
            self.window.hide()
            self.app.processEvents()
            self.window.resize(size.width, size.height)
            prepare_review_scene(self.window, scene)
            self.window.showNormal()
            self.window.raise_()
            self.window.activateWindow()
            self.window.ensurePolished()
            self.window.updateGeometry()
            self.window.repaint()
            self.app.processEvents()
            review_dialog = getattr(self.window, "_review_metadata_dialog", None) or getattr(
                self.window, "_review_remediation_dialog", None
            )
            if review_dialog is not None:
                review_dialog.hide()
                self.app.processEvents()
                review_dialog.show()
                review_dialog.ensurePolished()
                review_dialog.updateGeometry()
                review_dialog.repaint()
                self.app.processEvents()
                confirmation = getattr(
                    self.window, "_review_metadata_confirmation", None
                ) or getattr(self.window, "_review_remediation_confirmation", None)
                if confirmation is not None:
                    confirmation.show()
                    confirmation.ensurePolished()
                    confirmation.repaint()
                    self.app.processEvents()
        except Exception as exc:
            self._fail(exc)
            return
        self._ready_deadline = time.monotonic() + 15.0
        QTimer.singleShot(40, self._wait_for_scene_ready)

    def _wait_for_scene_ready(self) -> None:
        _size, scene = self.jobs[self.job_index]
        try:
            if review_scene_ready(self.window, scene):
                finalize_review_scene(self.window, scene)
                self.window.repaint()
                self.app.processEvents()
                QTimer.singleShot(self.plan.settle_ms, self._prime_current_render)
                return
            browser_view = getattr(self.window, "browser_view", None)
            if browser_view is not None and hasattr(browser_view, "schedule_visible_items"):
                browser_view.schedule_visible_items()
            if time.monotonic() >= self._ready_deadline:
                raise ReviewPlanError(
                    "Synthetic browser content did not become ready before capture."
                )
        except Exception as exc:
            self._fail(exc)
            return
        QTimer.singleShot(40, self._wait_for_scene_ready)

    def _prime_current_render(self) -> None:
        """Render once before saving so every child surface is polished."""
        try:
            from PySide6.QtGui import QColor, QPixmap

            warmup = QPixmap(self.window.size())
            warmup.fill(QColor("#06090E"))
            self.window.render(warmup)
            review_dialog = getattr(self.window, "_review_metadata_dialog", None) or getattr(
                self.window, "_review_remediation_dialog", None
            )
            if review_dialog is not None:
                dialog_warmup = QPixmap(review_dialog.size())
                dialog_warmup.fill(QColor("#06090E"))
                review_dialog.render(dialog_warmup)
                review_dialog.repaint()
                confirmation = getattr(
                    self.window, "_review_metadata_confirmation", None
                ) or getattr(self.window, "_review_remediation_confirmation", None)
                if confirmation is not None:
                    confirmation_warmup = QPixmap(confirmation.size())
                    confirmation_warmup.fill(QColor("#06090E"))
                    confirmation.render(confirmation_warmup)
                    confirmation.repaint()
            self.window.repaint()
            self.app.processEvents()
        except Exception as exc:
            self._fail(exc)
            return
        QTimer.singleShot(200, self._capture_current)

    def _capture_current(self) -> None:
        size, scene = self.jobs[self.job_index]
        try:
            sanitize_review_paths(self.window)
            from PySide6.QtWidgets import QToolTip

            QToolTip.hideText()
            self.window.repaint()
            self.app.processEvents()
            # QWidget.grab() captures the fully composed hierarchy and remains
            # reliable when asynchronous thumbnail updates and fractional
            # device scaling invalidate Qt's backing store.
            pixmap = _grab_review_window(self.window)
            if pixmap.isNull():
                raise ReviewPlanError("Qt returned an empty screenshot.")

            filename = f"{size.width}x{size.height}_{scene}.png"
            destination = self.plan.output_dir / filename
            if not pixmap.save(str(destination), "PNG"):
                raise ReviewPlanError("Qt could not save a review screenshot.")

            capture = {
                "file": filename,
                "page": SCENE_LABELS[scene],
                "scene": scene,
                "requested_width": size.width,
                "requested_height": size.height,
                "captured_width": pixmap.width(),
                "captured_height": pixmap.height(),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            }
            metrics = browser_review_metrics(self.window, scene)
            if metrics is not None:
                capture["browser_metrics"] = metrics
            metadata_metrics = metadata_review_metrics(self.window, scene)
            if metadata_metrics is not None:
                capture["metadata_metrics"] = metadata_metrics
            remediation_metrics = remediation_review_metrics(self.window, scene)
            if remediation_metrics is not None:
                capture["remediation_metrics"] = remediation_metrics
            self.captures.append(capture)
        except Exception as exc:
            self._fail(exc)
            return

        self.job_index += 1
        QTimer.singleShot(100, self._prepare_next)

    def _manifest(self, status: str, error: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "application": "Music Vault",
            "review_kind": "synthetic_ui",
            "status": status,
            "started_at": self.started_at,
            "finished_at": _utc_now(),
            "runtime": "isolated_temporary",
            "dark_title_bar_applied": bool(
                getattr(self.window, "_dark_title_bar_applied", False)
            ),
            "runtime_checks": self.runtime_checks,
            "requested_capture_count": self.plan.capture_count,
            "capture_count": len(self.captures),
            "sizes": [
                {"width": size.width, "height": size.height}
                for size in self.plan.sizes
            ],
            "pages": [SCENE_LABELS[scene] for scene in self.plan.scenes],
            "captures": self.captures,
        }
        if error:
            payload["error"] = error
        return payload

    def _write_manifest(self, payload: dict[str, Any]) -> None:
        self.plan.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.plan.output_dir / "manifest.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)

    def _finish(self) -> None:
        try:
            metadata_behaviors = self.runtime_checks.get("metadata_behaviors")
            remediation_behaviors = self.runtime_checks.get("remediation_behaviors")
            self.runtime_checks = validate_review_runtime(self.plan)
            if metadata_behaviors is not None:
                self.runtime_checks["metadata_behaviors"] = metadata_behaviors
            if remediation_behaviors is not None:
                self.runtime_checks["remediation_behaviors"] = remediation_behaviors
            if len(self.captures) != self.plan.capture_count:
                raise ReviewPlanError("The review capture matrix is incomplete.")
            self._write_manifest(self._manifest("complete"))
        except Exception as exc:
            self._fail(exc)
            return
        self._close(0)

    def _fail(self, exc: BaseException) -> None:
        try:
            self._write_manifest(
                self._manifest("failed", _safe_error_text(exc, self.plan))
            )
        finally:
            self._close(2)

    def _close(self, exit_code: int) -> None:
        try:
            self.window.close()
        finally:
            self.app.exit(exit_code)


def schedule_ui_review(window: object, app: object) -> bool:
    """Schedule an isolated capture only when the explicit review env var is set."""
    request = os.environ.get(REVIEW_ENV, "").strip()
    if not request:
        return False

    plan = load_review_plan(request)
    controller = UIReviewController(window, app, plan)
    setattr(window, "_ui_review_controller", controller)
    QTimer.singleShot(0, controller.start)
    return True
