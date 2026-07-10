from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .paths import (
    config_path,
    data_dir,
    database_path as default_database_path,
    default_downloads_dir,
    path_resolution_source,
    project_root,
    watchtower_status_path,
    youtube_api_key_path,
)


SCHEMA_VERSION = 1
APP_VERSION = "1.0"
SYNC_FIELDS = (
    "last_sync_at",
    "last_sync_status",
    "last_sync_playlist_title",
    "last_sync_new_items",
    "last_sync_imported_count",
    "last_sync_error",
)


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

    missing = 0
    for row in rows:
        try:
            if not Path(row["path"]).exists():
                missing += 1
        except Exception:
            missing += 1

    return missing


def _api_ready() -> bool:
    api_key_path = youtube_api_key_path()

    try:
        return bool(api_key_path.read_text(encoding="utf-8", errors="ignore").strip())
    except Exception:
        return False


def _ffmpeg_ready() -> bool:
    tools_root = Path.home() / "Documents" / "MusicVaultTools" / "ffmpeg"

    if not tools_root.exists():
        return False

    return any((bin_dir / "ffmpeg.exe").exists() for bin_dir in tools_root.glob("*/bin"))


def _previous_sync(status_file: Path) -> dict:
    empty_sync = {field: None for field in SYNC_FIELDS}

    try:
        payload = json.loads(status_file.read_text(encoding="utf-8"))
        sync = payload.get("sync") if isinstance(payload, dict) else None

        if not isinstance(sync, dict):
            return empty_sync

        return {field: sync.get(field) for field in SYNC_FIELDS}
    except Exception:
        return empty_sync


def _merge_section(payload: dict, section: str, values) -> None:
    if not isinstance(values, dict):
        return

    target = payload.get(section)
    if not isinstance(target, dict):
        return

    for key, value in values.items():
        if key in target:
            target[key] = value


def write_watchtower_status(db, config, extra=None) -> Path:
    """Write the stable, secret-free Music Vault status document for Watchtower."""
    resolved_project_root = project_root()
    resolved_data_dir = data_dir()
    status_file = watchtower_status_path()
    resolved_database_path = Path(
        getattr(db, "db_path", default_database_path())
    ).resolve()

    if isinstance(config, dict):
        download_folder = config.get("download_folder") or default_downloads_dir()
    else:
        download_folder = default_downloads_dir()

    api_ready = _api_ready()
    ffmpeg_ready = _ffmpeg_ready()
    health = {
        "ok": api_ready and ffmpeg_ready,
        "api_ready": api_ready,
        "ffmpeg_ready": ffmpeg_ready,
    }

    payload = {
        "schema_version": SCHEMA_VERSION,
        "app": "Music Vault",
        "app_version": APP_VERSION,
        "updated_at": _utc_now(),
        "health": health,
        "library": {
            "track_count": _count(db, "SELECT COUNT(*) FROM tracks"),
            "playlist_count": _count(db, "SELECT COUNT(*) FROM playlists"),
            "album_count": _count(
                db,
                "SELECT COUNT(DISTINCT COALESCE(NULLIF(TRIM(album), ''), 'Unknown Album')) FROM tracks",
            ),
            "artist_count": _count(
                db,
                "SELECT COUNT(DISTINCT COALESCE(NULLIF(TRIM(artist), ''), 'Unknown Artist')) FROM tracks",
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
        "paths": {
            "project_root": str(resolved_project_root),
            "data_dir": str(resolved_data_dir),
            "database": str(resolved_database_path),
            "downloads": str(Path(download_folder).resolve()),
            "config": str(config_path()),
            "status_file": str(status_file),
            "path_resolution_source": path_resolution_source(),
        },
    }

    if isinstance(extra, dict):
        for section in ("health", "playback", "sync"):
            _merge_section(payload, section, extra.get(section))

    resolved_data_dir.mkdir(parents=True, exist_ok=True)
    temporary_file = status_file.with_name(f"{status_file.name}.tmp")
    temporary_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary_file.replace(status_file)

    return status_file
