"""Pure audio-quality normalization and truthful presentation helpers.

This module deliberately knows nothing about yt-dlp, the network, SQLite, or
the Music Vault UI.  It is the small shared vocabulary used by download
planning, final-file inspection, and read-only quality presentation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .audio_quality_config import (
    BEST_ORIGINAL_PROFILE,
    DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS,
    MP3_320_COMPATIBILITY_PROFILE,
    normalize_download_quality_profile,
)


PRACTICAL_SOURCE_BITRATE_CEILING_KBPS = 512

SUPPORTED_SOURCE_CODECS = frozenset(
    {"opus", "aac", "vorbis", "mp3", "flac", "alac"}
)
LOSSLESS_SOURCE_CODECS = frozenset({"flac", "alac"})
SUPPORTED_STORED_AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac", ".webm"}
)

_CODEC_ALIASES = {
    "aac": "aac",
    "aac_latm": "aac",
    "libfdk_aac": "aac",
    "libvo_aacenc": "aac",
    "alac": "alac",
    "flac": "flac",
    "libopus": "opus",
    "libvorbis": "vorbis",
    "mp3": "mp3",
    "mp3adu": "mp3",
    "mp3float": "mp3",
    "mpeg audio layer 3": "mp3",
    "mpeg layer iii": "mp3",
    "opus": "opus",
    "vorbis": "vorbis",
}

_OUTPUT_EXTENSIONS = {
    "opus": ".opus",
    "aac": ".m4a",
    "vorbis": ".ogg",
    "mp3": ".mp3",
    "flac": ".flac",
    "alac": ".m4a",
}


def normalize_profile(value: object) -> str:
    """Return one of the two future-download profiles.

    Unknown values resolve to the product default rather than opting a user
    into a lossy compatibility transcode.
    """

    return normalize_download_quality_profile(value)


def normalize_codec(value: object) -> str | None:
    """Normalize safe codec aliases without guessing an unknown codec."""

    text = str(value or "").strip().casefold()
    if not text or text in {"none", "unknown", "null", "n/a"}:
        return None
    # FFmpeg/yt-dlp commonly exposes AAC as ``mp4a.40.2`` (and related
    # object-type suffixes).  The suffix changes the AAC profile, not the
    # codec family Music Vault needs for preservation verification.
    if text.startswith("mp4a"):
        return "aac"
    return _CODEC_ALIASES.get(text)


def normalize_extension(value: object) -> str | None:
    text = str(value or "").strip().casefold()
    if not text:
        return None
    if not text.startswith("."):
        text = f".{text}"
    return text if text in SUPPORTED_STORED_AUDIO_EXTENSIONS else None


def normalize_container(value: object, *, extension: object = None) -> str | None:
    """Normalize common probe/yt-dlp container names conservatively."""

    text = str(value or "").strip().casefold()
    tokens = {item.strip() for item in text.replace("/", ",").split(",") if item.strip()}
    if "webm" in tokens or text == "webm":
        return "webm"
    if "matroska" in tokens or text in {"mkv", "matroska"}:
        return "matroska"
    if tokens.intersection({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}):
        return "m4a"
    if tokens.intersection({"ogg", "oga", "opus"}):
        return "ogg"
    if tokens.intersection({"mp3", "mpeg"}):
        return "mp3"
    if "flac" in tokens:
        return "flac"
    if tokens.intersection({"aac", "adts"}):
        return "aac"
    if tokens.intersection({"wav", "wave"}):
        return "wav"

    suffix = normalize_extension(extension)
    return {
        ".webm": "webm",
        ".m4a": "m4a",
        ".mp3": "mp3",
        ".ogg": "ogg",
        ".opus": "ogg",
        ".flac": "flac",
        ".aac": "aac",
        ".wav": "wav",
    }.get(suffix)


def choose_output_extension(codec: object) -> str:
    """Choose a deterministic practical audio-only extension for a codec."""

    normalized = normalize_codec(codec)
    try:
        return _OUTPUT_EXTENSIONS[normalized]
    except KeyError:
        raise ValueError("The selected source codec is not safely supported.") from None


def classify_transformation(
    profile: object,
    *,
    source_codec: object,
    source_extension: object = None,
    source_has_video: bool = False,
) -> str:
    """Classify the planned transformation without making fidelity claims."""

    selected_profile = normalize_profile(profile)
    if selected_profile == MP3_320_COMPATIBILITY_PROFILE:
        return "lossy_transcode"

    codec = normalize_codec(source_codec)
    output_extension = choose_output_extension(codec)
    input_extension = normalize_extension(source_extension)
    if source_has_video or input_extension != output_extension:
        return "source_preserved_remux"
    return "none"


def profile_description(profile: object) -> str:
    selected = normalize_profile(profile)
    if selected == BEST_ORIGINAL_PROFILE:
        return (
            "Keeps the best useful source audio codec and avoids lossy re-encoding. "
            "Typical results are Opus or M4A/AAC. This preserves the source stream "
            "but does not make YouTube audio lossless."
        )
    return (
        f"Converts the best source audio to MP3 at "
        f"{DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS} kbps for compatibility. "
        "This cannot improve source fidelity and may use more storage."
    )


def _known_positive_int(value: object) -> int | None:
    try:
        normalized = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


@dataclass(frozen=True)
class QualityComparison:
    profile: str
    source_codec: str | None
    stored_codec: str | None
    source_bitrate_kbps: int | None
    stored_bitrate_kbps: int | None
    transformation_kind: str
    transformation_text: str
    codec_preserved: bool | None


def compare_source_and_stored(
    *,
    profile: object,
    source_codec: object,
    stored_codec: object,
    source_bitrate_kbps: int | None = None,
    stored_bitrate_kbps: int | None = None,
    transformation_kind: str | None = None,
) -> QualityComparison:
    """Return known comparison facts and deliberately honest wording."""

    selected_profile = normalize_profile(profile)
    source = normalize_codec(source_codec)
    stored = normalize_codec(stored_codec)
    preserved = None if source is None or stored is None else source == stored

    if selected_profile == MP3_320_COMPATIBILITY_PROFILE:
        kind = "lossy_transcode"
        wording = "Lossy compatibility transcode; not a fidelity upgrade"
    elif preserved is False:
        kind = "unknown"
        wording = "Quality verification failed; the stored codec differs from the source codec"
    elif preserved is None:
        kind = transformation_kind or "unknown"
        wording = "Source and stored codec comparison unavailable"
    else:
        kind = transformation_kind or "none"
        if kind == "source_preserved_remux":
            wording = "Source codec retained; container-only remux"
        else:
            wording = "Source codec retained; no lossy re-encoding"

    return QualityComparison(
        profile=selected_profile,
        source_codec=source,
        stored_codec=stored,
        source_bitrate_kbps=_known_positive_int(source_bitrate_kbps),
        stored_bitrate_kbps=_known_positive_int(stored_bitrate_kbps),
        transformation_kind=kind,
        transformation_text=wording,
        codec_preserved=preserved,
    )


__all__ = [
    "BEST_ORIGINAL_PROFILE",
    "DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS",
    "LOSSLESS_SOURCE_CODECS",
    "MP3_320_COMPATIBILITY_PROFILE",
    "PRACTICAL_SOURCE_BITRATE_CEILING_KBPS",
    "QualityComparison",
    "SUPPORTED_SOURCE_CODECS",
    "SUPPORTED_STORED_AUDIO_EXTENSIONS",
    "choose_output_extension",
    "classify_transformation",
    "compare_source_and_stored",
    "normalize_codec",
    "normalize_container",
    "normalize_extension",
    "normalize_profile",
    "profile_description",
]
