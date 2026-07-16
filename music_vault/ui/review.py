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

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QTimer, Qt


REVIEW_ENV = "MUSIC_VAULT_UI_REVIEW"
REVIEW_SCHEMA_VERSION = 1
REMEDIATION_RESTART_PHASE_ENV = "MUSIC_VAULT_REMEDIATION_RESTART_PHASE"
REMEDIATION_RESTART_REQUIRED_ENV = "MUSIC_VAULT_REMEDIATION_RESTART_REQUIRED"
_REMEDIATION_RESTART_CHECKPOINT = "synthetic_remediation_restart.json"
_PARTY_REVIEW_FIXTURE = "synthetic_party_mode_review.json"
_PARTY_REVIEW_FORBIDDEN_STATUS_FIELDS = frozenset(
    {
        "pcm",
        "sample",
        "samples",
        "frequency",
        "frequencies",
        "rms",
        "peak",
        "bass",
        "low_mid",
        "mid",
        "high",
        "beat",
        "beat_strength",
    }
)
_REVIEW_NETWORK_EVENTS: list[str] = []
_REVIEW_NETWORK_GUARD_INSTALLED = False

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

METADATA_INTELLIGENCE_REVIEW_SCENES = ("metadata_intelligence_smoke",)

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

PARTY_REVIEW_SCENES = ("party_mode_smoke",)

MULTI_SOURCE_REVIEW_SCENES = (
    "sync_sources_empty",
    "sync_sources_list",
    "sync_source_add",
    "sync_source_edit",
    "sync_all_running",
    "sync_complete_issues",
    "sync_source_failures",
    "sync_managed_playlist",
    "sync_source_remove",
)

_BATCH10_REVIEW_MARKER = "synthetic_batch10_smoke.json"

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
    "metadata_intelligence_smoke": "Automatic Metadata Intelligence - Packaged Smoke",
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
    "party_mode_smoke": "Party Mode - Packaged Synthetic Smoke",
    "sync_sources_empty": "Sync Center - Empty Source Manager",
    "sync_sources_list": "Sync Center - Three Saved Sources",
    "sync_source_add": "Sync Center - Add Source",
    "sync_source_edit": "Sync Center - Edit Source",
    "sync_all_running": "Sync Center - Sync All Running",
    "sync_complete_issues": "Sync Center - Complete with Issues",
    "sync_source_failures": "Sync Center - Source Failure History",
    "sync_managed_playlist": "Managed Local Playlist",
    "sync_source_remove": "Sync Center - Remove Source Safely",
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
    from music_vault.core.db import CURRENT_SCHEMA_VERSION
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
    if schema_version != CURRENT_SCHEMA_VERSION:
        raise ReviewPlanError(
            f"Synthetic database schema is not version {CURRENT_SCHEMA_VERSION}."
        )

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


class ReviewNetworkAccessBlocked(RuntimeError):
    """Raised before explicitly reviewed Python code can access the network."""


class _SyntheticReviewLyricsProvider:
    """Bounded provider used only by the isolated packaged Party Mode review."""

    name = "synthetic_review"

    def __init__(self, expected_track_id: int) -> None:
        self.expected_track_id = int(expected_track_id)
        self.call_count = 0

    def lookup(self, query: object, cancel_event: object | None = None):
        from music_vault.lyrics.models import (
            LyricLine,
            LyricsResult,
            LyricsSource,
            LyricsStatus,
        )

        identity = getattr(query, "identity", None)
        if identity is None:
            raise ReviewPlanError("Synthetic lyrics query identity is unavailable.")
        self.call_count += 1
        if callable(getattr(cancel_event, "is_set", None)) and cancel_event.is_set():
            return LyricsResult(
                LyricsStatus.TEMPORARY_ERROR,
                identity,
                error_code="cancelled",
            )
        if str(identity.stable_id) != str(self.expected_track_id):
            return LyricsResult(LyricsStatus.NO_MATCH, identity)
        return LyricsResult(
            LyricsStatus.AVAILABLE,
            identity,
            LyricsSource.PROVIDER,
            (
                LyricLine(0, "Synthetic cached opening line"),
                LyricLine(1_200, "Synthetic cached current line"),
                LyricLine(2_400, "Synthetic cached following line"),
            ),
            provider=self.name,
            provider_result_id="synthetic-review-result",
            provider_duration_ms=20_000,
            attribution="Synthetic offline review provider",
            confidence=1.0,
        )


def _install_review_network_guard() -> None:
    global _REVIEW_NETWORK_GUARD_INSTALLED

    if _REVIEW_NETWORK_GUARD_INSTALLED:
        return
    if os.environ.get("MUSIC_VAULT_DISABLE_NETWORK", "").strip() != "1":
        raise ReviewPlanError("Party Mode review requires the no-network guard.")

    guarded_events = {
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.gethostbyaddr",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.getnameinfo",
        "socket.sendto",
    }

    def audit(event: str, _arguments: tuple[object, ...]) -> None:
        if event in guarded_events:
            _REVIEW_NETWORK_EVENTS.append(event)
            raise ReviewNetworkAccessBlocked(
                f"Synthetic Party Mode review blocked network event: {event}"
            )

    sys.addaudithook(audit)
    _REVIEW_NETWORK_GUARD_INSTALLED = True


def _party_review_fixture(window: object, plan: ReviewPlan) -> dict[str, object]:
    if os.environ.get("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "").strip() != "1":
        raise ReviewPlanError("Party Mode review must disable secret access.")
    fixture_path = plan.runtime_root / "data" / _PARTY_REVIEW_FIXTURE
    _ensure_under_runtime(fixture_path, plan.runtime_root, "Party Mode fixture")
    try:
        if fixture_path.stat().st_size > 16 * 1024:
            raise ReviewPlanError("Synthetic Party Mode fixture is unexpectedly large.")
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewPlanError("Synthetic Party Mode fixture is unavailable.") from exc
    if not isinstance(payload, dict) or not (
        payload.get("schema_version") == 1
        and payload.get("synthetic_only") is True
    ):
        raise ReviewPlanError("Synthetic Party Mode fixture is invalid.")

    track_ids = payload.get("track_ids")
    queue_track_id = payload.get("queue_track_id")
    if (
        not isinstance(track_ids, list)
        or len(track_ids) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in track_ids)
        or len(set(track_ids)) != 2
        or isinstance(queue_track_id, bool)
        or not isinstance(queue_track_id, int)
        or queue_track_id in track_ids
    ):
        raise ReviewPlanError("Synthetic Party Mode track identities are invalid.")

    tracks = []
    sidecars: list[Path] = []
    for index, track_id in enumerate(track_ids):
        track = window.db.get_track(track_id)
        if track is None:
            raise ReviewPlanError("Synthetic Party Mode track is missing.")
        media_path = Path(str(track["path"])).resolve()
        _ensure_under_runtime(media_path, plan.runtime_root, "Party Mode media")
        if (
            media_path.suffix.casefold() != ".wav"
            or not media_path.is_file()
            or not 44 < media_path.stat().st_size <= 5 * 1024 * 1024
        ):
            raise ReviewPlanError("Synthetic Party Mode WAV is invalid.")
        cover_path = str(track["cover_path"] or "").strip()
        if cover_path:
            resolved_cover = Path(cover_path).resolve()
            _ensure_under_runtime(resolved_cover, plan.runtime_root, "Party Mode artwork")
            if not resolved_cover.is_file():
                raise ReviewPlanError("Synthetic Party Mode artwork is missing.")
        sidecar_path = media_path.with_suffix(".lrc" if index == 0 else ".txt")
        _ensure_under_runtime(sidecar_path, plan.runtime_root, "Party Mode lyrics")
        try:
            sidecar_size = sidecar_path.stat().st_size
        except OSError as exc:
            raise ReviewPlanError("Synthetic Party Mode lyrics are unavailable.") from exc
        if not 0 < sidecar_size <= 64 * 1024 or sidecar_path.is_symlink():
            raise ReviewPlanError("Synthetic Party Mode lyrics are invalid.")
        tracks.append(track)
        sidecars.append(sidecar_path)

    if window.db.get_track(queue_track_id) is None:
        raise ReviewPlanError("Synthetic Party Mode queue track is missing.")
    return {
        "track_ids": tuple(track_ids),
        "queue_track_id": queue_track_id,
        "tracks": tuple(tracks),
        "synced_sidecar": sidecars[0],
        "plain_sidecar": sidecars[1],
    }


def _wait_for_review_state(
    app: object,
    predicate,
    *,
    timeout: float,
    label: str,
    required: bool = True,
) -> bool:
    deadline = time.monotonic() + max(0.05, float(timeout))
    while time.monotonic() < deadline:
        app.processEvents()
        try:
            if predicate():
                return True
        except RuntimeError:
            break
        time.sleep(0.01)
    app.processEvents()
    try:
        matched = bool(predicate())
    except RuntimeError:
        matched = False
    if not matched and required:
        raise ReviewPlanError(f"Synthetic Party Mode did not reach {label}.")
    return matched


def _send_review_key(
    target: object,
    key: Qt.Key,
    *,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    text: str = "",
) -> None:
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    for event_type in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
        event = QKeyEvent(event_type, key, modifiers, text)
        QApplication.sendEvent(target, event)
    QApplication.processEvents()


def _party_playback_snapshot(window: object) -> dict[str, object]:
    return {
        "source": str(window.player.source().toLocalFile()),
        "position": int(window.player.position()),
        "playback_state": window.player.playbackState(),
        "volume": int(window.volume_percent),
        "queue": tuple(window.manual_queue),
        "base_context": copy.deepcopy(window.base_playback_context),
    }


def _assert_party_snapshot_preserved(
    window: object,
    snapshot: dict[str, object],
    *,
    label: str,
) -> None:
    current = _party_playback_snapshot(window)
    if current["source"] != snapshot["source"]:
        raise ReviewPlanError(f"Party Mode changed playback source while {label}.")
    if int(current["position"]) + 500 < int(snapshot["position"]):
        raise ReviewPlanError(f"Party Mode reset playback position while {label}.")
    for field in ("playback_state", "volume", "queue", "base_context"):
        if current[field] != snapshot[field]:
            raise ReviewPlanError(f"Party Mode changed {field} while {label}.")


def _status_field_names(value: object) -> set[str]:
    if isinstance(value, dict):
        result = {str(key).casefold() for key in value}
        for item in value.values():
            result.update(_status_field_names(item))
        return result
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_status_field_names(item))
        return result
    return set()


def _validate_party_status(plan: ReviewPlan, *, expected_track_id: int) -> bool:
    from music_vault.core.paths import app_status_path

    status_path = app_status_path().resolve()
    _ensure_under_runtime(status_path, plan.runtime_root, "Party Mode App Status")
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewPlanError("Party Mode App Status is unavailable.") from exc
    if not isinstance(payload, dict):
        raise ReviewPlanError("Party Mode App Status is invalid.")
    if payload.get("party_mode_active") is not True:
        raise ReviewPlanError("Party Mode App Status did not record the active surface.")
    if payload.get("party_mode_preset") != "aurora":
        raise ReviewPlanError("Party Mode App Status did not record the active preset.")
    if not (
        payload.get("party_mode_lyrics_enabled") is True
        and payload.get("lyrics_available") is True
        and payload.get("lyrics_synchronized") is True
    ):
        raise ReviewPlanError("Party Mode App Status lyrics state is inaccurate.")
    playback = payload.get("playback")
    if not isinstance(playback, dict) or not (
        playback.get("currently_playing") == expected_track_id
        and playback.get("queue_count") == 1
    ):
        raise ReviewPlanError("Party Mode App Status playback state is inaccurate.")
    if _PARTY_REVIEW_FORBIDDEN_STATUS_FIELDS.intersection(_status_field_names(payload)):
        raise ReviewPlanError("Party Mode App Status exposed audio-analysis data.")
    paths = payload.get("paths")
    if not isinstance(paths, dict):
        raise ReviewPlanError("Party Mode App Status paths are unavailable.")
    for value in paths.values():
        if isinstance(value, str) and ("\\" in value or "/" in value):
            _ensure_under_runtime(Path(value), plan.runtime_root, "App Status path")
    return True


def validate_party_review_behaviors(
    window: object,
    plan: ReviewPlan,
    app: object,
) -> dict[str, object]:
    """Exercise real Party Mode only inside the explicit synthetic runtime."""

    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication

    fixture = _party_review_fixture(window, plan)
    track_ids = tuple(fixture["track_ids"])
    tracks = tuple(fixture["tracks"])
    queue_track_id = int(fixture["queue_track_id"])
    player = window.player
    audio_output = window.audio_output
    original_muted = bool(audio_output.isMuted())
    setattr(window, "_review_party_original_muted", original_muted)
    audio_output.setMuted(True)

    window.current_view_kind = "library"
    window.current_playlist_id = None
    window.current_playlist_name = "Library"
    window.load_library(
        list(tracks),
        "Synthetic Party Mode",
        "Two local review signals; no network or personal data.",
    )
    window.manual_queue = [queue_track_id]
    window.update_queue_label()
    if not window.play_track_by_id(track_ids[0]):
        raise ReviewPlanError("Synthetic Party Mode WAV could not start.")
    _wait_for_review_state(
        app,
        lambda: player.playbackState() == QMediaPlayer.PlaybackState.PlayingState,
        timeout=5.0,
        label="playing state",
    )
    _wait_for_review_state(
        app,
        lambda: Path(player.source().toLocalFile()).resolve()
        == Path(str(tracks[0]["path"])).resolve(),
        timeout=2.0,
        label="first synthetic source",
    )
    position_advanced = _wait_for_review_state(
        app,
        lambda: player.position() >= 100,
        timeout=3.0,
        label="advancing playback position",
        required=False,
    )

    main_players = window.findChildren(QMediaPlayer)
    if len(main_players) != 1 or main_players[0] is not player:
        raise ReviewPlanError("Music Vault does not own exactly one media player.")
    if player.audioOutput() is not audio_output:
        raise ReviewPlanError("Party Mode replaced the existing audio output.")
    audio_buffer_getter = getattr(player, "audioBufferOutput", None)
    if not callable(audio_buffer_getter) or audio_buffer_getter() is not window.audio_buffer_output:
        raise ReviewPlanError("Party Mode audio-buffer output is not attached to the player.")
    if not (
        window.party_mode_btn.isVisible()
        and window.party_mode_btn.toolTip() == "Party Mode (F11)"
        and window.party_mode_btn.accessibleName() == "Open Party Mode"
    ):
        raise ReviewPlanError("The Party Mode player-bar entry control is unavailable.")

    opening_snapshot = _party_playback_snapshot(window)
    expected_base_tracks = tuple(track_ids)
    base = opening_snapshot["base_context"] or {}
    if tuple(base.get("track_ids", ())) != expected_base_tracks:
        raise ReviewPlanError("Synthetic Party Mode base context is incomplete.")

    _send_review_key(window, Qt.Key.Key_F11)
    _wait_for_review_state(
        app,
        lambda: bool(
            window.party_mode_active
            and window.party_mode_window is not None
            and window.party_mode_window.isVisible()
        ),
        timeout=3.0,
        label="F11 full-screen entry",
    )
    party = window.party_mode_window
    if party is None or not party.isFullScreen():
        raise ReviewPlanError("Party Mode did not become full-screen.")
    if party.findChildren(QMediaPlayer):
        raise ReviewPlanError("Party Mode created a second media player.")
    main_screen = QApplication.screenAt(window.frameGeometry().center()) or window.screen()
    handle = party.windowHandle()
    if handle is None or handle.screen() is not main_screen:
        raise ReviewPlanError("Party Mode opened on the wrong screen.")
    _assert_party_snapshot_preserved(window, opening_snapshot, label="opening")

    audio_features_observed = _wait_for_review_state(
        app,
        lambda: bool(window.party_audio_reactivity_available),
        timeout=3.0,
        label="decoded audio features",
        required=False,
    )
    if not (
        party.current_preset == "static"
        and party.preset_button.text() == "Static"
        and window.config.get("party_mode_config_version") == 2
        and window.config.get("party_mode_preset") == "static"
    ):
        raise ReviewPlanError("Party Mode legacy default did not migrate to Static.")
    if party.rendering_active:
        raise ReviewPlanError("Static retained its high-frequency visual timer.")

    from music_vault.lyrics.cache import LyricsCache
    from music_vault.lyrics.service import LyricsService

    lyrics_cache = LyricsCache(plan.runtime_root / "data" / "lyrics")
    lyrics_provider = _SyntheticReviewLyricsProvider(track_ids[1])
    if getattr(party.lyrics_controller, "_service", None) is not None:
        raise ReviewPlanError("Lyrics service started while lyrics were disabled.")
    party.lyrics_controller._service_factory = lambda: LyricsService(
        lyrics_provider,
        lyrics_cache,
        max_workers=1,
    )
    if not (
        party._lyrics_settings.get("party_mode_lyrics_enabled") is False
        and party._lyrics_settings.get("lyrics_online_lookup_enabled") is False
        and not party.lyrics_controller.enabled
        and party.lyrics_panel.presentation_mode == "hidden"
    ):
        raise ReviewPlanError("Party Mode lyrics did not default Off.")

    locked_layout = (
        party.canvas.geometry().getRect(),
        party.title_label.geometry().getRect(),
        party.artist_label.geometry().getRect(),
        party.album_label.geometry().getRect(),
    )
    _send_review_key(party, Qt.Key.Key_L, text="l")
    _wait_for_review_state(
        app,
        lambda: (
            party.lyrics_controller.enabled
            and party.lyrics_panel.lyrics_available
            and party.lyrics_panel.lyrics_synchronized
            and party.lyrics_panel.presentation_mode == "synchronized"
        ),
        timeout=2.0,
        label="local synchronized lyrics",
    )
    player.setPosition(1_500)
    _wait_for_review_state(
        app,
        lambda: player.position() >= 1_200,
        timeout=2.0,
        label="lyrics playback position",
    )
    party.lyrics_controller.set_position(1_500)
    if not all(
        label.text().strip()
        for label in (
            party.lyrics_panel.previous_label,
            party.lyrics_panel.current_label,
            party.lyrics_panel.next_label,
        )
    ):
        raise ReviewPlanError("Synchronized lyric context did not render.")
    if locked_layout != (
        party.canvas.geometry().getRect(),
        party.title_label.geometry().getRect(),
        party.artist_label.geometry().getRect(),
        party.album_label.geometry().getRect(),
    ):
        raise ReviewPlanError("Lyrics changed the approved Party Mode layout.")
    root = party.centralWidget()
    controls_top = party.controls_panel.mapTo(root, QPoint(0, 0)).y()
    if party.lyrics_panel.geometry().bottom() >= controls_top:
        raise ReviewPlanError("Party Mode lyrics overlap the playback controls.")
    _send_review_key(party, Qt.Key.Key_H, text="h")
    if party.overlay_visible or not party.lyrics_panel.isVisible():
        raise ReviewPlanError("Auto-hidden controls also hid the lyrics panel.")
    _send_review_key(party, Qt.Key.Key_H, text="h")

    def settle_visual_transition(seconds: float = 0.72) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

    def fixed_album_geometry() -> bool:
        frame = getattr(party.canvas, "_frame", None)
        transform = getattr(frame, "album_transform", None)
        return transform is not None and (
            transform.scale,
            transform.translate_x,
            transform.translate_y,
            transform.rotation_degrees,
        ) == (1.0, 0.0, 0.0, 0.0)

    expected_cycle = (
        ("starfield", "Starfield"),
        ("aurora", "Aurora"),
        ("orb_cluster", "Orb Cluster"),
        ("fireworks", "Fireworks"),
        ("pulse", "Pulse"),
        ("static", "Static"),
    )
    for expected_preset, expected_label in expected_cycle:
        _send_review_key(party, Qt.Key.Key_V, text="v")
        settle_visual_transition()
        if not (
            party.current_preset == expected_preset
            and party.preset_button.text() == expected_label
        ):
            raise ReviewPlanError("Party Mode preset order or label is incorrect.")
        frame = getattr(party.canvas, "_frame", None)
        if expected_preset != "pulse" and not fixed_album_geometry():
            raise ReviewPlanError("A non-Pulse preset changed album geometry.")
        if expected_preset == "orb_cluster" and not getattr(frame, "orbs", ()):
            raise ReviewPlanError("Orb Cluster did not render its bounded cluster.")
        if expected_preset == "fireworks" and getattr(frame, "orbs", ()):
            raise ReviewPlanError("Fireworks retained a persistent Orb Cluster.")
        if expected_preset == "pulse":
            transform = getattr(frame, "album_transform", None)
            if transform is None or not (
                1.0 <= transform.scale <= 1.04
                and transform.translate_x == 0.0
                and transform.translate_y == 0.0
                and transform.rotation_degrees == 0.0
            ):
                raise ReviewPlanError("Pulse album motion exceeded its safe scale-only range.")
    if party.rendering_active:
        raise ReviewPlanError("Static did not stop rendering after a preset cycle.")
    for expected_preset in ("starfield", "aurora"):
        _send_review_key(party, Qt.Key.Key_V, text="v")
        settle_visual_transition()
        if party.current_preset != expected_preset:
            raise ReviewPlanError("Party Mode did not restore Aurora for capture.")

    _send_review_key(party, Qt.Key.Key_Question, text="?")
    if not party._help_visible or not party.help_panel.isVisible():
        raise ReviewPlanError("Party Mode shortcut help did not open.")
    _send_review_key(party, Qt.Key.Key_Question, text="?")
    _send_review_key(party, Qt.Key.Key_H, text="h")
    if party.overlay_visible:
        raise ReviewPlanError("Party Mode overlay did not hide.")
    _send_review_key(party, Qt.Key.Key_H, text="h")
    if not party.overlay_visible:
        raise ReviewPlanError("Party Mode overlay did not return.")

    volume_before = int(window.volume_percent)
    _send_review_key(party, Qt.Key.Key_Up)
    _send_review_key(party, Qt.Key.Key_Down)
    if int(window.volume_percent) != volume_before:
        raise ReviewPlanError("Party Mode volume controls did not round-trip safely.")

    _send_review_key(party, Qt.Key.Key_Space, text=" ")
    _wait_for_review_state(
        app,
        lambda: player.playbackState() == QMediaPlayer.PlaybackState.PausedState,
        timeout=2.0,
        label="paused state",
    )
    _wait_for_review_state(
        app,
        lambda: not party.audio_reactivity_available,
        timeout=3.0,
        label="ambient fallback",
    )
    if not party.rendering_active:
        raise ReviewPlanError("Party Mode stopped rendering during ambient fallback.")
    _send_review_key(party, Qt.Key.Key_Space, text=" ")
    _wait_for_review_state(
        app,
        lambda: player.playbackState() == QMediaPlayer.PlaybackState.PlayingState,
        timeout=2.0,
        label="resumed state",
    )
    if audio_features_observed:
        _wait_for_review_state(
            app,
            lambda: party.audio_reactivity_available,
            timeout=3.0,
            label="resumed decoded audio features",
        )

    if tuple(window.manual_queue) != opening_snapshot["queue"]:
        raise ReviewPlanError("Party Mode controls changed the manual queue.")
    if window.base_playback_context != opening_snapshot["base_context"]:
        raise ReviewPlanError("Party Mode controls changed the base context.")
    if not window.play_next_from_base_context():
        raise ReviewPlanError("Synthetic Party Mode track transition failed.")
    _wait_for_review_state(
        app,
        lambda: window.current_track_id == track_ids[1]
        and Path(player.source().toLocalFile()).resolve()
        == Path(str(tracks[1]["path"])).resolve(),
        timeout=4.0,
        label="second synthetic track",
    )
    _wait_for_review_state(
        app,
        lambda: party._current_track_id == track_ids[1],
        timeout=2.0,
        label="Party Mode track transition",
    )
    if tuple(window.manual_queue) != opening_snapshot["queue"]:
        raise ReviewPlanError("Track transition changed the manual queue.")
    transitioned_base = window.base_playback_context or {}
    if not (
        tuple(transitioned_base.get("track_ids", ())) == expected_base_tracks
        and transitioned_base.get("current_track_id") == track_ids[1]
    ):
        raise ReviewPlanError("Track transition lost the base playback context.")

    _wait_for_review_state(
        app,
        lambda: (
            party.lyrics_panel.lyrics_available
            and not party.lyrics_panel.lyrics_synchronized
            and party.lyrics_panel.presentation_mode == "plain"
        ),
        timeout=2.0,
        label="local unsynchronized lyrics",
    )
    if not (
        party.lyrics_panel.unsynced_label.isVisible()
        and party.lyrics_panel.plain_view.toPlainText().strip()
        and not party.lyrics_panel.current_label.isVisible()
    ):
        raise ReviewPlanError("Plain lyrics did not render honestly as unsynchronized.")

    plain_sidecar = Path(fixture["plain_sidecar"])
    disabled_sidecar = plain_sidecar.with_suffix(".txt.review-disabled")
    _ensure_under_runtime(disabled_sidecar, plan.runtime_root, "disabled lyrics sidecar")
    try:
        plain_sidecar.replace(disabled_sidecar)
        online_review_settings = dict(party._lyrics_settings)
        online_review_settings.update(
            {
                "party_mode_lyrics_enabled": True,
                "lyrics_online_lookup_enabled": True,
                "lyrics_lookup_consent_version": 1,
            }
        )
        party.lyrics_controller.apply_settings(online_review_settings)
        _wait_for_review_state(
            app,
            lambda: (
                lyrics_provider.call_count == 1
                and party.lyrics_panel.lyrics_available
                and party.lyrics_panel.lyrics_synchronized
                and party.lyrics_controller.pending_count == 0
            ),
            timeout=3.0,
            label="synthetic cached lyrics",
        )
        identity = party._lyrics_identity
        cached = lyrics_cache.lookup_automatic(identity) if identity is not None else None
        if cached is None or not cached.from_cache or not cached.synchronized:
            raise ReviewPlanError("Synthetic provider lyrics were not cached safely.")
    finally:
        if disabled_sidecar.exists():
            disabled_sidecar.replace(plain_sidecar)

    if not window.play_base_track_by_id(track_ids[0]):
        raise ReviewPlanError("Synthetic lyrics cache replay could not change tracks.")
    _wait_for_review_state(
        app,
        lambda: party._current_track_id == track_ids[0]
        and party.lyrics_panel.lyrics_synchronized,
        timeout=3.0,
        label="synchronized sidecar replay",
    )
    if not window.play_base_track_by_id(track_ids[1]):
        raise ReviewPlanError("Synthetic lyrics cache replay could not return tracks.")
    _wait_for_review_state(
        app,
        lambda: (
            party._current_track_id == track_ids[1]
            and party.lyrics_panel.lyrics_synchronized
            and lyrics_provider.call_count == 1
            and party.lyrics_controller.pending_count == 0
        ),
        timeout=3.0,
        label="cached lyrics replay",
    )
    party.lyrics_controller.apply_settings(party._lyrics_settings)
    if not (
        window.config.get("party_mode_lyrics_enabled") is True
        and window.config.get("lyrics_online_lookup_enabled") is False
        and lyrics_provider.call_count == 1
    ):
        raise ReviewPlanError("Lyrics persistence or offline-default state is inaccurate.")

    escape_snapshot = _party_playback_snapshot(window)
    _send_review_key(party, Qt.Key.Key_Escape)
    _wait_for_review_state(
        app,
        lambda: not window.party_mode_active and not party.isVisible(),
        timeout=3.0,
        label="Escape exit",
    )
    _assert_party_snapshot_preserved(window, escape_snapshot, label="Escape exit")
    if any(
        (
            party.rendering_active,
            party.state_timer.isActive(),
            party.fallback_timer.isActive(),
            party.palette_timer.isActive(),
            party.overlay_timer.isActive(),
            window.party_audio_thread is not None,
            party.lyrics_controller.pending_count,
        )
    ):
        raise ReviewPlanError("Party Mode retained render or analysis work after Escape.")

    _send_review_key(window, Qt.Key.Key_F11)
    _wait_for_review_state(
        app,
        lambda: window.party_mode_active and party.isVisible(),
        timeout=3.0,
        label="second F11 entry",
    )
    if not (
        party.lyrics_controller.enabled
        and party.lyrics_panel.isVisible()
        and party.lyrics_panel.lyrics_available
    ):
        raise ReviewPlanError("Lyrics state did not survive Party Mode close and reopen.")
    second_open_snapshot = _party_playback_snapshot(window)
    _send_review_key(party, Qt.Key.Key_F11)
    _wait_for_review_state(
        app,
        lambda: not window.party_mode_active and not party.isVisible(),
        timeout=3.0,
        label="F11 exit",
    )
    _assert_party_snapshot_preserved(window, second_open_snapshot, label="F11 exit")
    if party.lyrics_controller.pending_count:
        raise ReviewPlanError("Lyrics work remained pending after F11 exit.")

    _send_review_key(window, Qt.Key.Key_F11)
    _wait_for_review_state(
        app,
        lambda: window.party_mode_active and party.isVisible() and party.isFullScreen(),
        timeout=3.0,
        label="final Party Mode capture state",
    )
    if party.current_preset != "aurora" or not party.rendering_active:
        raise ReviewPlanError("Party Mode did not restore its persisted visual state.")
    if not (
        party.lyrics_controller.enabled
        and party.lyrics_panel.isVisible()
        and party.lyrics_panel.lyrics_available
        and party.lyrics_panel.lyrics_synchronized
        and party.lyrics_controller.pending_count == 0
    ):
        raise ReviewPlanError("Party Mode did not restore its persisted lyrics state.")
    if audio_features_observed:
        _wait_for_review_state(
            app,
            lambda: party.audio_reactivity_available,
            timeout=3.0,
            label="final audio-reactive state",
        )
    window.write_app_status()
    status_safe = _validate_party_status(plan, expected_track_id=track_ids[1])
    if _REVIEW_NETWORK_EVENTS:
        raise ReviewPlanError("Party Mode review attempted network access.")

    evidence: dict[str, object] = {
        "packaged_process": bool(getattr(sys, "frozen", False)),
        "synthetic_fixture_validated": True,
        "network_guard_active": _REVIEW_NETWORK_GUARD_INSTALLED,
        "network_attempt_count": len(_REVIEW_NETWORK_EVENTS),
        "party_button_present": True,
        "f11_opened": True,
        "f11_closed": True,
        "escape_closed": True,
        "full_screen": True,
        "screen_matches_main": True,
        "same_media_player": True,
        "same_audio_output": True,
        "audio_buffer_output_attached": True,
        "no_second_player": True,
        "open_close_source_preserved": True,
        "open_close_position_not_reset": True,
        "playback_state_preserved": True,
        "playback_position_advanced": position_advanced,
        "volume_preserved": True,
        "queue_preserved": True,
        "base_context_preserved": True,
        "audio_features_observed": audio_features_observed,
        "audio_backend_outcome": (
            "reactive" if audio_features_observed else "ambient_only"
        ),
        "ambient_fallback_verified": True,
        "static_default_migrated": True,
        "static_timer_stopped": True,
        "six_presets_verified": True,
        "pulse_verified": True,
        "starfield_verified": True,
        "aurora_verified": True,
        "orb_cluster_verified": True,
        "fireworks_verified": True,
        "overlay_controls_verified": True,
        "track_transition_verified": True,
        "lyrics_default_off": True,
        "lyrics_toggle_persisted": True,
        "synced_lyrics_verified": True,
        "plain_lyrics_verified": True,
        "lyrics_cache_verified": True,
        "lyrics_track_transition_verified": True,
        "lyrics_above_controls": True,
        "lyrics_visible_overlay_hidden": True,
        "synthetic_lyrics_provider": True,
        "lyrics_provider_call_count": lyrics_provider.call_count,
        "lyrics_tasks_bounded": True,
        "render_timer_stopped_on_exit": True,
        "analysis_worker_stopped_on_exit": True,
        "status_safe": status_safe,
        "no_pcm_status_fields": True,
        "temporary_output_muted": True,
    }
    setattr(window, "_review_party_metrics", evidence)
    return evidence


class _SyntheticIntelligenceTokenStore:
    """In-memory review consent marker; no credential path is opened."""

    def read(self) -> str:
        return "isolated-review-placeholder"


def _write_synthetic_intelligence_mp3(path: Path) -> None:
    """Create a tiny valid MP3 made only from deterministic synthetic frames."""

    path.parent.mkdir(parents=True, exist_ok=True)
    frame = (
        bytes.fromhex("fffb10c40003c00001a40000002000003480000004")
        + b"LAME3.100"
        + (b"U" * 64)
        + b"LAME3.100U"
    )
    path.write_bytes(frame * 12)


def _write_synthetic_intelligence_png(path: Path, color: str) -> None:
    """Create deterministic review artwork without provider or media access."""

    from PySide6.QtGui import QColor, QImage

    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(24, 24, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    if not image.save(str(path), "PNG"):
        raise ReviewPlanError("Synthetic metadata-intelligence artwork could not be created.")


@dataclass(frozen=True)
class _SyntheticIntelligenceArtworkRecord:
    path: Path
    provider_page_url: str


class _SyntheticIntelligenceArtworkStore:
    """Gap-aware fake store; it never performs network retrieval."""

    def __init__(self, record: _SyntheticIntelligenceArtworkRecord) -> None:
        self.record = record
        self.calls: list[tuple[object, dict[str, object]]] = []

    def fetch_for_gap(self, candidate: object, **kwargs: object):
        from music_vault.metadata.discogs_artwork import is_true_artwork_gap

        self.calls.append((candidate, dict(kwargs)))
        if not is_true_artwork_gap(
            kwargs.get("current_cover_path"),
            manual=bool(kwargs.get("manual")),
            locked=bool(kwargs.get("locked")),
        ):
            return None
        return self.record


class _SyntheticIntelligenceDiscogs:
    def __init__(self) -> None:
        self.calls: list[object] = []

    @staticmethod
    def _candidate(query: object):
        from music_vault.metadata.providers import (
            ProviderArtistCredit,
            ProviderArtworkCandidate,
            ProviderReleaseCandidate,
        )

        title = str(getattr(query, "title", "")).strip()
        folded = title.casefold()
        if folded == "neon notebook session":
            return None
        mapping = {
            "amber circuit": ("Aster Vale", "person", None),
            "glass meridian": ("Lowland Unit", "group", None),
            "cloud geometry": ("Sable Current", "group", "Guest Signal"),
            "static bloom": ("Violet Engine", "group", None),
            "velvet transit": ("Velvet Transit Unit", "group", None),
            "paper lantern": ("Copper Horizon", "group", None),
            "quiet prism": ("Juniper Arc", "duo", None),
        }
        artist, entity_type, featured = mapping.get(
            folded, (str(getattr(query, "artist", "") or "Synthetic Unit"), "group", None)
        )
        version = str(getattr(query, "version_type", "") or "unknown").casefold()
        is_live = folded == "static bloom" and version == "live"
        identity = {
            "amber circuit": "71001",
            "glass meridian": "71002",
            "cloud geometry": "71003",
            "static bloom": "71004" if not is_live else "71005",
            "velvet transit": "71006",
            "paper lantern": "71007",
            "quiet prism": "71008",
        }.get(folded, "71999")
        credits = [
            ProviderArtistCredit(
                artist,
                role="primary",
                artist_id=f"8{identity}",
                join_phrase=" feat. " if featured else "",
                entity_type=entity_type,
            )
        ]
        if featured:
            credits.append(
                ProviderArtistCredit(
                    featured,
                    role="featured",
                    artist_id="8710031",
                    entity_type="person",
                )
            )
        score = 96.0
        artwork = None
        if folded in {"paper lantern", "quiet prism"}:
            artwork = ProviderArtworkCandidate(
                source_url=f"https://i.discogs.com/synthetic-{identity}.png",
                provider_page_url=f"https://www.discogs.com/release/{identity}",
                release_id=identity,
                image_type="front",
                width=24,
                height=24,
            )
        return ProviderReleaseCandidate(
            provider="discogs",
            title=title,
            artist=artist,
            artist_credits=tuple(credits),
            album="Synthetic Catalogue Release",
            album_artist=artist,
            release_date="1987",
            original_release_date="1984",
            version_type="live" if is_live else "studio",
            version_label="Live at Synthetic Hall" if is_live else None,
            provider_score=score,
            release_id=identity,
            master_id="72004" if folded == "static bloom" else f"72{identity}",
            track_position="A1",
            label="Synthetic Catalogue Label",
            provider_reference=f"https://www.discogs.com/release/{identity}",
            artwork=artwork,
            is_official=not is_live,
            field_scores={
                name: score
                for name in (
                    "title",
                    "artist",
                    "artist_credits",
                    "album",
                    "album_artist",
                    "release_date",
                    "original_release_date",
                    "version_type",
                    "version_label",
                    "discogs_release_id",
                    "discogs_master_id",
                    "discogs_track_position",
                )
            },
        )

    def search(self, query: object, *, cancel_event=None):
        self.calls.append(query)
        candidate = self._candidate(query)
        return () if candidate is None else (candidate,)


class _SyntheticIntelligenceMusicBrainz:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def search(self, title: str, artist: str | None = None, *, cancel_event=None):
        from music_vault.metadata.musicbrainz_enricher import MetadataCandidate

        self.calls.append((title, artist))
        folded = str(title).casefold()
        if folded in {"neon notebook session", "static bloom"}:
            return ()
        mapping = {
            "amber circuit": "Aster Vale",
            "glass meridian": "Lowland Unit",
            "cloud geometry": "Sable Current",
            "velvet transit": "Velvet Transit Unit Alternate",
        }
        canonical_artist = mapping.get(folded, artist or "Synthetic Unit")
        candidate_title = (
            "Velvet Transit Alternate" if folded == "velvet transit" else title
        )
        return (
            MetadataCandidate(
                title=candidate_title,
                artist=canonical_artist,
                album="Synthetic Catalogue Release",
                release_date="1987",
                recording_id=f"synthetic-recording-{len(self.calls)}",
                release_id=f"synthetic-release-{len(self.calls)}",
                score=96,
                album_artist=canonical_artist,
            ),
        )


def validate_metadata_intelligence_review_behaviors(
    window: object,
    plan: ReviewPlan,
) -> dict[str, object]:
    """Run representative Batch 10.1 behavior inside the isolated app process."""

    from music_vault.metadata.intelligence import MetadataIntelligenceService
    from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore

    if os.environ.get("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "").strip() != "1":
        raise ReviewPlanError("Metadata-intelligence review requires no-secret mode.")
    database_path = Path(str(window.db.db_path)).resolve()
    _ensure_under_runtime(database_path, plan.runtime_root, "intelligence database")
    if int(window.db.conn.execute("PRAGMA user_version").fetchone()[0]) != 6:
        raise ReviewPlanError("Metadata-intelligence review requires schema 6.")
    api_key = plan.runtime_root / "data" / "youtube_api_key.txt"
    discogs_token = plan.runtime_root / "data" / "discogs_token.txt"
    if api_key.exists() or discogs_token.exists():
        raise ReviewPlanError("Synthetic review runtime unexpectedly contains a credential file.")

    memberships_before = int(
        window.db.conn.execute("SELECT COUNT(*) FROM playlist_track_origins").fetchone()[0]
    )
    synthetic_media = plan.runtime_root / "synthetic_media"
    exact_media = synthetic_media / "intelligence-exact.mp3"
    artwork_gap_media = synthetic_media / "intelligence-art-gap.mp3"
    existing_artwork = plan.runtime_root / "data" / "covers" / "synthetic-existing.png"
    replacement_artwork = (
        plan.runtime_root / "data" / "covers" / "discogs" / "synthetic-front.png"
    )
    for path in (exact_media, artwork_gap_media, existing_artwork, replacement_artwork):
        _ensure_under_runtime(path, plan.runtime_root, "synthetic intelligence artifact")
    _write_synthetic_intelligence_mp3(exact_media)
    _write_synthetic_intelligence_mp3(artwork_gap_media)
    _write_synthetic_intelligence_png(existing_artwork, "#22aa66")
    _write_synthetic_intelligence_png(replacement_artwork, "#805cff")

    from music_vault.metadata.tag_writer import (
        SafeTagWriter,
        TagWriteError,
        full_file_sha256,
        inspect_mp3,
    )

    class _ReviewTagWriter(SafeTagWriter):
        def __init__(self) -> None:
            self.last_error: str | None = None

        def _capture(self, operation, *args, **kwargs):
            try:
                return operation(*args, **kwargs)
            except TagWriteError as exc:
                cause = f":{exc.__cause__.__class__.__name__}" if exc.__cause__ else ""
                self.last_error = f"{exc}{cause}"
                raise

        def create_backup(self, *args, **kwargs):
            return self._capture(super().create_backup, *args, **kwargs)

        def prepare(self, *args, **kwargs):
            return self._capture(super().prepare, *args, **kwargs)

        def commit(self, *args, **kwargs):
            return self._capture(super().commit, *args, **kwargs)

    media_before = {
        "exact": inspect_mp3(exact_media),
        "art_gap": inspect_mp3(artwork_gap_media),
    }
    specifications = (
        ("exact", "Aster Vale - Amber Circuit", "Random Archive"),
        ("label", "Lowland Unit - Glass Meridian", "Synthetic Records"),
        ("featured", "Sable Current feat. Guest Signal - Cloud Geometry", "Fan Archive"),
        ("studio", "Violet Engine - Static Bloom", "Video Archive"),
        ("live", "Violet Engine - Static Bloom (Live at Synthetic Hall)", "Audience Capture"),
        ("conflict", "Velvet Transit Unit - Velvet Transit", "Loose Upload"),
        ("exclusive", "Independent Channel - Neon Notebook Session", "Independent Channel"),
        ("art_gap", "Copper Horizon - Paper Lantern", "Fan Mirror"),
        ("art_existing", "Juniper Arc - Quiet Prism", "Archive Channel"),
    )
    track_ids: dict[str, int] = {}
    with window.db.conn:
        for index, (key, title, uploader) in enumerate(specifications):
            if key == "exact":
                path = exact_media
            elif key == "art_gap":
                path = artwork_gap_media
            else:
                path = synthetic_media / f"intelligence-{index}.synthetic-audio"
            _ensure_under_runtime(path, plan.runtime_root, "synthetic intelligence media path")
            track_ids[key] = window.db.upsert_track(
                path,
                title=title,
                artist=uploader,
                album="Imported Placeholder",
                source_kind="youtube",
                source_video_id=f"mi{index:09d}",
                duration_seconds=210.0 + index,
                cover_path=(str(existing_artwork) if key == "art_existing" else None),
                commit=False,
            )
    store = MetadataIntelligenceJobStore(window.db)
    job_id = store.create_existing_library_job(tuple(track_ids.values()))
    discogs = _SyntheticIntelligenceDiscogs()
    musicbrainz = _SyntheticIntelligenceMusicBrainz()
    artwork_store = _SyntheticIntelligenceArtworkStore(
        _SyntheticIntelligenceArtworkRecord(
            path=replacement_artwork,
            provider_page_url="https://www.discogs.com/release/71007",
        )
    )
    tag_writer = _ReviewTagWriter()
    settings = {
        "metadata_intelligence_enabled": True,
        "metadata_discogs_enabled": True,
        "metadata_musicbrainz_secondary_enabled": True,
        "metadata_writeback_enabled": True,
        "metadata_fill_missing_artwork_enabled": True,
        "metadata_scan_existing_after_setup": False,
        "metadata_intelligence_consent_version": 1,
        "metadata_discogs_consent_version": 1,
    }
    service = MetadataIntelligenceService(
        window.db,
        settings,
        token_store=_SyntheticIntelligenceTokenStore(),
        discogs_provider_factory=lambda _token: discogs,
        musicbrainz_provider_factory=lambda: musicbrainz,
        tag_writer=tag_writer,
        artwork_store_factory=lambda _token: artwork_store,
    )
    result = service.analyze_existing_library()
    rows = {key: window.db.get_track(track_id) for key, track_id in track_ids.items()}
    exact_corrected = bool(
        rows["exact"]["title"] == "Amber Circuit"
        and rows["exact"]["artist"] == "Aster Vale"
        and rows["exact"]["release_date"] == "1987"
        and rows["exact"]["original_release_date"] == "1984"
    )
    label_credit_count = int(
        window.db.conn.execute(
            """
            SELECT COUNT(*) FROM track_artist_credits c JOIN artists a ON a.id=c.artist_id
            WHERE c.track_id=? AND lower(a.display_name)=lower('Synthetic Records')
            """,
            (track_ids["label"],),
        ).fetchone()[0]
    )
    featured = [
        tuple(row)
        for row in window.db.conn.execute(
            """
            SELECT a.display_name,c.role,a.entity_type FROM track_artist_credits c
            JOIN artists a ON a.id=c.artist_id WHERE c.track_id=? ORDER BY c.credit_order
            """,
            (track_ids["featured"],),
        )
    ]
    group_and_featured = bool(
        featured == [
            ("Sable Current", "primary", "group"),
            ("Guest Signal", "featured", "person"),
        ]
    )
    studio_live_preserved = bool(
        rows["studio"]["id"] != rows["live"]["id"]
        and rows["studio"]["version_type"] == "studio"
        and rows["live"]["version_type"] == "live"
        and rows["live"]["release_date"] in (None, "")
        and rows["live"]["original_release_date"] == "1984"
    )
    states = {
        str(row["reason"]): str(row["state"])
        for row in window.db.conn.execute(
            "SELECT reason,state FROM metadata_intelligence_items WHERE job_id=?",
            (job_id,),
        )
    }
    # Item reasons are all existing_library, so address the two decision rows by track.
    conflict_state = str(
        window.db.conn.execute(
            "SELECT state FROM metadata_intelligence_items WHERE job_id=? AND track_id=?",
            (job_id, track_ids["conflict"]),
        ).fetchone()[0]
    )
    exclusive_state = str(
        window.db.conn.execute(
            "SELECT state FROM metadata_intelligence_items WHERE job_id=? AND track_id=?",
            (job_id, track_ids["exclusive"]),
        ).fetchone()[0]
    )
    memberships_after = int(
        window.db.conn.execute("SELECT COUNT(*) FROM playlist_track_origins").fetchone()[0]
    )
    item_results = {
        int(row["track_id"]): (str(row["file_write_result"]), str(row["artwork_result"]))
        for row in window.db.conn.execute(
            """
            SELECT track_id,file_write_result,artwork_result
            FROM metadata_intelligence_items WHERE job_id=?
            """,
            (job_id,),
        )
    }
    media_after = {
        "exact": inspect_mp3(exact_media),
        "art_gap": inspect_mp3(artwork_gap_media),
    }
    from mutagen.id3 import ID3

    if tag_writer.last_error:
        raise ReviewPlanError(
            f"Synthetic metadata-intelligence tag write failed: {tag_writer.last_error}"
        )
    exact_tags = ID3(exact_media)
    gap_tags = ID3(artwork_gap_media)
    writeback_verified = bool(
        item_results[track_ids["exact"]][0] == "verified"
        and item_results[track_ids["art_gap"]][0] == "verified"
        and str(exact_tags.getall("TIT2")[0].text[0]) == "Amber Circuit"
        and str(exact_tags.getall("TPE1")[0].text[0]) == "Aster Vale"
        and str(gap_tags.getall("TIT2")[0].text[0]) == "Paper Lantern"
        and str(gap_tags.getall("TPE1")[0].text[0]) == "Copper Horizon"
    )
    audio_unchanged = all(
        media_after[key].audio_payload_sha256 == media_before[key].audio_payload_sha256
        and media_after[key].codec == media_before[key].codec
        and abs(media_after[key].duration_seconds - media_before[key].duration_seconds) <= 0.05
        for key in media_before
    )
    backup_root = database_path.parent / "backups" / "metadata_jobs" / str(job_id)
    backup_files = sorted(path for path in backup_root.glob("*.mp3") if path.is_file())
    expected_backup_hashes = sorted(
        (media_before["exact"].full_sha256, media_before["art_gap"].full_sha256)
    )
    exact_backup_verified = bool(
        len(backup_files) == 2
        and sorted(full_file_sha256(path) for path in backup_files)
        == expected_backup_hashes
    )
    from music_vault.metadata.service import MetadataService

    artwork_state = MetadataService(window.db).snapshot(track_ids["art_gap"]).fields["artwork"]
    missing_artwork_filled = bool(
        Path(str(rows["art_gap"]["cover_path"])).resolve() == replacement_artwork.resolve()
        and item_results[track_ids["art_gap"]][1] == "filled"
        and artwork_state.provider_reference == "https://www.discogs.com/release/71007"
    )
    existing_artwork_preserved = bool(
        Path(str(rows["art_existing"]["cover_path"])).resolve() == existing_artwork.resolve()
        and item_results[track_ids["art_existing"]][1] == "preserved_existing"
    )
    artwork_not_embedded = not exact_tags.getall("APIC") and not gap_tags.getall("APIC")
    all_track_paths_confined = all(
        Path(str(row["path"])).resolve().is_relative_to(plan.runtime_root.resolve())
        for row in window.db.conn.execute("SELECT path FROM tracks")
    )
    evidence: dict[str, object] = {
        "packaged_process": bool(getattr(sys, "frozen", False)),
        "schema_6": True,
        "exact_random_uploader_corrected": exact_corrected,
        "label_excluded_from_artist_credits": label_credit_count == 0,
        "group_and_featured_credits_structured": group_and_featured,
        "studio_live_tracks_remain_separate": studio_live_preserved,
        "unofficial_live_year_blank_original_date_separate": studio_live_preserved,
        "provider_conflict_requires_review": conflict_state == "review",
        "youtube_exclusive_fallback_reviewed": (
            exclusive_state == "review"
            and rows["exclusive"]["version_type"] == "youtube_exclusive"
        ),
        "source_memberships_preserved": memberships_after == memberships_before,
        "network_guard_active": _REVIEW_NETWORK_GUARD_INSTALLED,
        "network_attempt_count": len(_REVIEW_NETWORK_EVENTS),
        "no_secret_files": not api_key.exists() and not discogs_token.exists(),
        "synthetic_media_writes_confined_to_runtime": all_track_paths_confined,
        "file_writeback_enabled": settings["metadata_writeback_enabled"] is True,
        "high_confidence_tag_writeback_verified": writeback_verified,
        "exact_file_backups_verified": exact_backup_verified,
        "audio_payload_unchanged": audio_unchanged,
        "artwork_gap_fill_enabled": settings["metadata_fill_missing_artwork_enabled"] is True,
        "missing_artwork_filled": missing_artwork_filled,
        "valid_existing_artwork_preserved": existing_artwork_preserved,
        "artwork_attribution_persisted": missing_artwork_filled,
        "discogs_artwork_not_embedded": artwork_not_embedded,
        "artwork_store_call_count": len(artwork_store.calls),
        "discogs_query_count": len(discogs.calls),
        "musicbrainz_query_count": len(musicbrainz.calls),
        "processed_count": result.processed,
    }
    required = (
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
    )
    if any(evidence[name] is not True for name in required):
        raise ReviewPlanError("Synthetic packaged metadata-intelligence behavior failed.")
    if evidence["network_attempt_count"] != 0 or result.processed != len(specifications):
        raise ReviewPlanError("Synthetic metadata-intelligence work was incomplete or attempted network access.")
    setattr(window, "_review_metadata_intelligence_metrics", evidence)
    return evidence


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


def _batch10_review_sources() -> tuple[dict[str, object], ...]:
    """Return display-only source cards containing no personal identifiers."""

    common = {
        "source_kind": "youtube_playlist",
        "last_sync_at": "2026-07-15T18:30:00Z",
        "last_visible_count": 0,
        "last_new_count": 0,
        "last_imported_count": 0,
        "last_error": None,
    }
    return (
        {
            **common,
            "id": 7101,
            "external_id": "PLSYNTHETIC_SOURCE_A_001",
            "source_url": "https://www.youtube.com/playlist?list=PLSYNTHETIC_SOURCE_A_001",
            "label": "Morning Rotation",
            "remote_title": "Synthetic Source Alpha",
            "enabled": True,
            "sort_order": 0,
            "destination_kind": "playlist",
            "destination_playlist_id": 8101,
            "destination_playlist_name": "Morning Rotation Mix",
            "storage_key": "youtube_source_a_2f2fd3d001",
            "last_sync_status": "complete",
            "last_downloaded_count": 4,
            "last_imported_count": 4,
            "last_existing_count": 18,
            "last_failed_count": 0,
            "unresolved_failure_count": 0,
        },
        {
            **common,
            "id": 7102,
            "external_id": "PLSYNTHETIC_SOURCE_B_002",
            "source_url": "https://www.youtube.com/playlist?list=PLSYNTHETIC_SOURCE_B_002",
            "label": "Late Night Finds",
            "remote_title": "Synthetic Source Beta",
            "enabled": True,
            "sort_order": 1,
            "destination_kind": "playlist",
            "destination_playlist_id": 8102,
            "destination_playlist_name": "Late Night Finds",
            "storage_key": "youtube_source_b_5a52ca9202",
            "last_sync_status": "complete_with_issues",
            "last_downloaded_count": 2,
            "last_imported_count": 2,
            "last_existing_count": 11,
            "last_failed_count": 1,
            "unresolved_failure_count": 1,
            "last_error": "One unavailable playlist item remains recorded for review.",
        },
        {
            **common,
            "id": 7103,
            "external_id": "PLSYNTHETIC_SOURCE_C_003",
            "source_url": "https://www.youtube.com/playlist?list=PLSYNTHETIC_SOURCE_C_003",
            "label": "Library Discovery",
            "remote_title": "Synthetic Source Gamma",
            "enabled": False,
            "sort_order": 2,
            "destination_kind": "library",
            "destination_playlist_id": None,
            "destination_playlist_name": None,
            "storage_key": "youtube_source_c_a8e3f17c03",
            "last_sync_status": "failed",
            "last_downloaded_count": 0,
            "last_imported_count": 0,
            "last_existing_count": 7,
            "last_failed_count": 1,
            "unresolved_failure_count": 1,
            "last_error": "The synthetic provider was temporarily unavailable.",
        },
    )


def _batch10_review_summary() -> dict[str, int]:
    return {
        "enabled_sources": 2,
        "completed_sources": 1,
        "issue_sources": 1,
        "failed_sources": 1,
        "downloaded": 6,
        "existing": 36,
        "failed_items": 2,
    }


def _batch10_review_runs() -> tuple[dict[str, object], ...]:
    return (
        {
            "status": "complete_with_issues",
            "finished_at": "2026-07-15T18:30:00Z",
            "downloaded_count": 2,
            "existing_count": 11,
            "failed_count": 1,
        },
        {
            "status": "complete",
            "finished_at": "2026-07-14T18:30:00Z",
            "downloaded_count": 1,
            "existing_count": 12,
            "failed_count": 0,
        },
    )


def _batch10_review_failures() -> tuple[dict[str, str], ...]:
    return (
        {
            "title": "Unavailable synthetic playlist item",
            "reason": "This item is unavailable through the supported public/unlisted workflow.",
        },
    )


def _sync_center_widget(window: object):
    widget = getattr(window, "sync_center", None)
    if widget is None or not callable(getattr(widget, "apply_review_state", None)):
        raise ReviewPlanError("The persistent Sync Center review surface is unavailable.")
    return widget


def _close_review_sync_dialog(window: object) -> None:
    for attribute in (
        "_review_sync_source_dialog",
        "_review_sync_remove_confirmation",
    ):
        dialog = getattr(window, attribute, None)
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
        setattr(window, attribute, None)


def _batch10_review_normalizer(value: str):
    from music_vault.core.sync_sources import NormalizedYouTubeSource

    external_id = str(value or "").strip() or "PLSYNTHETIC_NEW_SOURCE_004"
    return NormalizedYouTubeSource(
        external_id=external_id,
        source_url=f"https://www.youtube.com/playlist?list={external_id}",
    )


def _prepare_batch10_sync_scene(window: object, scene: str) -> None:
    from music_vault.ui.sync_center import RemoveSourceDialog, SourceEditorDialog

    sources = _batch10_review_sources()
    summary = _batch10_review_summary()
    _set_page(window, "sync_page")
    widget = _sync_center_widget(window)
    if scene == "sync_sources_empty":
        widget.apply_review_state("empty")
        return

    state = {
        "sync_all_running": "syncing",
        "sync_complete_issues": "complete_with_issues",
        "sync_source_failures": "source_failures",
    }.get(scene, "sources")
    widget.apply_review_state(
        state,
        sources=sources,
        summary=summary,
        runs=_batch10_review_runs(),
        failures=(
            _batch10_review_failures() if scene == "sync_source_failures" else ()
        ),
        activity=(
            "Source 1 complete: 4 downloaded, 18 existing.\n"
            "Source 2 complete with one unavailable item.\n"
            "No network request was made by this review fixture."
        ),
    )
    if scene == "sync_source_failures":
        widget.detail_tabs.setCurrentWidget(widget.failure_history)
    if scene == "sync_source_add":
        dialog = SourceEditorDialog(
            playlists=(
                {"id": 8201, "name": "Synthetic Destination", "managing_source_id": None},
            ),
            normalize_source=_batch10_review_normalizer,
            parent=window,
        )
        dialog.source_value.setText("PLSYNTHETIC_NEW_SOURCE_004")
        dialog.label.setText("Weekend Discoveries")
        dialog.destination.setCurrentIndex(1)
        dialog.new_playlist_name.setText("Weekend Discoveries")
        setattr(window, "_review_sync_source_dialog", dialog)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
    elif scene == "sync_source_edit":
        dialog = SourceEditorDialog(
            source=sources[0],
            playlists=(
                {
                    "id": 8101,
                    "name": "Morning Rotation Mix",
                    "managing_source_id": 7101,
                },
            ),
            normalize_source=_batch10_review_normalizer,
            parent=window,
        )
        setattr(window, "_review_sync_source_dialog", dialog)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
    elif scene == "sync_source_remove":
        dialog = RemoveSourceDialog(sources[0], parent=window)
        setattr(window, "_review_sync_remove_confirmation", dialog)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()


def _prepare_batch10_managed_playlist_scene(window: object) -> None:
    row = window.db.conn.execute(
        """
        SELECT sources.destination_playlist_id, playlists.name
        FROM sync_sources AS sources
        JOIN playlists ON playlists.id=sources.destination_playlist_id
        WHERE sources.archived_at IS NULL
          AND sources.destination_kind='playlist'
        ORDER BY sources.sort_order, sources.id
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise ReviewPlanError("The synthetic managed-playlist scenario is unavailable.")
    playlist_id = int(row["destination_playlist_id"])
    playlist_name = str(row["name"])
    _set_page(window, "library_page")
    window.current_view_kind = "custom"
    window.current_playlist_id = playlist_id
    window.current_playlist_name = playlist_name
    window.load_library(
        window.db.get_playlist_tracks(playlist_id),
        playlist_name,
        "Managed by a saved source. Manual additions remain after synchronized tracks.",
    )
    refresh = getattr(window, "update_managed_playlist_presentation", None)
    if callable(refresh):
        refresh()


def _batch10_playlist_video_order(db: object, playlist_id: int) -> tuple[str, ...]:
    rows = db.conn.execute(
        """
        SELECT tracks.source_video_id, tracks.title
        FROM playlist_tracks
        JOIN tracks ON tracks.id=playlist_tracks.track_id
        WHERE playlist_tracks.playlist_id=?
        ORDER BY playlist_tracks.position, tracks.id
        """,
        (int(playlist_id),),
    ).fetchall()
    return tuple(
        str(row["source_video_id"] or row["title"] or "manual") for row in rows
    )


def _batch10_playback_snapshot(window: object) -> dict[str, object]:
    player = getattr(window, "player", None)
    return {
        "player": player,
        "player_state": player.playbackState() if player is not None else None,
        "player_source": player.source() if player is not None else None,
        "current_track_id": getattr(window, "current_track_id", None),
        "manual_queue": tuple(getattr(window, "manual_queue", ())),
        "base_playback_context": copy.deepcopy(
            getattr(window, "base_playback_context", None)
        ),
        "party_mode_window": getattr(window, "party_mode_window", None),
        "party_mode_active": bool(getattr(window, "party_mode_active", False)),
        "party_mode_preset": (
            getattr(window, "config", {}).get("party_mode_preset")
            if isinstance(getattr(window, "config", None), dict)
            else None
        ),
        "party_lyrics_enabled": (
            getattr(window, "config", {}).get("party_mode_lyrics_enabled")
            if isinstance(getattr(window, "config", None), dict)
            else None
        ),
        "lyrics_online_enabled": (
            getattr(window, "config", {}).get("lyrics_online_lookup_enabled")
            if isinstance(getattr(window, "config", None), dict)
            else None
        ),
    }


def _batch10_playback_unchanged(window: object, before: dict[str, object]) -> bool:
    after = _batch10_playback_snapshot(window)
    return all(
        (
            after[name] is before[name]
            if name in {"player", "party_mode_window"}
            else after[name] == before[name]
        )
        for name in before
    )


def validate_batch10_multi_source_behaviors(
    window: object,
    plan: ReviewPlan,
) -> dict[str, object]:
    """Exercise the complete Batch 10 scenario with injected offline providers.

    This hook runs only for an explicit, isolated UI-review plan. It writes no
    production fixture, opens no network connection, and never reads a key.
    A marker lets the harness launch one fresh process per capture without
    repeating the database scenario against the same disposable runtime.
    """

    marker = plan.runtime_root / "data" / _BATCH10_REVIEW_MARKER
    _ensure_under_runtime(marker, plan.runtime_root, "batch10_review_marker")
    if marker.is_file():
        try:
            cached = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReviewPlanError("The Batch 10 synthetic smoke marker is invalid.") from exc
        if not isinstance(cached, dict) or cached.get("scenario_completed") is not True:
            raise ReviewPlanError("The Batch 10 synthetic smoke marker is incomplete.")
        return cached

    if os.environ.get("MUSIC_VAULT_DISABLE_NETWORK") != "1":
        raise ReviewPlanError("Batch 10 smoke requires the explicit network-disabled runtime.")
    if (plan.runtime_root / "data" / "youtube_api_key.txt").exists():
        raise ReviewPlanError("Batch 10 smoke found an unexpected API-key file.")

    from PySide6.QtMultimedia import QMediaPlayer

    from music_vault.core.multi_source_sync import MultiSourceSyncOrchestrator
    from music_vault.core.playlist_membership import PlaylistMembershipService
    from music_vault.core.sync_result import (
        PlaylistSnapshot,
        PlaylistSnapshotItem,
        SyncFailure,
        SyncImportItem,
        SyncResult,
        utc_now,
    )
    from music_vault.core.sync_sources import SyncSourceService
    from music_vault.ui.sync_center import multi_source_status_payload

    db = window.db
    playback_before = _batch10_playback_snapshot(window)
    media_players_before = tuple(window.findChildren(QMediaPlayer))
    data_root = plan.runtime_root / "data"
    downloads = data_root / "youtube_downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    membership = PlaylistMembershipService(db)
    source_service = SyncSourceService(db, membership_service=membership)

    manual_path = data_root / "synthetic_sentinels" / "batch10_manual_x.synthetic-audio"
    manual_path.parent.mkdir(parents=True, exist_ok=True)
    manual_path.write_bytes(b"synthetic-batch10-manual-track\n")
    manual_track_id = db.upsert_track(
        manual_path,
        title="Manual Track X",
        artist="Synthetic Review",
        album="Batch 10 Offline Fixture",
        source_kind="local",
    )
    playlist_a = db.create_playlist("Batch 10 Managed A")
    playlist_b = db.create_playlist("Batch 10 Managed B")
    db.add_track_to_playlist(playlist_a, manual_track_id)

    source_a = source_service.create_source(
        "PLSYNTHETIC_SOURCE_A_001",
        label="Synthetic Source A",
        destination_kind="playlist",
        destination_playlist_id=playlist_a,
    )
    source_b = source_service.create_source(
        "PLSYNTHETIC_SOURCE_B_002",
        label="Synthetic Source B",
        destination_kind="playlist",
        destination_playlist_id=playlist_b,
    )
    source_c = source_service.create_source(
        "PLSYNTHETIC_SOURCE_C_003",
        label="Synthetic Source C",
        destination_kind="library",
    )
    source_service.reorder((source_a.id, source_b.id, source_c.id))
    added_source = source_service.get(source_a.id)
    stable_external_id = added_source.external_id
    stable_storage_key = added_source.storage_key
    edited_library = source_service.update_source(
        source_a.id,
        label="Synthetic Source A Edited",
        enabled=False,
        destination_kind="library",
    )
    edited_managed = source_service.update_source(
        source_a.id,
        label="Synthetic Source A",
        enabled=True,
        destination_kind="playlist",
        destination_playlist_id=playlist_a,
    )
    source_service.reorder((source_c.id, source_b.id, source_a.id))
    reordered_ids = tuple(source.id for source in source_service.list_active())
    source_service.reorder((source_a.id, source_b.id, source_c.id))
    restored_ids = tuple(source.id for source in source_service.list_active())
    add_source_persisted = bool(
        len(restored_ids) == 3
        and added_source.destination_playlist_id == playlist_a
        and added_source.destination_kind == "playlist"
    )
    edit_source_persisted = bool(
        edited_library.label == "Synthetic Source A Edited"
        and edited_library.enabled is False
        and edited_library.destination_kind == "library"
        and edited_library.destination_playlist_id is None
        and edited_managed.label == "Synthetic Source A"
        and edited_managed.enabled is True
        and edited_managed.destination_kind == "playlist"
        and edited_managed.destination_playlist_id == playlist_a
        and reordered_ids == (source_c.id, source_b.id, source_a.id)
        and restored_ids == (source_a.id, source_b.id, source_c.id)
    )
    edit_identity_stable = bool(
        edited_library.external_id
        == edited_managed.external_id
        == stable_external_id
    )
    edit_storage_key_stable = bool(
        edited_library.storage_key
        == edited_managed.storage_key
        == stable_storage_key
    )

    videos = {
        "A": "synvideoA01",
        "B": "synvideoB01",
        "C": "synvideoC01",
        "D": "synvideoD01",
        "E": "synvideoE01",
        "F": "synvideoF01",
        "U": "unavail0001",
    }
    snapshots: dict[int, tuple[tuple[str, str | None, str | None], ...]] = {
        source_a.id: (
            ("a-item-a", videos["A"], None),
            ("a-item-b-first", videos["B"], None),
            ("a-item-b-duplicate", videos["B"], None),
            ("a-item-c", videos["C"], None),
        ),
        source_b.id: (
            ("b-item-b", videos["B"], None),
            ("b-item-d", videos["D"], None),
            ("b-item-unavailable", videos["U"], "Unavailable synthetic item."),
        ),
        source_c.id: (("c-item-e", videos["E"], None),),
    }
    second_a_snapshot = (
        ("a-item-c", videos["C"], None),
        ("a-item-b-first", videos["B"], None),
        ("a-item-f", videos["F"], None),
    )
    source_calls: dict[int, int] = {}
    download_counts: dict[str, int] = {}
    provider_calls: list[int] = []

    class _OfflineSyncer:
        def __init__(self, config, _progress) -> None:
            self.config = config

        def sync(self) -> SyncResult:
            source_id = int(self.config.saved_source_id)
            provider_calls.append(source_id)
            call = source_calls.get(source_id, 0) + 1
            source_calls[source_id] = call
            if source_id == source_b.id and call >= 2:
                return SyncResult.failed_result(
                    "Synthetic top-level provider failure.",
                    playlist_id=source_b.external_id,
                    playlist_title="Synthetic Source B",
                    saved_source_id=source_id,
                    snapshot=PlaylistSnapshot.failed(
                        "Synthetic top-level provider failure.",
                        playlist_id=source_b.external_id,
                    ),
                )

            definitions = (
                second_a_snapshot
                if source_id == source_a.id and call >= 2
                else snapshots[source_id]
            )
            source = source_service.get(source_id)
            snapshot_items = tuple(
                PlaylistSnapshotItem(
                    source_item_id=item_id,
                    video_id=video_id,
                    source_position=position,
                    title=f"Synthetic Item {position + 1}",
                    availability_reason=unavailable,
                )
                for position, (item_id, video_id, unavailable) in enumerate(definitions)
            )
            snapshot = PlaylistSnapshot.completed(
                source.external_id,
                f"Synthetic Remote {source_id}",
                snapshot_items,
            )
            result = SyncResult(
                status="complete",
                playlist_id=source.external_id,
                playlist_title=f"Synthetic Remote {source_id}",
                visible_item_count=len(snapshot_items),
                saved_source_id=source_id,
                source_label=source.display_label,
                snapshot=snapshot,
                duplicate_occurrence_count=snapshot.duplicate_occurrence_count,
            )
            known = set(self.config.existing_video_ids)
            known.update(
                video_id
                for video_id, path in self.config.known_downloads or ()
                if Path(path).is_file()
            )
            occurrence_ids: dict[str, list[str]] = {}
            for item in snapshot.items:
                if item.video_id:
                    occurrence_ids.setdefault(item.video_id, []).append(item.source_item_id)
            processed: set[str] = set()
            for item in snapshot.items:
                if item.availability_reason:
                    result.add_failure(
                        SyncFailure(
                            item.video_id,
                            item.title,
                            item.availability_reason,
                            "unavailable",
                            item.source_item_id,
                        )
                    )
                    continue
                if not item.video_id or item.video_id in processed:
                    continue
                processed.add(item.video_id)
                if item.video_id in known:
                    result.existing_count += 1
                    result.successful_video_ids.add(item.video_id)
                    continue
                destination = Path(self.config.source_destination_dir)
                destination.mkdir(parents=True, exist_ok=True)
                media = destination / f"{item.video_id}.synthetic-audio"
                media.write_bytes(b"synthetic-batch10-source-track\n")
                result.new_item_count += 1
                result.downloaded_count += 1
                result.downloaded_paths.append(str(media))
                result.import_items.append(
                    SyncImportItem(
                        str(media),
                        item.video_id,
                        source_item_ids=tuple(occurrence_ids[item.video_id]),
                    )
                )
                result.successful_video_ids.add(item.video_id)
                download_counts[item.video_id] = download_counts.get(item.video_id, 0) + 1
                known.add(item.video_id)
            result.finished_at = utc_now()
            result.refresh_status()
            return result

    def syncer_factory(config, progress):
        return _OfflineSyncer(config, progress)

    def importer(target_db, item: SyncImportItem) -> int:
        return target_db.upsert_track(
            item.path,
            title=f"Synthetic Track {item.video_id}",
            artist="Synthetic Review",
            album="Batch 10 Offline Fixture",
            source_kind="youtube",
            source_video_id=item.video_id,
        )

    transitions: list[dict[str, object]] = []

    def orchestrator() -> MultiSourceSyncOrchestrator:
        return MultiSourceSyncOrchestrator(
            db,
            downloads,
            archive_file=data_root / "synthetic_batch10_archive.txt",
            source_service=source_service,
            membership_service=membership,
            syncer_factory=syncer_factory,
            importer=importer,
            transition_callback=lambda values: transitions.append(dict(values)),
        )

    first = orchestrator().sync_all_enabled()
    first_a_order = _batch10_playlist_video_order(db, playlist_a)
    first_b_order = _batch10_playlist_video_order(db, playlist_b)
    duplicate_rows = int(
        db.conn.execute(
            "SELECT COUNT(*) FROM sync_source_items "
            "WHERE source_id=? AND video_id=? AND removed_at IS NULL",
            (source_a.id, videos["B"]),
        ).fetchone()[0]
    )

    second = orchestrator().sync_selected((source_a.id,))
    second_a_order = _batch10_playlist_video_order(db, playlist_a)
    track_a = db.get_track_by_source_video_id(videos["A"])
    track_a_preserved = bool(track_a and Path(str(track_a["path"])).is_file())

    before_failure_b = _batch10_playlist_video_order(db, playlist_b)
    failed_b = orchestrator().sync_selected((source_b.id,))
    after_failure_b = _batch10_playlist_video_order(db, playlist_b)

    source_failure_counts = {
        int(row["sync_source_id"]): int(row["failure_count"])
        for row in db.conn.execute(
            """
            SELECT sync_source_id, COUNT(*) AS failure_count
            FROM sync_failures
            WHERE sync_source_id IS NOT NULL AND status='unresolved'
            GROUP BY sync_source_id
            """
        )
    }
    before_archive_a = _batch10_playlist_video_order(db, playlist_a)
    source_service.archive(source_a.id)
    after_archive_a = _batch10_playlist_video_order(db, playlist_a)

    status_sync = multi_source_status_payload(
        first,
        sync_source_count=3,
        enabled_sync_source_count=3,
    )
    write_status = getattr(window, "write_app_status", None)
    if not callable(write_status):
        raise ReviewPlanError("The App Status writer is unavailable during Batch 10 smoke.")
    write_status({"sync": status_sync})
    status_path = data_root / "music_vault_status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewPlanError("Batch 10 smoke did not produce valid App Status.") from exc
    status_sync_written = status.get("sync") if isinstance(status, dict) else None
    serialized_status = json.dumps(status_sync_written, sort_keys=True)
    forbidden_values = {
        source_a.external_id,
        source_b.external_id,
        source_c.external_id,
        source_a.display_label,
        source_b.display_label,
        source_c.display_label,
        *videos.values(),
    }
    aggregate_only_status = bool(
        isinstance(status_sync_written, dict)
        and status_sync_written.get("last_sync_batch_status")
        == "complete_with_issues"
        and status_sync_written.get("last_sync_playlist_title") is None
        and status_sync_written.get("last_sync_playlist_id") is None
        and status_sync_written.get("last_sync_failures") == []
        and all(value not in serialized_status for value in forbidden_values)
    )

    expected_first_a = (
        videos["A"],
        videos["B"],
        videos["C"],
        "Manual Track X",
    )
    expected_b = (videos["B"], videos["D"])
    expected_second_a = (
        videos["C"],
        videos["B"],
        videos["F"],
        "Manual Track X",
    )
    media_players_after = tuple(window.findChildren(QMediaPlayer))
    playback_preserved = _batch10_playback_unchanged(window, playback_before)
    required_order = [source_a.id, source_b.id, source_c.id, source_a.id, source_b.id]
    behaviors: dict[str, object] = {
        "schema_version": 1,
        "scenario_completed": True,
        "packaged_process": bool(getattr(sys, "frozen", False)),
        "network_attempt_count": len(_REVIEW_NETWORK_EVENTS),
        "api_key_absent": True,
        "add_source_persisted": add_source_persisted,
        "edit_source_persisted": edit_source_persisted,
        "edit_identity_stable": edit_identity_stable,
        "edit_storage_key_stable": edit_storage_key_stable,
        "source_crud": len(source_service.list_active()) == 2,
        "source_order_persisted": provider_calls == required_order,
        "sequential_execution": provider_calls == required_order,
        "source_a_duplicate_occurrences": duplicate_rows == 2,
        "cross_source_single_download": download_counts.get(videos["B"], 0) == 1,
        "first_playlist_a_order": first_a_order == expected_first_a,
        "first_playlist_b_order": first_b_order == expected_b,
        "library_only_source": source_c.destination_kind == "library",
        "unavailable_item_truthful": first.total_failed_items == 1,
        "aggregate_complete_with_issues": first.status == "complete_with_issues",
        "second_snapshot_order": second_a_order == expected_second_a,
        "remote_removal_preserves_media": track_a_preserved,
        "remote_removal_recorded": second.total_removed_occurrences == 2,
        "failed_enumeration_preserves_playlist": (
            failed_b.status == "failed"
            and before_failure_b == expected_b
            and after_failure_b == expected_b
        ),
        "archive_preserves_playlist": (
            before_archive_a == expected_second_a
            and after_archive_a == expected_second_a
        ),
        "source_specific_failures": (
            source_failure_counts.get(source_b.id, 0) == 1
            and source_failure_counts.get(source_a.id, 0) == 0
            and source_failure_counts.get(source_c.id, 0) == 0
        ),
        "aggregate_only_app_status": aggregate_only_status,
        "playback_preserved": playback_preserved,
        "queue_preserved": playback_preserved,
        "base_context_preserved": playback_preserved,
        "party_mode_preserved": (
            playback_preserved
            and callable(getattr(window, "toggle_party_mode", None))
            and getattr(window, "party_mode_btn", None) is not None
        ),
        "lyrics_preserved": playback_preserved,
        "same_media_player": (
            media_players_before == media_players_after
            and len(media_players_after) == 1
            and media_players_after[0] is getattr(window, "player", None)
        ),
        "downloaded_unique_count": len(download_counts),
        "source_run_count": int(
            db.conn.execute("SELECT COUNT(*) FROM sync_source_runs").fetchone()[0]
        ),
        "transition_count": len(transitions),
    }
    required_true = {
        key
        for key in behaviors
        if key
        not in {
            "schema_version",
            "packaged_process",
            "network_attempt_count",
            "downloaded_unique_count",
            "source_run_count",
            "transition_count",
        }
    }
    if any(behaviors.get(key) is not True for key in required_true):
        failed = sorted(key for key in required_true if behaviors.get(key) is not True)
        raise ReviewPlanError(
            "Batch 10 synthetic multi-source behavior failed: " + ", ".join(failed)
        )
    temporary = marker.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(behaviors, indent=2) + "\n", encoding="utf-8")
    temporary.replace(marker)
    return behaviors


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


def _close_review_metadata_intelligence_dialog(window: object) -> None:
    dialog = getattr(window, "_review_metadata_intelligence_dialog", None)
    if dialog is not None:
        dialog.close()
        dialog.deleteLater()
    setattr(window, "_review_metadata_intelligence_dialog", None)


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
    _close_review_metadata_intelligence_dialog(window)
    _close_review_remediation_dialog(window)
    _close_review_sync_dialog(window)
    if scene in METADATA_REVIEW_SCENES:
        _prepare_metadata_scene(window, scene)
    elif scene in METADATA_INTELLIGENCE_REVIEW_SCENES:
        from music_vault.ui.metadata_intelligence import MetadataIntelligenceDialog

        dialog = MetadataIntelligenceDialog(
            window.db,
            getattr(window, "metadata_intelligence_service", None),
            window,
        )
        setattr(window, "_review_metadata_intelligence_dialog", dialog)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
    elif scene in REMEDIATION_REVIEW_SCENES:
        _prepare_remediation_scene(window, scene)
    elif scene == "sync_managed_playlist":
        _prepare_batch10_managed_playlist_scene(window)
    elif scene in MULTI_SOURCE_REVIEW_SCENES:
        _prepare_batch10_sync_scene(window, scene)
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
    elif scene in PARTY_REVIEW_SCENES:
        _set_page(window, "library_page")
        search = getattr(window, "search_box", None)
        if search is not None:
            search.clear()
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

    if scene in PARTY_REVIEW_SCENES:
        return True

    if scene in METADATA_INTELLIGENCE_REVIEW_SCENES:
        dialog = getattr(window, "_review_metadata_intelligence_dialog", None)
        return bool(dialog is not None and dialog.isVisible())

    if scene in {"sync_source_add", "sync_source_edit"}:
        dialog = getattr(window, "_review_sync_source_dialog", None)
        return bool(dialog is not None and dialog.isVisible())
    if scene == "sync_source_remove":
        dialog = getattr(window, "_review_sync_remove_confirmation", None)
        return bool(dialog is not None and dialog.isVisible())

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
    if scene in {"sync_source_add", "sync_source_edit"}:
        dialog = getattr(window, "_review_sync_source_dialog", None)
        if dialog is not None:
            dialog.setFocus(Qt.FocusReason.OtherFocusReason)
        return
    if scene == "sync_source_remove":
        dialog = getattr(window, "_review_sync_remove_confirmation", None)
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


def multi_source_review_metrics(
    window: object,
    scene: str,
) -> dict[str, object] | None:
    if scene not in MULTI_SOURCE_REVIEW_SCENES:
        return None

    from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton

    dialog = getattr(window, "_review_sync_source_dialog", None) or getattr(
        window, "_review_sync_remove_confirmation", None
    )
    if scene == "sync_managed_playlist":
        badge = getattr(window, "playlist_managed_badge", None)
        table = getattr(window, "library_table", None)
        rendered = "\n".join(
            widget.text()
            for widget in window.findChildren(QLabel)
            if widget.isVisible()
        )
        return {
            "state": scene,
            "source_row_count": 0,
            "per_source_widget_count": 0,
            "selected_source_count": 0,
            "enabled_source_count": 0,
            "disabled_source_count": 0,
            "action_button_count": 0,
            "clipped_action_count": 0,
            "dialog_visible": False,
            "dialog_kind": None,
            "private_path_visible": bool(_WINDOWS_PATH_RE.search(rendered)),
            "api_key_field_visible": False,
            "managed_badge_visible": bool(badge is not None and badge.isVisible()),
            "managed_explanation_present": bool(
                badge is not None
                and "managed" in str(badge.text()).casefold()
                and "source" in rendered.casefold()
            ),
            "playlist_track_count": int(table.rowCount()) if table is not None else 0,
            "preservation_message_present": True,
        }

    widget = _sync_center_widget(window)
    source_list = widget.source_list
    buttons = [button for button in widget.findChildren(QPushButton) if button.isVisible()]
    clipped = 0
    for button in buttons:
        top_left = button.mapTo(widget, QPoint(0, 0))
        bounds = QRect(top_left, button.size())
        if not widget.rect().contains(bounds):
            clipped += 1
    per_source_widgets = sum(
        source_list.indexWidget(source_list.model().index(row, 0)) is not None
        for row in range(source_list.count())
    )
    enabled = sum(
        source_list.item(row).checkState() == Qt.CheckState.Checked
        for row in range(source_list.count())
    )
    visible_labels = [
        label.text()
        for label in widget.findChildren(QLabel)
        if label.isVisible()
    ]
    if dialog is not None:
        visible_labels.extend(
            label.text()
            for label in dialog.findChildren(QLabel)
            if label.isVisible()
        )
    rendered = "\n".join(visible_labels)
    api_fields = [
        field
        for owner in (widget, dialog)
        if owner is not None
        for field in owner.findChildren(QLineEdit)
        if field.isVisible()
        and "api key" in str(field.accessibleName() or field.placeholderText()).casefold()
    ]
    preservation = (
        scene != "sync_source_remove"
        or (
            "never deleted" in rendered.casefold()
            and "remain" in rendered.casefold()
        )
    )
    dialog_kind = None
    if dialog is not None and dialog is getattr(window, "_review_sync_source_dialog", None):
        dialog_kind = "source_editor"
    elif dialog is not None and dialog is getattr(
        window, "_review_sync_remove_confirmation", None
    ):
        dialog_kind = "remove_confirmation"
    return {
        "state": scene,
        "source_row_count": int(source_list.count()),
        "per_source_widget_count": int(per_source_widgets),
        "selected_source_count": len(source_list.selectedItems()),
        "enabled_source_count": int(enabled),
        "disabled_source_count": int(source_list.count() - enabled),
        "action_button_count": len(buttons),
        "clipped_action_count": clipped,
        "dialog_visible": bool(dialog is not None and dialog.isVisible()),
        "dialog_kind": dialog_kind,
        "private_path_visible": bool(_WINDOWS_PATH_RE.search(rendered)),
        "api_key_field_visible": bool(api_fields),
        "managed_badge_visible": False,
        "managed_explanation_present": False,
        "playlist_track_count": 0,
        "preservation_message_present": preservation,
        "batch_active": bool(getattr(widget, "_batch_active", False)),
        "status_property": str(widget.progress.property("syncState") or ""),
    }


def party_review_metrics(window: object, scene: str) -> dict[str, object] | None:
    if scene not in PARTY_REVIEW_SCENES:
        return None
    metrics = getattr(window, "_review_party_metrics", None)
    if not isinstance(metrics, dict):
        raise ReviewPlanError("Synthetic Party Mode metrics are unavailable.")
    return dict(metrics)


def _active_review_dialog(window: object):
    return (
        getattr(window, "_review_metadata_dialog", None)
        or getattr(window, "_review_metadata_intelligence_dialog", None)
        or getattr(window, "_review_remediation_dialog", None)
        or getattr(window, "_review_sync_source_dialog", None)
        or getattr(window, "_review_sync_remove_confirmation", None)
    )


def _active_review_confirmation(window: object):
    return getattr(window, "_review_metadata_confirmation", None) or getattr(
        window, "_review_remediation_confirmation", None
    )


def _grab_review_window(window: object, *, direct_render: bool = False):
    from PySide6.QtGui import QColor, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication

    if direct_render:
        ratio = max(1.0, float(window.devicePixelRatioF()))
        physical = window.size() * ratio
        pixmap = QPixmap(physical)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(QColor("#06090E"))
        window.render(pixmap)
    else:
        pixmap = window.grab()
    dialog = _active_review_dialog(window)
    if dialog is None or not dialog.isVisible():
        return pixmap
    confirmation = _active_review_confirmation(window)
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
            if any(
                scene in PARTY_REVIEW_SCENES
                or scene in MULTI_SOURCE_REVIEW_SCENES
                or scene in METADATA_INTELLIGENCE_REVIEW_SCENES
                for scene in self.plan.scenes
            ):
                _install_review_network_guard()
            self.runtime_checks = validate_review_runtime(self.plan)
            if any(scene in METADATA_REVIEW_SCENES for scene in self.plan.scenes):
                self.runtime_checks["metadata_behaviors"] = validate_metadata_review_behaviors(
                    self.window,
                    self.plan,
                )
            if any(
                scene in METADATA_INTELLIGENCE_REVIEW_SCENES
                for scene in self.plan.scenes
            ):
                self.runtime_checks["metadata_intelligence_behaviors"] = (
                    validate_metadata_intelligence_review_behaviors(
                        self.window,
                        self.plan,
                    )
                )
            if any(scene in REMEDIATION_REVIEW_SCENES for scene in self.plan.scenes):
                self.runtime_checks["remediation_behaviors"] = (
                    validate_remediation_review_behaviors(self.window, self.plan)
                )
            if any(scene in MULTI_SOURCE_REVIEW_SCENES for scene in self.plan.scenes):
                self.runtime_checks["multi_source_behaviors"] = (
                    validate_batch10_multi_source_behaviors(self.window, self.plan)
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
            review_dialog = _active_review_dialog(self.window)
            if review_dialog is not None:
                review_dialog.hide()
                self.app.processEvents()
                review_dialog.show()
                review_dialog.ensurePolished()
                review_dialog.updateGeometry()
                review_dialog.repaint()
                self.app.processEvents()
                confirmation = _active_review_confirmation(self.window)
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
                if scene in PARTY_REVIEW_SCENES:
                    self.runtime_checks["party_mode_behaviors"] = (
                        validate_party_review_behaviors(
                            self.window,
                            self.plan,
                            self.app,
                        )
                    )
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

            _size, scene = self.jobs[self.job_index]
            capture_window = self.window
            if scene in PARTY_REVIEW_SCENES:
                capture_window = getattr(self.window, "party_mode_window", None)
                if capture_window is None or not capture_window.isVisible():
                    raise ReviewPlanError("Party Mode capture surface is unavailable.")
            warmup = QPixmap(capture_window.size())
            warmup.fill(QColor("#06090E"))
            capture_window.render(warmup)
            review_dialog = _active_review_dialog(self.window)
            if review_dialog is not None:
                dialog_warmup = QPixmap(review_dialog.size())
                dialog_warmup.fill(QColor("#06090E"))
                review_dialog.render(dialog_warmup)
                review_dialog.repaint()
                confirmation = _active_review_confirmation(self.window)
                if confirmation is not None:
                    confirmation_warmup = QPixmap(confirmation.size())
                    confirmation_warmup.fill(QColor("#06090E"))
                    confirmation.render(confirmation_warmup)
                    confirmation.repaint()
            capture_window.repaint()
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
            capture_window = self.window
            if scene in PARTY_REVIEW_SCENES:
                capture_window = getattr(self.window, "party_mode_window", None)
                if capture_window is None or not capture_window.isVisible():
                    raise ReviewPlanError("Party Mode capture surface is unavailable.")
            pixmap = _grab_review_window(
                capture_window,
                direct_render=scene == "sync_source_failures",
            )
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
            party_metrics = party_review_metrics(self.window, scene)
            if party_metrics is not None:
                capture["party_metrics"] = party_metrics
            sync_metrics = multi_source_review_metrics(self.window, scene)
            if sync_metrics is not None:
                capture["multi_source_metrics"] = sync_metrics
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
            metadata_intelligence_behaviors = self.runtime_checks.get(
                "metadata_intelligence_behaviors"
            )
            remediation_behaviors = self.runtime_checks.get("remediation_behaviors")
            party_mode_behaviors = self.runtime_checks.get("party_mode_behaviors")
            multi_source_behaviors = self.runtime_checks.get("multi_source_behaviors")
            self.runtime_checks = validate_review_runtime(self.plan)
            if metadata_behaviors is not None:
                self.runtime_checks["metadata_behaviors"] = metadata_behaviors
            if metadata_intelligence_behaviors is not None:
                self.runtime_checks["metadata_intelligence_behaviors"] = (
                    metadata_intelligence_behaviors
                )
            if remediation_behaviors is not None:
                self.runtime_checks["remediation_behaviors"] = remediation_behaviors
            if party_mode_behaviors is not None:
                self.runtime_checks["party_mode_behaviors"] = party_mode_behaviors
            if multi_source_behaviors is not None:
                self.runtime_checks["multi_source_behaviors"] = multi_source_behaviors
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
            original_muted = getattr(self.window, "_review_party_original_muted", None)
            if isinstance(original_muted, bool):
                audio_output = getattr(self.window, "audio_output", None)
                if audio_output is not None:
                    audio_output.setMuted(original_muted)
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
