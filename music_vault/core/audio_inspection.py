"""Bounded, read-only audio inspection and deterministic final-path evidence."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile

from .audio_quality import (
    SUPPORTED_SOURCE_CODECS,
    SUPPORTED_STORED_AUDIO_EXTENSIONS,
    normalize_codec,
    normalize_container,
    normalize_extension,
)
from .safety import extract_source_video_id


MAX_INSPECTION_TIMEOUT_SECONDS = 10.0
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PARTIAL_ENDINGS = (".part", ".ytdl", ".tmp", ".temp")


class AudioInspectionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


class FinalPathEvidenceError(AudioInspectionError):
    pass


@dataclass(frozen=True)
class AudioInspection:
    path: Path
    extension: str | None
    container: str | None
    codec: str | None
    bitrate_kbps: int | None
    sample_rate_hz: int | None
    channels: int | None
    duration_seconds: float | None
    filesize_bytes: int
    audio_stream_count: int | None
    video_stream_count: int | None
    inspection_method: str


@dataclass(frozen=True)
class FinalAudioVerification:
    ok: bool
    failures: tuple[str, ...]
    expected_codec: str | None
    observed_codec: str | None


def _optional_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _optional_int(value: object) -> int | None:
    number = _optional_float(value)
    return int(round(number)) if number is not None else None


def _positive_float(value: object) -> float | None:
    number = _optional_float(value)
    return number if number is not None and number > 0 else None


def _positive_int(value: object) -> int | None:
    number = _optional_int(value)
    return number if number is not None and number > 0 else None


def _bitrate_kbps(value: object) -> int | None:
    bitrate = _optional_float(value)
    return int(round(bitrate / 1000.0)) if bitrate and bitrate > 0 else None


def _resolve_existing_file(path: str | Path) -> Path:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise AudioInspectionError(
            "file_not_found", "The final audio file does not exist."
        ) from None
    if not resolved.is_file():
        raise AudioInspectionError(
            "not_a_file", "The final audio evidence does not identify a file."
        )
    return resolved


def _inspect_with_ffprobe(
    path: Path,
    ffprobe_path: str | Path,
    *,
    timeout: float,
    runner: Callable[..., object],
) -> AudioInspection:
    probe = _resolve_existing_file(ffprobe_path)
    bounded_timeout = max(0.1, min(float(timeout), MAX_INSPECTION_TIMEOUT_SECONDS))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    command = [
        str(probe),
        "-v",
        "error",
        "-show_entries",
        "format=format_name,duration,bit_rate:"
        "stream=codec_type,codec_name,bit_rate,sample_rate,channels,duration:"
        "stream_disposition=attached_pic",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = runner(
            command,
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
        raise AudioInspectionError(
            "ffprobe_timeout", "Audio inspection exceeded the bounded safety timeout."
        ) from None
    except (OSError, ValueError):
        raise AudioInspectionError(
            "ffprobe_failed", "ffprobe could not be started safely."
        ) from None
    if int(getattr(completed, "returncode", 1)) != 0:
        raise AudioInspectionError(
            "ffprobe_failed", "ffprobe could not inspect the final audio file."
        )
    try:
        payload = json.loads(str(getattr(completed, "stdout", "") or ""))
    except (TypeError, ValueError):
        raise AudioInspectionError(
            "ffprobe_invalid_json", "ffprobe returned invalid inspection data."
        ) from None
    if not isinstance(payload, Mapping):
        raise AudioInspectionError(
            "ffprobe_invalid_json", "ffprobe returned invalid inspection data."
        )

    streams = payload.get("streams")
    stream_rows = streams if isinstance(streams, list) else []
    audio_streams = [
        row
        for row in stream_rows
        if isinstance(row, Mapping) and row.get("codec_type") == "audio"
    ]
    video_streams = [
        row
        for row in stream_rows
        if (
            isinstance(row, Mapping)
            and row.get("codec_type") == "video"
            and not (
                isinstance(row.get("disposition"), Mapping)
                and row["disposition"].get("attached_pic") in (1, True, "1")
            )
        )
    ]
    primary = audio_streams[0] if audio_streams else {}
    format_row = payload.get("format")
    if not isinstance(format_row, Mapping):
        format_row = {}
    duration = _positive_float(format_row.get("duration"))
    if duration is None:
        duration = _positive_float(primary.get("duration"))
    bitrate = _bitrate_kbps(primary.get("bit_rate"))
    if bitrate is None:
        bitrate = _bitrate_kbps(format_row.get("bit_rate"))
    extension = normalize_extension(path.suffix)
    return AudioInspection(
        path=path,
        extension=extension,
        container=normalize_container(
            format_row.get("format_name"), extension=extension
        ),
        codec=normalize_codec(primary.get("codec_name")),
        bitrate_kbps=bitrate,
        sample_rate_hz=_positive_int(primary.get("sample_rate")),
        channels=_positive_int(primary.get("channels")),
        duration_seconds=duration,
        filesize_bytes=int(path.stat().st_size),
        audio_stream_count=len(audio_streams),
        video_stream_count=len(video_streams),
        inspection_method="ffprobe",
    )


def _mutagen_codec(audio: object, path: Path) -> str | None:
    info = getattr(audio, "info", None)
    direct = normalize_codec(getattr(info, "codec", None))
    if direct:
        return direct
    class_name = type(audio).__name__.casefold()
    mime_text = " ".join(str(item).casefold() for item in (getattr(audio, "mime", None) or ()))
    combined = f"{class_name} {mime_text}"
    for marker, codec in (
        ("oggopus", "opus"),
        ("opus", "opus"),
        ("oggvorbis", "vorbis"),
        ("vorbis", "vorbis"),
        ("flac", "flac"),
        ("mpeg", "mp3"),
        ("mp3", "mp3"),
        ("aac", "aac"),
        ("mp4", "aac"),
    ):
        if marker in combined:
            return codec
    return {
        ".mp3": "mp3",
        ".opus": "opus",
        ".ogg": "vorbis",
        ".flac": "flac",
        ".aac": "aac",
        ".m4a": "aac",
    }.get(path.suffix.casefold())


def _inspect_with_mutagen(
    path: Path,
    *,
    mutagen_loader: Callable[..., object],
) -> AudioInspection:
    try:
        audio = mutagen_loader(str(path))
    except Exception:
        raise AudioInspectionError(
            "mutagen_failed", "The final audio file could not be inspected."
        ) from None
    info = getattr(audio, "info", None) if audio is not None else None
    if audio is None or info is None:
        raise AudioInspectionError(
            "mutagen_failed", "The final audio file could not be inspected."
        )
    extension = normalize_extension(path.suffix)
    codec = _mutagen_codec(audio, path)
    # These native audio-only containers cannot carry a video stream.  MP4/M4A
    # deliberately remains unverified in a Mutagen-only fallback and therefore
    # fails the strict post-download verifier until ffprobe is available.
    video_count = (
        0
        if extension in {".mp3", ".opus", ".ogg", ".flac", ".aac", ".wav"}
        else None
    )
    return AudioInspection(
        path=path,
        extension=extension,
        container=normalize_container(None, extension=extension),
        codec=codec,
        bitrate_kbps=_bitrate_kbps(getattr(info, "bitrate", None)),
        sample_rate_hz=_positive_int(
            getattr(info, "sample_rate", getattr(info, "sample_rate_hz", None))
        ),
        channels=_positive_int(getattr(info, "channels", None)),
        duration_seconds=_positive_float(getattr(info, "length", None)),
        filesize_bytes=int(path.stat().st_size),
        audio_stream_count=1,
        video_stream_count=video_count,
        inspection_method="mutagen",
    )


def inspect_audio_file(
    path: str | Path,
    *,
    ffprobe_path: str | Path | None = None,
    timeout: float = 5.0,
    runner: Callable[..., object] = subprocess.run,
    mutagen_loader: Callable[..., object] = MutagenFile,
) -> AudioInspection:
    """Inspect one local file without shell execution or mutation.

    Callers should pass the centrally discovered ffprobe path when available.
    Mutagen is a bounded fallback for native formats where it can establish the
    required facts safely.
    """

    resolved = _resolve_existing_file(path)
    if ffprobe_path is not None:
        return _inspect_with_ffprobe(
            resolved, ffprobe_path, timeout=timeout, runner=runner
        )
    return _inspect_with_mutagen(resolved, mutagen_loader=mutagen_loader)


def verify_final_audio(
    inspection: AudioInspection,
    *,
    expected_codec: object,
    expected_duration_seconds: float | None = None,
    duration_tolerance_seconds: float = 2.0,
) -> FinalAudioVerification:
    """Fail closed when a final download is not one supported audio stream."""

    failures: list[str] = []
    expected = normalize_codec(expected_codec)
    if not inspection.path.is_file():
        failures.append("final_file_missing")
    if inspection.extension not in SUPPORTED_STORED_AUDIO_EXTENSIONS:
        failures.append("unsupported_final_extension")
    if inspection.audio_stream_count != 1:
        failures.append("expected_exactly_one_audio_stream")
    if inspection.video_stream_count != 0:
        failures.append("final_video_stream_present_or_unverified")
    if inspection.codec not in SUPPORTED_SOURCE_CODECS:
        failures.append("unsupported_final_codec")
    if expected is None or inspection.codec != expected:
        failures.append("final_codec_mismatch")
    if not inspection.duration_seconds or inspection.duration_seconds <= 0:
        failures.append("final_duration_missing")
    elif expected_duration_seconds is not None and expected_duration_seconds > 0:
        tolerance = max(
            0.1,
            float(duration_tolerance_seconds),
            float(expected_duration_seconds) * 0.02,
        )
        if abs(inspection.duration_seconds - expected_duration_seconds) > tolerance:
            failures.append("final_duration_out_of_tolerance")
    if inspection.filesize_bytes <= 0:
        failures.append("final_file_empty")
    return FinalAudioVerification(
        ok=not failures,
        failures=tuple(failures),
        expected_codec=expected,
        observed_codec=inspection.codec,
    )


def require_verified_final_audio(
    inspection: AudioInspection,
    *,
    expected_codec: object,
    expected_duration_seconds: float | None = None,
    duration_tolerance_seconds: float = 2.0,
) -> AudioInspection:
    result = verify_final_audio(
        inspection,
        expected_codec=expected_codec,
        expected_duration_seconds=expected_duration_seconds,
        duration_tolerance_seconds=duration_tolerance_seconds,
    )
    if not result.ok:
        raise AudioInspectionError(
            "final_audio_verification_failed",
            "The downloaded file did not pass final audio quality verification: "
            + ", ".join(result.failures),
        )
    return inspection


def is_verified_audio_only_webm(
    path: str | Path,
    *,
    ffprobe_path: str | Path | None,
) -> bool:
    """Return whether a WebM is conclusively safe for audio-library reuse."""

    if ffprobe_path is None or Path(path).suffix.casefold() != ".webm":
        return False
    try:
        inspection = inspect_audio_file(path, ffprobe_path=ffprobe_path)
    except (AudioInspectionError, OSError, RuntimeError, ValueError):
        return False
    return bool(
        inspection.extension == ".webm"
        and inspection.audio_stream_count is not None
        and inspection.audio_stream_count >= 1
        and inspection.video_stream_count == 0
        and inspection.codec in SUPPORTED_SOURCE_CODECS
    )


def is_verified_reusable_audio(
    path: str | Path,
    *,
    ffprobe_path: str | Path | None = None,
) -> bool:
    """Fail closed unless an existing candidate is one playable audio file."""

    try:
        inspection = inspect_audio_file(path, ffprobe_path=ffprobe_path)
    except (AudioInspectionError, OSError, RuntimeError, ValueError):
        return False
    return bool(
        inspection.extension in SUPPORTED_STORED_AUDIO_EXTENSIONS
        and inspection.audio_stream_count == 1
        and inspection.video_stream_count == 0
        and inspection.codec in SUPPORTED_SOURCE_CODECS
        and inspection.duration_seconds is not None
        and inspection.duration_seconds > 0
        and inspection.filesize_bytes > 0
    )


class DeterministicFinalPathTracker:
    """Collect only yt-dlp hook/result evidence; never scan a directory."""

    def __init__(self, destination_dir: str | Path, source_video_id: str) -> None:
        video_id = str(source_video_id or "").strip()
        if not _VIDEO_ID_RE.fullmatch(video_id):
            raise ValueError("A valid source video identity is required.")
        self.destination_dir = Path(destination_dir).expanduser().resolve()
        self.source_video_id = video_id
        self._paths: list[Path] = []

    @property
    def evidence_paths(self) -> tuple[Path, ...]:
        return tuple(self._paths)

    def record_path(self, value: object) -> None:
        text = str(value or "").strip()
        if not text:
            return
        try:
            candidate = Path(text).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return
        if candidate not in self._paths:
            self._paths.append(candidate)

    def _record_info(self, info: object) -> None:
        if not isinstance(info, Mapping):
            return
        for key in ("filepath", "filename", "_filename"):
            self.record_path(info.get(key))
        requested = info.get("requested_downloads")
        if isinstance(requested, list):
            for item in requested:
                if isinstance(item, Mapping):
                    for key in ("filepath", "filename"):
                        self.record_path(item.get(key))
        thumbnails = info.get("thumbnails")
        if isinstance(thumbnails, list):
            for item in thumbnails:
                if isinstance(item, Mapping):
                    self.record_path(item.get("filepath"))

    def progress_hook(self, event: Mapping[str, Any]) -> None:
        if str(event.get("status") or "").casefold() != "finished":
            return
        for key in ("filepath", "filename"):
            self.record_path(event.get(key))
        self._record_info(event.get("info_dict"))

    def postprocessor_hook(self, event: Mapping[str, Any]) -> None:
        if str(event.get("status") or "").casefold() != "finished":
            return
        for key in ("filepath", "filename"):
            self.record_path(event.get(key))
        self._record_info(event.get("info_dict"))

    def record_result(self, info: Mapping[str, Any]) -> None:
        self._record_info(info)

    def resolve_final_path(self, *, expected_extension: object = None) -> Path:
        expected = (
            normalize_extension(expected_extension)
            if expected_extension is not None
            else None
        )
        if expected_extension is not None and expected is None:
            raise FinalPathEvidenceError(
                "unsupported_expected_extension",
                "The planned output extension is not supported.",
            )

        candidates: list[Path] = []
        violation: str | None = None
        for path in self._paths:
            lowered_name = path.name.casefold()
            if lowered_name.endswith(_PARTIAL_ENDINGS):
                violation = violation or "partial_file_evidence"
                continue
            extension = normalize_extension(path.suffix)
            if extension is None or not path.is_file():
                continue
            if not path.is_relative_to(self.destination_dir):
                violation = violation or "final_path_outside_destination"
                continue
            if extract_source_video_id(path) != self.source_video_id:
                violation = violation or "source_identity_mismatch"
                continue
            if expected is not None and extension != expected:
                violation = violation or "unexpected_final_extension"
                continue
            if path not in candidates:
                candidates.append(path)

        if violation is not None:
            raise FinalPathEvidenceError(
                violation, "yt-dlp final-path evidence failed closed."
            )
        if not candidates:
            raise FinalPathEvidenceError(
                "missing_final_path", "No verified final audio path was reported by yt-dlp."
            )
        if len(candidates) != 1:
            raise FinalPathEvidenceError(
                "ambiguous_final_path",
                "Several unexpected final audio candidates were reported by yt-dlp.",
            )
        return candidates[0]


__all__ = [
    "AudioInspection",
    "AudioInspectionError",
    "DeterministicFinalPathTracker",
    "FinalAudioVerification",
    "FinalPathEvidenceError",
    "inspect_audio_file",
    "is_verified_audio_only_webm",
    "is_verified_reusable_audio",
    "require_verified_final_audio",
    "verify_final_audio",
]
