from __future__ import annotations

import hashlib
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
    if schema_version != 2:
        raise ReviewPlanError("Synthetic database schema is not version 2.")

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


def prepare_review_scene(window: object, scene: str) -> None:
    if scene == "library":
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
            pixmap = self.window.grab()
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
            self.runtime_checks = validate_review_runtime(self.plan)
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
