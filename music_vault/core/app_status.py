from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from music_vault.version import APP_VERSION, RELEASE_CHANNEL

from .ffmpeg import discover_ffmpeg
from .paths import (
    app_status_path,
    config_path,
    data_dir,
    database_path as default_database_path,
    default_downloads_dir,
    discogs_token_path,
    path_resolution_source,
    project_root,
    youtube_api_key_path,
)
from .safety import sanitize_error_text


SCHEMA_VERSION = 1
LEGACY_SYNC_FIELDS = (
    "last_sync_at",
    "last_sync_status",
    "last_sync_playlist_title",
    "last_sync_new_items",
    "last_sync_imported_count",
    "last_sync_error",
)
OPTIONAL_SYNC_FIELDS = (
    "last_sync_playlist_id",
    "last_sync_visible_item_count",
    "last_sync_downloaded_count",
    "last_sync_existing_count",
    "last_sync_failed_count",
    "last_sync_failures",
)
MULTI_SOURCE_SYNC_FIELDS = (
    "sync_source_count",
    "enabled_sync_source_count",
    "active_sync_batch",
    "active_sync_source_index",
    "last_sync_batch_status",
    "last_sync_batch_source_count",
    "last_sync_batch_complete_count",
    "last_sync_batch_issue_count",
    "last_sync_batch_failed_count",
    "last_sync_batch_downloaded_count",
    "last_sync_batch_imported_count",
    "last_sync_batch_item_failure_count",
)
SYNC_FIELDS = LEGACY_SYNC_FIELDS + OPTIONAL_SYNC_FIELDS + MULTI_SOURCE_SYNC_FIELDS


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _count(db, query: str) -> int:
    try:
        row = db.conn.execute(query).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _missing_track_count(db) -> int:
    try:
        rows = db.conn.execute("SELECT path FROM tracks").fetchall()
    except Exception:
        return 0
    return sum(1 for row in rows if not Path(row["path"]).is_file())


def _api_ready() -> bool:
    if os.environ.get("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "").strip() == "1":
        return False
    try:
        return bool(youtube_api_key_path().read_text(encoding="utf-8", errors="ignore").strip())
    except Exception:
        return False


def _discogs_ready() -> bool:
    if os.environ.get("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "").strip() == "1":
        return False
    try:
        # Read only enough local state to report readiness. The value is never
        # returned, logged, or included in App Status.
        return bool(discogs_token_path().read_text(encoding="utf-8", errors="ignore").strip())
    except Exception:
        return False


def _metadata_intelligence_summary(db, config) -> dict:
    try:
        consent_version = int(config.get("metadata_intelligence_consent_version") or 0)
    except (AttributeError, TypeError, ValueError, OverflowError):
        consent_version = 0
    enabled = bool(
        isinstance(config, dict)
        and config.get("metadata_intelligence_enabled") is True
        and consent_version >= 1
    )
    summary = {
        "metadata_intelligence_enabled": enabled,
        "metadata_intelligence_job_status": None,
        "metadata_intelligence_total": 0,
        "metadata_intelligence_analyzed": 0,
        "metadata_intelligence_applied": 0,
        "metadata_intelligence_review_count": 0,
        "discogs_ready": _discogs_ready(),
    }
    try:
        job = db.conn.execute(
            "SELECT status FROM metadata_intelligence_jobs "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if job is not None:
            summary["metadata_intelligence_job_status"] = str(job[0])
        counts = db.conn.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN state NOT IN ('created', 'queued') THEN 1 ELSE 0 END),
                SUM(CASE WHEN state IN ('applied', 'complete') THEN 1 ELSE 0 END),
                SUM(CASE WHEN state IN ('needs_review', 'review') THEN 1 ELSE 0 END)
            FROM metadata_intelligence_items
            """
        ).fetchone()
        if counts is not None:
            summary["metadata_intelligence_total"] = int(counts[0] or 0)
            summary["metadata_intelligence_analyzed"] = int(counts[1] or 0)
            summary["metadata_intelligence_applied"] = int(counts[2] or 0)
            summary["metadata_intelligence_review_count"] = int(counts[3] or 0)
    except Exception:
        pass
    return summary


def _ffmpeg_ready(config=None) -> bool:
    configured = config.get("ffmpeg_location") if isinstance(config, dict) else None
    portable_tools = (
        config.get("portable_ffmpeg_location") if isinstance(config, dict) else None
    )
    return discover_ffmpeg(
        configured_location=configured,
        portable_tools_location=portable_tools,
        probe=False,
    ).ready


def _previous_sync(status_file: Path) -> dict:
    empty = {field: None for field in SYNC_FIELDS}
    try:
        payload = json.loads(status_file.read_text(encoding="utf-8"))
        sync = payload.get("sync") if isinstance(payload, dict) else None
        if isinstance(sync, dict):
            return _sanitize_sync_values(
                {field: sync.get(field) for field in SYNC_FIELDS}
            )
    except Exception:
        pass
    return empty


def _merge_section(payload: dict, section: str, values) -> None:
    if isinstance(values, dict) and isinstance(payload.get(section), dict):
        if section == "sync":
            values = _sanitize_sync_values(values)
        payload[section].update(values)


def _sanitize_sync_values(values: dict) -> dict:
    # App Status accepts only stable aggregate/legacy keys. This prevents a
    # future source view model from accidentally exporting a URL, label,
    # playlist identity, membership snapshot, folder, or per-item detail.
    sanitized = {key: value for key, value in values.items() if key in SYNC_FIELDS}
    if sanitized.get("last_sync_error") is not None:
        sanitized["last_sync_error"] = sanitize_error_text(sanitized["last_sync_error"])
    failures = sanitized.get("last_sync_failures")
    if isinstance(failures, list):
        cleaned = []
        for failure in failures[:25]:
            if isinstance(failure, dict):
                failure = dict(failure)
                failure["reason"] = sanitize_error_text(failure.get("reason"))
            cleaned.append(failure)
        sanitized["last_sync_failures"] = cleaned
    # Batch 10 keeps these legacy keys for schema compatibility but no longer
    # exports source identities or item-level failure detail.
    sanitized["last_sync_playlist_title"] = None
    sanitized["last_sync_playlist_id"] = None
    sanitized["last_sync_error"] = None
    sanitized["last_sync_failures"] = []
    return sanitized


def write_app_status(db, config, extra=None) -> Path:
    """Atomically write the stable, secret-free external app status document."""
    root = project_root()
    resolved_data_dir = data_dir()
    status_file = app_status_path()
    database = Path(getattr(db, "db_path", default_database_path())).resolve()
    downloads = (
        config.get("download_folder") if isinstance(config, dict) else None
    ) or default_downloads_dir()
    api_ready = _api_ready()
    ffmpeg_ready = _ffmpeg_ready(config)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "app": "Music Vault",
        "app_version": APP_VERSION,
        "release_channel": RELEASE_CHANNEL,
        "updated_at": _utc_now(),
        "health": {
            "ok": api_ready and ffmpeg_ready,
            "api_ready": api_ready,
            "ffmpeg_ready": ffmpeg_ready,
        },
        "library": {
            "track_count": _count(db, "SELECT COUNT(*) FROM tracks"),
            "playlist_count": _count(db, "SELECT COUNT(*) FROM playlists"),
            "album_count": _count(
                db, "SELECT COUNT(DISTINCT NULLIF(TRIM(album), '')) FROM tracks"
            ),
            "artist_count": _count(
                db, "SELECT COUNT(DISTINCT NULLIF(TRIM(artist), '')) FROM tracks"
            ),
            "missing_track_count": _missing_track_count(db),
        },
        "playback": {
            "currently_playing": None,
            "current_title": None,
            "current_artist": None,
            "current_album": None,
            "is_playing": False,
            "shuffle_enabled": False,
            "autoplay_enabled": True,
            "repeat_mode": "off",
            "queue_count": 0,
        },
        "sync": _previous_sync(status_file),
        "party_mode_active": False,
        "party_mode_preset": "static",
        "audio_reactivity_available": False,
        "party_mode_lyrics_enabled": bool(
            isinstance(config, dict)
            and config.get("party_mode_lyrics_enabled") is True
        ),
        "lyrics_available": False,
        "lyrics_synchronized": False,
        **_metadata_intelligence_summary(db, config),
        "paths": {
            "project_root": str(root),
            "data_dir": str(resolved_data_dir),
            "database": str(database),
            "downloads": str(Path(downloads).resolve()),
            "config": str(config_path()),
            "status_file": str(status_file),
            "path_resolution_source": path_resolution_source(),
        },
    }
    if isinstance(extra, dict):
        for section in ("health", "playback", "sync"):
            _merge_section(payload, section, extra.get(section))
        for field in (
            "party_mode_active",
            "party_mode_preset",
            "audio_reactivity_available",
            "party_mode_lyrics_enabled",
            "lyrics_available",
            "lyrics_synchronized",
            "metadata_intelligence_enabled",
            "metadata_intelligence_job_status",
            "metadata_intelligence_total",
            "metadata_intelligence_analyzed",
            "metadata_intelligence_applied",
            "metadata_intelligence_review_count",
            "discogs_ready",
        ):
            if field in extra:
                payload[field] = extra[field]

    resolved_data_dir.mkdir(parents=True, exist_ok=True)
    temporary = status_file.with_name(f"{status_file.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(status_file)
    return status_file


# A neutral short alias is convenient for future callers.
write_status = write_app_status
