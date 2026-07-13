from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping


PORTABLE_MARKER_NAME = "music-vault.portable.json"
PORTABLE_MARKER_VERSION = 1
DATA_LOCATOR_VERSION = 1
_LOGGER = logging.getLogger(__name__)
_configured_data_directory: Path | None = None


@dataclass(frozen=True)
class RuntimeRootResolution:
    root: Path
    source: str
    marker_path: Path | None = None
    warning: str | None = None


@dataclass(frozen=True)
class WritableLocationResult:
    path: Path
    writable: bool
    error: str | None = None


@dataclass(frozen=True)
class DataDirectoryConfiguration:
    path: Path
    configured: bool
    persisted: bool
    locator_path: Path | None = None
    error: str | None = None


def _is_project_root(candidate: Path) -> bool:
    return (
        candidate.is_dir()
        and (candidate / "run.py").is_file()
        and (candidate / "music_vault").is_dir()
    )


def _read_portable_marker(candidate: Path) -> dict | None:
    marker = candidate / PORTABLE_MARKER_NAME
    try:
        if not marker.is_file() or marker.stat().st_size > 64 * 1024:
            return None
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    version = payload.get("schema_version", payload.get("portable_marker_version", 1))
    product = payload.get("product", payload.get("app", "Music Vault"))
    portable = payload.get("portable", True)
    if version != PORTABLE_MARKER_VERSION or product != "Music Vault" or portable is not True:
        return None
    return payload


def _portable_marker(candidate: Path) -> Path | None:
    return candidate / PORTABLE_MARKER_NAME if _read_portable_marker(candidate) is not None else None


def _portable_marker_near_executable(executable: Path) -> tuple[Path, Path] | None:
    for root in (executable.parent, executable.parent.parent):
        marker = _portable_marker(root)
        if marker is not None:
            return root.resolve(), marker.resolve()
    return None


def resolve_runtime_root(
    *,
    environ: Mapping[str, str] | None = None,
    frozen: bool | None = None,
    executable: str | Path | None = None,
    source_file: str | Path | None = None,
    cwd: str | Path | None = None,
) -> RuntimeRootResolution:
    """Resolve the application/runtime root without depending on shell CWD when frozen."""
    environment = os.environ if environ is None else environ
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    executable_path = Path(executable or sys.executable).expanduser().resolve()
    source_path = Path(source_file or __file__).resolve()
    working_directory = Path(cwd or Path.cwd()).resolve()

    override = str(environment.get("MUSIC_VAULT_PROJECT_ROOT", "")).strip()
    if override:
        candidate = Path(override).expanduser().resolve()
        if _is_project_root(candidate) or _portable_marker(candidate) is not None:
            return RuntimeRootResolution(candidate, "environment_override")
        _LOGGER.warning("Ignoring an invalid MUSIC_VAULT_PROJECT_ROOT override.")

    if is_frozen:
        portable = _portable_marker_near_executable(executable_path)
        if portable is not None:
            root, marker = portable
            return RuntimeRootResolution(root, "portable_marker", marker)

    source_root = source_path.parents[2]
    if _is_project_root(source_root):
        return RuntimeRootResolution(source_root, "source_package")

    if is_frozen:
        dist_dir = executable_path.parent.parent
        if dist_dir.name.casefold() == "dist":
            candidate = dist_dir.parent
            if _is_project_root(candidate):
                return RuntimeRootResolution(candidate, "executable_parent")
        warning = (
            "No portable marker or development project root was found; "
            "using the executable directory."
        )
        return RuntimeRootResolution(
            executable_path.parent,
            "executable_fallback",
            warning=warning,
        )

    return RuntimeRootResolution(
        working_directory,
        "cwd_fallback",
        warning="No source project root was found; using the current working directory.",
    )


@lru_cache(maxsize=1)
def _resolved_project_root() -> tuple[Path, str]:
    resolution = resolve_runtime_root()
    if resolution.warning:
        _LOGGER.warning(resolution.warning)
    return resolution.root, resolution.source


def project_root() -> Path:
    return _resolved_project_root()[0]


def path_resolution_source() -> str:
    return _resolved_project_root()[1]


def portable_marker_path() -> Path | None:
    if path_resolution_source() != "portable_marker":
        return None
    return _portable_marker(project_root())


def portable_root() -> Path | None:
    return project_root() if portable_marker_path() is not None else None


def is_portable_mode() -> bool:
    return portable_root() is not None


def _marker_data_directory() -> Path | None:
    root = portable_root()
    if root is None:
        return None
    payload = _read_portable_marker(root) or {}
    value = payload.get("data_directory", "data")
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return root / "data"
    configured = Path(value).expanduser()
    if configured.is_absolute():
        _LOGGER.warning(
            "Ignoring an absolute portable data_directory; marker data must remain "
            "under the portable root."
        )
        return root / "data"
    resolved = (root / configured).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        _LOGGER.warning("Ignoring a portable data_directory outside the portable root.")
        return root / "data"
    return resolved


def data_directory_locator_path(
    *,
    executable: str | Path | None = None,
    local_app_data: str | Path | None = None,
) -> Path | None:
    base_value = local_app_data or os.environ.get("LOCALAPPDATA", "")
    if not str(base_value).strip():
        return None
    executable_path = Path(executable or sys.executable).expanduser().resolve()
    identity = hashlib.sha256(str(executable_path).casefold().encode("utf-8")).hexdigest()[:24]
    return (
        Path(base_value).expanduser().resolve()
        / "Music Vault"
        / "runtime-locations"
        / f"{identity}.json"
    )


def _located_data_directory() -> Path | None:
    if not is_portable_mode():
        return None
    locator = data_directory_locator_path()
    if locator is None:
        return None
    try:
        if not locator.is_file() or locator.stat().st_size > 16 * 1024:
            return None
        payload = json.loads(locator.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != DATA_LOCATOR_VERSION:
            return None
        value = payload.get("data_directory")
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            return None
        return Path(value).expanduser().resolve()
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def data_dir() -> Path:
    if _configured_data_directory is not None:
        return _configured_data_directory
    located = _located_data_directory()
    if located is not None:
        return located
    marked = _marker_data_directory()
    if marked is not None:
        return marked
    return project_root() / "data"


def data_directory_source() -> str:
    if _configured_data_directory is not None:
        return "configured"
    if _located_data_directory() is not None:
        return "portable_locator"
    if _marker_data_directory() is not None:
        return "portable_marker"
    return "project_data"


def _check_writable_directory(path: Path, *, create: bool) -> WritableLocationResult:
    candidate = path.expanduser().resolve()
    try:
        if candidate.exists() and not candidate.is_dir():
            return WritableLocationResult(candidate, False, "The selected location is not a folder.")
        if create:
            candidate.mkdir(parents=True, exist_ok=True)
            probe_root = candidate
        elif candidate.is_dir():
            probe_root = candidate
        else:
            probe_root = candidate.parent
            while not probe_root.exists() and probe_root != probe_root.parent:
                probe_root = probe_root.parent
            if not probe_root.is_dir() or not os.access(probe_root, os.W_OK):
                return WritableLocationResult(
                    candidate,
                    False,
                    "The selected location cannot be created with the current user account.",
                )
            return WritableLocationResult(candidate, True)

        handle, probe_name = tempfile.mkstemp(prefix=".music-vault-write-test-", dir=probe_root)
        os.close(handle)
        Path(probe_name).unlink()
        return WritableLocationResult(candidate, True)
    except OSError:
        return WritableLocationResult(
            candidate,
            False,
            "The selected location is not writable with the current user account.",
        )


def runtime_root_check(root: str | Path | None = None) -> WritableLocationResult:
    """Return a user-displayable writability result without creating the root."""
    return _check_writable_directory(Path(root) if root is not None else project_root(), create=False)


def data_directory_check(
    path: str | Path | None = None,
    *,
    create: bool = False,
) -> WritableLocationResult:
    return _check_writable_directory(Path(path) if path is not None else data_dir(), create=create)


def configure_data_dir(
    path: str | Path,
    *,
    persist: bool = True,
    create: bool = True,
) -> DataDirectoryConfiguration:
    """Select a writable runtime-data directory before database construction."""
    global _configured_data_directory
    check = data_directory_check(path, create=create)
    if not check.writable:
        return DataDirectoryConfiguration(check.path, False, False, error=check.error)

    locator: Path | None = None
    persisted = False
    persistence_error: str | None = None
    if persist:
        if not is_portable_mode():
            persistence_error = "Persistent data-location selection is available in portable mode only."
        elif check.path == _marker_data_directory():
            # The marker's default/declared path is already durable and moves
            # with the portable folder; no machine-local pointer is needed.
            locator = data_directory_locator_path()
            try:
                if locator is not None and locator.exists():
                    locator.unlink()
                persisted = True
            except OSError:
                persistence_error = "The previous portable data-location pointer could not be removed."
        else:
            locator = data_directory_locator_path()
            if locator is None:
                persistence_error = "Local App Data is unavailable for the portable data-location pointer."
            else:
                try:
                    locator.parent.mkdir(parents=True, exist_ok=True)
                    temporary = locator.with_name(f"{locator.name}.tmp")
                    temporary.write_text(
                        json.dumps(
                            {
                                "schema_version": DATA_LOCATOR_VERSION,
                                "data_directory": str(check.path),
                            },
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    os.replace(temporary, locator)
                    persisted = True
                except OSError:
                    persistence_error = "The portable data-location pointer could not be saved."

    _configured_data_directory = check.path
    return DataDirectoryConfiguration(
        check.path,
        True,
        persisted,
        locator,
        persistence_error,
    )


def clear_configured_data_dir() -> None:
    """Clear only the in-process override; persisted portable selection remains."""
    global _configured_data_directory
    _configured_data_directory = None


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
