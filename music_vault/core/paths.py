from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path


def _is_project_root(candidate: Path) -> bool:
    return (
        candidate.is_dir()
        and (candidate / "run.py").is_file()
        and (candidate / "music_vault").is_dir()
    )


@lru_cache(maxsize=1)
def _resolved_project_root() -> tuple[Path, str]:
    override = os.environ.get("MUSIC_VAULT_PROJECT_ROOT", "").strip()

    if override:
        candidate = Path(override).expanduser().resolve()
        if _is_project_root(candidate):
            return candidate, "environment_override"

    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        dist_dir = executable.parent.parent

        if dist_dir.name.lower() == "dist":
            candidate = dist_dir.parent
            if _is_project_root(candidate):
                return candidate, "executable_parent"

    source_root = Path(__file__).resolve().parents[2]
    if _is_project_root(source_root):
        return source_root, "source_package"

    return Path.cwd().resolve(), "cwd_fallback"


def project_root() -> Path:
    return _resolved_project_root()[0]


def path_resolution_source() -> str:
    return _resolved_project_root()[1]


def data_dir() -> Path:
    return project_root() / "data"


def database_path() -> Path:
    return data_dir() / "music_vault.sqlite3"


def config_path() -> Path:
    return data_dir() / "music_vault_config.json"


def youtube_api_key_path() -> Path:
    return data_dir() / "youtube_api_key.txt"


def youtube_download_archive_path() -> Path:
    return data_dir() / "youtube_download_archive.txt"


def youtube_failed_ids_path() -> Path:
    return data_dir() / "youtube_failed_ids.txt"


def covers_dir() -> Path:
    return data_dir() / "covers"


def manual_covers_dir() -> Path:
    return covers_dir() / "manual"


def cover_art_archive_dir() -> Path:
    return covers_dir() / "providers" / "cover_art_archive"


def artist_images_dir() -> Path:
    return data_dir() / "artist_images"


def artist_image_files_dir() -> Path:
    return artist_images_dir() / "files"


def artist_image_index_path() -> Path:
    return artist_images_dir() / "index.json"


def metadata_reports_dir() -> Path:
    return data_dir() / "metadata_reports"


def metadata_job_backups_dir() -> Path:
    return data_dir() / "backups" / "metadata_jobs"


def default_downloads_dir() -> Path:
    return data_dir() / "youtube_downloads"


def app_status_path() -> Path:
    return data_dir() / "music_vault_status.json"


def watchtower_status_path() -> Path:
    """Compatibility alias for status consumers created before Batch 2."""
    return app_status_path()


def assets_dir() -> Path:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        bundle_root = Path(getattr(sys, "_MEIPASS", executable.parent)).resolve()

        for candidate in (
            bundle_root / "assets",
            executable.parent / "_internal" / "assets",
            executable.parent / "assets",
        ):
            if candidate.is_dir():
                return candidate

    return project_root() / "assets"


def icon_path() -> Path:
    return assets_dir() / "icons" / "music_vault.ico"
