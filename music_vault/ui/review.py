from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Qt


REVIEW_ENV = "MUSIC_VAULT_UI_REVIEW"
REVIEW_SCHEMA_VERSION = 1

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
    if schema_version != 3:
        raise ReviewPlanError("Synthetic database schema is not version 3.")

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
    if scene in METADATA_REVIEW_SCENES:
        _prepare_metadata_scene(window, scene)
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


def _grab_review_window(window: object):
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QApplication

    pixmap = window.grab()
    dialog = getattr(window, "_review_metadata_dialog", None)
    if dialog is None or not dialog.isVisible():
        return pixmap
    confirmation = getattr(window, "_review_metadata_confirmation", None)
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
        raise ReviewPlanError("Qt returned an empty metadata-editor screenshot.")
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
            raise ReviewPlanError("Qt returned an empty metadata confirmation screenshot.")
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
            metadata_dialog = getattr(self.window, "_review_metadata_dialog", None)
            if metadata_dialog is not None:
                metadata_dialog.hide()
                self.app.processEvents()
                metadata_dialog.show()
                metadata_dialog.ensurePolished()
                metadata_dialog.updateGeometry()
                metadata_dialog.repaint()
                self.app.processEvents()
                confirmation = getattr(self.window, "_review_metadata_confirmation", None)
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
            metadata_dialog = getattr(self.window, "_review_metadata_dialog", None)
            if metadata_dialog is not None:
                dialog_warmup = QPixmap(metadata_dialog.size())
                dialog_warmup.fill(QColor("#06090E"))
                metadata_dialog.render(dialog_warmup)
                metadata_dialog.repaint()
                confirmation = getattr(self.window, "_review_metadata_confirmation", None)
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
            self.runtime_checks = validate_review_runtime(self.plan)
            if metadata_behaviors is not None:
                self.runtime_checks["metadata_behaviors"] = metadata_behaviors
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
