from __future__ import annotations

"""Prepare and verify the one bounded Batch 11 quality E2E gate.

Stage A is entirely synthetic and offline.  It drives the production quality
planner, inspection, importer, source reconciliation, and schema APIs, then
prepares one existing packaged UI-review scene for playback/queue/Party Mode.
Stage B helpers only capture and verify a live schema migration; the guarded
PowerShell wrapper exclusively owns the opt-in EXE launch.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.dev import batch11_acceptance as acceptance  # noqa: E402
from music_vault.core.youtube_sync import (  # noqa: E402
    AuthorizedYouTubePlaylistSyncer,
)


STAGE_A_MANIFEST_NAME = "stage-a-manifest.json"
STAGE_A_SUMMARY_NAME = "stage-a-summary.json"
STAGE_B_SUMMARY_NAME = "stage-b-summary.json"
FINAL_SUMMARY_NAME = "batch11-e2e-summary.json"
REVIEW_PLAN_NAME = "batch11-review-plan.json"
REVIEW_OUTPUT_SUFFIX = "_Review"
NETWORK_GUARD_DIRECTORY = "MusicVault_Batch10_6_NetworkGuard"
PREPARATION_NETWORK_REPORT_NAME = "batch11-preparation-network-report.json"
NETWORK_REPORT_NAME = "batch11-network-report.json"
SYNTHETIC_DURATION_SECONDS = 4.0
PARTY_REVIEW_DURATION_SECONDS = 45.0


VIDEO_IDS = {
    "opus": "B11OPUS0001",
    "aac": "B11AAC00001",
    "shared": "B11SHARED01",
    "compatibility": "B11COMP0001",
    "muxed": "B11MUXED001",
}
SOURCE_IDS = {
    "a": "PLB11SOURCEA0000000000000000000000",
    "b": "PLB11SOURCEB0000000000000000000000",
    "c": "PLB11SOURCEC0000000000000000000000",
}


class SyntheticGateFailure(acceptance.AcceptanceFailure):
    """An aggregate-only failure from the synthetic quality scenario."""


class _OfflineAudit:
    EVENTS = frozenset(
        {
            "socket.connect",
            "socket.connect_ex",
            "socket.getaddrinfo",
            "socket.gethostbyaddr",
            "socket.gethostbyname",
            "socket.gethostbyname_ex",
            "socket.getnameinfo",
            "socket.sendto",
        }
    )

    def __init__(self) -> None:
        self.attempt_count = 0
        self.secret_file_open_attempt_count = 0

    def install(self) -> None:
        def audit(event: str, arguments: tuple[object, ...]) -> None:
            if event in self.EVENTS:
                self.attempt_count += 1
                raise SyntheticGateFailure("synthetic_network_access_blocked")
            if event == "open" and arguments and _is_secret_named_path(arguments[0]):
                self.secret_file_open_attempt_count += 1
                raise SyntheticGateFailure("synthetic_secret_file_access_blocked")

        sys.addaudithook(audit)


def _is_secret_named_path(candidate: object) -> bool:
    """Recognize credential filenames without resolving or opening a path."""

    if not isinstance(candidate, (str, bytes, os.PathLike)):
        return False
    try:
        basename = Path(os.fsdecode(candidate)).name.casefold()
    except (TypeError, ValueError):
        return False
    return basename.endswith(("_api_key.txt", "_token.txt"))


def _run_local(command: Sequence[str], *, timeout: float = 45.0) -> None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(1.0, min(float(timeout), 120.0)),
            check=False,
            shell=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SyntheticGateFailure("bounded_local_media_command_failed") from None
    if completed.returncode != 0:
        raise SyntheticGateFailure("bounded_local_media_command_failed")


def _ffmpeg_pair() -> tuple[Path, Path]:
    from music_vault.core.ffmpeg import discover_ffmpeg

    result = discover_ffmpeg()
    if not result.ready or result.ffmpeg_path is None or result.ffprobe_path is None:
        raise SyntheticGateFailure("ffmpeg_and_ffprobe_required")
    return result.ffmpeg_path, result.ffprobe_path


def _generate_sine(
    ffmpeg: Path,
    destination: Path,
    *,
    codec: str,
    container: str | None = None,
    frequency: int = 440,
    duration_seconds: float = SYNTHETIC_DURATION_SECONDS,
    channels: int = 2,
) -> None:
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={int(frequency)}:duration={float(duration_seconds)}",
        "-ac",
        str(int(channels)),
        "-ar",
        "48000",
        "-c:a",
        codec,
    ]
    if codec == "libopus":
        command.extend(("-b:a", "128k"))
    elif codec == "aac":
        command.extend(("-b:a", "160k"))
    elif codec == "libmp3lame":
        command.extend(("-b:a", "320k"))
    elif codec == "libvorbis":
        command.extend(("-q:a", "5"))
    if container:
        command.extend(("-f", container))
    command.append(str(destination))
    _run_local(command)


def _generate_muxed_aac(ffmpeg: Path, destination: Path) -> None:
    _run_local(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=160x90:rate=12:duration={SYNTHETIC_DURATION_SECONDS}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=720:duration={SYNTHETIC_DURATION_SECONDS}",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-q:v",
            "8",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            str(destination),
        ]
    )


def _create_media_fixtures(runtime: Path, ffmpeg: Path) -> dict[str, Path]:
    fixtures = runtime / "synthetic-inputs"
    fixtures.mkdir(parents=True)
    paths = {
        "opus": fixtures / "source-opus.webm",
        "aac": fixtures / "source-aac.m4a",
        "shared": fixtures / "source-shared.webm",
        "compatibility": fixtures / "source-compatibility.webm",
        "muxed": fixtures / "source-muxed-aac.mp4",
        "ogg": fixtures / "playback-vorbis.ogg",
    }
    _generate_sine(ffmpeg, paths["opus"], codec="libopus", container="webm", frequency=440)
    _generate_sine(ffmpeg, paths["aac"], codec="aac", frequency=520)
    _generate_sine(ffmpeg, paths["shared"], codec="libopus", container="webm", frequency=600)
    _generate_sine(
        ffmpeg,
        paths["compatibility"],
        codec="libopus",
        container="webm",
        frequency=680,
    )
    _generate_muxed_aac(ffmpeg, paths["muxed"])
    _generate_sine(ffmpeg, paths["ogg"], codec="libvorbis", frequency=760)
    return paths


def _source_formats(kind: str, source: Path) -> tuple[dict[str, Any], ...]:
    common = {
        "filesize": int(source.stat().st_size),
        "duration": SYNTHETIC_DURATION_SECONDS,
        "asr": 48000,
        "audio_channels": 2,
    }
    if kind in {"opus", "shared", "compatibility"}:
        return (
            {
                **common,
                "format_id": f"synthetic-ranked-{kind}",
                "ext": "webm",
                "container": "webm",
                "acodec": "opus",
                "vcodec": "none",
                "abr": 128,
                "quality": 4,
            },
        )
    if kind == "aac":
        return (
            {
                **common,
                "format_id": "synthetic-ranked-aac",
                "ext": "m4a",
                "container": "m4a_dash",
                "acodec": "mp4a.40.2",
                "vcodec": "none",
                "abr": 160,
                "quality": 5,
            },
        )
    if kind == "muxed":
        return (
            {
                **common,
                "format_id": "synthetic-ranked-muxed-aac",
                "ext": "mp4",
                "container": "mp4",
                "acodec": "mp4a.40.2",
                "vcodec": "mpeg4",
                "abr": 160,
                "quality": 3,
            },
        )
    raise SyntheticGateFailure("synthetic_source_kind_invalid")


class FakeYouTubeDataAPI:
    """A local, bounded playlist enumerator with no HTTP capability."""

    def __init__(self, datasets: Mapping[str, tuple[str, ...]]) -> None:
        self.datasets = dict(datasets)
        self.call_count = 0

    def enumerate(self, playlist_id: str) -> tuple[str, ...]:
        self.call_count += 1
        try:
            return self.datasets[playlist_id]
        except KeyError:
            raise SyntheticGateFailure("synthetic_playlist_missing") from None


class FakeYtDlpDownloader:
    """A deterministic local stand-in that applies the production plan."""

    def __init__(
        self,
        *,
        inputs: Mapping[str, Path],
        kinds_by_video: Mapping[str, str],
        ffmpeg: Path,
        ffprobe: Path,
    ) -> None:
        self.inputs = dict(inputs)
        self.kinds_by_video = dict(kinds_by_video)
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.call_count = 0
        self.verified_audio_only_count = 0
        self.source_codec_preserved_count = 0
        self.compatibility_transcode_count = 0
        self.muxed_fallback_count = 0
        self.production_option_verification_count = 0

    def acquire(self, video_id: str, destination: Path, profile: str):
        from music_vault.core.audio_inspection import (
            DeterministicFinalPathTracker,
            inspect_audio_file,
            require_verified_final_audio,
        )
        from music_vault.core.youtube_audio_options import build_audio_download_plan

        self.call_count += 1
        kind = self.kinds_by_video[video_id]
        source = self.inputs[kind]
        formats = _source_formats(kind, source)
        plan = build_audio_download_plan(formats, profile)
        destination.mkdir(parents=True, exist_ok=True)
        final = destination / f"Synthetic acceptance [{video_id}]{plan.output_extension}"
        if plan.profile == "mp3_320_compatibility":
            command = [
                str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(source), "-map", "0:a:0", "-vn", "-c:a", "libmp3lame",
                "-b:a", "320k", str(final),
            ]
        elif kind == "aac":
            shutil.copy2(source, final)
            command = None
        else:
            command = [
                str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(source), "-map", "0:a:0", "-vn", "-c:a", "copy", str(final),
            ]
        if command is not None:
            _run_local(command)

        tracker = DeterministicFinalPathTracker(destination, video_id)
        tracker.postprocessor_hook(
            {
                "status": "finished",
                "filepath": str(final),
                "info_dict": {"filepath": str(final)},
            }
        )
        resolved = tracker.resolve_final_path(expected_extension=plan.output_extension)
        inspection = inspect_audio_file(resolved, ffprobe_path=self.ffprobe)
        require_verified_final_audio(
            inspection,
            expected_codec=plan.expected_final_codec,
            expected_duration_seconds=SYNTHETIC_DURATION_SECONDS,
        )
        self.verified_audio_only_count += 1
        if plan.profile == "best_original":
            self.source_codec_preserved_count += 1
        else:
            self.compatibility_transcode_count += 1
        if plan.source.has_video:
            self.muxed_fallback_count += 1
        source_format = plan.source
        quality_facts = {
            "acquisition_profile": plan.profile,
            "source_format_id": source_format.format_id,
            "source_extension": source_format.extension,
            "source_container": source_format.container,
            "source_codec": source_format.codec,
            "source_bitrate_kbps": source_format.bitrate_kbps,
            "source_sample_rate_hz": source_format.sample_rate_hz,
            "source_channels": source_format.channels,
            "source_filesize_bytes": int(source.stat().st_size),
            "stored_extension": inspection.extension,
            "stored_container": inspection.container,
            "stored_codec": inspection.codec,
            "stored_bitrate_kbps": inspection.bitrate_kbps,
            "stored_sample_rate_hz": inspection.sample_rate_hz,
            "stored_channels": inspection.channels,
            "stored_filesize_bytes": inspection.filesize_bytes,
            "transformation_kind": plan.transformation_kind,
            "inspection_state": "inspected",
            "provenance": {
                "kind": "synthetic_offline_batch11_acceptance",
                "source_selected_dynamically": True,
                "codec_verified": True,
            },
            "inspected_at": acceptance.utc_now(),
        }
        return resolved, plan, inspection, quality_facts


class FakeYoutubeDLBoundary:
    """Local yt-dlp transport boundary used by the production sync worker."""

    def __init__(self, options: Mapping[str, Any], downloader: FakeYtDlpDownloader) -> None:
        self.options = dict(options)
        self.downloader = downloader

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None

    def extract_info(self, url: str, *, download: bool):
        from music_vault.core.youtube_audio_options import (
            build_audio_download_plan,
            build_yt_dlp_audio_options,
        )

        video_id = str(url).rsplit("=", 1)[-1]
        kind = self.downloader.kinds_by_video[video_id]
        source = self.downloader.inputs[kind]
        formats = _source_formats(kind, source)
        if not download:
            return {
                "id": video_id,
                "duration": SYNTHETIC_DURATION_SECONDS,
                "upload_date": "20260721",
                "formats": list(formats),
            }

        processors = self.options.get("postprocessors")
        if not isinstance(processors, list) or not processors:
            raise SyntheticGateFailure("production_download_options_missing")
        extract = processors[0]
        profile = (
            "best_original"
            if extract.get("preferredcodec") == "best"
            else "mp3_320_compatibility"
        )
        plan = build_audio_download_plan(formats, profile)
        expected = build_yt_dlp_audio_options(
            plan,
            embed_thumbnail=profile == "mp3_320_compatibility",
            retain_thumbnail=profile == "best_original",
        )
        for key in ("format", "writethumbnail", "embedthumbnail", "postprocessors"):
            if self.options.get(key) != expected.get(key):
                raise SyntheticGateFailure("production_download_options_mismatch")
        self.downloader.production_option_verification_count += 1
        destination = Path(str(self.options["outtmpl"])).parent
        final, _plan, _inspection, _facts = self.downloader.acquire(
            video_id,
            destination,
            profile,
        )
        info = {
            "id": video_id,
            "filepath": str(final),
            "filename": str(final),
            "upload_date": "20260721",
            "requested_downloads": [{"filepath": str(final)}],
        }
        event = {
            "status": "finished",
            "filepath": str(final),
            "filename": str(final),
            "info_dict": info,
        }
        for hook in self.options.get("progress_hooks", ()):
            hook(event)
        for hook in self.options.get("postprocessor_hooks", ()):
            hook(event)
        return info


class FakeSyncer(AuthorizedYouTubePlaylistSyncer):
    def __init__(
        self,
        config,
        *,
        api: FakeYouTubeDataAPI,
        progress=None,
    ) -> None:
        from music_vault.core.runtime_policy import runtime_policy_for

        policy = runtime_policy_for()
        if policy.network_allowed or policy.secrets_allowed:
            raise SyntheticGateFailure("synthetic_syncer_requires_offline_no_secret_mode")
        # Production construction correctly refuses acceptance mode.  Initialize
        # only its ordinary in-memory/path state so this local boundary can use
        # the exact inherited sync() and download-option implementation without
        # weakening that production guard or touching a credential source.
        self.config = config
        self.progress = progress or (lambda _message: None)
        self._ffmpeg_discovery = None
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self.config.archive_file.parent.mkdir(parents=True, exist_ok=True)
        self.api = api

    def _extract_playlist_entries_via_api(self):
        playlist_id = self._playlist_id()
        video_ids = self.api.enumerate(playlist_id)
        entries = [
            {
                "id": video_id,
                "source_item_id": (
                    f"synthetic-occurrence-{playlist_id[-3:]}-{index}"
                ),
                "position": index,
                "title": f"Synthetic item {index + 1}",
                "unavailable_reason": None,
            }
            for index, video_id in enumerate(video_ids)
        ]
        return playlist_id, "Synthetic offline source", entries


def _wait_for(predicate, app, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def _verify_native_qt_playback(paths: Sequence[Path]) -> dict[str, Any]:
    from PySide6.QtCore import QUrl
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    player = QMediaPlayer()
    output = QAudioOutput()
    output.setMuted(True)
    player.setAudioOutput(output)
    verified: list[str] = []
    try:
        for path in paths:
            player.stop()
            player.setSource(QUrl.fromLocalFile(str(path.resolve())))
            loaded = _wait_for(
                lambda: player.duration() > 0
                or player.mediaStatus() == QMediaPlayer.MediaStatus.InvalidMedia,
                app,
                8.0,
            )
            if not loaded or player.mediaStatus() == QMediaPlayer.MediaStatus.InvalidMedia:
                raise SyntheticGateFailure("native_media_load_failed")
            player.play()
            if not _wait_for(
                lambda: player.playbackState() == QMediaPlayer.PlaybackState.PlayingState,
                app,
                4.0,
            ):
                raise SyntheticGateFailure("native_media_play_failed")
            player.pause()
            if not _wait_for(
                lambda: player.playbackState() == QMediaPlayer.PlaybackState.PausedState,
                app,
                2.0,
            ):
                raise SyntheticGateFailure("native_media_pause_failed")
            seek_target = min(max(250, player.duration() // 2), max(250, player.duration() - 250))
            player.setPosition(seek_target)
            if not _wait_for(
                lambda: abs(player.position() - seek_target) <= 250,
                app,
                3.0,
            ):
                raise SyntheticGateFailure("native_media_seek_failed")
            player.play()
            if not _wait_for(
                lambda: player.playbackState() == QMediaPlayer.PlaybackState.PlayingState,
                app,
                2.0,
            ):
                raise SyntheticGateFailure("native_media_resume_failed")
            verified.append(path.suffix.casefold())
    finally:
        player.stop()
    return {
        "formats_loaded": sorted(set(verified)),
        "load_play_pause_seek_resume": True,
        "single_qmedia_player": True,
    }


def _verify_source_transport(track_ids: Mapping[str, int]) -> dict[str, Any]:
    """Exercise queue/base-context invariants on the real window, offscreen."""

    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication

    from music_vault.app import MusicVaultWindow

    app = QApplication.instance() or QApplication([])
    window = MusicVaultWindow()
    window.audio_output.setMuted(True)
    base = [track_ids["opus"], track_ids["aac"], track_ids["muxed"]]
    queued = [track_ids["compatibility"], track_ids["shared"]]
    try:
        window.base_playback_context = {
            "kind": "library",
            "playlist_id": None,
            "playlist_name": "Synthetic quality acceptance",
            "track_ids": list(base),
            "current_track_id": base[0],
        }
        if not window.play_track_by_id(
            base[0], capture_base_context=False, show_missing_warning=False
        ):
            raise SyntheticGateFailure("source_transport_initial_track_failed")
        window.manual_queue = list(queued)
        window.play_next()
        if not (
            window.current_track_id == queued[0]
            and window.manual_queue == queued[1:]
            and window.base_playback_context["current_track_id"] == base[0]
        ):
            raise SyntheticGateFailure("source_transport_fifo_first_failed")
        window.play_next()
        if not (
            window.current_track_id == queued[1]
            and not window.manual_queue
            and window.base_playback_context["current_track_id"] == base[0]
        ):
            raise SyntheticGateFailure("source_transport_fifo_second_failed")
        window.play_next()
        if not (
            window.current_track_id == base[1]
            and window.base_playback_context["current_track_id"] == base[1]
        ):
            raise SyntheticGateFailure("source_transport_queue_return_failed")
        window.play_previous()
        if window.current_track_id != base[0]:
            raise SyntheticGateFailure("source_transport_previous_failed")

        window.toggle_shuffle()
        if not window.shuffle_enabled or window.autoplay_enabled:
            raise SyntheticGateFailure("source_transport_shuffle_exclusion_failed")
        window.toggle_autoplay()
        if not window.autoplay_enabled or window.shuffle_enabled:
            raise SyntheticGateFailure("source_transport_auto_exclusion_failed")
        window.cycle_repeat()
        window.cycle_repeat()
        if window.repeat_mode != "one":
            raise SyntheticGateFailure("source_transport_repeat_one_failed")
        window.manual_queue = [queued[0]]
        current_before_repeat = window.current_track_id
        window.on_media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
        if not (
            window.current_track_id == current_before_repeat
            and window.manual_queue == [queued[0]]
        ):
            raise SyntheticGateFailure("source_transport_repeat_one_queue_guard_failed")
        window.cycle_repeat()
        window.on_media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
        if window.current_track_id != queued[0] or window.manual_queue:
            raise SyntheticGateFailure("source_transport_end_of_media_queue_failed")
        window.on_media_error(QMediaPlayer.Error.ResourceError, "synthetic error")
        if not _wait_for(
            lambda: (
                window.current_track_id == base[1]
                and not window._handling_media_error
            ),
            app,
            3.0,
        ):
            raise SyntheticGateFailure("source_transport_error_continuation_failed")
        return {
            "fifo_queue": True,
            "base_context_return": True,
            "next_previous": True,
            "auto_shuffle_mutual_exclusion": True,
            "repeat_one_blocks_queue": True,
            "end_of_media_queue_progression": True,
            "playback_error_continuation": True,
            "same_media_player": len(window.findChildren(QMediaPlayer)) == 1,
        }
    finally:
        window.close()
        app.processEvents()


def _create_party_fixture(runtime: Path, db, ffmpeg: Path) -> dict[str, Any]:
    data = runtime / "data"
    media = data / "synthetic-party-media"
    media.mkdir(parents=True)
    ids: list[int] = []
    for index, frequency in enumerate((310, 350, 390), start=1):
        path = media / f"party-signal-{index}.wav"
        _generate_sine(
            ffmpeg,
            path,
            codec="pcm_s16le",
            frequency=frequency,
            duration_seconds=PARTY_REVIEW_DURATION_SECONDS,
            channels=1,
        )
        track_id = db.upsert_track(
            path,
            title=f"Synthetic Party Signal {index}",
            artist="Music Vault Acceptance",
            album="Synthetic Offline Signals",
            duration_seconds=PARTY_REVIEW_DURATION_SECONDS,
            source_kind="local",
        )
        ids.append(track_id)
    media.joinpath("party-signal-1.lrc").write_text(
        "[00:00.00]Synthetic opening line\n[00:01.20]Synthetic current line\n"
        "[00:02.40]Synthetic following line\n",
        encoding="utf-8",
    )
    media.joinpath("party-signal-2.txt").write_text(
        "Synthetic plain lyric line one\nSynthetic plain lyric line two\n",
        encoding="utf-8",
    )
    fixture = {
        "schema_version": 1,
        "synthetic_only": True,
        "track_ids": ids[:2],
        "queue_track_id": ids[2],
    }
    acceptance.atomic_write_json(data / "synthetic_party_mode_review.json", fixture)
    return {"track_count": 3, "fixture_valid": True}


def _runtime_config(runtime: Path, downloads: Path) -> dict[str, Any]:
    return {
        "onboarding_completed": True,
        "download_folder": str(downloads),
        "audio_quality": "320",
        "download_quality_profile": "best_original",
        "compatibility_mp3_bitrate_kbps": 320,
        "volume_percent": 23,
        "artist_image_fetch_enabled": False,
        "metadata_intelligence_enabled": False,
        "metadata_discogs_enabled": False,
        "metadata_musicbrainz_secondary_enabled": False,
        "metadata_writeback_enabled": False,
        "metadata_fill_missing_artwork_enabled": False,
        "party_mode_lyrics_enabled": False,
    }


def _review_plan(runtime: Path, output: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runtime_root": str(runtime),
        "output_dir": str(output),
        "sizes": [{"width": 1280, "height": 720}],
        "scenes": ["party_mode_smoke"],
        "settle_ms": 100,
        "expected_capture_count": 1,
    }


def _quality_database_metrics(database: Path) -> dict[str, Any]:
    connection = acceptance.readonly(database, immutable=False)
    try:
        track_count = int(connection.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
        acquisition_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM track_media_quality "
                "WHERE acquisition_profile IN ('best_original','mp3_320_compatibility')"
            ).fetchone()[0]
        )
        shared_rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM tracks WHERE source_video_id=?",
                (VIDEO_IDS["shared"],),
            ).fetchone()[0]
        )
        shared_occurrences = int(
            connection.execute(
                "SELECT COUNT(*) FROM sync_source_items WHERE video_id=? AND removed_at IS NULL",
                (VIDEO_IDS["shared"],),
            ).fetchone()[0]
        )
        shared_track_ids = int(
            connection.execute(
                "SELECT COUNT(DISTINCT track_id) FROM sync_source_items "
                "WHERE video_id=? AND removed_at IS NULL",
                (VIDEO_IDS["shared"],),
            ).fetchone()[0]
        )
        output_rows = connection.execute(
            "SELECT acquisition_profile,source_codec,stored_codec,stored_extension,"
            "transformation_kind,stored_filesize_bytes FROM track_media_quality "
            "WHERE acquisition_profile IN ('best_original','mp3_320_compatibility')"
        ).fetchall()
        source_profile_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT download_quality_profile,COUNT(*) FROM sync_sources "
                "GROUP BY download_quality_profile ORDER BY download_quality_profile"
            )
        }
        invalid = 0
        total_bytes = 0
        profile_counts: dict[str, int] = {}
        transformations: dict[str, int] = {}
        for row in output_rows:
            profile = str(row[0])
            source_codec = str(row[1] or "")
            stored_codec = str(row[2] or "")
            transformation = str(row[4])
            profile_counts[profile] = profile_counts.get(profile, 0) + 1
            transformations[transformation] = transformations.get(transformation, 0) + 1
            total_bytes += int(row[5] or 0)
            if profile == "best_original" and source_codec != stored_codec:
                invalid += 1
            if profile == "mp3_320_compatibility" and not (
                stored_codec == "mp3" and transformation == "lossy_transcode"
            ):
                invalid += 1
        runs = connection.execute(
            "SELECT source_preserved_count,source_preserved_remux_count,"
            "mp3_compatibility_transcode_count,quality_failure_count,total_stored_bytes "
            "FROM sync_source_runs ORDER BY id"
        ).fetchall()
        run_totals = [sum(int(row[index] or 0) for row in runs) for index in range(5)]
        if not (
            acquisition_count == 5
            and shared_rows == 1
            and shared_occurrences == 2
            and shared_track_ids == 1
            and invalid == 0
            and profile_counts == {
                "best_original": 4,
                "mp3_320_compatibility": 1,
            }
            and source_profile_counts == {
                "best_original": 1,
                "inherit": 1,
                "mp3_320_compatibility": 1,
            }
            and transformations == {
                "none": 1,
                "source_preserved_remux": 3,
                "lossy_transcode": 1,
            }
            and run_totals[0] + run_totals[1] + run_totals[2] == 5
            and run_totals[2] == 1
            and run_totals[3] == 0
            and run_totals[4] == total_bytes
        ):
            raise SyntheticGateFailure("synthetic_quality_database_reconciliation_failed")
        return {
            "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
            "track_count": track_count,
            "acquired_quality_row_count": acquisition_count,
            "profile_counts": profile_counts,
            "source_profile_counts": source_profile_counts,
            "transformation_counts": transformations,
            "shared_canonical_track_count": shared_rows,
            "shared_source_occurrence_count": shared_occurrences,
            "shared_distinct_track_count": shared_track_ids,
            "quality_fact_mismatch_count": invalid,
            "sync_run_count": len(runs),
            "sync_metric_totals": {
                "source_preserved": run_totals[0],
                "source_preserved_remux": run_totals[1],
                "mp3_compatibility_transcode": run_totals[2],
                "quality_failure": run_totals[3],
                "stored_bytes": run_totals[4],
            },
        }
    finally:
        connection.close()


def prepare_stage_a(
    *,
    project_root: Path,
    runtime: Path,
    review_output: Path,
) -> dict[str, Any]:
    runtime = acceptance.safe_temporary_root(runtime, must_exist=False)
    if review_output.resolve() != runtime.with_name(runtime.name + REVIEW_OUTPUT_SUFFIX):
        raise SyntheticGateFailure("unsafe_review_output")
    if review_output.exists():
        raise SyntheticGateFailure("review_output_already_exists")
    executable = project_root / "dist" / "MusicVault" / "MusicVault.exe"
    if not executable.is_file():
        raise SyntheticGateFailure("official_executable_unavailable")
    if (project_root / "dist" / "MusicVault" / "data").exists():
        raise SyntheticGateFailure("distribution_runtime_data_folder_present")

    audit = _OfflineAudit()
    audit.install()
    project_runtime_before = acceptance.runtime_guard(project_root, content=True)
    project_database_before = acceptance.file_guard(
        project_root / "data" / "music_vault.sqlite3",
        content=True,
    )

    runtime.mkdir(parents=True)
    (runtime / "music_vault").mkdir()
    (runtime / "run.py").write_text(
        "# isolated Batch 11 packaged acceptance marker\n",
        encoding="utf-8",
    )
    acceptance.atomic_write_json(
        runtime / "music-vault.portable.json",
        {
            "schema_version": 1,
            "product": "Music Vault",
            "portable": True,
            "data_directory": "data",
        },
    )
    data = runtime / "data"
    downloads = data / "youtube_downloads"
    downloads.mkdir(parents=True)
    acceptance.atomic_write_json(
        data / "music_vault_config.json",
        _runtime_config(runtime, downloads),
    )

    ffmpeg, ffprobe = _ffmpeg_pair()
    fixtures = _create_media_fixtures(runtime, ffmpeg)

    from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
    from music_vault.core import youtube_sync as youtube_sync_module
    from music_vault.core.multi_source_sync import MultiSourceSyncOrchestrator
    from music_vault.core.paths import clear_configured_data_dir, configure_data_dir
    from music_vault.core.sync_sources import SyncSourceService

    if CURRENT_SCHEMA_VERSION != acceptance.POST_SCHEMA_VERSION:
        raise SyntheticGateFailure("unexpected_application_schema_version")
    configuration = configure_data_dir(data, persist=False, create=True)
    if not configuration.configured or configuration.path != data.resolve():
        raise SyntheticGateFailure("synthetic_data_directory_not_isolated")
    database = data / "music_vault.sqlite3"
    db = MusicVaultDB(database, youtube_download_root=downloads)
    try:
        service = SyncSourceService(db)
        source_profiles = {
            "a": "best_original",
            "b": "mp3_320_compatibility",
            "c": "inherit",
        }
        for key in ("a", "b", "c"):
            playlist = db.create_playlist(f"Synthetic Source {key.upper()}")
            service.create_source(
                f"https://www.youtube.com/playlist?list={SOURCE_IDS[key]}",
                label=f"Synthetic Source {key.upper()}",
                destination_kind="playlist",
                destination_playlist_id=playlist,
                download_quality_profile=source_profiles[key],
            )

        datasets = {
            SOURCE_IDS["a"]: (VIDEO_IDS["opus"], VIDEO_IDS["aac"], VIDEO_IDS["shared"]),
            SOURCE_IDS["b"]: (VIDEO_IDS["compatibility"], VIDEO_IDS["shared"]),
            SOURCE_IDS["c"]: (VIDEO_IDS["muxed"],),
        }
        kinds = {video_id: kind for kind, video_id in VIDEO_IDS.items()}
        api = FakeYouTubeDataAPI(datasets)
        downloader = FakeYtDlpDownloader(
            inputs=fixtures,
            kinds_by_video=kinds,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )

        def factory(config, progress):
            return FakeSyncer(config, api=api, progress=progress)

        orchestrator = MultiSourceSyncOrchestrator(
            db,
            downloads,
            archive_file=data / "youtube_download_archive.txt",
            download_quality_profile="best_original",
            compatibility_mp3_bitrate_kbps=320,
            ffmpeg_location=ffmpeg.parent,
            source_service=service,
            syncer_factory=factory,
        )
        if FakeSyncer.sync is not AuthorizedYouTubePlaylistSyncer.sync:
            raise SyntheticGateFailure("production_sync_worker_not_exercised")
        original_youtube_dl = youtube_sync_module.yt_dlp.YoutubeDL
        youtube_sync_module.yt_dlp.YoutubeDL = (
            lambda options: FakeYoutubeDLBoundary(options, downloader)
        )
        try:
            outcome = orchestrator.sync_all_enabled()
        finally:
            youtube_sync_module.yt_dlp.YoutubeDL = original_youtube_dl
        if not (
            outcome.status == "complete"
            and outcome.total_downloaded == 5
            and outcome.total_imported == 5
            and outcome.total_existing == 1
            and outcome.total_failed_items == 0
            and api.call_count == 3
            and downloader.call_count == 5
            and downloader.verified_audio_only_count == 5
            and downloader.source_codec_preserved_count == 4
            and downloader.compatibility_transcode_count == 1
            and downloader.muxed_fallback_count == 1
            and downloader.production_option_verification_count == 5
            and outcome.reused_quality_profile_counts == {"best_original": 1}
            and outcome.reused_stored_codec_counts == {"opus": 1}
        ):
            raise SyntheticGateFailure("synthetic_multi_source_outcome_failed")

        outputs = {
            str(row[0]): Path(str(row[1]))
            for row in db.conn.execute(
                "SELECT source_video_id,path FROM tracks WHERE source_video_id IS NOT NULL"
            )
        }
        native_playback = _verify_native_qt_playback(
            (
                outputs[VIDEO_IDS["opus"]],
                outputs[VIDEO_IDS["aac"]],
                outputs[VIDEO_IDS["compatibility"]],
                fixtures["ogg"],
            )
        )
        party_fixture = _create_party_fixture(runtime, db, ffmpeg)
        quality_metrics = _quality_database_metrics(database)
        transport_track_ids = {
            kind: int(
                db.conn.execute(
                    "SELECT id FROM tracks WHERE source_video_id=?",
                    (video_id,),
                ).fetchone()[0]
            )
            for kind, video_id in VIDEO_IDS.items()
        }
    finally:
        db.close()

    try:
        source_transport = _verify_source_transport(transport_track_ids)
    finally:
        clear_configured_data_dir()

    acceptance.atomic_write_json(runtime / REVIEW_PLAN_NAME, _review_plan(runtime, review_output))
    if audit.attempt_count:
        raise SyntheticGateFailure("synthetic_network_attempt_observed")
    if audit.secret_file_open_attempt_count:
        raise SyntheticGateFailure("synthetic_secret_file_access_observed")
    manifest = {
        "evidence_schema_version": acceptance.EVIDENCE_SCHEMA_VERSION,
        "stage": "isolated_packaged_quality_scenario",
        "prepared_at": acceptance.utc_now(),
        "runtime_token": hashlib_sha256(str(runtime).casefold()),
        "project_runtime_before": project_runtime_before,
        "project_database_before": project_database_before,
        "quality_metrics_before_packaged_launch": quality_metrics,
        "fake_data_api_call_count": api.call_count,
        "fake_downloader_call_count": downloader.call_count,
        "verified_audio_only_output_count": downloader.verified_audio_only_count,
        "source_codec_preserved_output_count": (
            downloader.source_codec_preserved_count
        ),
        "compatibility_transcode_output_count": (
            downloader.compatibility_transcode_count
        ),
        "muxed_audio_fallback_output_count": downloader.muxed_fallback_count,
        "production_sync_worker_exercised": True,
        "production_download_option_verification_count": (
            downloader.production_option_verification_count
        ),
        "reused_quality_profile_counts": dict(
            outcome.reused_quality_profile_counts
        ),
        "reused_stored_codec_counts": dict(outcome.reused_stored_codec_counts),
        "native_playback": native_playback,
        "source_transport": source_transport,
        "party_fixture": party_fixture,
        "synthetic_only": True,
        "network_attempt_count": audit.attempt_count,
        "secret_file_open_attempt_count": audit.secret_file_open_attempt_count,
        "credential_contents_read": False,
        "personal_values_emitted": False,
    }
    acceptance.atomic_write_json(runtime / STAGE_A_MANIFEST_NAME, manifest)
    return manifest


def hashlib_sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_stage_a(
    *,
    project_root: Path,
    runtime: Path,
    review_output: Path,
    graceful_close_confirmed: bool,
    external_network_connection_observed: bool,
) -> dict[str, Any]:
    runtime = acceptance.safe_temporary_root(runtime, must_exist=True)
    if not graceful_close_confirmed:
        raise SyntheticGateFailure("packaged_process_not_closed_gracefully")
    if external_network_connection_observed:
        raise SyntheticGateFailure("packaged_external_network_connection_observed")
    manifest = acceptance.read_json(runtime / STAGE_A_MANIFEST_NAME)
    if manifest.get("runtime_token") != hashlib_sha256(str(runtime).casefold()):
        raise SyntheticGateFailure("stage_a_runtime_manifest_mismatch")
    if not (
        manifest.get("production_sync_worker_exercised") is True
        and int(manifest.get("production_download_option_verification_count", -1))
        == int(manifest.get("fake_downloader_call_count", -2))
        == 5
    ):
        raise SyntheticGateFailure("production_download_path_not_verified")
    if not (
        int(manifest.get("secret_file_open_attempt_count", -1)) == 0
        and manifest.get("credential_contents_read") is False
    ):
        raise SyntheticGateFailure("stage_a_secret_access_evidence_failed")
    review = acceptance.read_json(review_output / "manifest.json")
    if not (
        review.get("status") == "complete"
        and int(review.get("capture_count", 0)) == 1
        and review.get("runtime_checks", {}).get("party_mode_behaviors", {}).get(
            "packaged_process"
        )
        is True
        and review.get("runtime_checks", {}).get("party_mode_behaviors", {}).get(
            "queue_preserved"
        )
        is True
        and review.get("runtime_checks", {}).get("party_mode_behaviors", {}).get(
            "track_transition_verified"
        )
        is True
    ):
        raise SyntheticGateFailure("packaged_playback_queue_party_review_failed")
    preparation_network = acceptance.verify_network_report(
        runtime / NETWORK_GUARD_DIRECTORY / PREPARATION_NETWORK_REPORT_NAME
    )
    network = acceptance.verify_network_report(
        runtime / NETWORK_GUARD_DIRECTORY / NETWORK_REPORT_NAME
    )
    quality_after = _quality_database_metrics(runtime / "data" / "music_vault.sqlite3")
    if quality_after != manifest.get("quality_metrics_before_packaged_launch"):
        raise SyntheticGateFailure("packaged_launch_changed_quality_inventory")
    status = acceptance.read_json(runtime / "data" / "music_vault_status.json")
    library = status.get("library") if isinstance(status.get("library"), Mapping) else {}
    expected_summary = quality_after["profile_counts"]
    if not (
        int(library.get("quality_best_original_count", -1))
        == int(expected_summary.get("best_original", 0))
        and int(library.get("quality_mp3_compatibility_count", -1))
        == int(expected_summary.get("mp3_320_compatibility", 0))
    ):
        raise SyntheticGateFailure("app_status_quality_aggregation_failed")
    if acceptance.runtime_guard(project_root, content=True) != manifest.get(
        "project_runtime_before"
    ):
        raise SyntheticGateFailure("project_runtime_changed_during_isolated_stage")
    if acceptance.file_guard(
        project_root / "data" / "music_vault.sqlite3", content=True
    ) != manifest.get("project_database_before"):
        raise SyntheticGateFailure("project_database_changed_during_isolated_stage")
    if (project_root / "dist" / "MusicVault" / "data").exists():
        raise SyntheticGateFailure("distribution_runtime_data_folder_created")
    summary = {
        "summary_schema_version": acceptance.SUMMARY_SCHEMA_VERSION,
        "stage": "isolated_packaged_quality_scenario",
        "status": "passed",
        "schema_version": quality_after["schema_version"],
        "fake_data_api_call_count": int(manifest["fake_data_api_call_count"]),
        "fake_downloader_call_count": int(manifest["fake_downloader_call_count"]),
        "verified_audio_only_output_count": int(
            manifest["verified_audio_only_output_count"]
        ),
        "source_codec_preserved_output_count": int(
            manifest["source_codec_preserved_output_count"]
        ),
        "compatibility_transcode_output_count": int(
            manifest["compatibility_transcode_output_count"]
        ),
        "muxed_audio_fallback_output_count": int(
            manifest["muxed_audio_fallback_output_count"]
        ),
        "production_sync_worker_exercised": bool(
            manifest["production_sync_worker_exercised"]
        ),
        "production_download_option_verification_count": int(
            manifest["production_download_option_verification_count"]
        ),
        "reused_quality_profile_counts": manifest[
            "reused_quality_profile_counts"
        ],
        "reused_stored_codec_counts": manifest["reused_stored_codec_counts"],
        "quality": quality_after,
        "native_playback": manifest["native_playback"],
        "source_transport": manifest["source_transport"],
        "packaged_playback_queue_party_mode": True,
        "packaged_process_closed_gracefully": True,
        "source_preparation_network": preparation_network,
        "network": network,
        "project_runtime_unchanged": True,
        "app_status_quality_aggregate_verified": True,
        "distribution_data_folder_absent": True,
        "secret_file_open_attempt_count": 0,
        "credential_contents_read": False,
        "personal_values_emitted": False,
    }
    acceptance.atomic_write_json(runtime / STAGE_A_SUMMARY_NAME, summary)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch 11 essential E2E helper")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare-stage-a")
    prepare.add_argument("--project-root", type=Path, required=True)
    prepare.add_argument("--runtime", type=Path, required=True)
    prepare.add_argument("--review-output", type=Path, required=True)

    verify = commands.add_parser("verify-stage-a")
    verify.add_argument("--project-root", type=Path, required=True)
    verify.add_argument("--runtime", type=Path, required=True)
    verify.add_argument("--review-output", type=Path, required=True)
    verify.add_argument("--graceful-close-confirmed", action="store_true")
    verify.add_argument("--external-network-connection-observed", action="store_true")

    live_prepare = commands.add_parser("prepare-live")
    live_prepare.add_argument("--project-root", type=Path, required=True)
    live_prepare.add_argument("--evidence-root", type=Path, required=True)

    live_verify = commands.add_parser("verify-live")
    live_verify.add_argument("--project-root", type=Path, required=True)
    live_verify.add_argument("--evidence-root", type=Path, required=True)
    live_verify.add_argument("--network-report", type=Path, required=True)
    live_verify.add_argument("--graceful-close-confirmed", action="store_true")
    live_verify.add_argument("--external-network-connection-observed", action="store_true")

    combine = commands.add_parser("combine")
    combine.add_argument("--stage-a", type=Path, required=True)
    combine.add_argument("--stage-b", type=Path)
    combine.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare-stage-a":
            prepare_stage_a(
                project_root=args.project_root.resolve(),
                runtime=args.runtime,
                review_output=args.review_output.resolve(),
            )
            print("Batch 11 isolated Stage A prepared.")
        elif args.command == "verify-stage-a":
            summary = verify_stage_a(
                project_root=args.project_root.resolve(),
                runtime=args.runtime,
                review_output=args.review_output.resolve(),
                graceful_close_confirmed=args.graceful_close_confirmed,
                external_network_connection_observed=(
                    args.external_network_connection_observed
                ),
            )
            print(json.dumps(summary, sort_keys=True))
        elif args.command == "prepare-live":
            evidence = acceptance.safe_temporary_root(args.evidence_root, must_exist=True)
            acceptance.prepare_live_manifest(
                project_root=args.project_root,
                evidence_root=evidence,
            )
            print("Batch 11 controlled live baseline and rollback backup verified.")
        elif args.command == "verify-live":
            evidence = acceptance.safe_temporary_root(args.evidence_root, must_exist=True)
            manifest = acceptance.read_json(evidence / "live-baseline.json")
            summary = acceptance.verify_live_migration(
                project_root=args.project_root,
                manifest=manifest,
                network_report=args.network_report,
                graceful_close_confirmed=args.graceful_close_confirmed,
                external_network_connection_observed=(
                    args.external_network_connection_observed
                ),
            )
            acceptance.atomic_write_json(evidence / STAGE_B_SUMMARY_NAME, summary)
            print(json.dumps(summary, sort_keys=True))
        else:
            stage_a = acceptance.read_json(args.stage_a)
            stage_b = acceptance.read_json(args.stage_b) if args.stage_b else None
            combined = acceptance.combine_summaries(stage_a, stage_b)
            acceptance.atomic_write_json(args.output, combined)
            print(f"Batch 11 aggregate acceptance JSON: {args.output.resolve()}")
    except Exception:
        print("Batch 11 essential E2E failed closed.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
