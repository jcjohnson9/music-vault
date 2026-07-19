from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import math
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import wave
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
METADATA_INTELLIGENCE_SCENES = ("metadata_intelligence_smoke",)
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
PARTY_SCENES = ("party_mode_smoke",)
MULTI_SOURCE_SCENES = (
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
RUNTIME_PREFIX = "MusicVault_Batch7_UI_Runtime_"
OUTPUT_PREFIX = "MusicVault_UI_Review_Output_"
REMEDIATION_RESTART_PHASE_ENV = "MUSIC_VAULT_REMEDIATION_RESTART_PHASE"
REMEDIATION_RESTART_REQUIRED_ENV = "MUSIC_VAULT_REMEDIATION_RESTART_REQUIRED"
REMEDIATION_RESTART_CHECKPOINT = "synthetic_remediation_restart.json"
PARTY_REVIEW_FIXTURE = "synthetic_party_mode_review.json"
PARTY_REVIEW_SAMPLE_RATE = 48_000
PARTY_REVIEW_DURATION_SECONDS = 20

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
            *METADATA_INTELLIGENCE_SCENES,
            *REMEDIATION_SCENES,
            *PARTY_SCENES,
            *MULTI_SOURCE_SCENES,
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
    (runtime / "profile" / "LocalAppData").mkdir()
    (runtime / "profile" / "RoamingAppData").mkdir()
    (runtime / "temp").mkdir()
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


def write_synthetic_party_wav(destination: Path, *, variant: int) -> None:
    """Write a bounded, original test signal using only the standard library."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    frequencies = (
        (86.0, 410.0, 2_900.0),
        (132.0, 880.0, 4_600.0),
    )[variant % 2]
    frame_count = PARTY_REVIEW_SAMPLE_RATE * PARTY_REVIEW_DURATION_SECONDS
    frames = bytearray()
    for index in range(frame_count):
        moment = index / PARTY_REVIEW_SAMPLE_RATE
        section = int(moment / 0.75) % len(frequencies)
        frequency = frequencies[section]
        envelope = 0.14
        if int(moment * 4.0) != int(max(0.0, moment - 0.025) * 4.0):
            envelope = 0.24
        sample = math.sin(math.tau * frequency * moment) * envelope
        frames.extend(struct.pack("<h", round(sample * 32_767)))

    with wave.open(str(destination), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(PARTY_REVIEW_SAMPLE_RATE)
        target.writeframes(frames)

    with wave.open(str(destination), "rb") as source:
        if (
            source.getnchannels() != 1
            or source.getsampwidth() != 2
            or source.getframerate() != PARTY_REVIEW_SAMPLE_RATE
            or source.getnframes() != frame_count
        ):
            raise RuntimeError("Synthetic Party Mode WAV failed validation.")


def seed_synthetic_runtime(
    project_root: Path,
    runtime: Path,
    *,
    include_party: bool = False,
) -> dict[str, int]:
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
        "party_mode_preset": "pulse",
        "party_mode_quality": "medium",
        "party_mode_frame_rate": "30",
        "party_mode_reduced_motion": False,
        "party_mode_show_artwork": True,
        "party_mode_auto_hide_overlay": True,
        "party_mode_overlay_timeout_seconds": 3,
    }
    (data / "music_vault_config.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )

    for index in range(18):
        generated_artwork(covers / f"synthetic_cover_{index + 1}.png", index)

    party_wavs: tuple[Path, ...] = ()
    party_sidecars: tuple[Path, ...] = ()
    if include_party:
        party_media = data / "synthetic_party_mode"
        party_media.mkdir()
        party_wavs = (
            party_media / "synthetic_party_signal_a.wav",
            party_media / "synthetic_party_signal_b.wav",
        )
        for variant, wav_path in enumerate(party_wavs):
            write_synthetic_party_wav(wav_path, variant=variant)
        party_sidecars = (
            party_wavs[0].with_suffix(".lrc"),
            party_wavs[1].with_suffix(".txt"),
        )
        party_sidecars[0].write_text(
            "[00:00.00]Synthetic opening line\n"
            "[00:01.20]Synthetic current line\n"
            "[00:02.40]Synthetic following line\n",
            encoding="utf-8",
        )
        party_sidecars[1].write_text(
            "Synthetic unsynchronized review line one.\n\n"
            "Synthetic unsynchronized review line two.\n",
            encoding="utf-8",
        )

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
        from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB

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
        party_track_ids: list[int] = []
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
            elif include_party and index >= 298:
                party_index = index - 298
                sentinel = party_wavs[party_index]
                track_title = f"Synthetic Party Signal {party_index + 1}"
                duration_seconds = PARTY_REVIEW_DURATION_SECONDS
            else:
                sentinel = media / f"track_{index + 1:04d}.synthetic-audio"
                sentinel.write_bytes(b"synthetic-review-sentinel\n")
            if index == 0:
                album = "A Shared Synthetic Album Title"
                album_artist = "Aster Resolved Artist"
                canonical_year = "2001"
                synthetic_master_id = "99000001"
            elif index == 1:
                album = "A Shared Synthetic Album Title"
                album_artist = "Boreal No Match Artist"
                canonical_year = "2001"
                synthetic_master_id = "99000002"
            elif index == 2:
                album = (
                    "A Very Long Synthetic Album Name Designed To Exercise Safe "
                    "Elision Without Overlap"
                )
                album_artist = "Cinder Ambiguous Artist"
                canonical_year = "1999"
                synthetic_master_id = "99000003"
            elif include_party and index >= 298:
                party_index = index - 298
                album = "Synthetic Party Mode"
                album_artist = "Music Vault Review"
                canonical_year = None
                synthetic_master_id = "99000999"
            else:
                album_index = (index - 3) % 97
                album = f"Synthetic Album {album_index:03d}"
                album_artist = f"Synthetic Album Artist {album_index % 60:03d}"
                canonical_year = (
                    str(1980 + album_index % 44) if album_index % 4 else None
                )
                synthetic_master_id = str(99000100 + album_index)
            if include_party and index >= 298:
                artist = "Music Vault Review"
                cover_path = str(
                    (covers / f"synthetic_cover_{16 + (index - 298)}.png").resolve()
                )
            else:
                artist = artists[index % len(artists)]
                cover_path = (
                    str((covers / f"synthetic_cover_{index % 18 + 1}.png").resolve())
                    if index % 11 != 0
                    else None
                )
            track_id = db.upsert_track(
                sentinel,
                title=track_title,
                artist=artist,
                album=album,
                album_artist=album_artist,
                year=canonical_year,
                cover_path=cover_path,
                duration_seconds=duration_seconds,
                source_kind="youtube" if index == 0 else "local",
                source_video_id="abcdefghijk" if index == 0 else None,
                source_upload_date="2024-03-02" if index == 0 else None,
            )
            track_ids.append(track_id)
            # Give the synthetic browser fixture durable release-family
            # identity so schema-v7 grouping remains deterministic even when
            # its 300 tracks deliberately exercise 200 performer identities.
            # These local-only fictional IDs never trigger provider access.
            db.update_track_metadata(
                track_id,
                discogs_master_id=synthetic_master_id,
            )
            if include_party and index >= 298:
                party_track_ids.append(track_id)

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
        if schema != CURRENT_SCHEMA_VERSION or integrity != "ok":
            raise RuntimeError("Synthetic database validation failed.")

        if include_party:
            (data / PARTY_REVIEW_FIXTURE).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "synthetic_only": True,
                        "track_ids": party_track_ids,
                        "queue_track_id": track_ids[2],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
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
        "schema_version": schema,
        "track_count": len(track_ids),
        "playlist_count": len(playlist_specs),
        "synthetic_album_target": 100,
        "synthetic_artist_target": 200,
        "artist_image_fetch_enabled_by_default": False,
        "synthetic_mp3_count": 1,
        "synthetic_party_wav_count": len(party_wavs),
        "synthetic_party_lrc_count": sum(
            path.suffix.casefold() == ".lrc" for path in party_sidecars
        ),
        "synthetic_party_txt_count": sum(
            path.suffix.casefold() == ".txt" for path in party_sidecars
        ),
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
    if captured_scenes.intersection(METADATA_INTELLIGENCE_SCENES):
        runtime_checks = payload.get("runtime_checks") or {}
        behaviors = runtime_checks.get("metadata_intelligence_behaviors") or {}
        required_behaviors = {
            "schema_current",
            "exact_random_uploader_corrected",
            "label_excluded_from_artist_credits",
            "group_and_featured_credits_structured",
            "studio_live_tracks_remain_separate",
            "unofficial_live_dates_withheld",
            "provider_conflict_terminal_best_available",
            "ordinary_review_count_zero",
            "youtube_exclusive_source_fallback",
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
        }
        if any(behaviors.get(name) is not True for name in required_behaviors):
            raise RuntimeError(
                "Synthetic packaged metadata-intelligence validation failed."
            )
        if int(behaviors.get("network_attempt_count", -1)) != 0:
            raise RuntimeError(
                "Synthetic metadata-intelligence smoke attempted network access."
            )
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
    if captured_scenes.intersection(PARTY_SCENES):
        runtime_checks = payload.get("runtime_checks") or {}
        party_behaviors = runtime_checks.get("party_mode_behaviors") or {}
        required_behaviors = {
            "synthetic_fixture_validated",
            "network_guard_active",
            "party_button_present",
            "f11_opened",
            "f11_closed",
            "escape_closed",
            "full_screen",
            "screen_matches_main",
            "same_media_player",
            "same_audio_output",
            "audio_buffer_output_attached",
            "no_second_player",
            "open_close_source_preserved",
            "open_close_position_not_reset",
            "playback_state_preserved",
            "volume_preserved",
            "queue_preserved",
            "base_context_preserved",
            "ambient_fallback_verified",
            "static_default_migrated",
            "static_timer_stopped",
            "six_presets_verified",
            "pulse_verified",
            "starfield_verified",
            "aurora_verified",
            "orb_cluster_verified",
            "fireworks_verified",
            "overlay_controls_verified",
            "track_transition_verified",
            "lyrics_default_off",
            "lyrics_toggle_persisted",
            "synced_lyrics_verified",
            "plain_lyrics_verified",
            "lyrics_cache_verified",
            "lyrics_track_transition_verified",
            "lyrics_above_controls",
            "lyrics_visible_overlay_hidden",
            "synthetic_lyrics_provider",
            "lyrics_tasks_bounded",
            "render_timer_stopped_on_exit",
            "analysis_worker_stopped_on_exit",
            "status_safe",
            "no_pcm_status_fields",
        }
        if any(party_behaviors.get(name) is not True for name in required_behaviors):
            raise RuntimeError("Synthetic packaged Party Mode validation failed.")
        if party_behaviors.get("audio_backend_outcome") not in {
            "reactive",
            "ambient_only",
        }:
            raise RuntimeError("Synthetic Party Mode audio outcome is invalid.")
        if int(party_behaviors.get("network_attempt_count", -1)) != 0:
            raise RuntimeError("Synthetic Party Mode attempted network access.")
    if captured_scenes.intersection(MULTI_SOURCE_SCENES):
        runtime_checks = payload.get("runtime_checks") or {}
        behaviors = runtime_checks.get("multi_source_behaviors") or {}
        required_behaviors = {
            "scenario_completed",
            "api_key_absent",
            "add_source_persisted",
            "edit_source_persisted",
            "edit_identity_stable",
            "edit_storage_key_stable",
            "source_crud",
            "source_order_persisted",
            "sequential_execution",
            "source_a_duplicate_occurrences",
            "cross_source_single_download",
            "first_playlist_a_order",
            "first_playlist_b_order",
            "library_only_source",
            "unavailable_item_truthful",
            "aggregate_complete_with_issues",
            "second_snapshot_order",
            "remote_removal_preserves_media",
            "remote_removal_recorded",
            "failed_enumeration_preserves_playlist",
            "archive_preserves_playlist",
            "source_specific_failures",
            "aggregate_only_app_status",
            "playback_preserved",
            "queue_preserved",
            "base_context_preserved",
            "party_mode_preserved",
            "lyrics_preserved",
            "same_media_player",
        }
        if any(behaviors.get(name) is not True for name in required_behaviors):
            raise RuntimeError("Synthetic packaged multi-source behavior validation failed.")
        if int(behaviors.get("network_attempt_count", -1)) != 0:
            raise RuntimeError("Synthetic multi-source smoke attempted network access.")

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
        scene = capture.get("scene")
        if scene not in PARTY_SCENES and (
            brand_signal < 0.04 or player_signal < player_threshold
        ):
            raise RuntimeError(
                f"Synthetic screenshot is missing shared application chrome: {filename}"
            )

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
        if scene in PARTY_SCENES:
            metrics = capture.get("party_metrics")
            if not isinstance(metrics, dict):
                raise RuntimeError("Synthetic Party Mode capture lacks aggregate metrics.")
            if metrics.get("audio_backend_outcome") not in {
                "reactive",
                "ambient_only",
            }:
                raise RuntimeError("Synthetic Party Mode capture lacks an audio outcome.")
            if metrics.get("ambient_fallback_verified") is not True:
                raise RuntimeError("Synthetic Party Mode fallback was not verified.")
        if scene in MULTI_SOURCE_SCENES:
            metrics = capture.get("multi_source_metrics")
            if not isinstance(metrics, dict):
                raise RuntimeError("Synthetic Sync Center capture lacks aggregate metrics.")
            if int(metrics.get("per_source_widget_count", -1)) != 0:
                raise RuntimeError("Sync Center allocated a QWidget per source row.")
            if int(metrics.get("clipped_action_count", -1)) != 0:
                raise RuntimeError("Sync Center actions are clipped at a review size.")
            if metrics.get("private_path_visible"):
                raise RuntimeError("Sync Center exposed a private absolute path.")
            if metrics.get("api_key_field_visible"):
                raise RuntimeError("Sync Center exposed an API-key field.")
            if metrics.get("preservation_message_present") is not True:
                raise RuntimeError("Source-removal preservation messaging is incomplete.")
            if scene == "sync_sources_empty":
                if int(metrics.get("source_row_count", -1)) != 0:
                    raise RuntimeError("Empty Sync Center unexpectedly contains a source.")
            elif scene == "sync_managed_playlist":
                if not metrics.get("managed_badge_visible") or not metrics.get(
                    "managed_explanation_present"
                ):
                    raise RuntimeError("Managed playlist presentation is incomplete.")
                if int(metrics.get("playlist_track_count", 0)) <= 0:
                    raise RuntimeError("Managed playlist review has no tracks.")
            else:
                if int(metrics.get("source_row_count", 0)) != 3:
                    raise RuntimeError("Sync Center did not render all three sources.")
                if int(metrics.get("selected_source_count", 0)) <= 0:
                    raise RuntimeError("Sync Center lacks a clear selected source.")
                if int(metrics.get("disabled_source_count", 0)) != 1:
                    raise RuntimeError("Sync Center lacks a clear disabled source.")
            expected_dialog = {
                "sync_source_add": "source_editor",
                "sync_source_edit": "source_editor",
                "sync_source_remove": "remove_confirmation",
            }.get(scene)
            if expected_dialog is not None and (
                metrics.get("dialog_visible") is not True
                or metrics.get("dialog_kind") != expected_dialog
            ):
                raise RuntimeError("Synthetic Sync Center dialog is unavailable.")
            if scene == "sync_all_running" and metrics.get("batch_active") is not True:
                raise RuntimeError("Sync All running state is not active.")
            if scene == "sync_complete_issues" and metrics.get(
                "status_property"
            ) != "complete_with_issues":
                raise RuntimeError("Complete-with-issues status treatment is missing.")

    database = runtime / "data" / "music_vault.sqlite3"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()
    from music_vault.core.db import CURRENT_SCHEMA_VERSION

    if schema != CURRENT_SCHEMA_VERSION or integrity != "ok":
        raise RuntimeError("Synthetic database failed final validation.")
    if (runtime / "data" / "youtube_api_key.txt").exists():
        raise RuntimeError("Synthetic runtime unexpectedly contains an API key.")
    party_wavs = sorted((runtime / "data").rglob("*.wav"))
    if captured_scenes.intersection(PARTY_SCENES):
        expected_party_wavs = sorted(
            (runtime / "data" / "synthetic_party_mode").glob("*.wav")
        )
        if party_wavs != expected_party_wavs or len(party_wavs) != 2:
            raise RuntimeError("Synthetic Party Mode WAV inventory is invalid.")
        party_root = runtime / "data" / "synthetic_party_mode"
        if len(list(party_root.glob("*.lrc"))) != 1 or len(
            list(party_root.glob("*.txt"))
        ) != 1:
            raise RuntimeError("Synthetic Party Mode lyric inventory is invalid.")
    elif party_wavs:
        raise RuntimeError("Non-Party UI review unexpectedly generated WAV files.")
    if any((runtime / "data").rglob("*.pcm")) or any(
        (runtime / "data").rglob("*.raw")
    ):
        raise RuntimeError("Synthetic Party Mode persisted decoded audio data.")
    config = json.loads(
        (runtime / "data" / "music_vault_config.json").read_text(encoding="utf-8")
    )
    if config.get("artist_image_fetch_enabled") is not False:
        raise RuntimeError("Synthetic runtime persisted artist-photo fetching as enabled.")
    if captured_scenes.intersection(PARTY_SCENES):
        if not (
            config.get("party_mode_config_version") == 2
            and config.get("party_mode_preset") == "aurora"
            and config.get("party_mode_lyrics_enabled") is True
            and config.get("lyrics_online_lookup_enabled") is False
        ):
            raise RuntimeError("Synthetic Party Mode configuration validation failed.")
        status = json.loads(
            (runtime / "data" / "music_vault_status.json").read_text(encoding="utf-8")
        )
        if status.get("party_mode_active") is not False:
            raise RuntimeError("Party Mode remained active after packaged review shutdown.")
        forbidden = {
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

        def field_names(value: object) -> set[str]:
            if isinstance(value, dict):
                result = {str(key).casefold() for key in value}
                for item in value.values():
                    result.update(field_names(item))
                return result
            if isinstance(value, list):
                result: set[str] = set()
                for item in value:
                    result.update(field_names(item))
                return result
            return set()

        if forbidden.intersection(field_names(status)):
            raise RuntimeError("Party Mode App Status contains audio-analysis data.")
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

    runtime_checks: dict[str, object] = {}
    for manifest in manifests:
        child_checks = manifest.get("runtime_checks", {})
        if isinstance(child_checks, dict):
            runtime_checks.update(child_checks)

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
            "runtime_checks": runtime_checks,
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
        sizes = tuple(args.size or DEFAULT_SIZES)
        scenes = tuple(args.page or DEFAULT_SCENES)
        dataset = seed_synthetic_runtime(
            project_root,
            runtime,
            include_party=bool(set(scenes).intersection(PARTY_SCENES)),
        )
        if not 50 <= args.settle_ms <= 5000:
            raise ValueError("--settle-ms must be between 50 and 5000.")
        environment_base = os.environ.copy()
        environment_base.update(
            {
                "MUSIC_VAULT_PROJECT_ROOT": str(runtime),
                "HOME": str(runtime / "profile"),
                "USERPROFILE": str(runtime / "profile"),
                "LOCALAPPDATA": str(runtime / "profile" / "LocalAppData"),
                "APPDATA": str(runtime / "profile" / "RoamingAppData"),
                "TEMP": str(runtime / "temp"),
                "TMP": str(runtime / "temp"),
                "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS": "1",
                "MUSIC_VAULT_DISABLE_NETWORK": "1",
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
        if args.exe is not None and set(scenes).intersection(PARTY_SCENES):
            party_behaviors = (
                (manifest.get("runtime_checks") or {}).get(
                    "party_mode_behaviors", {}
                )
            )
            if party_behaviors.get("packaged_process") is not True:
                raise RuntimeError("Party Mode smoke did not run inside the packaged EXE.")
        if args.exe is not None and set(scenes).intersection(MULTI_SOURCE_SCENES):
            multi_source_behaviors = (
                (manifest.get("runtime_checks") or {}).get(
                    "multi_source_behaviors", {}
                )
            )
            if multi_source_behaviors.get("packaged_process") is not True:
                raise RuntimeError(
                    "Multi-source smoke did not run inside the packaged EXE."
                )
        if args.exe is not None and set(scenes).intersection(
            METADATA_INTELLIGENCE_SCENES
        ):
            intelligence_behaviors = (
                (manifest.get("runtime_checks") or {}).get(
                    "metadata_intelligence_behaviors", {}
                )
            )
            if intelligence_behaviors.get("packaged_process") is not True:
                raise RuntimeError(
                    "Metadata-intelligence smoke did not run inside the packaged EXE."
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
                    "schema_version": dataset["schema_version"],
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
