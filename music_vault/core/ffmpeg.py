"""Central, bounded FFmpeg/ffprobe discovery for Music Vault."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .paths import portable_root


MAX_PROBE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class FFmpegDiscoveryResult:
    ready: bool
    source: str
    bin_dir: Path | None = None
    ffmpeg_path: Path | None = None
    ffprobe_path: Path | None = None
    error: str | None = None
    error_code: str | None = None

    @property
    def yt_dlp_location(self) -> str | None:
        return str(self.bin_dir) if self.ready and self.bin_dir is not None else None


def _candidate_directories(location: str | Path) -> list[Path]:
    selected = Path(location).expanduser().resolve()
    if selected.name.casefold() in {"ffmpeg.exe", "ffprobe.exe"}:
        return [selected.parent]
    candidates = [selected, selected / "bin"]
    try:
        candidates.extend(
            child / "bin"
            for child in sorted(selected.iterdir(), key=lambda item: item.name.casefold())
            if child.is_dir()
        )
    except OSError:
        pass
    deduplicated: list[Path] = []
    for candidate in candidates:
        if candidate not in deduplicated:
            deduplicated.append(candidate)
    return deduplicated


def _pair_in(directory: Path) -> tuple[Path, Path] | None:
    ffmpeg_path = directory / "ffmpeg.exe"
    ffprobe_path = directory / "ffprobe.exe"
    if ffmpeg_path.is_file() and ffprobe_path.is_file():
        return ffmpeg_path.resolve(), ffprobe_path.resolve()
    return None


def _find_pair(location: str | Path) -> tuple[Path, Path] | None:
    for directory in _candidate_directories(location):
        pair = _pair_in(directory)
        if pair is not None:
            return pair
    return None


def _probe_pair(
    ffmpeg_path: Path,
    ffprobe_path: Path,
    *,
    timeout: float,
    runner: Callable[..., object],
) -> tuple[bool, str | None, str | None]:
    bounded_timeout = max(0.1, min(float(timeout), MAX_PROBE_TIMEOUT_SECONDS))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for name, executable in (("ffmpeg", ffmpeg_path), ("ffprobe", ffprobe_path)):
        try:
            completed = runner(
                [str(executable), "-version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=bounded_timeout,
                check=False,
                shell=False,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired:
            return False, f"{name}_probe_timeout", f"{name} did not respond within the safety timeout."
        except (OSError, ValueError):
            return False, f"{name}_probe_failed", f"{name} could not be started safely."
        if int(getattr(completed, "returncode", 1)) != 0:
            return False, f"{name}_probe_failed", f"{name} did not pass its version check."
    return True, None, None


def _result_for_pair(
    pair: tuple[Path, Path],
    source: str,
    *,
    probe: bool,
    timeout: float,
    runner: Callable[..., object],
) -> FFmpegDiscoveryResult:
    ffmpeg_path, ffprobe_path = pair
    if probe:
        ready, error_code, error = _probe_pair(
            ffmpeg_path,
            ffprobe_path,
            timeout=timeout,
            runner=runner,
        )
        if not ready:
            return FFmpegDiscoveryResult(
                False,
                source,
                ffmpeg_path.parent,
                ffmpeg_path,
                ffprobe_path,
                error,
                error_code,
            )
    return FFmpegDiscoveryResult(
        True,
        source,
        ffmpeg_path.parent,
        ffmpeg_path,
        ffprobe_path,
    )


def _explicit_result(
    location: str | Path,
    source: str,
    *,
    probe: bool,
    timeout: float,
    runner: Callable[..., object],
) -> FFmpegDiscoveryResult:
    pair = _find_pair(location)
    if pair is None:
        return FFmpegDiscoveryResult(
            False,
            source,
            error="The selected FFmpeg location must contain both ffmpeg.exe and ffprobe.exe.",
            error_code="incomplete_pair",
        )
    return _result_for_pair(pair, source, probe=probe, timeout=timeout, runner=runner)


def discover_ffmpeg(
    configured_location: str | Path | None = None,
    portable_tools_location: str | Path | None = None,
    *,
    probe: bool = True,
    timeout: float = 3.0,
    path_value: str | None = None,
    legacy_root: str | Path | None = None,
    runner: Callable[..., object] = subprocess.run,
) -> FFmpegDiscoveryResult:
    """Resolve a complete FFmpeg pair in stable priority order.

    User-provided locations fail clearly instead of silently falling through.
    Every subprocess receives an argv list, ``shell=False``, and a bounded timeout.
    """
    if configured_location is not None and str(configured_location).strip():
        return _explicit_result(
            configured_location,
            "configured",
            probe=probe,
            timeout=timeout,
            runner=runner,
        )

    selected_portable_tools = portable_tools_location
    if selected_portable_tools is None:
        root = portable_root()
        default_tools = root / "tools" / "ffmpeg" if root is not None else None
        if default_tools is not None and default_tools.exists():
            selected_portable_tools = default_tools
    if selected_portable_tools is not None and str(selected_portable_tools).strip():
        portable_pair = _find_pair(selected_portable_tools)
        if portable_pair is not None:
            return _result_for_pair(
                portable_pair,
                "portable_tools",
                probe=probe,
                timeout=timeout,
                runner=runner,
            )

    search_path = os.environ.get("PATH", "") if path_value is None else path_value
    ffmpeg_found = shutil.which("ffmpeg.exe", path=search_path)
    ffprobe_found = shutil.which("ffprobe.exe", path=search_path)
    if ffmpeg_found and ffprobe_found:
        ffmpeg_path = Path(ffmpeg_found).resolve()
        ffprobe_path = Path(ffprobe_found).resolve()
        if ffmpeg_path.parent == ffprobe_path.parent:
            return _result_for_pair(
                (ffmpeg_path, ffprobe_path),
                "path",
                probe=probe,
                timeout=timeout,
                runner=runner,
            )

    legacy = (
        Path(legacy_root).expanduser().resolve()
        if legacy_root is not None
        else Path.home() / "Documents" / "MusicVaultTools" / "ffmpeg"
    )
    pair = _find_pair(legacy)
    if pair is not None:
        return _result_for_pair(
            pair,
            "legacy_tools",
            probe=probe,
            timeout=timeout,
            runner=runner,
        )

    return FFmpegDiscoveryResult(
        False,
        "not_found",
        error="FFmpeg and ffprobe were not found.",
        error_code="not_found",
    )
