"""Disposable, headless acceptance of Music Vault's actual acquisition stack.

``offline`` exercises packaged readiness, production planning/verification and
the real importer using a generated one-second signal. ``network`` adds one
anonymous transfer of Blender's CC-BY open-film fixture used by upstream yt-dlp.
Neither mode constructs the application window, enumerates a playlist, reads
credentials, uses a personal database, or starts a metadata provider.

This developer entry point is deliberately fixed-purpose: it accepts no URL,
database, cookie, provider, arbitrary output file or download-folder argument.
The frozen executable dispatches here before importing the GUI. Only aggregate
``acceptance.json`` survives; fixture media and the temporary DB are removed.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Iterator


# Upstream replaced its deleted test video with Blender's Big Buck Bunny.
# https://github.com/yt-dlp/yt-dlp/commit/4ba601f
# Film: (c) 2008 Blender Foundation / www.bigbuckbunny.org, CC BY 3.0.
# https://peach.blender.org/about/ — only disposable audio, never redistribution.
PUBLIC_TEST_VIDEO_ID = "YE7VzlLtp-4"
TEMP_PREFIX = "MusicVault_Acquisition_"
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
MAX_ACQUISITION_SECONDS = 120
EVIDENCE_NAME = "acceptance.json"
_ROOT_NAME = re.compile(r"MusicVault_Acquisition_[0-9a-f]{32}\Z")
_CREDENTIAL_NAMES = frozenset({"youtube_api_key.txt", "discogs_token.txt"})
_PUBLIC_MEDIA_HOSTS = ("youtube.com", "googlevideo.com", "ytimg.com")


def _public_media_host(raw: object) -> bool:
    if not isinstance(raw, str):
        return False
    host = raw.casefold().rstrip(".")
    return host == "youtubei.googleapis.com" or any(
        host == domain or host.endswith("." + domain) for domain in _PUBLIC_MEDIA_HOSTS
    )


class AcceptanceFailure(RuntimeError):
    """A fixed, non-identifying acceptance failure code."""


def safe_new_root(value: str | Path) -> Path:
    """Reject existing directories, junction aliases, and repository roots."""
    candidate = Path(value).absolute()
    temporary = Path(tempfile.gettempdir()).resolve()
    resolved = candidate.resolve()
    if (
        not _ROOT_NAME.fullmatch(candidate.name)
        or candidate.parent.resolve() != temporary
        or resolved.parent != temporary
        or candidate.exists()
        or candidate.is_symlink()
    ):
        raise AcceptanceFailure("unsafe_acceptance_root")
    return resolved


def _within(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


class RuntimeGuard:
    """Fail closed for credential reads, outside writes and optional network.

    The audit hook constrains Python. Child processes are the existing bounded
    Deno/FFmpeg/ffprobe calls, never arbitrary user-supplied commands; they
    inherit the disposable working directory and isolated environment.
    """

    def __init__(self, root: Path, *, network: bool) -> None:
        self.root = root.resolve()
        self.network = network
        self.active = True
        self.credential_open_attempts = 0
        self.outside_write_attempts = 0
        self.outside_database_attempts = 0
        self.blocked_network_attempts = 0

    def _write(self, raw: object) -> None:
        if isinstance(raw, int):
            return
        if isinstance(raw, (str, bytes)) and os.fsdecode(raw).casefold() == os.devnull.casefold():
            # subprocess.DEVNULL opens the OS null device read/write. It is
            # not a file or an escape from the disposable runtime.
            return
        try:
            path = Path(os.fsdecode(raw))
        except (TypeError, ValueError):
            raise AcceptanceFailure("invalid_write_target") from None
        if not _within(path, self.root):
            self.outside_write_attempts += 1
            raise AcceptanceFailure("outside_runtime_write_blocked")

    def __call__(self, event: str, args: tuple) -> None:
        if not self.active:
            return
        if event == "open" and args:
            raw = args[0]
            if not isinstance(raw, int):
                try:
                    name = Path(os.fsdecode(raw)).name.casefold()
                except (TypeError, ValueError):
                    name = ""
                if name in _CREDENTIAL_NAMES:
                    self.credential_open_attempts += 1
                    raise AcceptanceFailure("credential_open_blocked")
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            if (
                isinstance(mode, str) and any(letter in mode for letter in "wax+")
            ) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)):
                self._write(raw)
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime"}:
            self._write(args[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            self._write(args[0])
            self._write(args[1])
        elif event == "sqlite3.connect" and args:
            if not _within(Path(str(args[0])), self.root):
                self.outside_database_attempts += 1
                raise AcceptanceFailure("outside_runtime_database_blocked")
        elif event in {
            "socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.sendto"
        }:
            if not self.network:
                self.blocked_network_attempts += 1
                raise AcceptanceFailure("offline_network_blocked")
            if event == "socket.getaddrinfo" and not _public_media_host(args[0]):
                self.blocked_network_attempts += 1
                raise AcceptanceFailure("non_fixture_network_blocked")

    def summary(self) -> dict[str, int]:
        return {
            "credential_open_attempts": self.credential_open_attempts,
            "outside_write_attempts": self.outside_write_attempts,
            "outside_database_attempts": self.outside_database_attempts,
            "blocked_network_attempts": self.blocked_network_attempts,
        }


@contextmanager
def isolated_runtime(root: Path, *, network: bool) -> Iterator[RuntimeGuard]:
    previous_environment = dict(os.environ)
    previous_directory = Path.cwd()
    previous_bytecode = sys.dont_write_bytecode
    previous_temp = tempfile.tempdir
    guard = RuntimeGuard(root, network=network)
    # Create the portable marker before any application path resolver imports.
    (root / "music-vault.portable.json").write_text(
        json.dumps({"schema_version": 1, "product": "Music Vault", "portable": True}),
        encoding="utf-8",
    )
    for name in ("data", "temp", "profile", "profile/local", "profile/roaming"):
        (root / name).mkdir(parents=True, exist_ok=True)
    values = {
        "MUSIC_VAULT_PROJECT_ROOT": str(root),
        "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS": "1",
        "MUSIC_VAULT_ACCEPTANCE_NO_NETWORK": "0" if network else "1",
        "MUSIC_VAULT_DISABLE_NETWORK": "0" if network else "1",
        "HOME": str(root / "profile"),
        "USERPROFILE": str(root / "profile"),
        "LOCALAPPDATA": str(root / "profile/local"),
        "APPDATA": str(root / "profile/roaming"),
        "TMP": str(root / "temp"),
        "TEMP": str(root / "temp"),
        "TMPDIR": str(root / "temp"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    # Do not inherit accepted API/provider values or a stale UI-review request.
    for key in tuple(os.environ):
        if key.startswith("MUSIC_VAULT_") or key in {
            "YOUTUBE_API_KEY", "DISCOGS_TOKEN", "NETRC", "YTDLP_CONFIG"
        } or key.casefold() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            os.environ.pop(key, None)
    os.environ.update(values)
    sys.dont_write_bytecode = True
    # Keep tempfile's trusted base fixed for the production public-media root
    # validation. yt-dlp receives an explicit temp directory; Deno inherits it.
    os.chdir(root)
    sys.addaudithook(guard)
    try:
        from music_vault.core import paths

        paths._resolved_project_root.cache_clear()
        paths.clear_configured_data_dir()
        if paths.project_root() != root or not _within(paths.data_dir(), root):
            raise AcceptanceFailure("runtime_path_resolution_failed")
        for target in (
            paths.database_path(), paths.config_path(), paths.app_status_path(),
            paths.default_downloads_dir(), paths.covers_dir(),
            paths.metadata_job_backups_dir(), paths.youtube_download_archive_path(),
        ):
            if not _within(target, root):
                raise AcceptanceFailure("runtime_write_target_not_isolated")
        yield guard
    finally:
        guard.active = False
        paths_module = sys.modules.get("music_vault.core.paths")
        if paths_module is not None:
            paths_module._resolved_project_root.cache_clear()
            paths_module.clear_configured_data_dir()
        os.chdir(previous_directory)
        os.environ.clear()
        os.environ.update(previous_environment)
        sys.dont_write_bytecode = previous_bytecode
        tempfile.tempdir = previous_temp


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _offline_item(root: Path, ffmpeg):
    """Use actual FFmpeg/quality planning/verification; no provider mocks."""
    from music_vault.core.audio_inspection import inspect_audio_file, require_verified_final_audio
    from music_vault.core.sync_result import SyncImportItem
    from music_vault.core.youtube_audio_options import build_audio_download_plan

    destination = root / "data/youtube_downloads"
    destination.mkdir(parents=True)
    path = destination / "Acceptance signal [abcdefghijk].flac"
    completed = subprocess.run(
        [str(ffmpeg.ffmpeg_path), "-nostdin", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=48000",
         "-c:a", "flac", "-y", str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=15, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise AcceptanceFailure("offline_fixture_generation_failed")
    plan = build_audio_download_plan([
        {"format_id": "fixture", "ext": "flac", "acodec": "flac", "vcodec": "none",
         "asr": 48000, "audio_channels": 1, "duration": 1.0,
         "filesize": path.stat().st_size}
    ], "best_original")
    inspection = inspect_audio_file(path, ffprobe_path=ffmpeg.ffprobe_path)
    require_verified_final_audio(
        inspection, expected_codec=plan.expected_final_codec, expected_duration_seconds=1.0
    )
    facts = {
        "acquisition_profile": plan.profile,
        "source_format_id": plan.source.format_id,
        "source_extension": plan.source.extension,
        "source_codec": plan.source.codec,
        "stored_extension": inspection.extension,
        "stored_container": inspection.container,
        "stored_codec": inspection.codec,
        "stored_filesize_bytes": inspection.filesize_bytes,
        "stored_sample_rate_hz": inspection.sample_rate_hz,
        "stored_channels": inspection.channels,
        "transformation_kind": plan.transformation_kind,
        "inspection_state": "inspected",
        "provenance": "youtube_download_verified",
        "inspected_at": datetime.now(timezone.utc).isoformat(),
    }
    return SyncImportItem(str(path), "abcdefghijk", quality_facts=facts)


def verify_import_boundary(root: Path, item, ffmpeg) -> dict[str, object]:
    """Exercise actual identity/quality/import code without a sync source run."""
    from music_vault.core.audio_inspection import (
        AudioInspectionError, inspect_audio_file, require_verified_final_audio,
    )
    from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB
    from music_vault.core.multi_source_sync import MultiSourceSyncOrchestrator
    from music_vault.core.sync_result import SyncResult

    path = Path(item.path).resolve()
    if not _within(path, root) or not path.is_file():
        raise AcceptanceFailure("acquired_file_not_isolated")
    if not item.quality_facts or item.quality_facts.get("inspection_state") != "inspected":
        raise AcceptanceFailure("missing_verified_quality_facts")
    inspection = inspect_audio_file(path, ffprobe_path=ffmpeg.ffprobe_path)
    require_verified_final_audio(inspection, expected_codec=item.quality_facts.get("stored_codec"))
    if inspection.filesize_bytes <= 0 or inspection.filesize_bytes > MAX_DOWNLOAD_BYTES:
        raise AcceptanceFailure("download_size_outside_budget")
    before_media = _hash_file(path)
    archive = root / "data/youtube_download_archive.txt"
    archive_before = archive.read_bytes() if archive.exists() else None
    database = MusicVaultDB(
        root / "data/music_vault.sqlite3", backup_dir=root / "data/backups",
        youtube_download_root=root / "data/youtube_downloads",
    )
    try:
        orchestrator = MultiSourceSyncOrchestrator(
            database, root / "data/youtube_downloads", archive_file=archive,
            ffmpeg_location=ffmpeg.yt_dlp_location,
        )
        counts = []
        for _ in range(2):
            result = SyncResult("complete", None, None, import_items=[item])
            imported = orchestrator._import_source_items(result, {}, {})
            if imported != 1 or result.failed_count or len(result.successful_video_ids) != 1:
                raise AcceptanceFailure("production_import_failed")
            counts.append(database.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
        if counts != [1, 1]:
            raise AcceptanceFailure("canonical_import_not_idempotent")
        quality_count = database.conn.execute("SELECT COUNT(*) FROM track_media_quality").fetchone()[0]
        identity_count = database.conn.execute("SELECT COUNT(*) FROM source_track_identities").fetchone()[0]
        if quality_count != 1 or identity_count != 1:
            raise AcceptanceFailure("quality_or_identity_not_materialized")
        if database.conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AcceptanceFailure("fixture_integrity_check_failed")
        schema_version = database.conn.execute("PRAGMA user_version").fetchone()[0]
        if schema_version != CURRENT_SCHEMA_VERSION:
            raise AcceptanceFailure("fixture_schema_version_mismatch")
        if database.conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise AcceptanceFailure("fixture_foreign_keys_disabled")
        if database.conn.execute("PRAGMA foreign_key_check").fetchall():
            raise AcceptanceFailure("fixture_foreign_key_check_failed")
        # A truncated file must fail verification before it could reach import.
        rejected = root / "data/youtube_downloads/rejected [lmnopqrstuv].flac"
        rejected.write_bytes(b"fLaC\x00")
        rejected_safely = False
        try:
            observed = inspect_audio_file(rejected, ffprobe_path=ffmpeg.ffprobe_path)
            require_verified_final_audio(observed, expected_codec="flac")
        except AudioInspectionError:
            rejected_safely = True
        rejected.unlink()
        if not rejected_safely:
            raise AcceptanceFailure("truncated_media_not_rejected")
        archive_after = archive.read_bytes() if archive.exists() else None
        if archive_before != archive_after:
            raise AcceptanceFailure("diagnostic_wrote_sync_archive")
        if _hash_file(path) != before_media:
            raise AcceptanceFailure("import_changed_media_or_tags")
        return {
            "schema_version": schema_version,
            "track_count": 1, "canonical_identity_count": identity_count,
            "quality_row_count": quality_count, "second_import_track_count": counts[1],
            "sqlite_integrity_ok": True, "foreign_keys_ok": True,
            "truncated_media_rejected": True, "archive_unchanged": True,
            "media_and_tags_unchanged_by_import": True,
            "stored_bytes": inspection.filesize_bytes,
            "stored_codec": inspection.codec,
            "duration_seconds": round(float(inspection.duration_seconds or 0), 3),
            "transformation_kind": item.quality_facts.get("transformation_kind"),
        }
    finally:
        database.close()


def run_acceptance(root: Path, *, mode: str) -> dict[str, object]:
    if mode not in {"offline", "network"}:
        raise AcceptanceFailure("unsupported_acceptance_mode")
    root = safe_new_root(root)
    root.mkdir()
    started = time.monotonic()
    summary: dict[str, object] = {
        "evidence_schema_version": 1,
        "mode": mode,
        "frozen_executable": bool(getattr(sys, "frozen", False)),
        "sample": "blender_cc_by_open_film" if mode == "network" else "generated_signal",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "passed": False,
        "playlist_enumeration_performed": False,
        "metadata_provider_lookup_performed": False,
        "personal_runtime_used": False,
    }
    guard: RuntimeGuard | None = None
    phase = "isolation"
    try:
        # Discover the app's existing executable pair before isolating HOME.
        # This is read-only tool discovery (never application config or data);
        # otherwise the normal legacy-tools fallback disappears in a clean
        # acceptance profile. Probe the selected pair only inside the guard.
        from music_vault.core.ffmpeg import discover_ffmpeg

        phase = "readiness"
        selected_ffmpeg = discover_ffmpeg(probe=False)
        phase = "isolation"
        with isolated_runtime(root, network=mode == "network") as guard:
            # Suppress backend output entirely: no raw extractor errors, signed
            # URLs, media names, or unexpected absolute paths enter evidence.
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                from music_vault.core.acquisition_runtime import acquisition_readiness
                from music_vault.core.ffmpeg import discover_ffmpeg

                phase = "readiness"
                readiness = acquisition_readiness()
                summary["readiness"] = readiness.public_summary()
                if not readiness.ready:
                    raise AcceptanceFailure("acquisition_runtime_not_ready")
                ffmpeg = discover_ffmpeg(configured_location=selected_ffmpeg.bin_dir)
                summary["ffmpeg_ready"] = ffmpeg.ready
                if not ffmpeg.ready:
                    raise AcceptanceFailure("ffmpeg_not_ready")
                phase = "media_acquisition" if mode == "network" else "offline_fixture"
                if mode == "network":
                    from music_vault.core.youtube_sync import acquire_public_audio

                    item = acquire_public_audio(
                        PUBLIC_TEST_VIDEO_ID, root / "data/youtube_downloads",
                        ffmpeg_location=ffmpeg.yt_dlp_location,
                        max_download_bytes=MAX_DOWNLOAD_BYTES,
                        timeout_seconds=MAX_ACQUISITION_SECONDS,
                    )
                else:
                    item = _offline_item(root, ffmpeg)
                phase = "verification_import"
                summary["pipeline"] = verify_import_boundary(root, item, ffmpeg)
                if any(guard.summary().values()):
                    raise AcceptanceFailure("isolation_guard_was_triggered")
                summary["passed"] = True
    except Exception as exc:
        # Our own messages are fixed codes; arbitrary provider exception text
        # deliberately never escapes. A failure does not look like a healthy
        # downloader merely because the readiness check passed.
        summary["failed_phase"] = phase
        summary["failure_code"] = (
            str(exc) if type(exc) is AcceptanceFailure else f"{phase}_failed"
        )
        # Preserve the production stage/reason, never its transient backend
        # text or media URLs. This distinguishes a healthy stack from an
        # actual transfer, transformation or verification failure.
        from music_vault.core.acquisition_diagnostics import AcquisitionDiagnostic

        diagnostic = getattr(exc, "diagnostic", None)
        if isinstance(diagnostic, AcquisitionDiagnostic):
            summary["acquisition_diagnostic"] = diagnostic.to_dict()
    finally:
        summary["guard"] = guard.summary() if guard is not None else {}
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
        # Only this freshly created, name-validated root is removable. No
        # configurable cleanup target or preexisting data enters this path.
        try:
            for child in root.iterdir():
                if child.is_symlink():
                    child.unlink()
                elif child.is_dir():
                    if not _within(child, root):
                        raise OSError("Owned runtime contains a redirected directory")
                    shutil.rmtree(child)
                else:
                    child.unlink()
            summary["runtime_payload_removed"] = True
        except OSError:
            summary["runtime_payload_removed"] = False
            summary["passed"] = False
            summary["cleanup_failure"] = "owned_runtime_cleanup_failed"
        (root / EVIDENCE_NAME).write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "network"), required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.runtime_root
    try:
        result = run_acceptance(root, mode=args.mode)
    except AcceptanceFailure:
        if sys.stderr is not None:
            print("Acquisition acceptance refused an unsafe runtime root.", file=sys.stderr)
        return 2
    if sys.stdout is not None:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    # Direct script mode needs only the source import root, never a system
    # Python installation; the PowerShell wrapper supplies the project venv.
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main())
