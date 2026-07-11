from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path


DEFAULT_SIZES = ((1100, 720), (1440, 900), (1920, 1080))
DEFAULT_SCENES = (
    "library",
    "albums",
    "artists_fetch_disabled",
    "artists_fetch_enabled",
    "sync_center",
    "settings",
    "empty_playlist",
)
METADATA_SCENES = (
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
REMEDIATION_SCENES = (
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
RUNTIME_PREFIX = "MusicVault_Batch7_UI_Runtime_"
OUTPUT_PREFIX = "MusicVault_UI_Review_Output_"
REMEDIATION_RESTART_PHASE_ENV = "MUSIC_VAULT_REMEDIATION_RESTART_PHASE"
REMEDIATION_RESTART_REQUIRED_ENV = "MUSIC_VAULT_REMEDIATION_RESTART_REQUIRED"
REMEDIATION_RESTART_CHECKPOINT = "synthetic_remediation_restart.json"

_SYNTHETIC_MP3_BASE64 = (
    "//sQxAADwAABpAAAACAAADSAAAAETEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFX/+xLE"
    "KYPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFX/+xDEU4PAAAGk"
    "AAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVf/7EsR9A8AAAaQAAAAg"
    "AAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVf/7EMSnA8AAAaQAAAAgAAA0gAAABF"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBV//sSxNCDwAABpAAAACAAADSAAAEVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVV//sQxNYDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVX/+xLE1YPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVf/7EsTVg8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/7"
    "EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
)


def parse_size(value: str) -> tuple[int, int]:
    try:
        width_text, height_text = value.lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("Sizes must use WIDTHxHEIGHT.") from exc
    if not 800 <= width <= 4096 or not 600 <= height <= 2160:
        raise argparse.ArgumentTypeError("Size is outside the supported desktop range.")
    return width, height


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture sanitized Music Vault UI screenshots from synthetic data."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Screenshot directory. Defaults to a retained TEMP directory.",
    )
    parser.add_argument(
        "--exe",
        type=Path,
        help="Packaged MusicVault.exe to review instead of source run.py.",
    )
    parser.add_argument(
        "--size",
        action="append",
        type=parse_size,
        help="Repeatable WIDTHxHEIGHT capture size.",
    )
    parser.add_argument(
        "--page",
        action="append",
        choices=(
            *DEFAULT_SCENES,
            "artists",
            "no_results",
            *METADATA_SCENES,
            *REMEDIATION_SCENES,
        ),
        help="Repeatable review scene. Defaults to the standard application matrix.",
    )
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="Use Qt offscreen rendering for automated source review.",
    )
    parser.add_argument(
        "--scale",
        type=float,
        choices=(1.0, 1.25, 1.5),
        help="Optional Qt scale factor for a separate high-DPI review process.",
    )
    parser.add_argument("--settle-ms", type=int, default=450)
    parser.add_argument("--timeout", type=int, default=180)
    return parser.parse_args()


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_output_directory(project_root: Path, requested: Path | None) -> Path:
    if requested is None:
        return Path(tempfile.mkdtemp(prefix=OUTPUT_PREFIX)).resolve()

    output = requested.expanduser().resolve()
    if is_relative_to(output, project_root):
        permitted = (project_root / ".ui-review").resolve()
        if output != permitted and not is_relative_to(output, permitted):
            raise ValueError(
                "Repository-contained review output is allowed only under .ui-review/."
            )
    output.mkdir(parents=True, exist_ok=True)
    return output


def create_runtime_root(project_root: Path) -> Path:
    runtime = Path(tempfile.mkdtemp(prefix=RUNTIME_PREFIX)).resolve()
    if is_relative_to(runtime, project_root):
        raise RuntimeError("Synthetic runtime unexpectedly resolved inside the repository.")
    (runtime / "run.py").write_text("# synthetic project marker\n", encoding="utf-8")
    (runtime / "music_vault").mkdir()
    (runtime / "data").mkdir()
    (runtime / "profile").mkdir()
    return runtime


def copy_public_assets(project_root: Path, runtime: Path) -> None:
    source_icons = project_root / "assets" / "icons"
    target_icons = runtime / "assets" / "icons"
    target_icons.mkdir(parents=True)
    for name in ("music_vault.ico", "music_vault_icon.png"):
        source = source_icons / name
        if source.is_file():
            shutil.copy2(source, target_icons / name)
    shutil.copytree(source_icons / "ui", target_icons / "ui")


def generated_artwork(destination: Path, index: int) -> None:
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPen

    colors = (
        ("#1DB954", "#133C55"),
        ("#5B8DEF", "#352D63"),
        ("#F3B84B", "#7B2D3A"),
        ("#25D366", "#1D3557"),
        ("#8B5CF6", "#0F766E"),
        ("#E76F51", "#264653"),
        ("#3A86FF", "#8338EC"),
    )
    first, second = colors[index % len(colors)]
    image = QImage(512, 512, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#0A0F15"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    gradient = QLinearGradient(QPointF(0, 0), QPointF(512, 512))
    gradient.setColorAt(0.0, QColor(first))
    gradient.setColorAt(1.0, QColor(second))
    painter.fillRect(QRectF(0, 0, 512, 512), gradient)
    pen = QPen(QColor(255, 255, 255, 190), 12)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    baseline = 260 + (index % 3) * 12
    for offset, height in enumerate((48, 102, 158, 88, 132, 62, 112)):
        x = 72 + offset * 60
        painter.drawLine(x, baseline - height // 2, x, baseline + height // 2)
    painter.setPen(QPen(QColor(255, 255, 255, 80), 4))
    painter.drawRoundedRect(QRectF(38, 38, 436, 436), 38, 38)
    painter.end()
    if not image.save(str(destination), "PNG"):
        raise RuntimeError("Could not generate synthetic review artwork.")


def seed_synthetic_runtime(project_root: Path, runtime: Path) -> dict[str, int]:
    data = runtime / "data"
    downloads = data / "youtube_downloads"
    covers = data / "synthetic_artwork"
    media = data / "synthetic_sentinels"
    covers.mkdir()
    media.mkdir()

    config = {
        "download_folder": str(downloads),
        "audio_quality": "320",
        "volume_percent": 23,
        "artist_image_fetch_enabled": False,
    }
    (data / "music_vault_config.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )

    for index in range(18):
        generated_artwork(covers / f"synthetic_cover_{index + 1}.png", index)

    previous_environment = {
        name: os.environ.get(name)
        for name in ("MUSIC_VAULT_PROJECT_ROOT", "HOME", "USERPROFILE")
    }
    os.environ["MUSIC_VAULT_PROJECT_ROOT"] = str(runtime)
    os.environ["HOME"] = str(runtime / "profile")
    os.environ["USERPROFILE"] = str(runtime / "profile")

    project_text = str(project_root)
    if project_text not in sys.path:
        sys.path.insert(0, project_text)

    try:
        from music_vault.core import paths
        from music_vault.core.db import MusicVaultDB

        paths._resolved_project_root.cache_clear()
        if paths.project_root().resolve() != runtime:
            raise RuntimeError("Music Vault did not resolve the synthetic runtime.")

        database = data / "music_vault.sqlite3"
        db = MusicVaultDB(
            database,
            backup_dir=data / "backups",
            youtube_download_root=downloads,
        )
        special_artists = (
            "Aster Resolved Artist",
            "Boreal No Match Artist",
            "Cinder Ambiguous Artist",
            "Distant Loading Artist",
            "Eclipse Temporary Artist",
            "Fallow Corrupt Artist",
            "Synthetic Ensemble With A Deliberately Long Artist Name That Must Elide",
            "Cedar & Signal",
            "Northbound Echo featuring The Quiet Current",
            None,
        )
        artists = special_artists + tuple(
            f"Synthetic Artist {index:03d}"
            for index in range(len(special_artists), 200)
        )
        titles = (
            "Midnight Signal",
            "Paper Constellations",
            "Borrowed Weather",
            "Quiet Circuit",
            "Local Horizon",
            "Windows After Rain",
            "A Deliberately Long Synthetic Track Title That Must Elide Without Breaking Layout",
        )

        track_ids: list[int] = []
        for index in range(300):
            track_title = f"{titles[index % len(titles)]} {index + 1:04d}"
            duration_seconds = 150 + index * 3
            if index == 0:
                from mutagen.id3 import ID3, TIT2, TPE1

                from music_vault.metadata.tag_writer import inspect_mp3

                sentinel = media / "track_0001.mp3"
                sentinel.write_bytes(base64.b64decode(_SYNTHETIC_MP3_BASE64))
                track_title = "Synthetic Review Song (Official Video)"
                tags = ID3()
                tags.add(TIT2(encoding=3, text=[track_title]))
                tags.add(TPE1(encoding=3, text=[str(artists[index])]))
                tags.save(sentinel, v2_version=3)
                duration_seconds = inspect_mp3(sentinel).duration_seconds
            else:
                sentinel = media / f"track_{index + 1:04d}.synthetic-audio"
                sentinel.write_bytes(b"synthetic-review-sentinel\n")
            if index == 0:
                album = "A Shared Synthetic Album Title"
                album_artist = "Aster Resolved Artist"
                canonical_year = "2001"
            elif index == 1:
                album = "A Shared Synthetic Album Title"
                album_artist = "Boreal No Match Artist"
                canonical_year = "2001"
            elif index == 2:
                album = (
                    "A Very Long Synthetic Album Name Designed To Exercise Safe "
                    "Elision Without Overlap"
                )
                album_artist = "Cinder Ambiguous Artist"
                canonical_year = "1999"
            else:
                album_index = (index - 3) % 97
                album = f"Synthetic Album {album_index:03d}"
                album_artist = f"Synthetic Album Artist {album_index % 60:03d}"
                canonical_year = (
                    str(1980 + album_index % 44) if album_index % 4 else None
                )
            track_id = db.upsert_track(
                sentinel,
                title=track_title,
                artist=artists[index % len(artists)],
                album=album,
                album_artist=album_artist,
                year=canonical_year,
                cover_path=(
                    str((covers / f"synthetic_cover_{index % 18 + 1}.png").resolve())
                    if index % 11 != 0
                    else None
                ),
                duration_seconds=duration_seconds,
                source_kind="youtube" if index == 0 else "local",
                source_video_id="abcdefghijk" if index == 0 else None,
                source_upload_date="2024-03-02" if index == 0 else None,
            )
            track_ids.append(track_id)

        playlist_specs = (
            ("Synthetic Focus Mix", track_ids[:10]),
            ("After-Hours Local Review", track_ids[8:18]),
            (
                "A Very Long Synthetic Playlist Name That Must Elide Cleanly In The Sidebar",
                track_ids[::3],
            ),
            ("Empty Playlist", ()),
        )
        for name, members in playlist_specs:
            playlist_id = db.create_playlist(name)
            for track_id in members:
                db.add_track_to_playlist(playlist_id, track_id)

        schema = int(db.conn.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(db.conn.execute("PRAGMA integrity_check").fetchone()[0])
        db.close()
        if schema != 4 or integrity != "ok":
            raise RuntimeError("Synthetic database validation failed.")
    finally:
        try:
            paths._resolved_project_root.cache_clear()  # type: ignore[name-defined]
        except (NameError, AttributeError):
            pass
        for name, value in previous_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    if (data / "youtube_api_key.txt").exists():
        raise RuntimeError("Synthetic runtime unexpectedly contains an API key.")
    return {
        "track_count": len(track_ids),
        "playlist_count": len(playlist_specs),
        "synthetic_album_target": 100,
        "synthetic_artist_target": 200,
        "artist_image_fetch_enabled_by_default": False,
        "synthetic_mp3_count": 1,
    }


def write_review_plan(
    runtime: Path,
    output: Path,
    sizes: tuple[tuple[int, int], ...],
    scenes: tuple[str, ...],
    settle_ms: int,
) -> Path:
    plan = {
        "schema_version": 1,
        "runtime_root": str(runtime),
        "output_dir": str(output),
        "sizes": [{"width": width, "height": height} for width, height in sizes],
        "scenes": list(scenes),
        "settle_ms": settle_ms,
        "expected_capture_count": len(sizes) * len(scenes),
    }
    path = runtime / "ui_review_plan.json"
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return path


def request_graceful_close(process_id: int) -> bool:
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    requested = False
    enum_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _lparam):
        nonlocal requested
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == process_id and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, 0x0010, 0, 0)
            requested = True
        return True

    user32.EnumWindows(enum_type(callback), 0)
    return requested


def wait_for_review(process: subprocess.Popen[str], timeout: int) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    if process.poll() is None:
        request_graceful_close(process.pid)
        grace_deadline = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < grace_deadline:
            time.sleep(0.2)
    if process.poll() is None:
        raise TimeoutError(
            "Music Vault did not close gracefully; it was not force-terminated."
        )
    return process.communicate()


def validate_remediation_restart_checkpoint(
    runtime: Path,
    *,
    packaged: bool,
) -> dict[str, int | bool]:
    """Validate only aggregate persisted-job facts between review processes."""

    checkpoint_path = runtime / "data" / REMEDIATION_RESTART_CHECKPOINT
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("Synthetic remediation restart checkpoint is invalid.")
    job_id = payload.get("job_id")
    creator_pid = payload.get("creator_pid")
    partial_analyzed = payload.get("partial_analyzed")
    total = payload.get("total")
    provider_requests = payload.get("provider_requests")
    if (
        not isinstance(job_id, str)
        or not job_id
        or isinstance(creator_pid, bool)
        or not isinstance(creator_pid, int)
        or creator_pid <= 0
        or partial_analyzed != 1
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total <= int(partial_analyzed)
        or provider_requests != 1
        or bool(payload.get("creator_packaged")) is not packaged
    ):
        raise RuntimeError("Synthetic remediation restart evidence is incomplete.")

    database = runtime / "data" / "music_vault.sqlite3"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT status, analyzed_items, total_items
            FROM metadata_remediation_jobs WHERE id=?
            """,
            (job_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None or not (
        str(row["status"]) == "paused"
        and int(row["analyzed_items"]) == 1
        and int(row["total_items"]) == total
    ):
        raise RuntimeError("Synthetic remediation partial job was not persisted.")
    return {
        "checkpoint_schema_version": 1,
        "partial_analyzed": 1,
        "total": total,
        "creator_packaged": packaged,
    }


def validate_output(
    output: Path,
    runtime: Path,
    expected_count: int,
) -> dict[str, object]:
    manifest_path = output / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("status") != "complete":
        raise RuntimeError("Synthetic UI review manifest did not complete.")
    if payload.get("capture_count") != expected_count:
        raise RuntimeError("Synthetic UI review capture matrix is incomplete.")

    captured_scenes = {
        str(capture.get("scene"))
        for capture in payload.get("captures", [])
        if isinstance(capture, dict)
    }
    if captured_scenes.intersection(METADATA_SCENES):
        runtime_checks = payload.get("runtime_checks") or {}
        metadata_behaviors = runtime_checks.get("metadata_behaviors") or {}
        required_behaviors = {
            "manual_save",
            "candidate_apply",
            "artwork_replace",
            "undo",
            "approved_snapshot",
            "queue_context_preserved",
            "playlist_membership_preserved",
        }
        if any(metadata_behaviors.get(name) is not True for name in required_behaviors):
            raise RuntimeError("Synthetic packaged metadata behavior validation failed.")
    if captured_scenes.intersection(REMEDIATION_SCENES):
        runtime_checks = payload.get("runtime_checks") or {}
        remediation_behaviors = runtime_checks.get("remediation_behaviors") or {}
        required_behaviors = {
            "dashboard_available",
            "non_destructive_analysis",
            "high_confidence_apply",
            "ambiguous_unchanged",
            "exact_media_backup",
            "tag_update_verified",
            "audio_payload_unchanged",
            "rollback_exact",
            "resumable_after_restart",
            "partial_job_persisted_by_prior_process",
            "fresh_process_database_service_resume",
            "history_audited",
            "safe_app_status",
            "queue_and_membership_preserved",
            "synthetic_provider_only",
            "public_provider_call_count_zero",
        }
        if any(
            remediation_behaviors.get(name) is not True
            for name in required_behaviors
        ):
            raise RuntimeError("Synthetic packaged remediation validation failed.")

    from PySide6.QtGui import QImage

    for capture in payload.get("captures", []):
        filename = capture["file"]
        if Path(filename).name != filename:
            raise RuntimeError("Synthetic screenshot name escaped the output directory.")
        image_path = output / filename
        image = QImage(str(image_path))
        if image.isNull() or image.width() < 800 or image.height() < 600:
            raise RuntimeError(f"Invalid synthetic screenshot: {filename}")
        if capture.get("sha256") != hashlib.sha256(image_path.read_bytes()).hexdigest():
            raise RuntimeError(f"Synthetic screenshot hash mismatch: {filename}")
        colors = set()
        for x_part in range(1, 32):
            for y_part in range(1, 24):
                x = min(image.width() - 1, image.width() * x_part // 32)
                y = min(image.height() - 1, image.height() * y_part // 24)
                colors.add(image.pixelColor(x, y).rgba())
        if len(colors) < 8:
            raise RuntimeError(f"Synthetic screenshot lacks visual content: {filename}")

        requested_width = max(1, int(capture.get("requested_width", image.width())))
        requested_height = max(1, int(capture.get("requested_height", image.height())))
        scale_x = image.width() / requested_width
        scale_y = image.height() / requested_height

        def signal_fraction(left: int, top: int, right: int, bottom: int) -> float:
            x0 = max(0, min(image.width() - 1, int(left * scale_x)))
            y0 = max(0, min(image.height() - 1, int(top * scale_y)))
            x1 = max(x0 + 1, min(image.width(), int(right * scale_x)))
            y1 = max(y0 + 1, min(image.height(), int(bottom * scale_y)))
            sample = [
                image.pixelColor(x, y)
                for x in range(x0, x1, max(1, int(3 * scale_x)))
                for y in range(y0, y1, max(1, int(3 * scale_y)))
            ]
            visible = sum(
                1
                for color in sample
                if color.alpha() >= 128
                and max(color.red(), color.green(), color.blue()) >= 45
            )
            return visible / max(1, len(sample))

        brand_signal = signal_fraction(10, 10, 244, 100)
        player_signal = signal_fraction(
            256,
            requested_height - 160,
            requested_width - 10,
            requested_height - 8,
        )
        # Player controls keep a mostly fixed pixel footprint while the bar grows
        # wider at desktop review sizes. Scale the density threshold so a valid
        # 1920-wide player is not rejected merely for having more empty bar space.
        player_threshold = 0.025 * min(1.0, 1440 / requested_width)
        if brand_signal < 0.04 or player_signal < player_threshold:
            raise RuntimeError(
                f"Synthetic screenshot is missing shared application chrome: {filename}"
            )

        scene = capture.get("scene")
        if scene in {
            "albums",
            "artists",
            "artists_fetch_disabled",
            "artists_fetch_enabled",
        }:
            metrics = capture.get("browser_metrics")
            if not isinstance(metrics, dict):
                raise RuntimeError("Synthetic browser capture lacks aggregate metrics.")
            if int(metrics.get("model_rows", 0)) <= 0:
                raise RuntimeError("Synthetic browser model did not populate.")
            if int(metrics.get("visible_key_count", 0)) <= 0:
                raise RuntimeError("Synthetic browser reported no visible items.")
            if int(metrics.get("per_item_widget_count", -1)) != 0:
                raise RuntimeError("Synthetic browser created per-item QWidget cards.")
            if int(metrics.get("public_provider_call_count", -1)) != 0:
                raise RuntimeError("Synthetic review attempted a public artist provider call.")
            if not metrics.get("synthetic_provider_active"):
                raise RuntimeError("Synthetic artist provider safety mode was not active.")
            if scene in {"artists", "artists_fetch_disabled"}:
                if metrics.get("artist_fetch_enabled"):
                    raise RuntimeError("Disabled artist-photo review unexpectedly enabled fetching.")
                if int(metrics.get("synthetic_provider_call_count", 0)) != 0:
                    raise RuntimeError("Disabled artist-photo review made a provider request.")
            if scene == "artists_fetch_enabled" and not metrics.get("artist_fetch_enabled"):
                raise RuntimeError("Enabled artist-photo review did not enable in-memory consent.")
            if scene == "artists_fetch_enabled":
                states = metrics.get("image_states") or {}
                if int(states.get("ready", 0)) <= 0:
                    raise RuntimeError(
                        "Enabled artist-photo review did not render a resolved cached portrait."
                    )
        if scene in METADATA_SCENES:
            metrics = capture.get("metadata_metrics")
            if not isinstance(metrics, dict):
                raise RuntimeError("Synthetic metadata capture lacks aggregate metrics.")
            if int(metrics.get("editable_field_count", 0)) != 6:
                raise RuntimeError("Synthetic metadata editor does not expose six fields.")
            if not metrics.get("source_upload_date_is_read_only"):
                raise RuntimeError("Synthetic metadata source date is not read-only.")
            if not metrics.get("database_only_message_present"):
                raise RuntimeError("Synthetic metadata editor lacks the file-writeback boundary.")
            if not metrics.get("synthetic_provider_active"):
                raise RuntimeError("Synthetic metadata provider safety mode is inactive.")
            if int(metrics.get("public_provider_call_count", -1)) != 0:
                raise RuntimeError("Synthetic metadata review attempted a public provider call.")
            if scene == "metadata_manual_artwork" and not metrics.get("manual_artwork_staged"):
                raise RuntimeError("Synthetic manual artwork was not staged in memory.")
            if scene == "metadata_manual_artwork" and not metrics.get(
                "artwork_effective_present"
            ):
                raise RuntimeError("Synthetic manual-artwork scene lacks current artwork.")
            if scene == "metadata_no_artwork" and metrics.get("artwork_effective_present"):
                raise RuntimeError("Synthetic no-artwork scene still has effective artwork.")
            if scene in {"metadata_manual_artwork", "metadata_no_artwork"} and not metrics.get(
                "artwork_editor_visible"
            ):
                raise RuntimeError("Synthetic artwork editor is outside the captured viewport.")
            if scene == "metadata_undo_confirmation" and not metrics.get("undo_confirmation_visible"):
                raise RuntimeError("Synthetic undo confirmation was not visible.")
        if scene in REMEDIATION_SCENES:
            metrics = capture.get("remediation_metrics")
            if not isinstance(metrics, dict):
                raise RuntimeError("Synthetic remediation capture lacks aggregate metrics.")
            if not metrics.get("dialog_visible"):
                raise RuntimeError("Synthetic remediation dashboard is not visible.")
            if int(metrics.get("metric_card_count", 0)) != 10:
                raise RuntimeError("Synthetic remediation dashboard lacks aggregate cards.")
            if int(metrics.get("control_count", 0)) < 12:
                raise RuntimeError("Synthetic remediation dashboard lacks required controls.")
            if not metrics.get("synthetic_provider_active"):
                raise RuntimeError("Synthetic remediation provider safety mode is inactive.")
            if int(metrics.get("public_provider_call_count", -1)) != 0:
                raise RuntimeError("Synthetic remediation review attempted a public request.")
            if metrics.get("private_path_visible"):
                raise RuntimeError("Synthetic remediation table exposed a private path.")
            if (
                int(metrics.get("review_geometry_widget_count", 0)) < 20
                or int(metrics.get("review_geometry_overlap_count", -1)) != 0
                or int(metrics.get("review_geometry_clipped_count", -1)) != 0
                or int(metrics.get("review_group_clipped_count", -1)) != 0
            ):
                raise RuntimeError(
                    "Synthetic remediation controls overlap or are clipped."
                )
            if scene == "remediation_empty" and metrics.get("job_present"):
                raise RuntimeError("Synthetic empty remediation scene contains a job.")
            if scene != "remediation_empty" and not metrics.get("job_present"):
                raise RuntimeError("Synthetic remediation state lacks its job.")
            if scene in {
                "remediation_high_confirmation",
                "remediation_insufficient_disk",
                "remediation_rollback_confirmation",
            } and not metrics.get("confirmation_visible"):
                raise RuntimeError("Synthetic remediation confirmation is not visible.")
            if scene in {
                "remediation_needs_review",
                "remediation_ambiguous",
                "remediation_no_match",
                "remediation_artwork_comparison",
            } and int(metrics.get("selected_row_count", 0)) != 1:
                raise RuntimeError("Synthetic remediation review selection is missing.")
            if scene in {
                "remediation_needs_review",
                "remediation_artwork_comparison",
            } and (
                int(metrics.get("release_choice_count", 0)) < 1
                or not metrics.get("release_identity_complete")
            ):
                raise RuntimeError(
                    "Synthetic remediation review lacks a complete release identity."
                )
            if scene == "remediation_artwork_comparison":
                if not metrics.get("current_artwork_rendered") or not metrics.get(
                    "candidate_artwork_rendered"
                ):
                    raise RuntimeError(
                        "Synthetic artwork comparison did not render both previews."
                    )
                if metrics.get("artwork_field_selected"):
                    raise RuntimeError(
                        "Synthetic artwork comparison was preapproved unexpectedly."
                    )

    database = runtime / "data" / "music_vault.sqlite3"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()
    if schema != 4 or integrity != "ok":
        raise RuntimeError("Synthetic database failed final validation.")
    if (runtime / "data" / "youtube_api_key.txt").exists():
        raise RuntimeError("Synthetic runtime unexpectedly contains an API key.")
    config = json.loads(
        (runtime / "data" / "music_vault_config.json").read_text(encoding="utf-8")
    )
    if config.get("artist_image_fetch_enabled") is not False:
        raise RuntimeError("Synthetic runtime persisted artist-photo fetching as enabled.")
    return payload


def safe_delete_runtime(runtime: Path) -> None:
    temp_root = Path(tempfile.gettempdir()).resolve()
    resolved = runtime.resolve()
    if not is_relative_to(resolved, temp_root) or not resolved.name.startswith(RUNTIME_PREFIX):
        raise RuntimeError("Refusing to delete an unverified synthetic runtime path.")
    shutil.rmtree(resolved)


def write_aggregate_manifest(
    output: Path,
    manifests: list[dict[str, object]],
    sizes: tuple[tuple[int, int], ...],
    scenes: tuple[str, ...],
) -> dict[str, object]:
    if not manifests:
        raise RuntimeError("No synthetic UI review captures completed.")

    captures = [
        capture
        for manifest in manifests
        for capture in manifest.get("captures", [])  # type: ignore[union-attr]
    ]
    expected_count = len(sizes) * len(scenes)
    if len(captures) != expected_count:
        raise RuntimeError("Synthetic UI review subprocess matrix is incomplete.")

    pages: list[str] = []
    for manifest in manifests:
        for page in manifest.get("pages", []):  # type: ignore[union-attr]
            if isinstance(page, str) and page not in pages:
                pages.append(page)

    aggregate = dict(manifests[0])
    aggregate.update(
        {
            "status": "complete",
            "finished_at": manifests[-1].get("finished_at"),
            "capture_process_count": len(manifests),
            "requested_capture_count": expected_count,
            "capture_count": len(captures),
            "sizes": [
                {"width": width, "height": height}
                for width, height in sizes
            ],
            "pages": pages,
            "captures": captures,
            "dark_title_bar_applied": all(
                bool(manifest.get("dark_title_bar_applied"))
                for manifest in manifests
            ),
            "runtime_checks": manifests[-1].get("runtime_checks", {}),
        }
    )
    destination = output / "manifest.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return aggregate


def main() -> int:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[2]
    output = safe_output_directory(project_root, args.output)
    runtime = create_runtime_root(project_root)
    process: subprocess.Popen[str] | None = None
    completed = False

    try:
        copy_public_assets(project_root, runtime)
        dataset = seed_synthetic_runtime(project_root, runtime)
        sizes = tuple(args.size or DEFAULT_SIZES)
        scenes = tuple(args.page or DEFAULT_SCENES)
        if not 50 <= args.settle_ms <= 5000:
            raise ValueError("--settle-ms must be between 50 and 5000.")
        environment_base = os.environ.copy()
        environment_base.update(
            {
                "MUSIC_VAULT_PROJECT_ROOT": str(runtime),
                "HOME": str(runtime / "profile"),
                "USERPROFILE": str(runtime / "profile"),
                "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS": "1",
                # The provider factory accepts this only alongside the explicit
                # isolated review plan/root. It guarantees review cannot reach
                # public artist metadata or image services.
                "MUSIC_VAULT_ARTIST_IMAGE_PROVIDER": "synthetic",
            }
        )
        environment_base.pop(REMEDIATION_RESTART_PHASE_ENV, None)
        environment_base.pop(REMEDIATION_RESTART_REQUIRED_ENV, None)
        if args.offscreen:
            environment_base["QT_QPA_PLATFORM"] = "offscreen"
        if args.scale is not None:
            environment_base["QT_SCALE_FACTOR"] = str(args.scale)

        if args.exe is not None:
            executable = args.exe.expanduser().resolve()
            if not executable.is_file():
                raise FileNotFoundError(f"Packaged executable not found: {executable}")
            command = [str(executable)]
        else:
            command = [sys.executable, "-B", str(project_root / "run.py")]

        restart_checkpoint: dict[str, int | bool] | None = None
        remediation_scenes = tuple(
            scene for scene in scenes if scene in REMEDIATION_SCENES
        )
        if remediation_scenes:
            # The preparatory launch is intentionally a real application
            # process. It persists a one-item partial job and exits normally.
            # The first accepted capture then runs in another fresh process and
            # must resume that exact durable job before validation can pass.
            preparation_plan = write_review_plan(
                runtime,
                output,
                (sizes[0],),
                (remediation_scenes[0],),
                args.settle_ms,
            )
            preparation_environment = environment_base.copy()
            preparation_environment["MUSIC_VAULT_UI_REVIEW"] = str(preparation_plan)
            preparation_environment[REMEDIATION_RESTART_PHASE_ENV] = "prepare"
            process = subprocess.Popen(
                command,
                cwd=runtime,
                env=preparation_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            stdout, stderr = wait_for_review(process, args.timeout)
            if process.returncode != 0:
                summary = (stderr or stdout).strip()[-1200:]
                raise RuntimeError(
                    "Music Vault remediation restart preparation exited with code "
                    f"{process.returncode}: {summary}"
                )
            process = None
            restart_checkpoint = validate_remediation_restart_checkpoint(
                runtime,
                packaged=args.exe is not None,
            )
            environment_base[REMEDIATION_RESTART_REQUIRED_ENV] = (
                "packaged" if args.exe is not None else "source"
            )

        # A fresh process per page/size avoids stale Qt native backing-store
        # regions after rapid stacked-page changes on Windows. It also keeps
        # every screenshot independently reproducible.
        child_manifests: list[dict[str, object]] = []
        for size in sizes:
            for scene in scenes:
                plan = write_review_plan(
                    runtime,
                    output,
                    (size,),
                    (scene,),
                    args.settle_ms,
                )
                environment = environment_base.copy()
                environment["MUSIC_VAULT_UI_REVIEW"] = str(plan)
                process = subprocess.Popen(
                    command,
                    cwd=runtime,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                stdout, stderr = wait_for_review(process, args.timeout)
                if process.returncode != 0:
                    summary = (stderr or stdout).strip()[-1200:]
                    raise RuntimeError(
                        "Music Vault UI review exited with code "
                        f"{process.returncode}: {summary}"
                    )
                process = None
                child_manifests.append(validate_output(output, runtime, 1))

        write_aggregate_manifest(output, child_manifests, sizes, scenes)
        manifest = validate_output(output, runtime, len(sizes) * len(scenes))
        if restart_checkpoint is not None:
            remediation_behaviors = (
                (manifest.get("runtime_checks") or {}).get(
                    "remediation_behaviors", {}
                )
            )
            if args.exe is not None and remediation_behaviors.get(
                "fresh_packaged_process_resume"
            ) is not True:
                raise RuntimeError(
                    "Synthetic restart was not resumed by a second packaged process."
                )
        enabled_captures = [
            capture
            for capture in manifest.get("captures", [])
            if capture.get("scene") == "artists_fetch_enabled"
        ]
        if enabled_captures and not any(
            int((capture.get("browser_metrics") or {}).get(
                "synthetic_provider_call_count", 0
            ))
            > 0
            for capture in enabled_captures
        ):
            raise RuntimeError(
                "Synthetic artist provider was never exercised by enabled review scenes."
            )
        wrong_data = project_root / "dist" / "MusicVault" / "data"
        if wrong_data.exists():
            raise RuntimeError("dist/MusicVault/data was created during UI review.")

        completed = True
        print(
            json.dumps(
                {
                    "status": "complete",
                    "mode": "packaged" if args.exe else "source",
                    "output_dir": str(output),
                    "capture_count": manifest["capture_count"],
                    "sizes": manifest["sizes"],
                    "pages": manifest["pages"],
                    "dark_title_bar_applied": manifest.get("dark_title_bar_applied"),
                    "schema_version": 4,
                    "config_volume_percent": 23,
                    "api_key_present": False,
                    "artist_provider": "synthetic_no_network",
                    "remediation_provider": "synthetic_no_network",
                    "remediation_restart_process_count": (
                        2 if restart_checkpoint is not None else 0
                    ),
                    "remediation_fresh_packaged_resume": bool(
                        restart_checkpoint is not None and args.exe is not None
                    ),
                    "scale_factor": args.scale or 1.0,
                    "dataset": dataset,
                },
                indent=2,
            )
        )
        return 0
    finally:
        if process is not None and process.poll() is None:
            print(
                f"Synthetic runtime retained because process {process.pid} is still running: {runtime}",
                file=sys.stderr,
            )
        else:
            safe_delete_runtime(runtime)
        if not completed:
            print(f"Review output retained for diagnosis: {output}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
