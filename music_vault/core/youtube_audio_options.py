"""Network-free YouTube audio format selection and yt-dlp option planning."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .audio_quality import (
    BEST_ORIGINAL_PROFILE,
    DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS,
    LOSSLESS_SOURCE_CODECS,
    MP3_320_COMPATIBILITY_PROFILE,
    PRACTICAL_SOURCE_BITRATE_CEILING_KBPS,
    SUPPORTED_SOURCE_CODECS,
    choose_output_extension,
    classify_transformation,
    normalize_codec,
    normalize_container,
    normalize_extension,
    normalize_profile,
)


_SUPPORTED_SOURCE_EXTENSIONS = frozenset(
    {".webm", ".m4a", ".mp4", ".ogg", ".opus", ".mp3", ".flac", ".aac", ".mkv"}
)


class SourceFormatSelectionError(ValueError):
    """Raised when metadata exposes no safely preservable audio source."""


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


def _rank_number(value: object) -> float:
    number = _optional_float(value)
    return number if number is not None else -1.0


@dataclass(frozen=True)
class SourceAudioFormat:
    format_id: str
    extension: str | None
    container: str | None
    codec: str | None
    bitrate_kbps: int | None
    sample_rate_hz: int | None
    channels: int | None
    filesize_bytes: int | None
    duration_seconds: float | None
    audio_only: bool
    has_video: bool
    is_drm: bool
    provider_order: int
    provider_preference: float = -1.0
    quality_rank: float = -1.0
    source_preference: float = -1.0
    high_bitrate_justified: bool = False

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        provider_order: int = 0,
    ) -> "SourceAudioFormat":
        raw_codec = value.get("acodec")
        codec = normalize_codec(raw_codec)
        raw_extension = value.get("ext")
        extension_text = str(raw_extension or "").strip().casefold()
        extension = f".{extension_text.lstrip('.')}" if extension_text else None
        raw_video_codec = str(value.get("vcodec") or "").strip().casefold()
        has_video = raw_video_codec not in {"", "none", "null", "unknown"}
        is_drm = bool(value.get("has_drm") is True or value.get("drm_family"))
        bitrate = _positive_int(value.get("abr"))
        filesize = _optional_int(value.get("filesize"))
        if filesize is None:
            filesize = _optional_int(value.get("filesize_approx"))
        justified = bool(value.get("music_vault_high_bitrate_justified"))
        if codec in LOSSLESS_SOURCE_CODECS:
            justified = True
        return cls(
            format_id=str(value.get("format_id") or "").strip(),
            extension=extension,
            container=normalize_container(value.get("container"), extension=extension),
            codec=codec,
            bitrate_kbps=bitrate,
            sample_rate_hz=_positive_int(value.get("asr")),
            channels=_positive_int(value.get("audio_channels")),
            filesize_bytes=filesize,
            duration_seconds=_positive_float(value.get("duration")),
            audio_only=not has_video,
            has_video=has_video,
            is_drm=is_drm,
            provider_order=int(provider_order),
            provider_preference=_rank_number(value.get("preference")),
            quality_rank=_rank_number(value.get("quality")),
            source_preference=_rank_number(value.get("source_preference")),
            high_bitrate_justified=justified,
        )

    @property
    def rank_key(self) -> tuple[int, float, float, float]:
        # yt-dlp exposes ``formats`` in its own ranked order.  Preserve that
        # provider order as the primary fact; numeric bitrate is deliberately
        # absent so an oversized stream cannot win for that reason alone.
        return (
            self.provider_order,
            self.provider_preference,
            self.quality_rank,
            self.source_preference,
        )


@dataclass(frozen=True)
class FormatEligibility:
    eligible: bool
    reason: str | None = None


def source_format_eligibility(
    source: SourceAudioFormat,
    *,
    bitrate_ceiling_kbps: int = PRACTICAL_SOURCE_BITRATE_CEILING_KBPS,
) -> FormatEligibility:
    if not source.format_id:
        return FormatEligibility(False, "missing_format_id")
    if source.is_drm:
        return FormatEligibility(False, "drm")
    if source.codec not in SUPPORTED_SOURCE_CODECS:
        return FormatEligibility(False, "unsupported_or_missing_audio_codec")
    if source.extension not in _SUPPORTED_SOURCE_EXTENSIONS:
        return FormatEligibility(False, "unsupported_container")
    if (
        source.bitrate_kbps is not None
        and source.bitrate_kbps > max(1, int(bitrate_ceiling_kbps))
        and not source.high_bitrate_justified
    ):
        return FormatEligibility(False, "impractical_known_bitrate")
    return FormatEligibility(True)


def _coerce_formats(
    formats: Iterable[SourceAudioFormat | Mapping[str, Any]],
) -> list[SourceAudioFormat]:
    normalized: list[SourceAudioFormat] = []
    for provider_order, item in enumerate(formats):
        if isinstance(item, SourceAudioFormat):
            normalized.append(
                item
                if item.provider_order != 0 or provider_order == 0
                else replace(item, provider_order=provider_order)
            )
        else:
            normalized.append(
                SourceAudioFormat.from_mapping(item, provider_order=provider_order)
            )
    return normalized


def eligible_source_audio_formats(
    formats: Iterable[SourceAudioFormat | Mapping[str, Any]],
    *,
    bitrate_ceiling_kbps: int = PRACTICAL_SOURCE_BITRATE_CEILING_KBPS,
) -> tuple[SourceAudioFormat, ...]:
    return tuple(
        item
        for item in _coerce_formats(formats)
        if source_format_eligibility(
            item, bitrate_ceiling_kbps=bitrate_ceiling_kbps
        ).eligible
    )


def select_source_audio_format(
    formats: Iterable[SourceAudioFormat | Mapping[str, Any]],
    *,
    bitrate_ceiling_kbps: int = PRACTICAL_SOURCE_BITRATE_CEILING_KBPS,
) -> SourceAudioFormat:
    """Choose one provider-ranked stream, preferring audio-only candidates."""

    candidates = list(
        eligible_source_audio_formats(
            formats, bitrate_ceiling_kbps=bitrate_ceiling_kbps
        )
    )
    if not candidates:
        raise SourceFormatSelectionError(
            "No supported, non-DRM audio source was available."
        )
    audio_only = [item for item in candidates if item.audio_only]
    selection_pool = audio_only or [item for item in candidates if item.has_video]
    if not selection_pool:
        raise SourceFormatSelectionError(
            "No usable audio-only or extractable muxed source was available."
        )
    return max(selection_pool, key=lambda item: item.rank_key)


@dataclass(frozen=True)
class AudioDownloadPlan:
    profile: str
    source: SourceAudioFormat
    output_extension: str
    expected_final_codec: str
    transformation_kind: str
    compatibility_mp3_bitrate_kbps: int | None

    @property
    def source_codec_preserved(self) -> bool:
        return self.profile == BEST_ORIGINAL_PROFILE


def build_audio_download_plan(
    formats: Iterable[SourceAudioFormat | Mapping[str, Any]],
    profile: object,
    *,
    compatibility_mp3_bitrate_kbps: int = DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS,
    bitrate_ceiling_kbps: int = PRACTICAL_SOURCE_BITRATE_CEILING_KBPS,
) -> AudioDownloadPlan:
    selected_profile = normalize_profile(profile)
    source = select_source_audio_format(
        formats, bitrate_ceiling_kbps=bitrate_ceiling_kbps
    )
    if selected_profile == MP3_320_COMPATIBILITY_PROFILE:
        return AudioDownloadPlan(
            profile=selected_profile,
            source=source,
            output_extension=".mp3",
            expected_final_codec="mp3",
            transformation_kind="lossy_transcode",
            compatibility_mp3_bitrate_kbps=DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS,
        )
    return AudioDownloadPlan(
        profile=selected_profile,
        source=source,
        output_extension=choose_output_extension(source.codec),
        expected_final_codec=str(source.codec),
        transformation_kind=classify_transformation(
            selected_profile,
            source_codec=source.codec,
            source_extension=source.extension,
            source_has_video=source.has_video,
        ),
        compatibility_mp3_bitrate_kbps=None,
    )


def build_yt_dlp_audio_options(
    plan: AudioDownloadPlan,
    *,
    embed_thumbnail: bool = True,
) -> dict[str, Any]:
    """Build the profile-specific, single-stream yt-dlp option fragment.

    The chosen format ID comes exclusively from the provider metadata.  No
    YouTube format number, browser-cookie option, or fallback expression is
    embedded here.
    """

    extract_audio: dict[str, str] = {
        "key": "FFmpegExtractAudio",
        "preferredcodec": (
            "best" if plan.profile == BEST_ORIGINAL_PROFILE else "mp3"
        ),
    }
    if plan.profile == MP3_320_COMPATIBILITY_PROFILE:
        extract_audio["preferredquality"] = str(
            DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS
        )

    postprocessors: list[dict[str, str]] = [
        extract_audio,
        {"key": "FFmpegMetadata"},
    ]
    if embed_thumbnail:
        postprocessors.append({"key": "EmbedThumbnail"})

    return {
        "format": plan.source.format_id,
        "noplaylist": True,
        "writethumbnail": bool(embed_thumbnail),
        "write_all_thumbnails": False,
        "embedthumbnail": bool(embed_thumbnail),
        "addmetadata": True,
        "postprocessors": postprocessors,
    }


__all__ = [
    "AudioDownloadPlan",
    "FormatEligibility",
    "SourceAudioFormat",
    "SourceFormatSelectionError",
    "build_audio_download_plan",
    "build_yt_dlp_audio_options",
    "eligible_source_audio_formats",
    "select_source_audio_format",
    "source_format_eligibility",
]
